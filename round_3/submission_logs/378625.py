"""
Round 3 trading algorithm (v3 — regression fixes vs sub 377797)
---------------------------------------------------------------
Products
  - HYDROGEL_PACK         spot, uncorrelated, wide spread (~16)
  - VELVETFRUIT_EXTRACT   spot, tight spread (~5), delta-hedge vehicle
  - VEV_K for K in {4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500}
    European calls on VELVETFRUIT_EXTRACT, r = 0, TTE = 7 "Solvenarian days"
    from day 1 (so on day D, TTE = 8 - D - ts/1e6 days).

Why this revision (post-mortem on submission 377797 = $211.21)
  Comparing 377486 ($455.82, best) → 377797 ($211.21, current):
    - HP identical (+$288 both)
    - 5300 short doubled from -60 to -120  → live +$72 → +$256 (good)
    - 5500 short doubled from -60 to -120  → live +$16 → **-$63** (bad!)
    - VE hedging swung from $0 → **-$375** (dominated the regression)
  Net: bigger basket helped 5300, hurt 5500, and crashed VE (delta spread cost).

Fixes applied here:
  1. 5500 structural target trimmed back from -120 to -60. Its edge/risk ratio
     is worse than 5300 (option price ~6-7 seashells ⇒ a 1-tick MTM move is a
     big fraction of notional; vega is smaller; persistent-rich edge is
     roughly the same in $ terms).
  2. Delta hedge deadband widened 40 → 60 and cooldown 500 → 1200 ticks.
     v3 (60/100/60/60 basket) had this hedge never fire; v4 (80/120/120/120)
     fired it 8-10 times at ~$40 of spread cost each.
  3. Passive-first VE hedging: post at best_bid+1 / best_ask-1 first and only
     cross the spread when the edge persists past the cooldown. Saves ~4/5 of
     the VE spread when the counterparty is on our side anyway.
  4. Kept the passive VEV top-up quotes (they helped fills on 5100/5300).

Entry points
  * Trader.run(state)   — IMC-Prosperity SDK adapter.
  * Trader.decide(...)  — pure function used by backtest.py.
"""
from __future__ import annotations

import math
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional


