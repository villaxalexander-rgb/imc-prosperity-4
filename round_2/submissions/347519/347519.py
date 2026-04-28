"""
IMC Prosperity 4 - Round 2 V9
 
Changes from V8:
 
CHANGE 1 - MAF bid reduced: 3,000 → 1,000
  Break-even MAF bid = expected_finals_pnl × 0.25 ≈ 10,927 × 0.25 = 2,732.
  Bidding 3,000 was a net LOSS of ~268 XRECs vs not winning MAF at all.
  At 1,000: win value = (10,927 × 1.25) − 1,000 = +1,732 vs not winning.
  Most teams bid 0 (don't implement bid()), so 1,000 almost certainly wins top 50%.
  If concerned about competition, 1,500 is still safe: +1,232 net benefit.
 
CHANGE 2 - ACO passive ask floored at max(best_ask−1, int(fair)+1)
  When bot's ask side temporarily dips (e.g. ask=9995), best_ask−1=9994 would
  place us selling far below EMA. The floor ensures we never passive-sell below
  our fair value estimate. Impact: minor (~15−50 XRECs per run), but correct.
  Buy side unchanged — no bid cap to avoid recreating the death-spiral risk.
 
UNCHANGED from V8:
  - EMA re-enabled (alpha=0.05), adapts to actual R2 market level
  - ACO aggressive takes at EMA±3 (sweeps full book depth when mispriced)
  - ACO passive bid at best_bid+1 (inside spread, first in queue)
  - ACO behind-bot skew at inv_ratio ±0.5 extremes
  - IPR: Phase 1 sweep to FV+15, Phase 2 passive offer capped at 15 units
  - IPR drift correction rate 0.008
"""
 
