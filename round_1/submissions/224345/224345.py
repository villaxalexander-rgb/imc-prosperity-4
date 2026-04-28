import json
from datamodel import OrderDepth, TradingState, Order
from typing import List

class Trader:
    LIMITS = {"ASH_COATED_OSMIUM": 80, "INTARIAN_PEPPER_ROOT": 80}

    # IPR — slope is a prior, gets overridden by live estimation after warmup
    IPR_SLOPE_PRIOR      = 0.001002   # starting assumption, updated by regression
    IPR_DIRECTIONAL_TARGET = 80
    IPR_DRIFT_CORRECTION = 0.008
    IPR_PHASE2_MAX_SELL  = 15
    IPR_MAX_ENTRY_PREMIUM = 12

    # ACO — EMA re-enabled (TWEAK 3 reverted) to track shifting fair value
    ACO_EMA_ALPHA   = 0.04            # slow EMA: adapts to structural FV shifts
    ACO_EMA_WARMUP  = 20              # ticks before EMA is trusted over FV=10000
    ACO_BASE_OFFSET = 3

    def run(self, state: TradingState):
        result = {}
        try:
            saved = json.loads(state.traderData) if state.traderData else {}
        except:
            saved = {}

        for product in state.order_depths:
            if product == "ASH_COATED_OSMIUM":
                orders, saved = self.trade_aco(state, saved)
            elif product == "INTARIAN_PEPPER_ROOT":
                orders, saved = self.trade_ipr(state, saved)
            else:
                orders = []
            result[product] = orders

        return result, 0, json.dumps(saved)

    # ─────────────────────────────────────────────────────────────────────
    # INTARIAN_PEPPER_ROOT — adaptive slope estimation
    # ─────────────────────────────────────────────────────────────────────
    def trade_ipr(self, state: TradingState, saved: dict):
        product = "INTARIAN_PEPPER_ROOT"
        od = state.order_depths.get(product)
        if not od:
            return [], saved

        pos = state.position.get(product, 0)
        limit = self.LIMITS[product]
        ts = state.timestamp
        orders = []

        has_bids = bool(od.buy_orders)
        has_asks = bool(od.sell_orders)
        best_ask = min(od.sell_orders) if has_asks else None
        best_bid = max(od.buy_orders) if has_bids else None

        # Anchor base price to first observation
        if "ipr_base" not in saved:
            mid = ((best_bid + best_ask) / 2.0) if (has_bids and has_asks) else (best_ask or best_bid)
            if mid is None:
                return orders, saved
            saved["ipr_base"] = mid - self.IPR_SLOPE_PRIOR * ts
            saved["ipr_slope"] = self.IPR_SLOPE_PRIOR
            # Seed rolling regression buffers
            saved["reg_sum_x"]  = 0.0
            saved["reg_sum_y"]  = 0.0
            saved["reg_sum_xx"] = 0.0
            saved["reg_sum_xy"] = 0.0
            saved["reg_n"]      = 0

        slope = saved.get("ipr_slope", self.IPR_SLOPE_PRIOR)
        fair  = saved["ipr_base"] + slope * ts

        # Drift correction + rolling slope update
        if has_bids and has_asks:
            mid = (best_bid + best_ask) / 2.0
            error = mid - fair
            saved["ipr_base"] += max(-1.0, min(1.0, self.IPR_DRIFT_CORRECTION * error))
            fair = saved["ipr_base"] + slope * ts

            # Incremental linear regression: fit slope from all observations so far
            # y = price, x = timestamp → slope = (n*Σxy - Σx*Σy) / (n*Σxx - (Σx)²)
            n   = saved["reg_n"] + 1
            sx  = saved["reg_sum_x"]  + ts
            sy  = saved["reg_sum_y"]  + mid
            sxx = saved["reg_sum_xx"] + ts * ts
            sxy = saved["reg_sum_xy"] + ts * mid
            saved.update({"reg_n": n, "reg_sum_x": sx, "reg_sum_y": sy,
                          "reg_sum_xx": sxx, "reg_sum_xy": sxy})

            # Only update slope after 50+ observations (avoid noisy early estimates)
            if n >= 50:
                denom = n * sxx - sx * sx
                if abs(denom) > 1e-9:
                    live_slope = (n * sxy - sx * sy) / denom
                    # Blend prior and live estimate: trust live more as n grows
                    blend = min(1.0, (n - 50) / 200.0)   # 0 at n=50, 1 at n=250
                    saved["ipr_slope"] = (1 - blend) * self.IPR_SLOPE_PRIOR + blend * live_slope

        # Phase 1: accumulate to target
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

        # Phase 2: hold, passive offer
        else:
            if has_bids:
                ask_px   = max(int(fair) + 8, best_bid + 1)
                sell_cap = min(self.IPR_PHASE2_MAX_SELL, limit + pos)
                if sell_cap > 0:
                    orders.append(Order(product, ask_px, -sell_cap))

        return orders, saved

    # ─────────────────────────────────────────────────────────────────────
    # ASH_COATED_OSMIUM — adaptive FV + FIXED inventory skew
    # ─────────────────────────────────────────────────────────────────────
    def trade_aco(self, state: TradingState, saved: dict):
        product = "ASH_COATED_OSMIUM"
        od = state.order_depths.get(product)
        if not od or not od.buy_orders or not od.sell_orders:
            return [], saved

        pos   = state.position.get(product, 0)
        limit = self.LIMITS[product]
        orders = []

        best_bid = max(od.buy_orders)
        best_ask = min(od.sell_orders)
        mid = (best_bid + best_ask) / 2.0

        # EMA fair value — re-enabled to handle structural FV shifts
        n_aco = saved.get("aco_n", 0) + 1
        saved["aco_n"] = n_aco
        if n_aco == 1:
            saved["aco_ema"] = mid         # seed EMA to first observed mid
        ema = self.ACO_EMA_ALPHA * mid + (1 - self.ACO_EMA_ALPHA) * saved["aco_ema"]
        saved["aco_ema"] = ema

        # Blend: use raw mid early (before EMA warms up), pure EMA after warmup
        blend = min(1.0, n_aco / self.ACO_EMA_WARMUP)
        fair  = blend * ema + (1 - blend) * mid

        buy_cap  = limit - pos
        sell_cap = limit + pos
        inv_ratio = pos / limit

        # Aggressive takes: only when bot genuinely crosses FV
        if best_ask <= fair - 2 and buy_cap > 0:
            for price in sorted(od.sell_orders.keys()):
                if price > fair - 2 or buy_cap <= 0:
                    break
                vol = min(-od.sell_orders[price], buy_cap)
                orders.append(Order(product, price, vol))
                buy_cap -= vol

        if best_bid >= fair + 2 and sell_cap > 0:
            for price in sorted(od.buy_orders.keys(), reverse=True):
                if price < fair + 2 or sell_cap <= 0:
                    break
                vol = min(od.buy_orders[price], sell_cap)
                orders.append(Order(product, price, -vol))
                sell_cap -= vol

        # ── FIXED inventory skew ────────────────────────────────────────
        # BUG FIX: the old min/max clamp to best_bid+1 / best_ask-1 made
        # skew non-functional (bot half-spread ~8 ticks > any offset we set).
        # New logic: when inventory is extreme, STEP BEHIND the bot's quote
        # on the bad side, forcing the bot to fill us only on the good side.

        if buy_cap > 0:
            if inv_ratio > 0.5:
                # Too long — go passive behind bot's bid (discourage more buys)
                our_bid = best_bid - 1
            elif inv_ratio < -0.5:
                # Too short — go aggressive inside spread (urgently want buys)
                our_bid = best_bid + 2
            else:
                our_bid = best_bid + 1    # normal: one tick inside
            our_bid = min(our_bid, int(fair) - 1)   # never bid above FV
            if our_bid > 0 and our_bid < best_ask:
                orders.append(Order(product, our_bid, buy_cap))

        if sell_cap > 0:
            if inv_ratio < -0.5:
                # Too short — go passive behind bot's ask (stop adding to short)
                our_ask = best_ask + 1
            elif inv_ratio > 0.5:
                # Too long — go aggressive inside spread (urgently want sells)
                our_ask = best_ask - 2
            else:
                our_ask = best_ask - 1   # normal: one tick inside
            our_ask = max(our_ask, int(fair) + 1)   # never ask below FV
            if our_ask > best_bid:
                orders.append(Order(product, our_ask, -sell_cap))

        return orders, saved