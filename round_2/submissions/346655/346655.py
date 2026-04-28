"""
IMC Prosperity 4 - Round 2 V8
 
Changes from current (V7-style):
 
CHANGE 1 - ACO aggressive takes RE-ADDED using EMA fair value (±3)
  V5 had takes at fixed FV±2, which caused death spiral in R2 (bot bids hit 10002+).
  V7 removed takes entirely — safe but leaves alpha on table.
  V8 restores takes at EMA±3. With EMA tracking actual market level (~10002-10008),
  takes only fire when ask is genuinely cheap (e.g. ask=9998 when EMA=10003).
  Bot normally asks at 10016+, so take-buys fire ~4 ticks/1000; take-sells fire
  only if bot bids spike above EMA+3 (rare). Zero death-spiral risk.
  Estimated gain: +100-300 XRECs per run at 80% volume.
 
CHANGE 2 - ACO EMA alpha tuned to 0.05 (from 0.06)
  Slightly slower EMA = more stable fair value = fewer false takes.
  ACO has -0.49 tick autocorrelation (mean-reverting); slower EMA = better FV anchor.
 
UNCHANGED from V7:
  - IPR: sweep all ask levels up to FV+15 for fast Phase 1 entry
  - ACO: passive quotes at best_bid+1 / best_ask-1 (no hardcoded FV offset)
  - ACO: behind-bot skew for extreme inventory (inv_ratio > 0.5 or < -0.5)
  - EMA re-enabled (alpha=0.05), NOT hardcoded 10000
  - bid() method returns MAF=3000 for Round 2 Market Access Fee
"""
 
from datamodel import OrderDepth, TradingState, Order
from typing import List
import json
 
 
class Trader:
 
    MAF = 3000
    LIMITS = {"ASH_COATED_OSMIUM": 80, "INTARIAN_PEPPER_ROOT": 80}
 
    IPR_SLOPE              = 0.001002
    IPR_DIRECTIONAL_TARGET = 80
    IPR_MAX_ENTRY_PREMIUM  = 15
 
    ACO_EMA_ALPHA          = 0.05   # Slightly slower than V7 (0.06→0.05) for more stable FV
    ACO_TAKE_THRESHOLD     = 3      # Take when ask ≤ EMA-3 or bid ≥ EMA+3
 
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
        """Round 2 Market Access Fee bid — top 50% bidders get 25% more quote volume."""
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
 
        # Initialise linear fair-value anchor from first observed mid
        if "ipr_base" not in saved:
            mid = ((best_bid + best_ask) / 2.0) if (has_bids and has_asks) \
                  else (best_ask or best_bid)
            if mid is None:
                return orders, saved
            saved["ipr_base"] = mid - self.IPR_SLOPE * ts
 
        fair = saved["ipr_base"] + self.IPR_SLOPE * ts
 
        # Drift correction: nudge base toward observed mid (rate 0.008)
        if has_bids and has_asks:
            mid   = (best_bid + best_ask) / 2.0
            error = mid - fair
            saved["ipr_base"] += max(-1.0, min(1.0, 0.008 * error))
            fair = saved["ipr_base"] + self.IPR_SLOPE * ts
 
        # Phase 1: sweep ALL ask levels up to FV + premium (fast accumulation)
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
 
        # Phase 2: hold position, passive offer capped at 15 units
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
 
        # EMA fair value — adapts to actual market level (critical for R2)
        aco_ema = saved.get("aco_ema", mid)
        aco_ema = self.ACO_EMA_ALPHA * mid + (1 - self.ACO_EMA_ALPHA) * aco_ema
        saved["aco_ema"] = aco_ema
        fair = aco_ema
 
        buy_cap   = limit - pos
        sell_cap  = limit + pos
        inv_ratio = pos / limit
 
        # ── Aggressive takes (CHANGE 1: re-added with EMA-based fair value) ──
        # Only fire when market is genuinely mispriced vs EMA.
        # Bot normally asks 10016+, so take-buys fire only on rare cheap asks.
        # No death-spiral risk: EMA tracks actual price level, not hardcoded 10000.
        if buy_cap > 0:
            for price in sorted(od.sell_orders.keys()):
                if price <= fair - self.ACO_TAKE_THRESHOLD and buy_cap > 0:
                    vol = min(-od.sell_orders[price], buy_cap)
                    orders.append(Order(product, price, vol))
                    buy_cap -= vol
                else:
                    break  # sell_orders sorted ascending; stop when no longer cheap
 
        if sell_cap > 0:
            for price in sorted(od.buy_orders.keys(), reverse=True):
                if price >= fair + self.ACO_TAKE_THRESHOLD and sell_cap > 0:
                    vol = min(od.buy_orders[price], sell_cap)
                    orders.append(Order(product, price, -vol))
                    sell_cap -= vol
                else:
                    break  # buy_orders sorted descending; stop when no longer rich
 
        # ── Passive quotes with behind-bot inventory skew ──────────────────────
        # Quoting inside the bot's spread: bid at best_bid+1, ask at best_ask-1.
        # At extreme inventory (±50% of limit), step behind bot to slow one side.
        if buy_cap > 0:
            if inv_ratio > 0.5:
                our_bid = best_bid - 1   # too long: give up fill priority on bid
            else:
                our_bid = best_bid + 1   # inside spread: first in queue
            if our_bid < best_ask:       # sanity: don't cross spread
                orders.append(Order(product, our_bid, buy_cap))
 
        if sell_cap > 0:
            if inv_ratio < -0.5:
                our_ask = best_ask + 1   # too short: give up fill priority on ask
            else:
                our_ask = best_ask - 1   # inside spread: first in queue
            if our_ask > best_bid:       # sanity: don't cross spread
                orders.append(Order(product, our_ask, -sell_cap))
 
        return orders, saved