from datamodel import OrderDepth, TradingState, Order
from typing import List
import json
 
 
class Trader:
 
    MAF    = 1000                 # CHANGE 1: was 3,000 (net-negative with old value)
    LIMITS = {"ASH_COATED_OSMIUM": 80, "INTARIAN_PEPPER_ROOT": 80}
 
    IPR_SLOPE              = 0.001002
    IPR_DIRECTIONAL_TARGET = 80
    IPR_MAX_ENTRY_PREMIUM  = 15
 
    ACO_EMA_ALPHA      = 0.05
    ACO_TAKE_THRESHOLD = 3
 
    def run(self, state: TradingState) -> tuple[dict[str, list[Order]], int, str]:
        result = {}
        try:
            saved = json.loads(state.traderData) if state.traderData else {}
        except:
            saved = {}
 
        for product in state.order_depths:
            if product == "ASH_COATED_OSMIUM":
                orders, saved = self.trade_aco(state, saved)
                result[product] = orders
            elif product == "INTARIAN_PEPPER_ROOT":
                orders, saved = self.trade_ipr(state, saved)
                result[product] = orders
            else:
                result[product] = []
 
        return result, 0, json.dumps(saved)
 
    def bid(self):
        """Round 2 Market Access Fee — top 50% bidders win +25% quote volume."""
        return self.MAF
 
    # ─── IPR ────────────────────────────────────────────────────────────────────
 
    def trade_ipr(self, state: TradingState, saved: dict) -> tuple[List[Order], dict]:
        product = "INTARIAN_PEPPER_ROOT"
        orders  = []
 
        od = state.order_depths.get(product)
        if not od:
            return orders, saved
 
        pos   = state.position.get(product, 0)
        limit = self.LIMITS[product]
        ts    = state.timestamp
 
        has_bids = bool(od.buy_orders)
        has_asks = bool(od.sell_orders)
        best_ask = min(od.sell_orders) if has_asks else None
        best_bid = max(od.buy_orders)  if has_bids else None
 
        if "ipr_base" not in saved:
            mid = ((best_bid + best_ask) / 2.0) if (has_bids and has_asks) \
                  else (best_ask or best_bid)
            if mid is None:
                return orders, saved
            saved["ipr_base"] = mid - self.IPR_SLOPE * ts
 
        fair = saved["ipr_base"] + self.IPR_SLOPE * ts
 
        if has_bids and has_asks:
            mid   = (best_bid + best_ask) / 2.0
            error = mid - fair
            saved["ipr_base"] += max(-1.0, min(1.0, 0.008 * error))
            fair = saved["ipr_base"] + self.IPR_SLOPE * ts
 
        # Phase 1: sweep ALL ask levels up to FV + premium
        if pos < self.IPR_DIRECTIONAL_TARGET and has_asks:
            remaining = min(limit - pos, self.IPR_DIRECTIONAL_TARGET - pos)
            for ask_px in sorted(od.sell_orders.keys()):
                if remaining <= 0:
                    break
                if ask_px > fair + self.IPR_MAX_ENTRY_PREMIUM:
                    break
                qty = min(-od.sell_orders[ask_px], remaining)
                orders.append(Order(product, ask_px, qty))
                remaining -= qty
            return orders, saved
 
        # Phase 2: hold, passive offer capped at 15 units
        if has_bids and pos > 0:
            sell_cap = min(15, limit + pos)
            pass_ask = max(int(fair + 8), best_bid + 1)
            if sell_cap > 0:
                orders.append(Order(product, pass_ask, -sell_cap))
 
        return orders, saved
 
    # ─── ACO ────────────────────────────────────────────────────────────────────
 
    def trade_aco(self, state: TradingState, saved: dict) -> tuple[List[Order], dict]:
        product = "ASH_COATED_OSMIUM"
        orders  = []
 
        od = state.order_depths.get(product)
        if not od or not od.buy_orders or not od.sell_orders:
            return orders, saved
 
        pos      = state.position.get(product, 0)
        limit    = self.LIMITS[product]
        best_bid = max(od.buy_orders)
        best_ask = min(od.sell_orders)
        mid      = (best_bid + best_ask) / 2.0
 
        aco_ema = saved.get("aco_ema", mid)
        aco_ema = self.ACO_EMA_ALPHA * mid + (1 - self.ACO_EMA_ALPHA) * aco_ema
        saved["aco_ema"] = aco_ema
        fair = aco_ema
 
        buy_cap   = limit - pos
        sell_cap  = limit + pos
        inv_ratio = pos / limit
 
        # Aggressive takes: sweep book when genuinely mispriced vs EMA
        if buy_cap > 0:
            for price in sorted(od.sell_orders.keys()):
                if price <= fair - self.ACO_TAKE_THRESHOLD and buy_cap > 0:
                    vol = min(-od.sell_orders[price], buy_cap)
                    orders.append(Order(product, price, vol))
                    buy_cap -= vol
                else:
                    break
 
        if sell_cap > 0:
            for price in sorted(od.buy_orders.keys(), reverse=True):
                if price >= fair + self.ACO_TAKE_THRESHOLD and sell_cap > 0:
                    vol = min(od.buy_orders[price], sell_cap)
                    orders.append(Order(product, price, -vol))
                    sell_cap -= vol
                else:
                    break
 
        # Passive quotes: inside the bot's spread, with inventory skew at extremes
        if buy_cap > 0:
            if inv_ratio > 0.5:
                our_bid = best_bid - 1          # too long: step behind bot
            else:
                our_bid = best_bid + 1          # normal: first in queue
            if our_bid < best_ask:
                orders.append(Order(product, our_bid, buy_cap))
 
        if sell_cap > 0:
            if inv_ratio < -0.5:
                our_ask = best_ask + 1          # too short: step behind bot
            else:
                # CHANGE 2: floor at fair+1 to avoid selling below EMA
                our_ask = max(best_ask - 1, int(fair) + 1)
            if our_ask > best_bid:
                orders.append(Order(product, our_ask, -sell_cap))
 
        return orders, saved