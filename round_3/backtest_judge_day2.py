"""
Replay the exact judge's live day-2 book against the new trader.
Only aggressive fills (strictly crossing the spread) count, which matches how
the live judge environment fills orders.
"""
from __future__ import annotations
import json, io
import pandas as pd
from collections import defaultdict

from trader import Trader, POSITION_LIMIT, VEV_PRODUCTS, VE, HP
from backtest import build_books_for_tick, execute_aggressive


def main(log_path: str):
    with open(log_path) as f:
        data = json.load(f)
    act = pd.read_csv(io.StringIO(data["activitiesLog"]), sep=";")
    act = act.sort_values(["day", "timestamp"]).reset_index(drop=True)

    trader = Trader()
    positions: dict = defaultdict(int)
    cash: dict = defaultdict(float)
    last_mid: dict = {}
    fills_count: dict = defaultdict(int)

    for (day, ts), g in act.groupby(["day", "timestamp"], sort=False):
        books = build_books_for_tick(g)
        for p, b in books.items():
            if b.mid is not None:
                last_mid[p] = b.mid
        orders = trader.decide(int(day), int(ts), books, dict(positions))
        for o in orders:
            book = books.get(o.product)
            if book is None:
                continue
            # Live judge fills ONLY aggressive crosses; passive orders don't fill reliably
            if o.quantity > 0 and book.best_ask is not None and o.price >= book.best_ask.price:
                f, c = execute_aggressive(o, book, positions[o.product], POSITION_LIMIT[o.product])
            elif o.quantity < 0 and book.best_bid is not None and o.price <= book.best_bid.price:
                f, c = execute_aggressive(o, book, positions[o.product], POSITION_LIMIT[o.product])
            else:
                continue  # ignore passive orders in this "judge-like" sim
            positions[o.product] += f
            cash[o.product] += c
            if f != 0:
                fills_count[o.product] += abs(f)

    ALL = [HP, VE] + VEV_PRODUCTS
    total = 0.0
    print(f"\n=== Simulated new Trader against JUDGE day-2 book (aggressive-only) ===")
    for p in sorted(ALL, key=lambda x: -(cash[x] + positions[x] * last_mid.get(x, 0.0))):
        pnl = cash[p] + positions[p] * last_mid.get(p, 0.0)
        total += pnl
        if fills_count.get(p, 0) > 0 or abs(positions[p]) > 0:
            print(f"  {p:<22} PnL={pnl:>10,.2f}  pos={positions[p]:>5}  fills={fills_count.get(p,0)}")
    print(f"\nTOTAL PnL: {total:,.2f}")


if __name__ == "__main__":
    main("new_submission/377046.log")
