"""
Round 3 trading algorithm (v5 — scale VE MM after sub 387668 = $1051.73)
------------------------------------------------------------------------
Sub 387668 confirmed VE market-making is the new alpha source:
  HP                     +$288  (unchanged baseline)
  VEV options total      +$260
  VELVETFRUIT_EXTRACT    +$503  ← NEW! pure MM on incoming flow
  TOTAL                  +$1052

This revision (v5) pushes VE MM further:
  - Always-on MM (was gated by 50k-ts cooldown; now fires every tick)
  - Quote size doubled 30 → 60, with optional second deep layer (60+60=120)
  - Inventory skew strengthened 3.0 → 4.0, anchored to hedge_target so quotes
    naturally pull position toward the delta-hedge level
  - Emergency aggressive hedge only when |delta_drift| ≥ 2× deadband (was 1×)
  - Wing call overwrite (6000/6500) REMOVED — zero fills due to queue priority
  - All EXEC_FAIR_BLEND structural blends ≤ 0.3 (post-mortem on -$251 from 5400
    BLEND=1.0 in sub 387184: structural smile dislocations DON'T revert within
    a day; live MTM marks at session-end mid, not expiry → pure cost)
-------------------------------------------------------------------------------
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

Fixes from previous revision (still in place):
  - 5500 trimmed to -60, hedge deadband=60, passive-first VE hedging.

Why this revision (post-mortem on 378625 = $664.94)
  Per-product fill rates:
    VEV_5400  target=160  filled=  5  (3%)   PnL=  +$5.5   ← biggest miss
    VEV_5100  target=120  filled= 33  (28%)  PnL= +$103
    VEV_5200  target=-40  filled=  0  (0%)   PnL=    $0
    VEV_5300  target=-160 filled=-120 (75%)  PnL= +$256
    VEV_5500  target=-60  filled=-60 (100%)  PnL=  +$16
    HP                                       PnL= +$288
  HP was identical to prior subs ⇒ that's the steady ~$288/day baseline.
  The big leak: VEV_5400 (rock-solid 1-vol-pt cheap) only filled 5/180 because
  aggressive takes used `structural_fair`, which already prices the cheapness
  ⇒ ask_price is always above structural_fair ⇒ takes never fire. Passive
  posts in an illiquid 1-2-tick spread don't fill either.

New levers in v4:
  1. EXEC_FAIR_BLEND table: per-strike blend between structural_fair and
     smile_level for AGGRESSIVE takes. 5400 set to 1.0 (pure smile_level) so
     fair_price is BS(smile_mean) — *above* observed mid — and we cross the
     spread to capture the structural cheapness. 5300/5500 at 0.7, 5100/5200
     at 0.5, 5000 at 0.0.
  2. 5400 target boosted 160 → 180 (90% of position limit). 5300 short to -180.
  3. HP take threshold tightened: full-size take at |dev|>=2 (as before), plus
     a small "light take" at |dev|>=1 with size capped by inventory brake.
     HP has lag-1 autocorr ≈ -0.13 so 1-tick deviations are still mean-reverting.

Risks acknowledged:
  - 5400 longs at smile_level fair: if 5400's "cheap" status is *rational* (not
    a mispricing), we overpay. But across days 0/1/2 the residual is -0.9 to
    -1.0 vol pts with SD only 0.2, so the dislocation is highly persistent.
  - HP light takes increase trade count and adverse-selection exposure;
    inventory brake (~80% size cut at limit) protects on accumulation.

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

# OFFICIAL POSITION LIMITS (from Round 3 brief):
#   HYDROGEL_PACK         : 200  (was 50 — 4× capacity unlock)
#   VELVETFRUIT_EXTRACT   : 200
#   VEV_K (every voucher) : 300  (was 200 — 1.5× capacity unlock)
POSITION_LIMIT: Dict[str, int] = {
    HP: 200,
    VE: 200,
}
for p in VEV_PRODUCTS:
    POSITION_LIMIT[p] = 300

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

# Vega-aware threshold scaling (from Frankfurt Hedgehogs writeup):
# Low-vega options (wings, near-expiry) need wider thresholds because a given
# vol-pt move translates to less $ — false signals are more frequent.
# Threshold multiplier = clamp(vega_ref / vega_K, 0.7, 1.6).
VEGA_REF = 250.0  # ATM vega at 7-day TTE
VEV_SIZE_CAP = 25          # transient kicker cap (small — basket is the main alpha)

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
    # Limit is now 300 (was 200). Scale targets ~1.5× to match.
    5100: +180,   # cheap (was 120)
    5400: +180,   # was 120 — but EXEC_FAIR_BLEND[5400]=0 so only passive fills
    5300: -270,   # winning leg, scaled to ~90% of 300 (was -180)
    5500: -90,    # was -60
}

# How much of smile_level vs structural_fair to use for AGGRESSIVE takes.
# 0.0 = pure structural_fair (only trade transient deviations away from current
#       per-strike regime — conservative).
# 1.0 = pure smile_level (try to capture the full structural dislocation by
#       crossing the spread to fair-by-smile-mean — aggressive).
# Live submission 378625 ($665) had this implicitly at 0.0 and filled only
# 5/180 of VEV_5400 (the largest, most reliable anomaly).  Pushing 5400 to 1.0
# turns its fair_price into BS(smile_mean) — a price ABOVE the observed mid —
# so aggressive long takes fire at market asks and we capture the +1 vol-pt
# structural cheapness.  Same logic applies (in reverse) to 5500.
EXEC_FAIR_BLEND: Dict[int, float] = {
    # POST-MORTEM (sub 387184 = $201, regression of $464):
    # All structural-blend trades suffered MTM losses because structural
    # smile dislocations DO NOT REVERT WITHIN A DAY. Live MTM marks at
    # session-end mid (not expiry), so paying above mid for "structurally
    # cheap" longs (or selling below mid for "structurally rich" shorts)
    # eats the dislocation as pure cost. Only WITHIN-DAY mean reversion
    # is capturable. All blends now ≤ 0.3.
    5000: 0.0,
    5100: 0.0,
    5200: 0.0,
    5300: 0.3,   # rich; small blend — still captures bids slightly above structural
    5400: 0.0,   # forced to 0: structural cheapness persists; aggressive lifts lose
    5500: 0.3,   # rich; small blend
}

# Hedging is costly (VE spread = 5 ticks crossed per rebalance) and the basket
# is ~vega-balanced by construction (2 longs, 2-3 shorts with matched notional).
# Only hedge if net delta is large and persistent — else accept small directional
# risk.  DEADBAND raised to 120 (was 60) because with basket targets up to ±160
# per strike, normal delta swings from fill noise are bigger.
DELTA_HEDGE_DEADBAND = 120
DELTA_BUDGET_HEADROOM = 0.8


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
        self._last_hedge_ts: int = -1_000_000
        # Cooldown is in *timestamp* units (1 tick = 100 ts). 50 000 = 500 ticks
        # of quiet period between any VE hedge orders — suppresses churn.
        self._hedge_cooldown_ticks: int = 50_000
        # Live polynomial smile: rolling history of (m_t_k, iv_obs) tuples used
        # to fit a quadratic IV(m) = a + b*m + c*m^2.  Falls back to the static
        # STRUCTURAL_BIAS table until N_FIT_MIN samples are collected.
        self._smile_history: List[Tuple[float, float]] = []
        self._smile_coef: Optional[Tuple[float, float, float]] = None  # (c, b, a)
        # Live-tracked premium between HP mid and its EWMA (Hedgehogs-style).
        self._hp_mid_premium: float = 0.0
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

        # ---------------- VELVETFRUIT_EXTRACT: SCALED-UP explicit MM ----------------
        # Sub 6 lucky $503 with 46 hedge-driven fills.  v9 dials this up:
        #   - bigger MM cap (120, was 100) — less inventory headroom needed for hedge
        #   - bigger size per side (50, was 30) — typical counter volume is ~38
        #   - TWO layers when spread allows: tight (mid±1) for fill rate, wider
        #     (mid±2) for adverse-selection protection
        if ve_book is not None and ve_book.best_bid is not None and ve_book.best_ask is not None:
            ve_pos_now = positions.get(VE, 0)
            ve_skew = -2.5 * ve_pos_now / POSITION_LIMIT[VE]
            ve_mid_now = ve_book.mid
            ve_fair = ve_mid_now + ve_skew
            ve_spread = ve_book.best_ask.price - ve_book.best_bid.price
            MM_VE_CAP = 120
            ve_buy_room = max(0, MM_VE_CAP - ve_pos_now)
            ve_sell_room = max(0, MM_VE_CAP + ve_pos_now)
            if ve_spread >= 4:
                # Layer 1: tight (mid±1)
                bid1 = int(math.floor(ve_fair - 1))
                ask1 = int(math.ceil(ve_fair + 1))
                bid1 = max(bid1, int(ve_book.best_bid.price) + 1)
                ask1 = min(ask1, int(ve_book.best_ask.price) - 1)
                if bid1 > ve_book.best_bid.price and bid1 < ve_book.best_ask.price and ve_buy_room > 0:
                    sz = min(50, ve_buy_room)
                    orders.append(Order(VE, float(bid1), sz))
                    ve_buy_room -= sz
                if ask1 < ve_book.best_ask.price and ask1 > ve_book.best_bid.price and ve_sell_room > 0:
                    sz = min(50, ve_sell_room)
                    orders.append(Order(VE, float(ask1), -sz))
                    ve_sell_room -= sz
                # Layer 2: wider (mid±2) — only if spread >= 5 and we still have capacity
                if ve_spread >= 5:
                    bid2 = int(math.floor(ve_fair - 2))
                    ask2 = int(math.ceil(ve_fair + 2))
                    if bid2 > ve_book.best_bid.price and bid2 < bid1 and ve_buy_room > 0:
                        orders.append(Order(VE, float(bid2), min(40, ve_buy_room)))
                    if ask2 < ve_book.best_ask.price and ask2 > ask1 and ve_sell_room > 0:
                        orders.append(Order(VE, float(ask2), -min(40, ve_sell_room)))

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
                fair_prices_smile: Dict[int, float] = {}

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
                    # Update the live smile fit (used for diagnostics + kicker)
                    if T > 1e-9:
                        m_t_k = math.log(float(K) / ve_mid) / math.sqrt(T)
                        self._update_smile_fit(m_t_k, iv)
                    # Structural fair IV: STATIC bias table is empirically more
                    # robust than the polynomial fit for execution decisions
                    # (poly smooths over the 5400 anomaly, causing over-lifting).
                    structural_fair = smile_level + STRUCTURAL_BIAS.get(K, 0.0)
                    # residual vs the *structural* fair — only transient deviations
                    resid_transient = iv - structural_fair

                    # (a) baseline structural position
                    base = RV_BASKET_TARGETS.get(K, 0)

                    # (b) transient mean-reversion kicker — vega-scaled threshold.
                    #     Low-vega strikes need wider thresholds (Hedgehogs trick).
                    vega_K = bs_vega(ve_mid, float(K), T, structural_fair)
                    vega_scale = max(0.7, min(1.6, VEGA_REF / max(vega_K, 1.0)))
                    enter = VEV_RESID_ENTER * vega_scale
                    full = VEV_RESID_FULL * vega_scale
                    if abs(resid_transient) >= enter:
                        mag = min(1.0, (abs(resid_transient) - enter) / max(full - enter, 1e-9))
                        kicker = int(-math.copysign(VEV_SIZE_CAP * mag, resid_transient))
                    else:
                        kicker = 0
                    # combine, capped at the product's position limit
                    combined = base + kicker
                    combined = max(-POSITION_LIMIT[f"VEV_{K}"],
                                   min(POSITION_LIMIT[f"VEV_{K}"], combined))
                    desired[K] = combined

                    # Three fair prices:
                    #   fair_prices[K]       — execution fair; blend of structural_fair
                    #     and smile_level per EXEC_FAIR_BLEND[K]. Used for AGGRESSIVE
                    #     takes. Higher blend ⇒ pay more to capture structural edge.
                    #   fair_prices_smile[K] — pure smile-level fair; used for *passive*
                    #     quotes so we can post inside the spread near mid and capture
                    #     the structural cheapness/richness when counterparties cross.
                    #   structural_fair      — used only for the transient kicker signal.
                    blend = EXEC_FAIR_BLEND.get(K, 0.0)
                    exec_fair_iv = (1.0 - blend) * structural_fair + blend * smile_level
                    deltas[K] = bs_delta(ve_mid, float(K), T, exec_fair_iv)
                    fair_prices[K] = bs_call_price(ve_mid, float(K), T, exec_fair_iv)
                    fair_prices_smile[K] = bs_call_price(ve_mid, float(K), T, smile_level)

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
                        # PASSIVE top-up (long side): post TWO layers inside the
                        # spread — more aggressive (bid+2) with smaller size, and
                        # backed-off (bid+1) with bigger size.  Both only posted if
                        # below smile-fair so we still capture structural cheapness.
                        if (diff > 0 and book.best_bid is not None and
                                book.best_ask is not None):
                            smile_fair = fair_prices_smile.get(K, fair_price)
                            spread = book.best_ask.price - book.best_bid.price
                            # Tight layer: bid+2 (only if spread >= 3)
                            if spread >= 3:
                                post_px = book.best_bid.price + 2
                                if post_px < book.best_ask.price and post_px <= smile_fair:
                                    q = min(diff, 50)
                                    if q > 0:
                                        orders.append(Order(p, float(post_px), q))
                                        diff -= q
                            # Backed-off layer: bid+1 (always if spread >= 2)
                            if diff > 0 and spread >= 2:
                                post_px = book.best_bid.price + 1
                                if post_px < book.best_ask.price and post_px <= smile_fair:
                                    q = min(diff, 100)
                                    if q > 0:
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
                        # PASSIVE top-up (short side): post TWO layers inside the
                        # spread — ask-2 (more aggressive) and ask-1 (backed-off).
                        if (diff < 0 and book.best_bid is not None and
                                book.best_ask is not None):
                            smile_fair = fair_prices_smile.get(K, fair_price)
                            spread = book.best_ask.price - book.best_bid.price
                            # Tight layer: ask-2 (only if spread >= 3)
                            if spread >= 3:
                                post_px = book.best_ask.price - 2
                                if post_px > book.best_bid.price and post_px >= smile_fair:
                                    q = min(-diff, 50)
                                    if q > 0:
                                        orders.append(Order(p, float(post_px), -q))
                                        diff += q
                            # Backed-off layer: ask-1
                            if diff < 0 and spread >= 2:
                                post_px = book.best_ask.price - 1
                                if post_px > book.best_bid.price and post_px >= smile_fair:
                                    q = min(-diff, 100)
                                    if q > 0:
                                        orders.append(Order(p, float(post_px), -q))

                # 6b) PURE MM LAYER on wide-spread options — symmetric inside-spread
                # quotes that capture bid-ask regardless of directional signal.
                # Coverage extended: 5000 (spread 6), 5100 (4.4), 5200 (3) all MM'd;
                # tighter sizes/skews for the narrow strikes.
                #
                # MM size SCALED UP from 15 to 25 — counterparty volumes are 15-25
                # so we want to absorb full-size hits.
                # VEV limit raised to 300 — bigger MM sizes safe.
                _MM_CONFIG = {
                    5000: {"min_spread": 4, "size": 40, "skew_ticks": 1.5},
                    5100: {"min_spread": 3, "size": 40, "skew_ticks": 1.5},
                    5200: {"min_spread": 3, "size": 30, "skew_ticks": 1.0},
                }
                for K, cfg in _MM_CONFIG.items():
                    p = f"VEV_{K}"
                    book = books.get(p)
                    if book is None or book.best_bid is None or book.best_ask is None:
                        continue
                    spread = book.best_ask.price - book.best_bid.price
                    if spread < cfg["min_spread"]:
                        continue
                    pos = positions.get(p, 0)
                    limit = POSITION_LIMIT[p]
                    skew = -cfg["skew_ticks"] * pos / limit
                    fair_mm = 0.5 * (book.best_bid.price + book.best_ask.price) + skew
                    bid_px = int(math.floor(fair_mm - 1))
                    ask_px = int(math.ceil(fair_mm + 1))
                    bid_px = max(bid_px, int(book.best_bid.price) + 1)
                    ask_px = min(ask_px, int(book.best_ask.price) - 1)
                    buy_room = limit - pos
                    sell_room = limit + pos
                    if bid_px > book.best_bid.price and bid_px < book.best_ask.price and buy_room > 0:
                        orders.append(Order(p, float(bid_px), min(cfg["size"], buy_room)))
                    if ask_px < book.best_ask.price and ask_px > book.best_bid.price and sell_room > 0:
                        orders.append(Order(p, float(ask_px), -min(cfg["size"], sell_room)))

                # 7) VE: always-on market making + opportunistic hedge.
                #    Submission 387668 confirmed VE MM is the new alpha source
                #    (+$503 vs $0 in prior subs). Now made always-on (was gated by
                #    50k-ts cooldown that suppressed ~80% of MM opportunities) and
                #    quote size doubled 30 → 60 to harvest more flow.
                #    The hedge "anchor" is encoded as inventory-skew on the MM
                #    quotes — when |delta_drift| > deadband, the skewed mid
                #    naturally pulls position toward the hedge target. Aggressive
                #    crossing only kicks in when drift exceeds 2× deadband
                #    (genuine emergency).
                if ve_book is not None and ve_book.best_bid and ve_book.best_ask:
                    realised_option_delta = sum(
                        positions.get(f"VEV_{K}", 0) * deltas.get(K, 0.0)
                        for K in VEV_TRADED_STRIKES)
                    hedge_target = int(round(-realised_option_delta))
                    hedge_target = max(-POSITION_LIMIT[VE], min(POSITION_LIMIT[VE], hedge_target))
                    delta_order = hedge_target - ve_pos
                    ve_spread = ve_book.best_ask.price - ve_book.best_bid.price

                    # ----- A. ALWAYS-ON MM LAYER -----
                    # Skew weighted by distance from hedge target so quotes pull
                    # toward target without crossing the spread.
                    skew_strength = 4.0  # was 3.0 — pull harder toward target
                    skew = -skew_strength * (ve_pos - hedge_target) / POSITION_LIMIT[VE]
                    ve_fair = ve_mid + skew
                    if ve_spread >= 3:
                        ve_bid_px = int(round(ve_fair - 1.0))
                        ve_ask_px = int(round(ve_fair + 1.0))
                        ve_bid_px = max(ve_bid_px, int(ve_book.best_bid.price) + 1)
                        ve_ask_px = min(ve_ask_px, int(ve_book.best_ask.price) - 1)
                        ve_buy_cap = POSITION_LIMIT[VE] - ve_pos
                        ve_sell_cap = POSITION_LIMIT[VE] + ve_pos
                        # Symmetric quotes — both sides always posted unless cap exhausted.
                        # Inventory skew above already pulls the fair toward hedge target.
                        if (ve_bid_px > ve_book.best_bid.price and
                                ve_bid_px < ve_book.best_ask.price and ve_buy_cap > 0):
                            orders.append(Order(VE, float(ve_bid_px), min(60, ve_buy_cap)))
                        if (ve_ask_px < ve_book.best_ask.price and
                                ve_ask_px > ve_book.best_bid.price and ve_sell_cap > 0):
                            orders.append(Order(VE, float(ve_ask_px), -min(60, ve_sell_cap)))
                        # SECOND LAYER deeper in spread (spread >= 4) — captures
                        # larger crossings at higher edge.
                        if ve_spread >= 4:
                            ve_bid_deep = max(int(round(ve_fair - 2.0)),
                                              int(ve_book.best_bid.price) + 1)
                            ve_ask_deep = min(int(round(ve_fair + 2.0)),
                                              int(ve_book.best_ask.price) - 1)
                            if (ve_bid_deep > ve_book.best_bid.price and
                                    ve_bid_deep < ve_bid_px and ve_buy_cap > 60):
                                orders.append(Order(VE, float(ve_bid_deep),
                                                    min(60, ve_buy_cap - 60)))
                            if (ve_ask_deep < ve_book.best_ask.price and
                                    ve_ask_deep > ve_ask_px and ve_sell_cap > 60):
                                orders.append(Order(VE, float(ve_ask_deep),
                                                    -min(60, ve_sell_cap - 60)))

                    # ----- B. EMERGENCY HEDGE: cross spread only on big drift -----
                    # Was triggered at |delta_order| >= 120; now require 240 (2×
                    # deadband). This avoids paying spread when MM skew can fix it.
                    if abs(delta_order) >= 2.0 * DELTA_HEDGE_DEADBAND:
                        if delta_order > 0:
                            q = min(delta_order, ve_book.best_ask.volume,
                                    POSITION_LIMIT[VE] - ve_pos)
                            if q > 0:
                                orders.append(Order(VE, ve_book.best_ask.price, q))
                        else:
                            q = min(-delta_order, ve_book.best_bid.volume,
                                    POSITION_LIMIT[VE] + ve_pos)
                            if q > 0:
                                orders.append(Order(VE, ve_book.best_bid.price, -q))

        # NOTE: WING CALL OVERWRITE (6000/6500) was removed in this revision.
        # Sub 387668 confirmed: zero fills despite 1000 ticks of posted -50 lots
        # at price 1.0. The bot quotes 16-20 contracts at the same price level
        # ahead of us in queue priority, and the buy-flow on those strikes
        # (~95 trades/day) consistently hits the bot first. Not worth the
        # ~3000 wasted orders/day.

        return orders

    # ----- live polynomial smile (Frankfurt Hedgehogs / Optiver-style) -----
    _N_FIT_MIN = 60        # need this many (m, iv) samples before we trust the fit
    _N_FIT_MAX = 600       # rolling window cap
    _FIT_REFRESH_EVERY = 50

    def _update_smile_fit(self, m_t_k: float, iv_obs: float):
        self._smile_history.append((m_t_k, iv_obs))
        if len(self._smile_history) > self._N_FIT_MAX:
            self._smile_history = self._smile_history[-self._N_FIT_MAX:]
        if (len(self._smile_history) >= self._N_FIT_MIN and
                len(self._smile_history) % self._FIT_REFRESH_EVERY == 0):
            xs = [p[0] for p in self._smile_history]
            ys = [p[1] for p in self._smile_history]
            n = len(xs)
            sx = sum(xs); sy = sum(ys)
            sxx = sum(x*x for x in xs); sxy = sum(x*y for x,y in zip(xs,ys))
            sxxx = sum(x*x*x for x in xs); sxxy = sum(x*x*y for x,y in zip(xs,ys))
            sxxxx = sum(x**4 for x in xs)
            # Solve normal equations for y = a + b*x + c*x^2
            A = [[n, sx, sxx], [sx, sxx, sxxx], [sxx, sxxx, sxxxx]]
            B = [sy, sxy, sxxy]
            try:
                # Cramer's rule (3x3) — avoid numpy
                def det3(m):
                    return (m[0][0]*(m[1][1]*m[2][2]-m[1][2]*m[2][1])
                            - m[0][1]*(m[1][0]*m[2][2]-m[1][2]*m[2][0])
                            + m[0][2]*(m[1][0]*m[2][1]-m[1][1]*m[2][0]))
                D = det3(A)
                if abs(D) > 1e-12:
                    Da = det3([[B[0],A[0][1],A[0][2]], [B[1],A[1][1],A[1][2]], [B[2],A[2][1],A[2][2]]])
                    Db = det3([[A[0][0],B[0],A[0][2]], [A[1][0],B[1],A[1][2]], [A[2][0],B[2],A[2][2]]])
                    Dc = det3([[A[0][0],A[0][1],B[0]], [A[1][0],A[1][1],B[1]], [A[2][0],A[2][1],B[2]]])
                    a = Da/D; b = Db/D; c = Dc/D
                    self._smile_coef = (c, b, a)
            except Exception:
                pass

    def _smile_iv(self, K: float, S: float, T: float, fallback_level: float) -> float:
        """Return polynomial-smile IV for strike K. Falls back to flat smile_level
        plus the static STRUCTURAL_BIAS until we have enough fitted samples."""
        if T <= 0 or S <= 0:
            return fallback_level
        if self._smile_coef is None:
            return fallback_level + STRUCTURAL_BIAS.get(int(K), 0.0)
        c, b, a = self._smile_coef
        m = math.log(K / S) / math.sqrt(T)
        return a + b * m + c * m * m

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
        # Two-tier takes:
        #   - Aggressive (|dev|>=2): take with full capacity (catches big reversions).
        #   - Light    (|dev|>=1):  take a small slice (1-tick edge, ~85% of ticks).
        # HP has lag-1 autocorr ≈ -0.13, so taking when mid is locally extreme is +EV.
        # Using inventory skew via `fair` for the size cap so we don't accumulate.
        inv_brake = max(0.2, 1.0 - abs(pos) / float(limit))   # taper as we approach limit
        if book.best_ask is not None and buy_cap > 0:
            if book.best_ask.price <= mid - 2:
                q = min(buy_cap, book.best_ask.volume)
                orders.append(Order(HP, book.best_ask.price, q))
                buy_cap -= q
            elif book.best_ask.price <= mid - 1:
                q = int(min(buy_cap, book.best_ask.volume, 15 * inv_brake))
                if q > 0:
                    orders.append(Order(HP, book.best_ask.price, q))
                    buy_cap -= q
        if book.best_bid is not None and sell_cap > 0:
            if book.best_bid.price >= mid + 2:
                q = min(sell_cap, book.best_bid.volume)
                orders.append(Order(HP, book.best_bid.price, -q))
                sell_cap -= q
            elif book.best_bid.price >= mid + 1:
                q = int(min(sell_cap, book.best_bid.volume, 15 * inv_brake))
                if q > 0:
                    orders.append(Order(HP, book.best_bid.price, -q))
                    sell_cap -= q

        # --- MAKE: single layer at mid ± 2 (sub 4's winning config) ---
        # Two-layer at mid±1 was tried in sub 5 and cost $75 (tight quotes got
        # adversely selected on 1-tick mean-reverting noise).  A single mid±2
        # layer is more conservative but historically prints $288 consistently.
        if book.best_bid is not None and book.best_ask is not None:
            bid_px = int(math.floor(fair - 2))
            ask_px = int(math.ceil(fair + 2))
            bid_px = max(bid_px, int(book.best_bid.price) + 1)
            ask_px = min(ask_px, int(book.best_ask.price) - 1)
            # HP limit is 200, counterparty volumes are ~12-15.  Quote 50 lots
            # so we can absorb full-size hits and accumulate inventory across
            # 4× the previous capacity.
            if bid_px > book.best_bid.price and bid_px < book.best_ask.price and buy_cap > 0:
                orders.append(Order(HP, float(bid_px), min(50, buy_cap)))
            if ask_px < book.best_ask.price and ask_px > book.best_bid.price and sell_cap > 0:
                orders.append(Order(HP, float(ask_px), -min(50, sell_cap)))
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