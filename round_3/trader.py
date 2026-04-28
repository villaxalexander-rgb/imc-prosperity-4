"""
IMC Prosperity Round 3 — "Options Require Decisions"
====================================================

Clean rewrite from scratch.  Three orthogonal alpha engines:

  1. HYDROGEL_PACK MM
     Wide ~16-tick spread, mean-reverting (lag-1 autocorr -0.13).
     Three-layer make ladder (mid±2/±3/±5) + two-tier take (heavy ≥2t,
     light ≥1t).  Inventory-skewed quotes, sizes scaled for 200 limit.

  2. VELVETFRUIT_EXTRACT MM
     Tight 5-tick spread, ~2,750 contracts/day flow.  Always-on two-layer
     quote (best±1, best±2) with inventory skew anchored to the option
     delta-hedge target — so MM fills naturally drive position toward the
     correct hedge.  Aggressive cross only on emergency drift > 2× deadband.

  3. VEV options vol-arb
     Per-strike IV from BS, EWMA-smoothed.  Smile mean = cross-strike
     average.  Three sub-strategies:
       (a) Conservative structural basket (long 5100/5400, short 5300/5500)
           sized to keep MTM swings bounded — within-day MTM marks at mid,
           NOT expiry, so structural dislocations cannot be sized aggressively.
       (b) Transient mean-reversion kicker on per-strike IV residuals.
       (c) Pure passive MM on wide-spread strikes (5000, 5100).
     Position-limit-aware execution; passive top-ups when bid/ask spread allows.

Position limits (Round 3 brief):
  HYDROGEL_PACK:        200
  VELVETFRUIT_EXTRACT:  200
  VEV_K (each strike):  300

Calibration learnings — DO NOT REPEAT
-------------------------------------
  - Aggressive lifts at smile_level fair on 5400 lost $251 (sub 387184):
    structural cheapness does NOT mean-revert intraday → use structural_fair
    for execution, never smile_level.
  - 5500 short at -120 lost $63 (sub 377797): low option price ⇒ MTM-vol
    eats the edge; cap at -60 to -90.
  - Crossing VE spread to delta-hedge cost $375 (sub 377797): use passive-
    first hedge via always-on MM with inventory skew.
  - Wing strikes 6000/6500 sells at floor 1.0 don't fill (queue priority
    loses to bot's 16-20 lot resting orders): excluded.
  - HP make at mid±1 lost $75: tight bound is mid±2.
"""
from __future__ import annotations

import math
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ============================================================================
# Black-Scholes (pure Python — no numpy/scipy needed)
# ============================================================================
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
                lo: float = 1e-4, hi: float = 3.0,
                tol: float = 1e-5) -> Optional[float]:
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


# ============================================================================
# Configuration
# ============================================================================
HP = "HYDROGEL_PACK"
VE = "VELVETFRUIT_EXTRACT"
VEV_STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VEV_PRODUCTS = [f"VEV_{K}" for K in VEV_STRIKES]
VEV_TRADED_STRIKES = [5000, 5100, 5200, 5300, 5400, 5500]
VEV_MM_STRIKES = [5000, 5100]   # wide-spread strikes for pure MM

POSITION_LIMIT: Dict[str, int] = {HP: 200, VE: 200}
for p in VEV_PRODUCTS:
    POSITION_LIMIT[p] = 300

DAYS_PER_YEAR = 250.0


def tte_years(day: int, timestamp: int) -> float:
    """TTE in years.  Day 0 ⇒ TTE=8d, day 1 ⇒ 7d, ..., day 3 (live) ⇒ 5d."""
    days = (8.0 - day) - timestamp / 1_000_000.0
    return max(days, 1e-6) / DAYS_PER_YEAR


