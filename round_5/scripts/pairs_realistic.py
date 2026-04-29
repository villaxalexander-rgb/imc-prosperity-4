"""
Realistic pairs-trade backtest: cross the spread on entry/exit, respect L1 depth.
Also tests cross-day stability — top pair must work on each of the 3 days.
"""
from __future__ import annotations
import sys, os, itertools
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np
import pandas as pd
from load_r5 import load_prices, mid_panel, THEMES


def fit_pair_levels(yA: pd.Series, yB: pd.Series):
    x = np.vstack([np.ones(len(yA)), yB.values]).T
    coef, *_ = np.linalg.lstsq(x, yA.values, rcond=None)
    a, b = coef
    return a, b


def realistic_backtest(
    prA: pd.DataFrame,  # full price book, columns include bid_price_1, ask_price_1
    prB: pd.DataFrame,
    common_idx: pd.Index,
    alpha: float, beta: float,
    z_enter: float = 2.0, z_exit: float = 0.0,
    z_window: int = 500,    # rolling z-score window
    size: int = 1,          # 1 unit of A long-spread = +1 A, -beta_round B
    spread_cost_factor: float = 1.0,  # 1.0 = pay full spread on entry+exit
):
    """
    Simulate pairs trade with rolling z-score:
      spread_t = midA_t - (alpha + beta*midB_t)
      z_t = (spread_t - rolling_mean) / rolling_std
    Entry crosses both spreads (pay full bid-ask each leg). Exit same.
    """
    midA = (prA["bid_price_1"] + prA["ask_price_1"]) / 2
    midB = (prB["bid_price_1"] + prB["ask_price_1"]) / 2
    spreadA = prA["ask_price_1"] - prA["bid_price_1"]
    spreadB = prB["ask_price_1"] - prB["bid_price_1"]

    pair_spread = midA - (alpha + beta * midB)
    rolling_mu = pair_spread.rolling(z_window, min_periods=50).mean()
    rolling_sd = pair_spread.rolling(z_window, min_periods=50).std()
    z = (pair_spread - rolling_mu) / rolling_sd
    z = z.fillna(0)

    pos = 0
    trades = []
    pnl_realized = 0.0
    entry_spread = None
    entry_cost = 0.0   # entry slippage paid

    midA_arr = midA.values
    midB_arr = midB.values
    pair_arr = pair_spread.values
    z_arr = z.values
    spA_arr = spreadA.values
    spB_arr = spreadB.values

    for i in range(50, len(midA_arr)):
        if pos == 0:
            if z_arr[i] > z_enter:
                pos = -1  # short spread: short A, long beta*B
                entry_spread = pair_arr[i]
                # cost: pay half-spread A (sell at bid -> lose spA/2 vs mid)
                #       pay half-spread |beta|*B
                entry_cost = (spA_arr[i] / 2 + abs(beta) * spB_arr[i] / 2) * spread_cost_factor
            elif z_arr[i] < -z_enter:
                pos = +1
                entry_spread = pair_arr[i]
                entry_cost = (spA_arr[i] / 2 + abs(beta) * spB_arr[i] / 2) * spread_cost_factor
        else:
            should_exit = (pos > 0 and z_arr[i] >= z_exit) or (pos < 0 and z_arr[i] <= z_exit)
            if should_exit:
                # exit cost: pay half-spread on each leg again
                exit_cost = (spA_arr[i] / 2 + abs(beta) * spB_arr[i] / 2) * spread_cost_factor
                # PnL on the spread move:
                #   pos=+1 (long spread) → profit = +(exit - entry) per unit
                #   pos=-1 (short spread) → profit = -(exit - entry) per unit
                pnl_unit = pos * (pair_arr[i] - entry_spread) - entry_cost - exit_cost
                pnl_realized += pnl_unit * size
                trades.append({"i": i, "pos": pos, "entry": entry_spread, "exit": pair_arr[i],
                               "pnl": pnl_unit, "z_exit": z_arr[i]})
                pos = 0
                entry_spread = None

    # Liquidate at end if still in
    if pos != 0:
        i = len(midA_arr) - 1
        exit_cost = (spA_arr[i] / 2 + abs(beta) * spB_arr[i] / 2) * spread_cost_factor
        pnl_unit = pos * (pair_arr[i] - entry_spread) - entry_cost - exit_cost
        pnl_realized += pnl_unit * size
        trades.append({"i": i, "pos": pos, "entry": entry_spread, "exit": pair_arr[i],
                       "pnl": pnl_unit, "forced_close": True})

    return {
        "pnl_total": pnl_realized,
        "n_trades": len(trades),
        "win_rate": (sum(1 for t in trades if t["pnl"] > 0) / len(trades)) if trades else 0,
        "avg_pnl_per_trade": (pnl_realized / len(trades)) if trades else 0,
        "trades": trades,
    }