# ---------------------------- Black-Scholes helpers ----------------------------
_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_price(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0.0 or sigma <= 0.0:
        return max(S - K, 0.0)
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / v
    d2 = d1 - v
    return S * _norm_cdf(d1) - K * _norm_cdf(d2)


def bs_delta(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0.0 or sigma <= 0.0:
        return 1.0 if S > K else 0.0
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / v
    return _norm_cdf(d1)


def bs_vega(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0.0 or sigma <= 0.0:
        return 0.0
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / v
    return S * _norm_pdf(d1) * math.sqrt(T)


def implied_vol(C: float, S: float, K: float, T: float,
                lo: float = 1e-4, hi: float = 3.0, tol: float = 1e-5) -> Optional[float]:
    intrinsic = max(S - K, 0.0)
    if T <= 0.0 or C <= intrinsic + 1e-8:
        return None
    f_lo = bs_call_price(S, K, T, lo) - C
    f_hi = bs_call_price(S, K, T, hi) - C
    if f_lo * f_hi > 0:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        f_mid = bs_call_price(S, K, T, mid) - C
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return 0.5 * (lo + hi)


# ----------------------------------- Config -----------------------------------

VE = "VELVETFRUIT_EXTRACT"
HP = "HYDROGEL_PACK"
VEV_STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VEV_PRODUCTS = [f"VEV_{K}" for K in VEV_STRIKES]

# CORRECTED POSITION LIMITS (from judge log: VE limit = 200)
POSITION_LIMIT: Dict[str, int] = {
    HP: 50,
    VE: 200,
}
for p in VEV_PRODUCTS:
    POSITION_LIMIT[p] = 200

# Strikes we actually trade — the rest either pin intrinsic or the 0.5 floor.
VEV_TRADED_STRIKES = [5000, 5100, 5200, 5300, 5400, 5500]

# TTE: 7 days on day 1 at ts 0  =>  days = 8 - day - ts/1e6
DAYS_PER_YEAR = 250.0


def tte_years(day: int, timestamp: int) -> float:
    days = (8.0 - day) - (timestamp / 1_000_000.0)
    return max(days, 1e-6) / DAYS_PER_YEAR


# ---- Signal thresholds ----
# We measure each strike's IV residual from the cross-strike SMILE AVERAGE IV
# at the same tick. Empirical SD of these residuals on days 0-2 is ~0.003,
# and VEV_5400's residual is ~-0.010 with std 0.003.
VEV_RESID_ENTER = 0.004    # enter at 4 vol-pts dislocation from current smile
VEV_RESID_FULL = 0.012     # full size at 12 vol-pts — matches 5400's anomaly
VEV_SIZE_CAP = 40          # smaller per-strike cap so total delta stays manageable

# STRUCTURAL SMILE RESIDUAL (persistent across historical days 0-2 AND live day 2).
# The smile has a consistent zig-zag: 5100 & 5400 trade cheap, 5300 & 5500 trade rich.
# This table is the "target" per-strike IV *offset* from the cross-strike mean.
# We target LONG where offset is negative, SHORT where offset is positive.
STRUCTURAL_BIAS: Dict[int, float] = {
    5000:  0.000,   # no reliable structural edge
    5100: -0.0025,  # cheap
    5200: +0.0015,  # slightly rich (minor)
    5300: +0.0055,  # rich
    5400: -0.0105,  # VERY cheap — the anomaly
    5500: +0.0050,  # rich
}

# Baseline RV basket — only held when the market actually offers favourable fills
# (i.e. when we can buy longs AT or BELOW structural-fair, and sell shorts AT or
# ABOVE it).  Sized modestly so entry slippage doesn't dominate.
#
# Vega-balanced: 2 longs (5100+5400) vs 2 shorts (5300+5500). Net delta is small.
RV_BASKET_TARGETS: Dict[int, int] = {
    5100: +80,    # passive+aggressive fills together hit ~33 live (good)
    5400: +120,   # anomaly is biggest here
    5300: -120,   # 5300 short scaled cleanly (+$184 vs v3 from doubling)
    5500: -60,    # TRIMMED from -120: live showed -$63 at 120, +$16 at 60.
                  # Low option price (~6-7) means per-contract MTM vol eats
                  # the edge; smaller position sizes edge/risk better.
}

# Hedging is costly (VE spread = 5 ticks crossed per rebalance).  Only hedge
# when delta is meaningfully stale; otherwise accept small directional risk.
# Widened from 40→60 and cooldown 500→1200 to suppress spurious rebalances
# on basket fill/drain noise (regressed sub 377797 burned -$375 here).
DELTA_HEDGE_DEADBAND = 60
DELTA_BUDGET_HEADROOM = 0.7


# -------------------------------- Data structures -------------------------------

@dataclass
class Quote:
    price: float
    volume: int


@dataclass
class OrderBook:
    bids: List[Quote] = field(default_factory=list)
    asks: List[Quote] = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[Quote]: return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[Quote]: return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return 0.5 * (self.best_bid.price + self.best_ask.price)
        return None


@dataclass
class Order:
    product: str
    price: float
    quantity: int


# ----------------------------------- Trader -----------------------------------

class Trader:
    def __init__(self) -> None:
        self._hp_mid_ewma: Optional[float] = None
        self._alpha_hp = 0.05
        # IV EWMA per strike — starts at None, populated from first observation
        self._iv_ewma: Dict[int, Optional[float]] = {K: None for K in VEV_STRIKES}
        self._alpha_iv = 0.25
        # Rate-limit the VE hedge rebalances
        self._last_hedge_ts: int = -10_000
        self._hedge_cooldown_ticks: int = 1200
        # Track consecutive passive-hedge attempts so we escalate to crossing
        # the spread only when the delta really won't go away.
        self._passive_hedge_attempts: int = 0

    # --------------------------- public ---------------------------

    def decide(
        self,
        day: int,
        timestamp: int,
        books: Dict[str, OrderBook],
        positions: Dict[str, int],
    ) -> List[Order]:
        orders: List[Order] = []
        T = tte_years(day, timestamp)

        hp_book = books.get(HP)
        ve_book = books.get(VE)
        ve_mid = ve_book.mid if ve_book and ve_book.mid else None

        # ---------------- HYDROGEL_PACK: tight MM + take on dislocations ----------------
        if hp_book is not None and hp_book.mid is not None:
            mid = hp_book.mid
            if self._hp_mid_ewma is None:
                self._hp_mid_ewma = mid
            else:
                self._hp_mid_ewma = (1 - self._alpha_hp) * self._hp_mid_ewma + self._alpha_hp * mid
            pos = positions.get(HP, 0)
            orders.extend(self._hydrogel(hp_book, mid, self._hp_mid_ewma, pos, POSITION_LIMIT[HP]))

        # ---------------- VEV options: smile-residual vol-arb ----------------
        if ve_mid is not None:
            # 1) compute per-strike IV using the live VE mid
            ivs: Dict[int, float] = {}
            deltas: Dict[int, float] = {}
            fair_prices: Dict[int, float] = {}
            for K in VEV_TRADED_STRIKES:
                book = books.get(f"VEV_{K}")
                if book is None or book.mid is None:
                    continue
                iv_obs = implied_vol(book.mid, ve_mid, float(K), T)
                if iv_obs is None:
                    continue
                # EWMA smoothing
                prev = self._iv_ewma[K]
                iv = iv_obs if prev is None else (1 - self._alpha_iv) * prev + self._alpha_iv * iv_obs
                self._iv_ewma[K] = iv
                ivs[K] = iv

            if len(ivs) >= 4:
                # 2) cross-strike smile "level" = mean of observed IVs (vol-level-agnostic)
                smile_level = sum(ivs.values()) / len(ivs)

                # 3) compute per-strike *fair* IV from the smile. Simplest: use the
                #    smile_level. (Slight skew could be modelled but isn't necessary —
                #    we only want to trade DISLOCATIONS from the current regime.)
                #    VEV_5400 empirically trades ~1 vol-pt below the mean. Don't bake
                #    it in — let the relative signal discover it.

                # 4) size target position per strike — combination of:
                #    (a) structural RV basket (long cheap strikes, short rich strikes)
                #    (b) transient mean-reversion on top of the structural bias
                desired: Dict[int, int] = {}
                for K, iv in ivs.items():
                    # Structural "fair" IV accounts for the persistent zig-zag smile
                    structural_fair = smile_level + STRUCTURAL_BIAS.get(K, 0.0)
                    # residual vs the *structural* fair — only transient deviations
                    resid_transient = iv - structural_fair

                    # (a) baseline structural position
                    base = RV_BASKET_TARGETS.get(K, 0)

                    # (b) transient mean-reversion kicker (small)
                    if abs(resid_transient) >= VEV_RESID_ENTER:
                        mag = min(1.0, (abs(resid_transient) - VEV_RESID_ENTER) /
                                  (VEV_RESID_FULL - VEV_RESID_ENTER))
                        kicker = int(-math.copysign(VEV_SIZE_CAP * mag, resid_transient))
                    else:
                        kicker = 0
                    # combine, capped at the product's position limit
                    combined = base + kicker
                    combined = max(-POSITION_LIMIT[f"VEV_{K}"],
                                   min(POSITION_LIMIT[f"VEV_{K}"], combined))
                    desired[K] = combined

                    # Delta and execution fair use structural_fair — prevents
                    # paying the bid-ask spread on basket entries for strikes that
                    # the market mid already correctly reflects.  Aggressive
                    # takes only fire on TRANSIENT deviations from this fair.
                    deltas[K] = bs_delta(ve_mid, float(K), T, structural_fair)
                    fair_prices[K] = bs_call_price(ve_mid, float(K), T, structural_fair)

                # 5) gate: ensure remaining VE hedge capacity before adding new longs/shorts
                current_option_delta = sum(positions.get(f"VEV_{K}", 0) * deltas.get(K, 0.0)
                                           for K in VEV_TRADED_STRIKES)
                ve_pos = positions.get(VE, 0)
                # the hedge we need to carry if we take the desired positions
                desired_option_delta = sum(desired.get(K, 0) * deltas.get(K, 0.0)
                                           for K in VEV_TRADED_STRIKES)
                hedge_required = -desired_option_delta
                # If hedge_required would exceed DELTA_BUDGET_HEADROOM * VE_limit, scale down
                cap = POSITION_LIMIT[VE] * DELTA_BUDGET_HEADROOM
                if abs(hedge_required) > cap and abs(hedge_required) > 1e-6:
                    scale = cap / abs(hedge_required)
                    desired = {K: int(desired.get(K, 0) * scale) for K in desired}

                # 6) place aggressive orders toward desired positions, *requiring* a
                #    price edge versus the fair BS price
                for K in VEV_TRADED_STRIKES:
                    if K not in desired:
                        continue
                    p = f"VEV_{K}"
                    book = books.get(p)
                    if book is None:
                        continue
                    pos = positions.get(p, 0)
                    diff = desired[K] - pos
                    fair_price = fair_prices.get(K, book.mid)

                    if diff > 0 and book.best_ask is not None:
                        for ask in book.asks:
                            if diff <= 0: break
                            if ask.price <= fair_price:
                                q = min(diff, ask.volume)
                                orders.append(Order(p, ask.price, q))
                                diff -= q
                            else:
                                break
                        # PASSIVE top-up: for basket longs, also post a passive bid
                        # strictly inside the spread at best_bid + 1.  Free fills
                        # when someone crosses us; no slippage cost.
                        if (diff > 0 and book.best_bid is not None and
                                book.best_ask is not None and
                                book.best_ask.price > book.best_bid.price + 1):
                            post_px = book.best_bid.price + 1
                            # Only post if that price is still below our fair
                            if post_px <= fair_price:
                                q = min(diff, 50)  # cap posted size
                                orders.append(Order(p, float(post_px), q))
                    elif diff < 0 and book.best_bid is not None:
                        for bid in book.bids:
                            if diff >= 0: break
                            if bid.price >= fair_price:
                                q = min(-diff, bid.volume)
                                orders.append(Order(p, bid.price, -q))
                                diff += q
                            else:
                                break
                        # PASSIVE top-up: for basket shorts, post a passive ask at
                        # best_ask - 1 while still at/above fair.
                        if (diff < 0 and book.best_bid is not None and
                                book.best_ask is not None and
                                book.best_ask.price > book.best_bid.price + 1):
                            post_px = book.best_ask.price - 1
                            if post_px >= fair_price:
                                q = min(-diff, 50)
                                orders.append(Order(p, float(post_px), -q))

                # 7) delta hedge — passive first, aggressive only on persistent drift.
                #    The VE book has a 5-tick spread and is typically 2-wide (best_ask
                #    = best_bid + 5), so posting at best_bid+1 / best_ask-1 saves ~4
                #    ticks per share if we get filled.  We escalate to crossing only
                #    after _passive_hedge_attempts has failed to close the gap.
                global_ts = day * 1_000_000 + timestamp
                if (ve_book is not None and ve_book.best_bid and ve_book.best_ask and
                        global_ts - self._last_hedge_ts >= self._hedge_cooldown_ticks):
                    realised_option_delta = sum(
                        positions.get(f"VEV_{K}", 0) * deltas.get(K, 0.0)
                        for K in VEV_TRADED_STRIKES)
                    hedge_target = int(round(-realised_option_delta))
                    hedge_target = max(-POSITION_LIMIT[VE], min(POSITION_LIMIT[VE], hedge_target))
                    delta_order = hedge_target - ve_pos
                    if abs(delta_order) >= DELTA_HEDGE_DEADBAND:
                        spread_wide = (ve_book.best_ask.price - ve_book.best_bid.price) >= 3
                        # Escalate to crossing when passive has been tried 3x or delta
                        # is extreme (>1.5× deadband → persistent imbalance).
                        escalate = (self._passive_hedge_attempts >= 3 or
                                    abs(delta_order) >= 1.5 * DELTA_HEDGE_DEADBAND)
                        if delta_order > 0:  # need to buy VE
                            if spread_wide and not escalate:
                                post_px = ve_book.best_bid.price + 1
                                q = min(delta_order, POSITION_LIMIT[VE] - ve_pos)
                                if q > 0:
                                    orders.append(Order(VE, float(post_px), q))
                                    self._last_hedge_ts = global_ts
                                    self._passive_hedge_attempts += 1
                            else:
                                q = min(delta_order, ve_book.best_ask.volume,
                                        POSITION_LIMIT[VE] - ve_pos)
                                if q > 0:
                                    orders.append(Order(VE, ve_book.best_ask.price, q))
                                    self._last_hedge_ts = global_ts
                                    self._passive_hedge_attempts = 0
                        else:  # need to sell VE
                            if spread_wide and not escalate:
                                post_px = ve_book.best_ask.price - 1
                                q = min(-delta_order, POSITION_LIMIT[VE] + ve_pos)
                                if q > 0:
                                    orders.append(Order(VE, float(post_px), -q))
                                    self._last_hedge_ts = global_ts
                                    self._passive_hedge_attempts += 1
                            else:
                                q = min(-delta_order, ve_book.best_bid.volume,
                                        POSITION_LIMIT[VE] + ve_pos)
                                if q > 0:
                                    orders.append(Order(VE, ve_book.best_bid.price, -q))
                                    self._last_hedge_ts = global_ts
                                    self._passive_hedge_attempts = 0
                    else:
                        # Delta is inside deadband — reset escalation counter.
                        self._passive_hedge_attempts = 0

        return orders

    # --------------------------- internals ---------------------------

    def _hydrogel(
        self,
        book: OrderBook,
        mid: float,
        ewma: float,
        pos: int,
        limit: int,
    ) -> List[Order]:
        """
        HP market maker. Two complementary mechanisms:
          A) aggressive TAKE when the book gives us 3+ ticks of edge vs mid (works live).
          B) passive QUOTE inside the spread (rarely fills live but free optionality).
        Inventory skew: push quotes against our inventory.
        """
        orders: List[Order] = []
        skew = -4.0 * pos / limit
        fair = mid + skew
        buy_cap = limit - pos
        sell_cap = limit + pos

        # --- TAKE ---
        # Tightened to 2 ticks of edge vs mid (live data shows |dev|>3 on only 69% of
        # ticks, |dev|>2 on ~85%).  Still conservative, but we catch more reversions.
        if book.best_ask is not None and buy_cap > 0 and book.best_ask.price <= mid - 2:
            q = min(buy_cap, book.best_ask.volume)
            orders.append(Order(HP, book.best_ask.price, q))
            buy_cap -= q
        if book.best_bid is not None and sell_cap > 0 and book.best_bid.price >= mid + 2:
            q = min(sell_cap, book.best_bid.volume)
            orders.append(Order(HP, book.best_bid.price, -q))
            sell_cap -= q

        # --- MAKE inside the spread ---
        if book.best_bid is not None and book.best_ask is not None:
            bid_px = int(math.floor(fair - 2))
            ask_px = int(math.ceil(fair + 2))
            bid_px = max(bid_px, int(book.best_bid.price) + 1)
            ask_px = min(ask_px, int(book.best_ask.price) - 1)
            if bid_px > book.best_bid.price and bid_px < book.best_ask.price and buy_cap > 0:
                orders.append(Order(HP, float(bid_px), min(20, buy_cap)))
            if ask_px < book.best_ask.price and ask_px > book.best_bid.price and sell_cap > 0:
                orders.append(Order(HP, float(ask_px), -min(20, sell_cap)))
        return orders

    # --------------------------- IMC SDK adapter ---------------------------

    def run(self, state):  # pragma: no cover
        books: Dict[str, OrderBook] = {}
        for product, depth in state.order_depths.items():
            bids = sorted(
                [Quote(float(p), int(abs(v))) for p, v in depth.buy_orders.items()],
                key=lambda q: -q.price,
            )
            asks = sorted(
                [Quote(float(p), int(abs(v))) for p, v in depth.sell_orders.items()],
                key=lambda q: q.price,
            )
            books[product] = OrderBook(bids=bids, asks=asks)
        day = getattr(state, "day", 0)
        orders = self.decide(day, state.timestamp, books, dict(state.position or {}))

        from datamodel import Order as PrOrder  # type: ignore
        out: Dict[str, List[PrOrder]] = defaultdict(list)
        for o in orders:
            out[o.product].append(PrOrder(o.product, int(round(o.price)), int(o.quantity)))
        return out, 0, json.dumps({})