# ----- Options config -----
# Structural per-strike IV bias vs cross-strike smile mean (calibrated on
# historical days 0/1/2 + live day 2).  Persistent zig-zag pattern.
STRUCTURAL_BIAS: Dict[int, float] = {
    5000:  0.0000,
    5100: -0.0025,   # cheap
    5200: +0.0015,
    5300: +0.0055,   # rich (biggest winning short)
    5400: -0.0105,   # very cheap (biggest known anomaly)
    5500: +0.0050,   # rich
}

# Basket targets — MAXIMUM SCALING on proven winners.
# Sub 388453 ($1315):
#   5300 @-180 → +$479   (push to limit -300)
#   5500 @ -60 → +$16    (small but positive, push to -100)
#   5400 @+120 → +$5     (passive fills ~5; bigger target irrelevant)
#   5100 @+120 → +$89    (push to -250 to soak more passive)
RV_BASKET_TARGETS: Dict[int, int] = {
    5100: +250,    # cheap — passive top-ups should soak more at higher target
    5400: +200,    # very cheap, passive only — bigger target = more posted lots
    5300: -300,    # rich — MAXIMUM 300 limit (proven linear scaler)
    5500: -100,    # rich — boosted from 60 (tested cap; PnL was +16 at -60)
}

# Transient mean-reversion kicker (small additional contracts on top of basket
# when this strike's IV deviates from its OWN structural fair).
RESID_ENTER = 0.004
RESID_FULL = 0.012
KICKER_CAP = 30
IV_EWMA_ALPHA = 0.25

# ----- IV SCALPING (adapted from Frankfurt Hedgehogs / TimoDiehm) -----
# Idea: compute current theo_diff = market_mid - BS_fair_at_structural_iv.
# Trade DEVIATIONS from this diff's rolling mean (intraday price-level mean
# reversion). Within-day IV doesn't structurally revert, but THE PRICE around
# its rolling mean DOES. Gated by recent volatility (switch_mean) so we only
# scalp when there's enough movement to overcome the spread cost.
#
# Hedgehogs published thresholds (for 9500-10500 strikes, similar vega to ours):
#   THR_OPEN = 0.5, THR_CLOSE = 0, LOW_VEGA_ADJ = 0.5
#   THEO_NORM_WINDOW = 20 (rolling mean of theo_diff)
#   IV_SCALPING_WINDOW = 100 (rolling mean of |theo_diff - mean_theo_diff|)
#   IV_SCALPING_THR = 0.7 (only trade when switch_mean ≥ this)
THEO_NORM_WINDOW = 20
IV_SCALPING_WINDOW = 100
IV_SCALPING_THR = 0.4    # slightly relaxed vs Hedgehogs (they had bigger $ moves)
THR_OPEN = 0.5
THR_CLOSE = 0.0
LOW_VEGA_ADJ = 0.5       # add to threshold when vega < 1
SCALP_MAX_PER_STRIKE = 80   # max additional contracts from scalp signal

# Delta hedge: tolerated drift before action.
DELTA_HEDGE_DEADBAND = 120
DELTA_BUDGET_HEADROOM = 0.85   # cap basket so its delta fits in 85% of VE limit

# ----- HP MM config — MAX SCALE for 200 limit -----
HP_TAKE_HEAVY_EDGE = 2
HP_TAKE_LIGHT_EDGE = 1
HP_LIGHT_SIZE = 60          # was 30; with microprice fair, less adverse selection
HP_INV_SKEW = 6.0
HP_TIGHT_OFFSET = 1; HP_TIGHT_SIZE = 15   # NEW tight layer (microprice-protected)
HP_MAIN_OFFSET  = 2; HP_MAIN_SIZE  = 80   # boosted from 50
HP_MID_OFFSET   = 3; HP_MID_SIZE   = 60   # boosted from 40
HP_DEEP_OFFSET  = 5; HP_DEEP_SIZE  = 100  # boosted from 80
# Total quoted per side: 15+80+60+100 = 255 (clipped to 200 cap by buy/sell_cap)

