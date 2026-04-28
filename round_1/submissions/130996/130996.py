"""
IMC Prosperity — ROUND 1 Trader v9 (momentum-hold PEPPER + clean MM OSMIUM)

Complete rethink after v8 returned 4,227. Previous versions all tried to
market-make PEPPER. Analysis of the log proves MM on PEPPER is STRICTLY
dominated by just holding long:

  Session drift on PEPPER: +101 ticks (11998 → 12099)
  Holding +50 long the whole session: MTM = +5050
  Best MM result: 1923. We LOSE ~3k to the drift every session.

Why MM fails on PEPPER: when price drifts up, our stale ask gets hit first.
We go short. Price continues up. We MTM-lose. Spread capture (2 ticks)
doesn't compensate for drift capture loss.

v9 strategy:
  OSMIUM: eric-style penny/join MM (worked — 2709 in v8). Keep as-is.
  PEPPER: MOMENTUM-HOLD.
    - Compute rolling drift from MM-mid (last N vs previous N)
    - If drift positive, target +50 long: take asks aggressively, no asks quoted
    - If drift negative, target -50 short: take bids aggressively, no bids quoted
    - If drift flat, MM passively
    - Only passive bid at FV-1 to top up position (never sell unless flipping)

Position limit: 50.

Author: Alexander Villax
"""

from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict, Optional, Tuple
import json


OSMIUM_PARAMS = {
    "fair_value": 10_000,
    "take_width": 1,
    "clear_width": 0,
    "disregard_edge": 1,
    "join_edge": 2,
    "default_edge": 2,          # v10: tightened 4 → 2 (we have 50 limit, not 20)
    "soft_position_limit": 30,  # v10: loosened 20 → 30 (allow more carry)
}

PEPPER_PARAMS = {
    "adverse_volume": 15,
    "hard_target": 50,          # v11: stay +50 always. no drift detection.
    "initial_load_slippage": 10,  # v11: cross up to FV+10 until first fill gets us to +50
    "maintain_slippage": 2,     # after initial load, only pay up to FV+2
}