def main():
    prices = load_prices()

    # Pre-build a per-product per-tick book frame (we'll need bid_1/ask_1)
    book = (prices.set_index(["global_ts", "product"])
                  [["bid_price_1", "ask_price_1"]]
                  .unstack("product"))
    # Cols are MultiIndex (field, product). Flatten:
    print(f"Book shape: {book.shape}")

    # Pair list ordered by mid-to-mid backtest from the previous run
    top_pairs = [
        ("MICROCHIP", "SQUARE", "TRIANGLE"),
        ("UV_VISOR", "AMBER", "MAGENTA"),
        ("MICROCHIP", "RECTANGLE", "SQUARE"),
        ("SLEEP_POD", "COTTON", "NYLON"),
        ("PEBBLES", "XS", "S"),
        ("PEBBLES", "S", "M"),
        ("PANEL", "1X2", "2X2"),
        ("PEBBLES", "XS", "XL"),
        ("MICROCHIP", "OVAL", "TRIANGLE"),
        ("SLEEP_POD", "POLYESTER", "SUEDE"),
        ("ROBOT", "IRONING", "VACUUMING"),
        ("ROBOT", "MOPPING", "VACUUMING"),
        ("PEBBLES", "M", "XL"),
        ("PANEL", "1X4", "2X4"),
        ("SNACKPACK", "CHOCOLATE", "VANILLA"),
        ("SNACKPACK", "RASPBERRY", "STRAWBERRY"),
        ("SNACKPACK", "PISTACHIO", "STRAWBERRY"),
    ]

    results = []
    for theme, A, B in top_pairs:
        Aname = f"{theme}_{A}"
        Bname = f"{theme}_{B}"
        # Per-day backtest
        per_day = {}
        all_idx = book.index
        m_full = book["bid_price_1"].copy()
        # quick mid panel fallback
        m_mid = (book["bid_price_1"] + book["ask_price_1"]) / 2
        # OLS fit on FULL period
        yA = m_mid[Aname].dropna()
        yB = m_mid[Bname].dropna()
        common = yA.index.intersection(yB.index)
        alpha, beta = fit_pair_levels(yA.loc[common], yB.loc[common])

        # Per-day backtest
        for day_n in (2, 3, 4):
            day_mask = (book.index >= day_n * 1_000_000) & (book.index < (day_n + 1) * 1_000_000)
            sub_idx = book.index[day_mask]
            prA = book.xs(Aname, axis=1, level="product").loc[sub_idx].dropna()
            prB = book.xs(Bname, axis=1, level="product").loc[sub_idx].dropna()
            common_d = prA.index.intersection(prB.index)
            res = realistic_backtest(prA.loc[common_d], prB.loc[common_d], common_d,
                                      alpha, beta, z_enter=2.0, z_exit=0.0)
            per_day[day_n] = res["pnl_total"]
        total = sum(per_day.values())

        # Full-period backtest (3 days continuous)
        prA = book.xs(Aname, axis=1, level="product").dropna()
        prB = book.xs(Bname, axis=1, level="product").dropna()
        common_all = prA.index.intersection(prB.index)
        full = realistic_backtest(prA.loc[common_all], prB.loc[common_all], common_all,
                                   alpha, beta, z_enter=2.0, z_exit=0.0)
        results.append({
            "pair": f"{Aname} / {Bname}",
            "beta": round(beta, 3),
            "pnl_d2": round(per_day[2], 0),
            "pnl_d3": round(per_day[3], 0),
            "pnl_d4": round(per_day[4], 0),
            "pnl_3day_sum": round(total, 0),
            "pnl_full_continuous": round(full["pnl_total"], 0),
            "n_trades_full": full["n_trades"],
            "win_rate_full": round(full["win_rate"], 2),
            "avg_pnl_per_trade": round(full["avg_pnl_per_trade"], 1),
        })

    df = pd.DataFrame(results)
    df = df.sort_values("pnl_full_continuous", ascending=False)
    out = os.path.join(os.path.dirname(__file__), "..", "experiments", "pairs_realistic.csv")
    df.to_csv(out, index=False)
    print()
    print("=" * 110)
    print("REALISTIC BACKTEST (cross spread on entry+exit, rolling z-score 500-tick window)")
    print("=" * 110)
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