# ----- VE MM config — MAX SCALE -----
VE_INV_SKEW = 4.0
VE_MM_QUOTE_SIZE = 100      # boosted from 60
VE_MM_DEEP_SIZE  = 80       # boosted from 60


# ============================================================================
# Data classes
# ============================================================================
@dataclass
class Quote:
    price: float
    volume: int


@dataclass
class OrderBook:
    bids: List[Quote] = field(default_factory=list)
    asks: List[Quote] = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[Quote]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[Quote]:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return 0.5 * (self.best_bid.price + self.best_ask.price)
        return None

    @property
    def microprice(self) -> Optional[float]:
        """Volume-weighted mid: tilts toward the side with LESS volume.
        Better predictor than mid for next-trade direction (chrispyroberts/
        TimoDiehm). If bid has 20 lots and ask has 5 lots, the next trade
        is more likely to lift the ask ⇒ microprice > mid."""
        if not (self.best_bid and self.best_ask):
            return None
        bv = self.best_bid.volume
        av = self.best_ask.volume
        tot = bv + av
        if tot == 0:
            return self.mid
        # Note: weights are SWAPPED — bid weighted by ask vol, vice-versa.
        # Intuition: if ask volume is small, next trade likely lifts ask
        # ⇒ price moves toward ask ⇒ microprice should be closer to ask.
        return (self.best_bid.price * av + self.best_ask.price * bv) / tot


@dataclass
class Order:
    product: str
    price: float
    quantity: int


