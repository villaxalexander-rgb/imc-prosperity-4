"""Backtest each of the 3 days independently to see per-day robustness."""
from __future__ import annotations
import pandas as pd
from collections import defaultdict
from trader import Trader, POSITION_LIMIT, VEV_PRODUCTS, VE, HP
from backtest import build_books_for_tick, execute_aggressive, execute_passive


def run_day(day: int) -> dict:
    df = pd.read_csv(f"prices_round_3_day_{day}.csv", sep=";")
    df["day"] = day
    df = df.sort_values(["day", "timestamp"]).reset_index(drop=True)

    trader = Trader()
    positions: dict = defaultdict(int)
    cash: dict = defaultdict(float)
    last_mid: dict = {}

    for (_, ts), g in df.groupby(["day", "timestamp"], sort=False):
        books = build_books_for_tick(g)
        for p, b in books.items():
            if b.mid is not None:
                last_mid[p] = b.mid
        orders = trader.decide(day, int(ts), books, dict(positions))
        for o in orders:
            book = books.get(o.product)
            if book is None:
                continue
            if o.quantity > 0 and book.best_ask is not None and o.price >= book.best_ask.price:
                f, c = execute_aggressive(o, book, positions[o.product], POSITION_LIMIT[o.product])
            elif o.quantity < 0 and book.best_bid is not None and o.price <= book.best_bid.price:
                f, c = execute_aggressive(o, book, positions[o.product], POSITION_LIMIT[o.product])
            else:
                f, c = execute_passive(o, book, positions[o.product], POSITION_LIMIT[o.product])
            positions[o.product] += f
            cash[o.product] += c

    ALL = [HP, VE] + VEV_PRODUCTS
    pnl = {p: cash[p] + positions[p] * last_mid.get(p, 0.0) for p in ALL}
    return pnl


if __name__ == "__main__":
    rows = []
    for d in range(3):
        pnl = run_day(d)
        total = sum(pnl.values())
        rows.append({"day": d, "total": total, **pnl})
    out = pd.DataFrame(rows).set_index("day")
    cols = ["total", HP, VE] + [f"VEV_{k}" for k in [5000,5100,5200,5300,5400,5500]]
    print(out[cols].round(0).to_string())
