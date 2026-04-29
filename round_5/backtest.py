"""
Round 5 backtester — replays the 3 days of historical L1 books against trader.py.

Order semantics:
  * Aggressive orders cross the displayed L1 (capped by displayed volume).
  * Passive orders posted strictly inside the spread fill probabilistically
    each tick (calibrated to match observed live fill rate ~2-5% per tick).
"""
from __future__ import annotations
import os
import sys
import pandas as pd
from collections import defaultdict
from typing import Dict

# Make `trader.py` and `scripts/load_r5.py` importable
HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "scripts"))

from trader import Trader, Quote, OrderBook, Order, POSITION_LIMIT, ALL_SIGNALS, SNACKPACK
from load_r5 import load_prices


# --------------------------------------------------------------
# Order execution model
# --------------------------------------------------------------

_PRNG = [0xC0FFEE]


def _rand() -> float:
    _PRNG[0] = (_PRNG[0] * 1664525 + 1013904223) & 0xFFFFFFFF
    return _PRNG[0] / 0xFFFFFFFF


def execute_aggressive(order: Order, book: OrderBook, pos: int) -> tuple:
    """Cross the displayed L1; fill capped by best-level volume."""
    if order.quantity > 0:
        if book.best_ask is None or order.price < book.best_ask.price:
            return 0, 0.0
        room = POSITION_LIMIT - pos
        qty = min(order.quantity, book.best_ask.volume, room)
        if qty <= 0:
            return 0, 0.0
        cash = -qty * book.best_ask.price
        return qty, cash
    elif order.quantity < 0:
        if book.best_bid is None or order.price > book.best_bid.price:
            return 0, 0.0
        room = POSITION_LIMIT + pos
        qty = min(-order.quantity, book.best_bid.volume, room)
        if qty <= 0:
            return 0, 0.0
        cash = qty * book.best_bid.price
        return -qty, cash
    return 0, 0.0


def execute_passive(order: Order, book: OrderBook, pos: int,
                    p_tick_fire: float = 0.05) -> tuple:
    """
    Passive fill model: order strictly inside displayed spread.
    Each tick, p_tick_fire chance someone crosses us; fill = min(our size,
    counterparty best volume, remaining position room).
    """
    if book.best_bid is None or book.best_ask is None:
        return 0, 0.0
    bid_p = book.best_bid.price
    ask_p = book.best_ask.price

    if order.quantity > 0:
        if not (order.price > bid_p and order.price < ask_p):
            return 0, 0.0
        if _rand() > p_tick_fire:
            return 0, 0.0
        room = POSITION_LIMIT - pos
        qty = min(order.quantity, book.best_ask.volume, room)
        if qty <= 0:
            return 0, 0.0
        return qty, -qty * order.price
    elif order.quantity < 0:
        if not (order.price < ask_p and order.price > bid_p):
            return 0, 0.0
        if _rand() > p_tick_fire:
            return 0, 0.0
        room = POSITION_LIMIT + pos
        qty = min(-order.quantity, book.best_bid.volume, room)
        if qty <= 0:
            return 0, 0.0
        return -qty, qty * order.price
    return 0, 0.0


# --------------------------------------------------------------
# Build OrderBook from the price-CSV row (3 levels per side)
# --------------------------------------------------------------

def build_books_from_group(group: pd.DataFrame) -> Dict[str, OrderBook]:
    books: Dict[str, OrderBook] = {}
    for _, row in group.iterrows():
        bids, asks = [], []
        for i in (1, 2, 3):
            bp = row.get(f"bid_price_{i}")
            bv = row.get(f"bid_volume_{i}")
            if pd.notna(bp) and pd.notna(bv) and bv > 0:
                bids.append(Quote(float(bp), int(bv)))
            ap = row.get(f"ask_price_{i}")
            av = row.get(f"ask_volume_{i}")
            if pd.notna(ap) and pd.notna(av) and av > 0:
                asks.append(Quote(float(ap), int(av)))
        bids.sort(key=lambda q: -q.price)
        asks.sort(key=lambda q: q.price)
        books[row["product"]] = OrderBook(bids=bids, asks=asks)
    return books


# --------------------------------------------------------------
# Run
# --------------------------------------------------------------

def main():
    print("Loading 3 days of R5 data...")
    prices = load_prices()
    prices = prices.sort_values(["day", "timestamp"]).reset_index(drop=True)

    trader = Trader()
    pos: Dict[str, int] = defaultdict(int)
    cash: Dict[str, float] = defaultdict(float)
    last_mid: Dict[str, float] = {}
    fill_count: Dict[str, int] = defaultdict(int)
    daily_pnl_at_end_of_day: Dict[int, float] = {}

    grouped = prices.groupby(["day", "timestamp"], sort=False)
    n_ticks = 0
    for (day, ts), g in grouped:
        books = build_books_from_group(g)
        for p, b in books.items():
            if b.mid is not None:
                last_mid[p] = b.mid

        orders = trader.decide(int(day), int(ts), books, dict(pos))
        for o in orders:
            book = books.get(o.product)
            if book is None:
                continue
            # Aggressive if crosses; else passive
            if o.quantity > 0 and book.best_ask is not None and o.price >= book.best_ask.price:
                f, c = execute_aggressive(o, book, pos[o.product])
            elif o.quantity < 0 and book.best_bid is not None and o.price <= book.best_bid.price:
                f, c = execute_aggressive(o, book, pos[o.product])
            else:
                f, c = execute_passive(o, book, pos[o.product])
            if f != 0:
                pos[o.product] += f
                cash[o.product] += c
                fill_count[o.product] += abs(f)

        n_ticks += 1
        if n_ticks % 5000 == 0:
            mtm = sum(cash[p] + pos[p] * last_mid.get(p, 0.0) for p in last_mid)
            print(f"  tick {n_ticks:>6}  day {day}  ts {ts:>7}  cumulative MTM = ${mtm:,.0f}")
        # Track end-of-day PnL
        if ts == 999900:
            mtm = sum(cash[p] + pos[p] * last_mid.get(p, 0.0) for p in last_mid)
            daily_pnl_at_end_of_day[int(day)] = mtm

    # Final MTM per product
    print("\n" + "=" * 84)
    print("R5 BACKTEST RESULTS")
    print("=" * 84)
    total = 0.0
    rows = []
    for p in sorted(set(last_mid).union(pos)):
        if p not in last_mid:
            continue
        mtm = cash[p] + pos[p] * last_mid[p]
        if abs(mtm) < 0.5 and pos[p] == 0 and fill_count[p] == 0:
            continue
        rows.append((p, mtm, pos[p], fill_count[p]))
        total += mtm
    rows.sort(key=lambda r: -r[1])
    print(f"{'product':<32} {'pnl':>12} {'pos':>6} {'fills':>8}")
    for p, mtm, pp, fc in rows:
        print(f"  {p:<30} ${mtm:>+11,.0f} {pp:>6} {fc:>8}")
    print(f"\n{'TOTAL':<32} ${total:>+11,.0f}")
    print()
    print("Per-day PnL (end-of-day MTM):")
    prev = 0.0
    for d in sorted(daily_pnl_at_end_of_day):
        delta = daily_pnl_at_end_of_day[d] - prev
        print(f"  day {d}: ${daily_pnl_at_end_of_day[d]:>+11,.0f}   (delta this day: ${delta:>+11,.0f})")
        prev = daily_pnl_at_end_of_day[d]


if __name__ == "__main__":
    main()