class Trader:
    POS_LIMIT = 50

    def __init__(self):
        pass

    # ------------------------------------------------------------------
    @staticmethod
    def _mm_mid(od: OrderDepth, adverse_vol: int) -> Optional[float]:
        mm_bids = [p for p, v in od.buy_orders.items() if v >= adverse_vol]
        mm_asks = [p for p, v in od.sell_orders.items() if -v >= adverse_vol]
        if mm_bids and mm_asks:
            return (max(mm_bids) + min(mm_asks)) / 2
        if od.buy_orders and od.sell_orders:
            return (min(od.buy_orders.keys()) + max(od.sell_orders.keys())) / 2
        return None

    # ==================================================================
    # OSMIUM — eric-style penny/join market making (unchanged from v8)
    # ==================================================================
    def _trade_osmium(self, state: TradingState) -> List[Order]:
        product = "ASH_COATED_OSMIUM"
        if product not in state.order_depths:
            return []
        od = state.order_depths[product]
        if not od.buy_orders or not od.sell_orders:
            return []

        p = OSMIUM_PARAMS
        fv = p["fair_value"]
        position = state.position.get(product, 0)

        orders: List[Order] = []
        buy_placed = 0
        sell_placed = 0

        # TAKE
        for ask in sorted(od.sell_orders.keys()):
            if ask <= fv - p["take_width"]:
                avail = -od.sell_orders[ask]
                room = self.POS_LIMIT - position - buy_placed
                q = min(avail, max(0, room))
                if q > 0:
                    orders.append(Order(product, ask, q))
                    buy_placed += q
            else:
                break

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid >= fv + p["take_width"]:
                avail = od.buy_orders[bid]
                room = self.POS_LIMIT + position - sell_placed
                q = min(avail, max(0, room))
                if q > 0:
                    orders.append(Order(product, bid, -q))
                    sell_placed += q
            else:
                break

        # CLEAR inventory at FV
        effective_pos = position + buy_placed - sell_placed
        if effective_pos > 0:
            for bid in sorted(od.buy_orders.keys(), reverse=True):
                if bid < fv + p["clear_width"] or effective_pos <= 0:
                    break
                avail = od.buy_orders[bid]
                room = self.POS_LIMIT + position - sell_placed
                q = min(avail, effective_pos, max(0, room))
                if q > 0:
                    orders.append(Order(product, bid, -q))
                    sell_placed += q
                    effective_pos -= q
        elif effective_pos < 0:
            for ask in sorted(od.sell_orders.keys()):
                if ask > fv - p["clear_width"] or effective_pos >= 0:
                    break
                avail = -od.sell_orders[ask]
                room = self.POS_LIMIT - position - buy_placed
                q = min(avail, -effective_pos, max(0, room))
                if q > 0:
                    orders.append(Order(product, ask, q))
                    buy_placed += q
                    effective_pos += q

        # MAKE — penny/join
        asks_above = [a for a in od.sell_orders if a > fv + p["disregard_edge"]]
        bids_below = [b for b in od.buy_orders if b < fv - p["disregard_edge"]]
        best_ask_above = min(asks_above) if asks_above else None
        best_bid_below = max(bids_below) if bids_below else None

        ask = fv + p["default_edge"]
        if best_ask_above is not None:
            if best_ask_above - fv <= p["join_edge"]:
                ask = best_ask_above
            else:
                ask = best_ask_above - 1

        bid = fv - p["default_edge"]
        if best_bid_below is not None:
            if fv - best_bid_below <= p["join_edge"]:
                bid = best_bid_below
            else:
                bid = best_bid_below + 1

        # Soft-limit skew
        effective_pos = position + buy_placed - sell_placed
        if effective_pos > p["soft_position_limit"]:
            ask -= 1
        elif effective_pos < -p["soft_position_limit"]:
            bid += 1

        max_buy = self.POS_LIMIT - position - buy_placed
        max_sell = self.POS_LIMIT + position - sell_placed
        if max_buy > 0:
            orders.append(Order(product, bid, max_buy))
        if max_sell > 0:
            orders.append(Order(product, ask, -max_sell))

        return orders

    # ==================================================================
    # PEPPER — momentum-hold
    # ==================================================================
    def _trade_pepper(self, state: TradingState, mem: dict) -> List[Order]:
        product = "INTARIAN_PEPPER_ROOT"
        if product not in state.order_depths:
            return []
        od = state.order_depths[product]
        if not od.buy_orders or not od.sell_orders:
            return []

        p = PEPPER_PARAMS
        fv = self._mm_mid(od, p["adverse_volume"])
        if fv is None:
            return []

        # Track whether we've hit full load yet
        loaded = mem.get("pepper_loaded", False)

        position = state.position.get(product, 0)
        orders: List[Order] = []
        buy_placed = 0
        sell_placed = 0

        target = p["hard_target"]   # always +50

        # Use aggressive slippage until first time we reach full load, then moderate
        if not loaded and position >= target:
            loaded = True
            mem["pepper_loaded"] = True
        slippage = p["maintain_slippage"] if loaded else p["initial_load_slippage"]

        # --- Drive toward target ---
        # If we need to be longer, take asks up to and slightly above FV
        # If we need to be shorter, take bids down to and slightly below FV
        fv_int = int(round(fv))

        if position < target:
            # buy at any price <= FV + slippage (ride drift even if we pay up)
            limit_px = fv_int + slippage
            for ask in sorted(od.sell_orders.keys()):
                if ask > limit_px:
                    break
                need = target - (position + buy_placed)
                if need <= 0:
                    break
                avail = -od.sell_orders[ask]
                room = self.POS_LIMIT - position - buy_placed
                q = min(avail, need, max(0, room))
                if q > 0:
                    orders.append(Order(product, ask, q))
                    buy_placed += q

        if position > target:
            limit_px = fv_int - slippage
            for bid in sorted(od.buy_orders.keys(), reverse=True):
                if bid < limit_px:
                    break
                need = (position - sell_placed) - target
                if need <= 0:
                    break
                avail = od.buy_orders[bid]
                room = self.POS_LIMIT + position - sell_placed
                q = min(avail, need, max(0, room))
                if q > 0:
                    orders.append(Order(product, bid, -q))
                    sell_placed += q

        # --- Passive top-up ---
        # If still need more in the target direction, post a passive quote at FV-1/FV+1
        effective_pos = position + buy_placed - sell_placed

        if target > 0:
            # we want long — only post passive bids, NO asks (don't sell into drift)
            if effective_pos < target:
                max_buy = self.POS_LIMIT - position - buy_placed
                if max_buy > 0:
                    best_bid = max(od.buy_orders.keys())
                    best_ask = min(od.sell_orders.keys())
                    # v11: penny aggressively during initial load, capped by slippage
                    bid_px = min(best_bid + 1, fv_int + slippage - 1)
                    if bid_px >= best_ask:
                        bid_px = best_ask - 1
                    orders.append(Order(product, bid_px, max_buy))
        elif target < 0:
            if effective_pos > target:
                max_sell = self.POS_LIMIT + position - sell_placed
                if max_sell > 0:
                    best_bid = max(od.buy_orders.keys())
                    best_ask = min(od.sell_orders.keys())
                    ask_px = max(best_ask - 1, fv_int + 1)
                    if ask_px <= best_bid:
                        ask_px = best_bid + 1
                    orders.append(Order(product, ask_px, -max_sell))
        else:
            # flat — symmetric MM
            best_bid = max(od.buy_orders.keys())
            best_ask = min(od.sell_orders.keys())
            bid_px = min(best_bid + 1, fv_int - 1)
            ask_px = max(best_ask - 1, fv_int + 1)
            if bid_px >= best_ask:
                bid_px = best_ask - 1
            if ask_px <= best_bid:
                ask_px = best_bid + 1
            if bid_px >= ask_px:
                bid_px = ask_px - 1
            max_buy = self.POS_LIMIT - position - buy_placed
            max_sell = self.POS_LIMIT + position - sell_placed
            if max_buy > 0:
                orders.append(Order(product, bid_px, max_buy))
            if max_sell > 0:
                orders.append(Order(product, ask_px, -max_sell))

        return orders

    # ------------------------------------------------------------------
    def run(self, state: TradingState):
        try:
            mem = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            mem = {}

        result: Dict[str, List[Order]] = {}

        o = self._trade_osmium(state)
        if o:
            result["ASH_COATED_OSMIUM"] = o

        p = self._trade_pepper(state, mem)
        if p:
            result["INTARIAN_PEPPER_ROOT"] = p

        return result, 0, json.dumps(mem)