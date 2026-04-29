"""
v34 — ARCHITECTURAL PIVOT toward $150k+.

After single-shot tweaks hit diminishing returns ($678 -> $0 -> -$47), the
next leap requires a fundamentally new alpha source. This file adds:

  CONCURRENT MM ON IDLE SOLO PRODUCTS

5 SOLO products earn extra spread during periods their MR signal is silent:
  - PEBBLES_S             (q=2, ms=14) — SOLO PnL $950, idle most ticks
  - PEBBLES_M             (q=2, ms=14) — SOLO PnL $810, idle most ticks
  - TRANSLATOR_SPACE_GRAY (q=2, ms=10) — SOLO PnL $96 (heavily idle)
  - MICROCHIP_CIRCLE      (q=2, ms=8)  — SOLO PnL $166 (heavily idle)
  - ROBOT_DISHES          (q=2, ms=5)  — SOLO PnL $1,019 (tight ms)

Gating prevents conflict with SOLO:
  - Skip if SOLO produced ANY orders this tick (solo_acted set)
  - Skip if |position| > q (don't push toward LIMIT when SOLO might fire next)

VOID_BLUE reverted to q=2. DARK_MATTER reverted to ms=12.
All other 49 products UNCHANGED from v32 LIVE baseline ($85,888).

Expected uplift: +$2-8k LIVE → target ~$88-94k.
Path to $150k will require stacking: pairs trading (v35), microprice MM skew
(v36), and tuning subsequent iterations.

History:
  v22 = friend's $85,113.88 baseline subset
  v31 = v22 + SPACE_GRAY MR + 3 size-ups -> $85,888 [LIVE]
  v32 = v31 + UV_AMBER tweak [no-op]
  v33 = v32 + VOID_BLUE q=2->3 [-$47, reverted]
  v34 = v32 + concurrent-MM on 5 SOLO products [THIS FILE]
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List
import math
import json


class Trader:
    LIMIT = 10
    THRESHOLD = 44000

    PAIRS = {}

    # ── Mean reversion products (33) ──────────────────────────────────────────
    SOLO = {
        # v4_best core
        "PEBBLES_XL":             {"window": 150, "min_hist": 150, "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 24, "keep": 170},
        "PEBBLES_L":              {"window": 200, "min_hist": 200, "enter": 2.0, "exit": 0.5, "size": 10, "max_spread": 22, "keep": 220},
        "PEBBLES_XS":             {"window": 150, "min_hist": 150, "enter": 2.0, "exit": 0.5, "size": 10, "max_spread": 16, "keep": 170},
        "ROBOT_IRONING":          {"window": 25,  "min_hist": 25,  "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 8,  "keep": 45},
        "ROBOT_MOPPING":          {"window": 300, "min_hist": 300, "enter": 1.5, "exit": 0.5, "size": 10, "max_spread": 12, "keep": 320},
        "MICROCHIP_RECTANGLE":    {"window": 200, "min_hist": 200, "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 11, "keep": 220},
        "SLEEP_POD_NYLON":        {"window": 200, "min_hist": 200, "enter": 2.0, "exit": 1.0, "size": 10, "max_spread": 11, "keep": 220},
        "TRANSLATOR_ASTRO_BLACK": {"window": 300, "min_hist": 300, "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 11, "keep": 320},
        "PEBBLES_M":              {"window": 100, "min_hist": 100, "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 14, "keep": 120},
        # v5 additions (kept)
        "UV_VISOR_YELLOW":        {"window": 500, "min_hist": 500, "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 18, "keep": 520},
        "SLEEP_POD_POLYESTER":    {"window": 300, "min_hist": 300, "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 14, "keep": 320},
        "MICROCHIP_OVAL":         {"window": 100, "min_hist": 100, "enter": 3.0, "exit": 1.0, "size": 10, "max_spread": 11, "keep": 120},
        "MICROCHIP_SQUARE":       {"window": 75,  "min_hist": 75,  "enter": 2.5, "exit": 1.0, "size": 10, "max_spread": 16, "keep": 95},
        "GALAXY_SOUNDS_SOLAR_FLAMES": {"window": 300, "min_hist": 300, "enter": 3.0, "exit": 0.5, "size": 10, "max_spread": 17, "keep": 320},
        # v6 additions (kept)
        "ROBOT_LAUNDRY":               {"window": 500, "min_hist": 500, "enter": 1.5, "exit": 0.5, "size": 10, "max_spread": 10, "keep": 520},
        "OXYGEN_SHAKE_EVENING_BREATH": {"window": 200, "min_hist": 200, "enter": 2.5, "exit": 1.0, "size": 10, "max_spread": 16, "keep": 220},
        "OXYGEN_SHAKE_MINT":           {"window": 200, "min_hist": 200, "enter": 1.5, "exit": 0.5, "size": 10, "max_spread": 15, "keep": 220},
        "ROBOT_VACUUMING":             {"window": 300, "min_hist": 300, "enter": 3.0, "exit": 0.5, "size": 10, "max_spread": 10, "keep": 320},
        # v7 additions (kept)
        "UV_VISOR_MAGENTA":  {"window": 500, "min_hist": 500, "enter": 1.5, "exit": 0.5, "size": 10, "max_spread": 17, "keep": 520},
        "SLEEP_POD_SUEDE":   {"window": 50,  "min_hist": 50,  "enter": 3.0, "exit": 0.5, "size": 10, "max_spread": 10, "keep": 70},
        "PANEL_1X2":         {"window": 300, "min_hist": 300, "enter": 2.5, "exit": 1.0, "size": 10, "max_spread": 15, "keep": 320},
        # v8 additions (kept; TRANSLATOR_ECLIPSE_CHARCOAL + SNACKPACK_STRAWBERRY switched to MM)
        "PANEL_2X2": {"window": 50, "min_hist": 50, "enter": 3.0, "exit": 0.5, "size": 10, "max_spread": 11, "keep": 70},
        # v9 additions (kept)
        "MICROCHIP_CIRCLE": {"window": 200, "min_hist": 200, "enter": 3.0, "exit": 1.0, "size": 10, "max_spread": 8, "keep": 220},
        "PEBBLES_S":        {"window": 300, "min_hist": 300, "enter": 3.0, "exit": 0.5, "size": 10, "max_spread": 16, "keep": 320},
        # v10 additions (kept)
        "ROBOT_DISHES":   {"window": 150, "min_hist": 150, "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 7, "keep": 170},
        "UV_VISOR_AMBER": {"window": 300, "min_hist": 300, "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 12, "keep": 320},
        # v11 addition (kept)
        "GALAXY_SOUNDS_BLACK_HOLES": {"window": 200, "min_hist": 200, "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 17, "keep": 220},
        # v16 additions (kept)
        "GALAXY_SOUNDS_DARK_MATTER":   {"window": 75,  "min_hist": 75,  "enter": 1.5, "exit": 0.5, "size": 10, "max_spread": 12, "keep": 95},
        "PANEL_2X4":                   {"window": 25,  "min_hist": 25,  "enter": 2.0, "exit": 0.5, "size": 10, "max_spread": 8,  "keep": 45},
        "OXYGEN_SHAKE_MORNING_BREATH": {"window": 50,  "min_hist": 50,  "enter": 3.0, "exit": 0.5, "size": 10, "max_spread": 13, "keep": 70},
        # v17 additions (kept)
        "UV_VISOR_RED":        {"window": 500, "min_hist": 500, "enter": 3.0, "exit": 1.0, "size": 10, "max_spread": 17, "keep": 520},
        "SLEEP_POD_LAMB_WOOL": {"window": 200, "min_hist": 200, "enter": 3.0, "exit": 0.5, "size": 10, "max_spread": 12, "keep": 220},
        "SNACKPACK_RASPBERRY": {"window": 200, "min_hist": 200, "enter": 2.5, "exit": 0.5, "size": 10, "max_spread": 17, "keep": 220},
        # v30: Re-add TRANSLATOR_SPACE_GRAY MR — friend's post-mortem reverted
        # this but my v22 live test confirmed it earns +$96. The friend's
        # comparison was contaminated by other simultaneous changes.
        "TRANSLATOR_SPACE_GRAY": {"window": 300, "min_hist": 300, "enter": 3.0, "exit": 0.5, "size": 10, "max_spread": 10, "keep": 320},
    }

    # ── Market making products (16) with per-product quote sizes ─────────────
    # q derived from exact backtester sweep (v21 balanced-next optimization)
    MM_QUOTE_SIZE = {
        # v31 — applying v27's LIVE-VALIDATED size-ups on friend's $85k base.
        # These 3 size-ups earned a confirmed +$678 in v27 with no regressions.
        "TRANSLATOR_ECLIPSE_CHARCOAL": 5,  # v27 PROVEN: q=3→5 added +$80
        "UV_VISOR_ORANGE":             4,
        "PANEL_4X4":                   5,  # v27 PROVEN: q=3→5 added +$239
        "SNACKPACK_CHOCOLATE":         2,
        "OXYGEN_SHAKE_GARLIC":         2,
        "SNACKPACK_STRAWBERRY":        4,
        "TRANSLATOR_VOID_BLUE":        2,  # v33 tested q=3, lost $47 (37% per-unit penalty); reverted
        "MICROCHIP_TRIANGLE":          3,
        "TRANSLATOR_GRAPHITE_MIST":    5,  # v27 PROVEN: q=3→5 added +$359
        "SLEEP_POD_COTTON":            4,
        "SNACKPACK_PISTACHIO":         4,
        "GALAXY_SOUNDS_SOLAR_WINDS":   4,
        "OXYGEN_SHAKE_CHOCOLATE":      1,
        "SNACKPACK_VANILLA":           4,
        "GALAXY_SOUNDS_PLANETARY_RINGS": 4,
        "PANEL_1X4":                   4,
    }

    MM_MAX_SPREAD = {
        "TRANSLATOR_ECLIPSE_CHARCOAL": 9,
        "MICROCHIP_TRIANGLE":          9,
        "SLEEP_POD_COTTON":            11,
        "TRANSLATOR_GRAPHITE_MIST":    10,
        "OXYGEN_SHAKE_GARLIC":         15,
        "GALAXY_SOUNDS_SOLAR_WINDS":   15,
        "OXYGEN_SHAKE_CHOCOLATE":      12,
        "SNACKPACK_VANILLA":           18,
        "GALAXY_SOUNDS_PLANETARY_RINGS": 13,
        "PANEL_1X4":                   8,
        "SNACKPACK_CHOCOLATE":         18,
        "SNACKPACK_PISTACHIO":         17,
    }

    MM_START_TS = {
        "TRANSLATOR_ECLIPSE_CHARCOAL": 10000,
        "TRANSLATOR_GRAPHITE_MIST":    5000,
        "SLEEP_POD_COTTON":            30000,
        "OXYGEN_SHAKE_GARLIC":         25000,
        "GALAXY_SOUNDS_SOLAR_WINDS":   30000,
        "GALAXY_SOUNDS_PLANETARY_RINGS": 65000,
        "PANEL_1X4":                   65000,
        "SNACKPACK_CHOCOLATE":         10000,
        "SNACKPACK_PISTACHIO":         10000,
    }

    MM_PRODUCTS = set(MM_QUOTE_SIZE.keys())

    # ── Concurrent MM on SOLO products during idle periods ──────────────────
    # When a SOLO product's z-signal is not firing (no orders this tick) and
    # current position is small, we can earn the spread with a tiny MM quote.
    # Each tick the gating is: skip if SOLO acted OR |position| > q.
    # Products picked: SOLO products with low PnL OR low signal-fire rate
    # where idle periods dominate (>90% of ticks).
    MM_ON_SOLO_IDLE = {
        "PEBBLES_S":             {"q": 2, "ms": 14, "start": 5000},
        "PEBBLES_M":             {"q": 2, "ms": 14, "start": 5000},
        "TRANSLATOR_SPACE_GRAY": {"q": 2, "ms": 10, "start": 5000},
        "MICROCHIP_CIRCLE":      {"q": 2, "ms": 8,  "start": 5000},
        "ROBOT_DISHES":          {"q": 2, "ms": 5,  "start": 5000},
    }

    def run(self, state: TradingState):
        memory = self._decode(state.traderData)
        memory.setdefault("mid", {})
        memory.setdefault("side", {})

        result: Dict[str, List[Order]] = {}

        # Mean reversion
        solo_acted = set()
        for product, params in self.SOLO.items():
            orders = self._solo_orders(product, params, state, memory)
            if orders:
                result[product] = orders
                solo_acted.add(product)

        # Market making (existing 16 products)
        for product in self.MM_PRODUCTS:
            od = state.order_depths.get(product)
            if od is None:
                continue
            orders = self._market_make(product, od, state.position.get(product, 0), state.timestamp)
            if orders:
                result[product] = orders

        # Concurrent MM on SOLO products during their idle periods.
        # Gate: skip if SOLO acted this tick OR position would force a side-skew.
        for product, cfg in self.MM_ON_SOLO_IDLE.items():
            if product in solo_acted:
                continue
            if state.timestamp < cfg["start"]:
                continue
            pos = state.position.get(product, 0)
            q = cfg["q"]
            # Only quote when position is near 0 — avoid pushing past LIMIT
            # if SOLO suddenly fires next tick wanting target ±10.
            if abs(pos) > q:
                continue
            od = state.order_depths.get(product)
            if od is None or not od.buy_orders or not od.sell_orders:
                continue
            best_bid = max(od.buy_orders)
            best_ask = min(od.sell_orders)
            if best_bid >= best_ask:
                continue
            spread = best_ask - best_bid
            if spread > cfg["ms"]:
                continue
            if spread >= 4:
                bid_p = best_bid + 1
                ask_p = best_ask - 1
            else:
                bid_p = best_bid
                ask_p = best_ask
            if bid_p >= ask_p:
                continue
            new_orders = []
            buy_cap  = self.LIMIT - pos
            sell_cap = self.LIMIT + pos
            if buy_cap > 0:
                new_orders.append(Order(product, bid_p,  min(q, buy_cap)))
            if sell_cap > 0:
                new_orders.append(Order(product, ask_p, -min(q, sell_cap)))
            if new_orders:
                # SOLO didn't act for this product (gated above), so safe to set.
                result[product] = new_orders

        trader_data = self._encode(memory)
        if len(trader_data) > self.THRESHOLD:
            mid = memory.get("mid", {})
            for product, params in self.SOLO.items():
                if product in mid:
                    mid[product] = mid[product][-params["keep"]:]
            trader_data = self._encode(memory)

        return result, 0, trader_data

    def _market_make(self, product: str, od: OrderDepth, position: int, timestamp: int) -> List[Order]:
        if timestamp < self.MM_START_TS.get(product, 0):
            return []
        if not od.buy_orders or not od.sell_orders:
            return []
        best_bid = max(od.buy_orders)
        best_ask = min(od.sell_orders)
        if best_bid >= best_ask:
            return []
        spread = best_ask - best_bid
        max_spread = self.MM_MAX_SPREAD.get(product)
        if max_spread is not None and spread > max_spread:
            return []
        if spread >= 4:
            bid_price = best_bid + 1
            ask_price = best_ask - 1
        else:
            bid_price = best_bid
            ask_price = best_ask
        if bid_price >= ask_price:
            return []
        quote_size = self.MM_QUOTE_SIZE.get(product, 1)
        orders = []
        buy_capacity  = self.LIMIT - position
        sell_capacity = self.LIMIT + position
        if buy_capacity > 0:
            orders.append(Order(product, bid_price,  min(quote_size, buy_capacity)))
        if sell_capacity > 0:
            orders.append(Order(product, ask_price, -min(quote_size, sell_capacity)))
        return orders

    def _solo_orders(self, product, params, state, memory):
        od = state.order_depths.get(product)
        if od is None:
            return []
        mid, spread = self._mid(od), self._spread(od)
        if mid is None or spread is None or spread > params["max_spread"]:
            return []
        hist = memory["mid"].get(product, [])
        hist.append(round(mid, 2))
        keep = params["keep"]
        if len(hist) > keep:
            hist = hist[-keep:]
        memory["mid"][product] = hist
        key = "solo_" + product
        side = int(memory["side"].get(key, 0))
        if len(hist) >= params["min_hist"]:
            z = self._zscore(hist, params["window"])
            if z is not None:
                if z > params["enter"]: side = -1
                elif z < -params["enter"]: side = 1
                elif abs(z) < params["exit"]: side = 0
        memory["side"][key] = side
        target = side * int(params["size"])
        return self._orders_to_target(product, od, state.position.get(product, 0), target)

    def _orders_to_target(self, product, od, current, target):
        target = max(-self.LIMIT, min(self.LIMIT, int(target)))
        diff = target - current
        bid, ask = self._bid_ask(od)
        if diff > 0:
            if ask is None: return []
            qty = min(diff, self.LIMIT - current)
            return [Order(product, int(ask) + 1, int(qty))] if qty > 0 else []
        if diff < 0:
            if bid is None: return []
            qty = min(-diff, self.LIMIT + current)
            return [Order(product, int(bid) - 1, -int(qty))] if qty > 0 else []
        return []

    def _zscore(self, hist, window):
        sample = hist[-min(window, len(hist)):]
        if len(sample) < 2: return None
        mean = sum(sample) / len(sample)
        var = sum((x - mean) ** 2 for x in sample) / (len(sample) - 1)
        std = math.sqrt(var)
        if std <= 1e-9: return None
        return (sample[-1] - mean) / std

    def _mid(self, od):
        bid, ask = self._bid_ask(od)
        if bid is None or ask is None: return None
        return (bid + ask) / 2.0

    def _spread(self, od):
        bid, ask = self._bid_ask(od)
        if bid is None or ask is None: return None
        return ask - bid

    @staticmethod
    def _bid_ask(od):
        bid = max(od.buy_orders) if od.buy_orders else None
        ask = min(od.sell_orders) if od.sell_orders else None
        return bid, ask

    @staticmethod
    def _encode(data):
        try:
            out = {"m": {}, "s": data.get("side", {})}
            for prod, prices in data.get("mid", {}).items():
                if not prices:
                    out["m"][prod] = []
                else:
                    base = round(prices[0] * 10)
                    deltas = [round((prices[i] - prices[i - 1]) * 10)
                              for i in range(1, len(prices))]
                    out["m"][prod] = [base] + deltas
            return json.dumps(out, separators=(',', ':'))
        except Exception:
            return ""

    @staticmethod
    def _decode(raw):
        if not raw:
            return {}
        try:
            compressed = json.loads(raw)
            if not isinstance(compressed, dict):
                return {}
            data = {"mid": {}, "side": compressed.get("s", {})}
            for prod, values in compressed.get("m", {}).items():
                if not values:
                    data["mid"][prod] = []
                else:
                    prices = [values[0] / 10.0]
                    for delta in values[1:]:
                        prices.append(prices[-1] + delta / 10.0)
                    data["mid"][prod] = prices
            return data
        except Exception:
            return {}