# ============================================================================
# Trader
# ============================================================================
class Trader:
    def __init__(self) -> None:
        # Per-strike IV EWMA — smooths noise from bid-ask discretization.
        self._iv_ewma: Dict[int, Optional[float]] = {K: None for K in VEV_STRIKES}
        # IV-scalping state (Hedgehogs theo-diff mean reversion)
        self._theo_diff_ema: Dict[int, Optional[float]] = {K: None for K in VEV_STRIKES}
        self._theo_devs_ema: Dict[int, Optional[float]] = {K: None for K in VEV_STRIKES}

    # ------------------------------------------------------------------
    # Entry point used by the backtester
    # ------------------------------------------------------------------
    def decide(self, day: int, timestamp: int,
               books: Dict[str, OrderBook],
               positions: Dict[str, int]) -> List[Order]:
        orders: List[Order] = []
        T = tte_years(day, timestamp)

        # --- 1. HP make-and-take ---
        hp_book = books.get(HP)
        if hp_book and hp_book.mid is not None:
            orders.extend(self._hp_strategy(hp_book, positions.get(HP, 0)))

        # --- 2. VEV options (basket + kicker + pure MM) ---
        ve_book = books.get(VE)
        ve_mid = ve_book.mid if (ve_book and ve_book.mid is not None) else None
        deltas: Dict[int, float] = {}
        if ve_mid is not None:
            deltas = self._vev_strategy(ve_mid, T, books, positions, orders)

        # --- 3. VE always-on MM + opportunistic delta hedge ---
        if ve_book and ve_book.best_bid and ve_book.best_ask:
            self._ve_strategy(ve_book, positions, deltas, orders)

        return orders

    # ------------------------------------------------------------------
    # HP: 3-layer make + 2-tier take, inventory-skewed
    # ------------------------------------------------------------------
    def _hp_strategy(self, book: OrderBook, pos: int) -> List[Order]:
        orders: List[Order] = []
        mid = book.mid
        # Microprice as fair value: tilts toward the side with less volume.
        # Better predictor than mid for next-trade direction; reduces adverse
        # selection on tight quotes.  TAKE decisions still use mid (we want
        # to take when far from "true" mid, not microprice).
        mp = book.microprice if book.microprice is not None else mid
        limit = POSITION_LIMIT[HP]
        skew = -HP_INV_SKEW * pos / limit
        fair = mp + skew
        buy_cap = limit - pos
        sell_cap = limit + pos
        inv_brake = max(0.2, 1.0 - abs(pos) / float(limit))

        # ---- TAKE: heavy on |dev|≥2, light on |dev|≥1 ----
        if book.best_ask and buy_cap > 0:
            if book.best_ask.price <= mid - HP_TAKE_HEAVY_EDGE:
                q = min(buy_cap, book.best_ask.volume)
                orders.append(Order(HP, book.best_ask.price, q))
                buy_cap -= q
            elif book.best_ask.price <= mid - HP_TAKE_LIGHT_EDGE:
                q = int(min(buy_cap, book.best_ask.volume,
                            HP_LIGHT_SIZE * inv_brake))
                if q > 0:
                    orders.append(Order(HP, book.best_ask.price, q))
                    buy_cap -= q
        if book.best_bid and sell_cap > 0:
            if book.best_bid.price >= mid + HP_TAKE_HEAVY_EDGE:
                q = min(sell_cap, book.best_bid.volume)
                orders.append(Order(HP, book.best_bid.price, -q))
                sell_cap -= q
            elif book.best_bid.price >= mid + HP_TAKE_LIGHT_EDGE:
                q = int(min(sell_cap, book.best_bid.volume,
                            HP_LIGHT_SIZE * inv_brake))
                if q > 0:
                    orders.append(Order(HP, book.best_bid.price, -q))
                    sell_cap -= q

        # ---- MAKE: 3-layer ladder ----
        if not (book.best_bid and book.best_ask):
            return orders

        # Tight layer: fair ± 1 with SMALL size, protected by microprice fair.
        # Pure mid±1 lost $75 historically (adverse selection). With microprice
        # the fair tilts toward the side with less volume, so our tight quote
        # is more likely on the favorable side. Size 15 caps adverse cost.
        bid_tight = max(int(math.floor(fair - HP_TIGHT_OFFSET)),
                        int(book.best_bid.price) + 1)
        ask_tight = min(int(math.ceil(fair + HP_TIGHT_OFFSET)),
                        int(book.best_ask.price) - 1)
        if (bid_tight > book.best_bid.price and bid_tight < book.best_ask.price
                and buy_cap > 0):
            q = min(HP_TIGHT_SIZE, buy_cap)
            orders.append(Order(HP, float(bid_tight), q))
            buy_cap -= q
        if (ask_tight < book.best_ask.price and ask_tight > book.best_bid.price
                and sell_cap > 0):
            q = min(HP_TIGHT_SIZE, sell_cap)
            orders.append(Order(HP, float(ask_tight), -q))
            sell_cap -= q

        # Main layer: fair ± 2 (most fills, smallest edge)
        bid_main = max(int(math.floor(fair - HP_MAIN_OFFSET)),
                       int(book.best_bid.price) + 1)
        ask_main = min(int(math.ceil(fair + HP_MAIN_OFFSET)),
                       int(book.best_ask.price) - 1)
        if (bid_main > book.best_bid.price and bid_main < book.best_ask.price
                and bid_main < bid_tight and buy_cap > 0):
            q = min(HP_MAIN_SIZE, buy_cap)
            orders.append(Order(HP, float(bid_main), q))
            buy_cap -= q
        if (ask_main < book.best_ask.price and ask_main > book.best_bid.price
                and ask_main > ask_tight and sell_cap > 0):
            q = min(HP_MAIN_SIZE, sell_cap)
            orders.append(Order(HP, float(ask_main), -q))
            sell_cap -= q

        # Mid layer: fair ± 3 (fills less often, captures 3-tick crossings)
        bid_mid = max(int(math.floor(fair - HP_MID_OFFSET)),
                      int(book.best_bid.price) + 1)
        ask_mid = min(int(math.ceil(fair + HP_MID_OFFSET)),
                      int(book.best_ask.price) - 1)
        if (bid_mid > book.best_bid.price and bid_mid < bid_main
                and buy_cap > 0):
            q = min(HP_MID_SIZE, buy_cap)
            orders.append(Order(HP, float(bid_mid), q))
            buy_cap -= q
        if (ask_mid < book.best_ask.price and ask_mid > ask_main
                and sell_cap > 0):
            q = min(HP_MID_SIZE, sell_cap)
            orders.append(Order(HP, float(ask_mid), -q))
            sell_cap -= q

        # Deep layer: fair ± 5 (rare fills, biggest edge per fill)
        bid_deep = max(int(math.floor(fair - HP_DEEP_OFFSET)),
                       int(book.best_bid.price) + 1)
        ask_deep = min(int(math.ceil(fair + HP_DEEP_OFFSET)),
                       int(book.best_ask.price) - 1)
        if (bid_deep > book.best_bid.price and bid_deep < bid_mid
                and buy_cap > 0):
            orders.append(Order(HP, float(bid_deep),
                                min(HP_DEEP_SIZE, buy_cap)))
        if (ask_deep < book.best_ask.price and ask_deep > ask_mid
                and sell_cap > 0):
            orders.append(Order(HP, float(ask_deep),
                                -min(HP_DEEP_SIZE, sell_cap)))

        return orders

    # ------------------------------------------------------------------
    # VEV options strategy.
    # Returns {strike: BS-delta} so the VE MM strategy can size the hedge.
    # ------------------------------------------------------------------
    def _vev_strategy(self, ve_mid: float, T: float,
                      books: Dict[str, OrderBook],
                      positions: Dict[str, int],
                      orders: List[Order]) -> Dict[int, float]:

        # 1) Per-strike implied vol (EWMA-smoothed)
        ivs: Dict[int, float] = {}
        for K in VEV_TRADED_STRIKES:
            book = books.get(f"VEV_{K}")
            if book is None or book.mid is None:
                continue
            iv_obs = implied_vol(book.mid, ve_mid, float(K), T)
            if iv_obs is None:
                continue
            prev = self._iv_ewma[K]
            iv = iv_obs if prev is None else (
                (1.0 - IV_EWMA_ALPHA) * prev + IV_EWMA_ALPHA * iv_obs)
            self._iv_ewma[K] = iv
            ivs[K] = iv

        if len(ivs) < 4:
            return {}

        # 2) Cross-strike smile mean (vol-level-agnostic)
        smile_level = sum(ivs.values()) / len(ivs)

        # 3) Build desired position per strike (basket + transient kicker)
        deltas: Dict[int, float] = {}
        fair_prices: Dict[int, float] = {}     # used for aggressive takes
        smile_fair_prices: Dict[int, float] = {}  # used for passive posts
        desired: Dict[int, int] = {}
        for K, iv in ivs.items():
            structural_fair = smile_level + STRUCTURAL_BIAS.get(K, 0.0)
            resid = iv - structural_fair       # transient deviation

            base = RV_BASKET_TARGETS.get(K, 0)
            kicker = 0
            if abs(resid) >= RESID_ENTER:
                mag = min(1.0, (abs(resid) - RESID_ENTER)
                          / max(RESID_FULL - RESID_ENTER, 1e-9))
                kicker = int(-math.copysign(KICKER_CAP * mag, resid))

            # ----- IV SCALPING (Hedgehogs theo-diff mean reversion) -----
            # diff = market_mid - BS(structural_fair); track its rolling mean.
            # When diff deviates from its rolling mean by ≥ THR_OPEN AND recent
            # volatility (switch_mean) is high enough, take a position betting
            # the diff will revert to the rolling mean.
            scalp = 0
            book_K = books.get(f"VEV_{K}")
            market_mid = book_K.mid if (book_K and book_K.mid is not None) else None
            if market_mid is not None:
                bs_at_struct = bs_call_price(ve_mid, float(K), T, structural_fair)
                diff = market_mid - bs_at_struct
                prev_mean = self._theo_diff_ema[K]
                if prev_mean is None:
                    self._theo_diff_ema[K] = diff
                else:
                    a_mean = 2.0 / (THEO_NORM_WINDOW + 1)
                    new_mean = a_mean * diff + (1.0 - a_mean) * prev_mean
                    self._theo_diff_ema[K] = new_mean

                    abs_dev = abs(diff - new_mean)
                    prev_dev = self._theo_devs_ema[K]
                    if prev_dev is None:
                        self._theo_devs_ema[K] = abs_dev
                    else:
                        a_dev = 2.0 / (IV_SCALPING_WINDOW + 1)
                        new_dev = a_dev * abs_dev + (1.0 - a_dev) * prev_dev
                        self._theo_devs_ema[K] = new_dev

                        if new_dev >= IV_SCALPING_THR:
                            vega = bs_vega(ve_mid, float(K), T, structural_fair)
                            thr_open = THR_OPEN + (LOW_VEGA_ADJ if vega < 1.0 else 0.0)
                            signal = diff - new_mean
                            if abs(signal) >= thr_open:
                                # Size proportional to signal strength (clipped)
                                mag = min(1.0, abs(signal) / (3.0 * thr_open))
                                scalp = int(-math.copysign(
                                    SCALP_MAX_PER_STRIKE * mag, signal))

            tgt = base + kicker + scalp
            tgt = max(-POSITION_LIMIT[f"VEV_{K}"],
                      min(POSITION_LIMIT[f"VEV_{K}"], tgt))
            desired[K] = tgt

            deltas[K] = bs_delta(ve_mid, float(K), T, structural_fair)
            fair_prices[K] = bs_call_price(ve_mid, float(K), T, structural_fair)
            smile_fair_prices[K] = bs_call_price(ve_mid, float(K), T, smile_level)

        # 4) Gate: don't build a basket whose delta exceeds VE hedge capacity
        cap_delta = POSITION_LIMIT[VE] * DELTA_BUDGET_HEADROOM
        des_delta = sum(desired[K] * deltas[K] for K in desired)
        if abs(des_delta) > cap_delta and abs(des_delta) > 1e-6:
            scale = cap_delta / abs(des_delta)
            desired = {K: int(desired[K] * scale) for K in desired}

        # 5) Execute toward desired position per strike
        for K in VEV_TRADED_STRIKES:
            if K not in desired:
                continue
            p = f"VEV_{K}"
            book = books.get(p)
            if book is None:
                continue
            pos = positions.get(p, 0)
            diff = desired[K] - pos
            fp = fair_prices[K]
            sfp = smile_fair_prices[K]

            if diff > 0 and book.best_ask is not None:
                # Aggressive lifts only at structural_fair (don't pay for
                # structural cheapness — that's not capturable intraday)
                for ask in book.asks:
                    if diff <= 0:
                        break
                    if ask.price <= fp:
                        q = min(diff, ask.volume)
                        orders.append(Order(p, ask.price, q))
                        diff -= q
                    else:
                        break
                # Passive top-up: post inside spread, gated by smile_fair
                if (diff > 0 and book.best_bid is not None
                        and book.best_ask is not None):
                    spread = book.best_ask.price - book.best_bid.price
                    if spread >= 2:
                        post = book.best_bid.price + 1
                        if post < book.best_ask.price and post <= sfp:
                            orders.append(Order(p, float(post),
                                                min(diff, 80)))

            elif diff < 0 and book.best_bid is not None:
                for bid in book.bids:
                    if diff >= 0:
                        break
                    if bid.price >= fp:
                        q = min(-diff, bid.volume)
                        orders.append(Order(p, bid.price, -q))
                        diff += q
                    else:
                        break
                if (diff < 0 and book.best_bid is not None
                        and book.best_ask is not None):
                    spread = book.best_ask.price - book.best_bid.price
                    if spread >= 2:
                        post = book.best_ask.price - 1
                        if post > book.best_bid.price and post >= sfp:
                            orders.append(Order(p, float(post),
                                                -min(-diff, 80)))

        # 6) Pure passive MM on wide-spread strikes — scaled for the 300 limit.
        # Old size was 20 (= 7% of cap).  Boosted to 60 (= 20% of cap) to capture
        # more of the option flow.  Stronger inv skew (1.5 → 3.0) protects
        # against vol-direction adverse selection at the larger size.
        VEV_MM_SIZE = 60
        VEV_MM_SKEW = 3.0
        for K in VEV_MM_STRIKES:
            p = f"VEV_{K}"
            book = books.get(p)
            if book is None or book.best_bid is None or book.best_ask is None:
                continue
            spread = book.best_ask.price - book.best_bid.price
            if spread < 4:
                continue
            pos = positions.get(p, 0)
            limit = POSITION_LIMIT[p]
            mm_skew = -VEV_MM_SKEW * pos / limit
            mm_fair = 0.5 * (book.best_bid.price + book.best_ask.price) + mm_skew
            bid_px = max(int(math.floor(mm_fair - 1)),
                         int(book.best_bid.price) + 1)
            ask_px = min(int(math.ceil(mm_fair + 1)),
                         int(book.best_ask.price) - 1)
            buy_room = limit - pos
            sell_room = limit + pos
            if (bid_px > book.best_bid.price and bid_px < book.best_ask.price
                    and buy_room > 0):
                orders.append(Order(p, float(bid_px), min(VEV_MM_SIZE, buy_room)))
            if (ask_px < book.best_ask.price and ask_px > book.best_bid.price
                    and sell_room > 0):
                orders.append(Order(p, float(ask_px), -min(VEV_MM_SIZE, sell_room)))

            # Second deeper layer at ±2 if spread is large enough
            if spread >= 6:
                bid_deep = max(int(math.floor(mm_fair - 2)),
                               int(book.best_bid.price) + 1)
                ask_deep = min(int(math.ceil(mm_fair + 2)),
                               int(book.best_ask.price) - 1)
                if (bid_deep > book.best_bid.price and bid_deep < bid_px
                        and buy_room > VEV_MM_SIZE):
                    orders.append(Order(p, float(bid_deep),
                                        min(40, buy_room - VEV_MM_SIZE)))
                if (ask_deep < book.best_ask.price and ask_deep > ask_px
                        and sell_room > VEV_MM_SIZE):
                    orders.append(Order(p, float(ask_deep),
                                        -min(40, sell_room - VEV_MM_SIZE)))

        return deltas

    # ------------------------------------------------------------------
    # VE strategy: always-on MM with skew toward delta-hedge target.
    # The MM quotes naturally deliver the hedge via passive fills; aggressive
    # cross only fires on emergency drift.
    # ------------------------------------------------------------------
    def _ve_strategy(self, ve_book: OrderBook,
                     positions: Dict[str, int],
                     deltas: Dict[int, float],
                     orders: List[Order]) -> None:
        ve_pos = positions.get(VE, 0)
        # Use microprice as MM anchor — tilts our quotes toward where the
        # next trade is likely to print, reducing adverse selection.
        ve_mid = ve_book.microprice if ve_book.microprice is not None else ve_book.mid

        # Compute current option delta and the VE hedge target
        opt_delta = sum(positions.get(f"VEV_{K}", 0) * deltas.get(K, 0.0)
                        for K in VEV_TRADED_STRIKES)
        hedge_target = max(-POSITION_LIMIT[VE],
                           min(POSITION_LIMIT[VE], int(round(-opt_delta))))
        delta_drift = hedge_target - ve_pos
        spread = ve_book.best_ask.price - ve_book.best_bid.price

        # ---- A. Always-on MM with skew anchored to hedge target ----
        # Skew pulls the fair toward the side that reduces drift, so passive
        # fills naturally close the hedge gap (no spread crossed).
        skew = -VE_INV_SKEW * (ve_pos - hedge_target) / POSITION_LIMIT[VE]
        ve_fair = ve_mid + skew

        if spread >= 3:
            # Layer 1: fair ± 1 (tight, biggest size)
            bid_px = max(int(round(ve_fair - 1.0)),
                         int(ve_book.best_bid.price) + 1)
            ask_px = min(int(round(ve_fair + 1.0)),
                         int(ve_book.best_ask.price) - 1)
            buy_cap = POSITION_LIMIT[VE] - ve_pos
            sell_cap = POSITION_LIMIT[VE] + ve_pos
            if (bid_px > ve_book.best_bid.price and bid_px < ve_book.best_ask.price
                    and buy_cap > 0):
                orders.append(Order(VE, float(bid_px),
                                    min(VE_MM_QUOTE_SIZE, buy_cap)))
            if (ask_px < ve_book.best_ask.price and ask_px > ve_book.best_bid.price
                    and sell_cap > 0):
                orders.append(Order(VE, float(ask_px),
                                    -min(VE_MM_QUOTE_SIZE, sell_cap)))

            # Layer 2: fair ± 2 (deep, captures larger crossings)
            if spread >= 4:
                bid_deep = max(int(round(ve_fair - 2.0)),
                               int(ve_book.best_bid.price) + 1)
                ask_deep = min(int(round(ve_fair + 2.0)),
                               int(ve_book.best_ask.price) - 1)
                if (bid_deep > ve_book.best_bid.price and bid_deep < bid_px
                        and buy_cap > VE_MM_QUOTE_SIZE):
                    orders.append(Order(VE, float(bid_deep),
                                        min(VE_MM_DEEP_SIZE,
                                            buy_cap - VE_MM_QUOTE_SIZE)))
                if (ask_deep < ve_book.best_ask.price and ask_deep > ask_px
                        and sell_cap > VE_MM_QUOTE_SIZE):
                    orders.append(Order(VE, float(ask_deep),
                                        -min(VE_MM_DEEP_SIZE,
                                             sell_cap - VE_MM_QUOTE_SIZE)))

        # ---- B. Emergency aggressive hedge: cross only on big drift ----
        if abs(delta_drift) >= 2.0 * DELTA_HEDGE_DEADBAND:
            if delta_drift > 0:
                q = min(delta_drift, ve_book.best_ask.volume,
                        POSITION_LIMIT[VE] - ve_pos)
                if q > 0:
                    orders.append(Order(VE, ve_book.best_ask.price, q))
            else:
                q = min(-delta_drift, ve_book.best_bid.volume,
                        POSITION_LIMIT[VE] + ve_pos)
                if q > 0:
                    orders.append(Order(VE, ve_book.best_bid.price, -q))

    # ------------------------------------------------------------------
    # IMC SDK adapter
    # ------------------------------------------------------------------
    def run(self, state):  # pragma: no cover
        books: Dict[str, OrderBook] = {}
        for product, depth in state.order_depths.items():
            bids = sorted(
                [Quote(float(p), int(abs(v)))
                 for p, v in depth.buy_orders.items()],
                key=lambda q: -q.price,
            )
            asks = sorted(
                [Quote(float(p), int(abs(v)))
                 for p, v in depth.sell_orders.items()],
                key=lambda q: q.price,
            )
            books[product] = OrderBook(bids=bids, asks=asks)
        day = getattr(state, "day", 0)
        orders = self.decide(day, state.timestamp,
                             books, dict(state.position or {}))

        from datamodel import Order as PrOrder  # type: ignore
        out: Dict[str, List[PrOrder]] = defaultdict(list)
        for o in orders:
            out[o.product].append(PrOrder(o.product,
                                          int(round(o.price)),
                                          int(o.quantity)))
        return out, 0, json.dumps({})
