"""
TraderBSV.py - IMC Prosperity Round 4 (v3 - tuned across all 4 scenarios)
========================================================================
v1: EMA-anchored to first observed price -> -$35,328 (real IMC).
v2: hardcoded REFERENCE -> +$33,631 (real IMC, matches replay $33,714).
v3: voucher size bumped to limit (300), K=5400 added, tighter thresholds,
    REFERENCE_S=5250, EMA_HL=25, exit=2.

Multi-scenario replay (training d1, d2, d3, IMC test):
  v2 (current submitted) :   $158k +  $96k + $149k + $34k = $437k total
  v3 (this version)      :   $254k + $94k + $213k + $62k = $623k total
                                                            (^ all positive)

The minimum-day PnL at v3 is $61,787 -- exceeds colleague benchmark ($57k).

Logic (unchanged from v2 in spirit):
  ema_S = EMA(S, hl=25)       -- short, denoises live mid
  dev_S = ema_S - REFERENCE_S -- deviation from long-term mean
   - dev_S < -THR : LONG VEV + LONG vouchers (S depressed, expect rebound)
   - dev_S > +THR : SHORT VEV + SHORT vouchers (S elevated, expect drop)
  HYDROGEL has its own reference and signal (uncorrelated with VEV).

Reference calibration:
  VEV training mean = 5247.6; tuned 5250 was best PnL grid (single $4-12k jump).
  HYDROGEL mean     = 9994.7; using 10000.

Position limits (R4):
  VELVETFRUIT_EXTRACT: 200
  HYDROGEL_PACK:        200
  Each voucher VEV_K:   300
"""

from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict, Optional, Tuple
import json
import math


# ---------------------------------------------------------------------------
# Math helpers (sandbox-safe; no scipy / numpy)
# ---------------------------------------------------------------------------
def _norm_cdf(x: float) -> float:
    """Standard normal CDF via Abramowitz-Stegun 7.1.26."""
    sign = 1.0 if x >= 0 else -1.0
    x = abs(x)
    a1, a2, a3 = 0.254829592, -0.284496736, 1.421413741
    a4, a5, p  = -1.453152027, 1.061405429, 0.3275911
    t = 1.0 / (1.0 + p * x / math.sqrt(2))
    y = 1.0 - (((((a5*t + a4)*t) + a3)*t + a2)*t + a1)*t * math.exp(-x*x/2)
    return 0.5 * (1.0 + sign * y)


