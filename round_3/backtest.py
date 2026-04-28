"""
Event-driven backtester for Round 3.

Replays prices_round_3_day_{0,1,2}.csv against the Trader class in trader.py.
Fills assume:
  - Aggressive orders eat the displayed depth (up to 3 levels in the CSV).
  - Passive orders posted strictly inside the spread have a small fill
    probability each tick, modelled as min(size, opposite-side size * p).
  - Any remaining position is marked-to-market at the final mid.

Outputs:
  - per-product PnL, total PnL
  - final positions
  - a CSV of per-tick PnL and inventory
"""

from __future__ import annotations

import pandas as pd
import numpy as np
from collections import defaultdict
from typing import Dict, List, Tuple

from trader import (
    Trader, Order, Quote, OrderBook,
    POSITION_LIMIT, VEV_PRODUCTS, VE, HP,
)


ALL_PRODUCTS = [HP, VE] + VEV_PRODUCTS


def load_prices() -> pd.DataFrame:
    dfs = []
    for d in range(3):
        df = pd.read_csv(f"prices_round_3_day_{d}.csv", sep=";")
        df["day"] = d
        dfs.append(df)
    return pd.concat(dfs, ignore_index=True)


def build_books_for_tick(group: pd.DataFrame) -> Dict[str, OrderBook]:
    """Convert the rows at one (day, timestamp) into a dict[product]->OrderBook."""
    books: Dict[str, OrderBook] = {}
    for _, row in group.iterrows():
        bids: List[Quote] = []
        asks: List[Quote] = []
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


def execute_aggressive(
    order: Order,
    book: OrderBook,
    pos: int,
    limit: int,
) -> Tuple[int, float]:
    """Execute an aggressive order against the displayed book."""
    fills = 0
    cash_delta = 0.0
    qty = order.quantity
    if qty > 0:
        room = limit - pos
        qty = min(qty, room)
        for ask in list(book.asks):
            if qty <= 0 or order.price < ask.price:
                break
            take = min(qty, ask.volume)
            cash_delta -= take * ask.price
            fills += take
            qty -= take
            ask.volume -= take
        book.asks = [a for a in book.asks if a.volume > 0]
    elif qty < 0:
        room = limit + pos
        qty = max(qty, -room)
        need = -qty
        for bid in list(book.bids):
            if need <= 0 or order.price > bid.price:
                break
            take = min(need, bid.volume)
            cash_delta += take * bid.price
            fills -= take
            need -= take
            bid.volume -= take
        book.bids = [b for b in book.bids if b.volume > 0]
    return fills, cash_delta


_PRNG = [0x12345678]

def _prng():
    _PRNG[0] = (_PRNG[0] * 1664525 + 1013904223) & 0xFFFFFFFF
    return _PRNG[0] / 0xFFFFFFFF


def execute_passive(
    order: Order,
    book: OrderBook,
    pos: int,
    limit: int,
    p_tick_fire: float = 0.02,   # calibrated from live: HP had ~2% fire rate
) -> Tuple[int, float]:
    """
    Realistic passive fill model (calibrated from the live judge output):
      - Our order must be strictly inside the bid-ask spread.
      - Each tick, probability `p_tick_fire` that a counterparty crosses us.
      - When it fires, fill = min(our size, opposite-side best size, position room).
    """
    if not book.bids or not book.asks:
        return 0, 0.0
    best_bid = book.best_bid.price
    best_ask = book.best_ask.price
    fills = 0
    cash_delta = 0.0
    qty = order.quantity
    inside = ((qty > 0 and order.price > best_bid and order.price < best_ask) or
              (qty < 0 and order.price < best_ask and order.price > best_bid))
    if not inside or _prng() > p_tick_fire:
        return 0, 0.0
    if qty > 0:
        counter = book.best_ask.volume
        fill = min(qty, counter, limit - pos)
        if fill > 0:
            cash_delta -= fill * order.price
            fills += fill
    elif qty < 0:
        counter = book.best_bid.volume
        fill = min(-qty, counter, limit + pos)
        if fill > 0:
            cash_delta += fill * order.price
            fills -= fill
    return fills, cash_delta


def run_backtest() -> dict:
    prices = load_prices()
    prices = prices.sort_values(["day", "timestamp"]).reset_index(drop=True)

    trader = Trader()
    positions: Dict[str, int] = defaultdict(int)
    cash: Dict[str, float] = defaultdict(float)
    fills_by_product: Dict[str, int] = defaultdict(int)

    equity_curve = []
    grouped = prices.groupby(["day", "timestamp"], sort=False)

    # Precompute last mid for MTM
    last_mid: Dict[str, float] = {}

    n_ticks = 0
    for (day, ts), g in grouped:
        books = build_books_for_tick(g)
        for p, b in books.items():
            if b.mid is not None:
                last_mid[p] = b.mid

        orders = trader.decide(int(day), int(ts), books, dict(positions))
        for o in orders:
            book = books.get(o.product)
            if book is None:
                continue
            # Classify: aggressive if crosses, passive otherwise
            if o.quantity > 0 and book.best_ask is not None and o.price >= book.best_ask.price:
                f, c = execute_aggressive(o, book, positions[o.product], POSITION_LIMIT[o.product])
            elif o.quantity < 0 and book.best_bid is not None and o.price <= book.best_bid.price:
                f, c = execute_aggressive(o, book, positions[o.product], POSITION_LIMIT[o.product])
            else:
                f, c = execute_passive(o, book, positions[o.product], POSITION_LIMIT[o.product])
            positions[o.product] += f
            cash[o.product] += c
            if f != 0:
                fills_by_product[o.product] += abs(f)

        n_ticks += 1
        if n_ticks % 5000 == 0:
            mtm_total = sum(cash[p] + positions[p] * last_mid.get(p, 0.0) for p in ALL_PRODUCTS)
            equity_curve.append({"day": int(day), "timestamp": int(ts), "equity": mtm_total})

    # Final MTM
    per_product_pnl = {}
    total_pnl = 0.0
    for p in ALL_PRODUCTS:
        mtm = cash[p] + positions[p] * last_mid.get(p, 0.0)
        per_product_pnl[p] = mtm
        total_pnl += mtm

    ec = pd.DataFrame(equity_curve)
    ec.to_csv("equity_curve.csv", index=False)

    return {
        "per_product_pnl": per_product_pnl,
        "final_positions": dict(positions),
        "fills": dict(fills_by_product),
        "total_pnl": total_pnl,
        "n_ticks": n_ticks,
    }


if __name__ == "__main__":
    result = run_backtest()
    print("\n==== Round 3 backtest ====")
    print(f"Ticks processed: {result['n_ticks']}")
    print(f"Total PnL:       {result['total_pnl']:,.2f}")
    print("\nPer-product PnL:")
    for p, v in sorted(result["per_product_pnl"].items(), key=lambda kv: -kv[1]):
        pos = result["final_positions"].get(p, 0)
        fills = result["fills"].get(p, 0)
        print(f"  {p:<22} PnL={v:>12,.2f}   final_pos={pos:>5}   fills={fills}")