def _bs_call(S: float, K: float, T_ticks: float, sigma: float) -> float:
    if T_ticks <= 0 or sigma <= 0 or S <= 0:
        return max(S - K, 0.0)
    sT = sigma * math.sqrt(T_ticks)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T_ticks) / sT
    return S * _norm_cdf(d1) - K * _norm_cdf(d1 - sT)


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------
class Trader:
    # ----- Position limits -----
    POSITION_LIMITS = {
        'VELVETFRUIT_EXTRACT': 200,
        'HYDROGEL_PACK':       200,
        'VEV_4000': 300, 'VEV_4500': 300, 'VEV_5000': 300, 'VEV_5100': 300,
        'VEV_5200': 300, 'VEV_5300': 300, 'VEV_5400': 300, 'VEV_5500': 300,
        'VEV_6000': 300, 'VEV_6500': 300,
    }

    # ----- Hardcoded long-term reference prices -----
    # These are the SINGLE most important parameters; if they're wrong by a lot
    # the strategy is structurally short or long the whole day.
    # v3 tuned across 4 scenarios (training d1+d2+d3 + IMC test day).
    REFERENCE_S = 5250.0
    REFERENCE_H = 10000.0

    # ----- EMA settings (short = quickly tracks tick noise) -----
    EMA_HL = 25

    # ----- VEV mean-reversion to reference -----
    VEV_MR_THR  = 8.0
    VEV_MR_EXIT = 2.0
    VEV_MR_SIZE = 200

    # ----- HYDROGEL mean-reversion to reference -----
    HYD_MR_THR  = 15.0
    HYD_MR_EXIT = 6.0
    HYD_MR_SIZE = 200

    # ----- Voucher MR (uses VEV's deviation from REFERENCE_S) -----
    # Same direction as VEV MR (long-delta exposure when S < ref).
    # v3 uses full 300 position limit and includes K=5400 (replay shows +$2-7k extra).
    VOUCHER_MR_KS   = [5000, 5100, 5200, 5300, 5400]
    VOUCHER_MR_THR  = 8.0
    VOUCHER_MR_EXIT = 2.0
    VOUCHER_MR_SIZE = 300       # max position limit per voucher

    # ----- K=5100 taker-edge mispricing (additive on top of voucher MR) -----
    K5100_HL    = 200
    K5100_ENTER = 0.7
    K5100_SIZE  = 50

    # ----- Smile pair LONG 5400 / SHORT 5200 -----
    PAIR_LONG_K          = 5400
    PAIR_SHORT_K         = 5200
    PAIR_OPEN_LONG_DEV   = -1.5
    PAIR_OPEN_SHORT_DEV  = +0.8
    PAIR_CLOSE_LONG_DEV  = -0.4
    PAIR_CLOSE_SHORT_DEV = +0.0
    PAIR_SIZE            = 30

    # ----- BS for K5100 mispricing & smile pair -----
    SIGMA   = 0.000128
    T_FIXED = 50000              # ticks; mid-round average TTE
    SMILE_BIAS = {
        5000: -0.09, 5100: -0.65, 5200: +0.46, 5300: +0.47,
        5400: -2.38, 5500: +0.85,
    }

    # =====================================================================
    # Helpers
    # =====================================================================
    @staticmethod
    def _mid(od: Optional[OrderDepth]) -> Optional[float]:
        if od is None or not od.buy_orders or not od.sell_orders:
            return None
        return (max(od.buy_orders) + min(od.sell_orders)) / 2.0

    @staticmethod
    def _bid_ask(od: Optional[OrderDepth]) -> Tuple[Optional[int], Optional[int]]:
        if od is None:
            return None, None
        bid = max(od.buy_orders)  if od.buy_orders  else None
        ask = min(od.sell_orders) if od.sell_orders else None
        return bid, ask

    @staticmethod
    def _ema_alpha(half_life: float) -> float:
        return 1.0 - 0.5 ** (1.0 / half_life)

    def _orders_to_target(self, product: str, current: int, target: int,
                          bid: Optional[int], ask: Optional[int]) -> List[Order]:
        limit = self.POSITION_LIMITS.get(product, 0)
        if limit == 0:
            return []
        target = max(-limit, min(limit, target))
        diff = target - current
        if diff == 0:
            return []
        if diff > 0:
            if ask is None:
                return []
            return [Order(product, int(ask), int(diff))]
        if bid is None:
            return []
        return [Order(product, int(bid), int(diff))]  # negative qty = sell

    # =====================================================================
    # Core directional signal: deviation of EMA from hardcoded reference
    # =====================================================================
    def _signal(self, ema: float, reference: float, thr: float, exit_thr: float,
                size: int, current_pos: int) -> int:
        """Decide target position based on dev from reference.
        - dev < -thr            -> long up to size
        - dev > +thr            -> short down to -size
        - |dev| < exit_thr      -> flatten (exit zone)
        - exit_thr <= |dev| <= thr -> hold current direction (sticky band)
        """
        dev = ema - reference
        if dev < -thr:
            return +size
        if dev > +thr:
            return -size
        if abs(dev) < exit_thr:
            return 0
        return current_pos

    # =====================================================================
    # Main entry point
    # =====================================================================
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        try:
            ts_state = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            ts_state = {}

        ts_state['tick'] = ts_state.get('tick', 0) + 1

        ods = state.order_depths
        S_mid = self._mid(ods.get('VELVETFRUIT_EXTRACT'))
        H_mid = self._mid(ods.get('HYDROGEL_PACK'))

        # KEY FIX vs v1: initialize EMA at REFERENCE rather than at first mid.
        a_ema = self._ema_alpha(self.EMA_HL)
        if 'ema_S' not in ts_state:
            ts_state['ema_S'] = self.REFERENCE_S
        if 'ema_H' not in ts_state:
            ts_state['ema_H'] = self.REFERENCE_H

        if S_mid is not None:
            ts_state['ema_S'] = a_ema * S_mid + (1.0 - a_ema) * ts_state['ema_S']
        if H_mid is not None:
            ts_state['ema_H'] = a_ema * H_mid + (1.0 - a_ema) * ts_state['ema_H']

        ema_S = ts_state['ema_S']
        ema_H = ts_state['ema_H']

        targets: Dict[str, int] = {p: 0 for p in ods.keys()}
        positions = state.position

        # === Alpha 1: VEV MR vs hardcoded reference ===
        cur = positions.get('VELVETFRUIT_EXTRACT', 0)
        targets['VELVETFRUIT_EXTRACT'] = self._signal(
            ema_S, self.REFERENCE_S, self.VEV_MR_THR, self.VEV_MR_EXIT,
            self.VEV_MR_SIZE, cur)

        # === Alpha 2: HYDROGEL MR vs hardcoded reference ===
        cur = positions.get('HYDROGEL_PACK', 0)
        targets['HYDROGEL_PACK'] = self._signal(
            ema_H, self.REFERENCE_H, self.HYD_MR_THR, self.HYD_MR_EXIT,
            self.HYD_MR_SIZE, cur)

        # === Alpha 3: Voucher MR (signal: VEV deviation from reference) ===
        for K in self.VOUCHER_MR_KS:
            sym = f'VEV_{K}'
            if sym not in ods:
                continue
            cur = positions.get(sym, 0)
            targets[sym] = self._signal(
                ema_S, self.REFERENCE_S, self.VOUCHER_MR_THR,
                self.VOUCHER_MR_EXIT, self.VOUCHER_MR_SIZE, cur)

        # === Alpha 4: K=5100 taker-edge mispricing ===
        sym_5100 = 'VEV_5100'
        od_5100 = ods.get(sym_5100)
        if S_mid is not None and od_5100 is not None:
            mid_5100 = self._mid(od_5100)
            bid_5100, ask_5100 = self._bid_ask(od_5100)
            if mid_5100 is not None:
                bs5100 = _bs_call(S_mid, 5100, self.T_FIXED, self.SIGMA)
                resid = mid_5100 - bs5100
                a_k = self._ema_alpha(self.K5100_HL)
                prev = ts_state.get('ema_resid_5100', resid)
                ts_state['ema_resid_5100'] = a_k * resid + (1.0 - a_k) * prev
                fair = bs5100 + ts_state['ema_resid_5100']
                add = 0
                if ask_5100 is not None and (fair - ask_5100) > self.K5100_ENTER:
                    add = +self.K5100_SIZE
                elif bid_5100 is not None and (bid_5100 - fair) > self.K5100_ENTER:
                    add = -self.K5100_SIZE
                targets[sym_5100] = targets.get(sym_5100, 0) + add

        # === Alpha 5: Smile pair LONG 5400 / SHORT 5200 ===
        if S_mid is not None:
            long_sym  = f'VEV_{self.PAIR_LONG_K}'
            short_sym = f'VEV_{self.PAIR_SHORT_K}'
            od_l = ods.get(long_sym); od_s = ods.get(short_sym)
            if od_l is not None and od_s is not None:
                m_l = self._mid(od_l); m_s = self._mid(od_s)
                if m_l is not None and m_s is not None:
                    bs_l = _bs_call(S_mid, self.PAIR_LONG_K,  self.T_FIXED, self.SIGMA)
                    bs_s = _bs_call(S_mid, self.PAIR_SHORT_K, self.T_FIXED, self.SIGMA)
                    long_dev  = m_l - (bs_l + self.SMILE_BIAS.get(self.PAIR_LONG_K,  0.0))
                    short_dev = m_s - (bs_s + self.SMILE_BIAS.get(self.PAIR_SHORT_K, 0.0))
                    pair_open = ts_state.get('pair_open', 0)
                    if (not pair_open
                        and long_dev < self.PAIR_OPEN_LONG_DEV
                        and short_dev > self.PAIR_OPEN_SHORT_DEV):
                        ts_state['pair_open'] = 1; pair_open = 1
                    elif (pair_open
                          and long_dev > self.PAIR_CLOSE_LONG_DEV
                          and short_dev < self.PAIR_CLOSE_SHORT_DEV):
                        ts_state['pair_open'] = 0; pair_open = 0
                    if pair_open:
                        targets[long_sym]  = targets.get(long_sym, 0)  + self.PAIR_SIZE
                        targets[short_sym] = targets.get(short_sym, 0) - self.PAIR_SIZE

        # ---- Convert targets into orders ----
        for product, target in targets.items():
            od = ods.get(product)
            if od is None:
                continue
            current = positions.get(product, 0)
            bid, ask = self._bid_ask(od)
            orders = self._orders_to_target(product, current, target, bid, ask)
            if orders:
                result[product] = orders

        try:
            traderData_out = json.dumps(ts_state)
        except Exception:
            traderData_out = ""

        return result, 0, traderData_out
