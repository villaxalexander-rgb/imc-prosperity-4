"""
Within-theme pairs-trade screen.
For each within-theme pair (10 themes x 10 pairs = 100 pairs):
  - OLS hedge ratio  Y = a + b*X
  - residual spread mean-reversion: AR(1) rho, half-life
  - z-score distribution
  - quick backtest at z thresholds {1.5, 2.0, 2.5}
Output ranked CSV + markdown summary.
"""
from __future__ import annotations
import sys, os, itertools
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np
import pandas as pd
from load_r5 import load_prices, mid_panel, THEMES


def fit_pair(yA: pd.Series, yB: pd.Series):
    """Return (alpha, beta, residual)."""
    x = np.vstack([np.ones(len(yA)), yB.values]).T
    coef, *_ = np.linalg.lstsq(x, yA.values, rcond=None)
    a, b = coef
    resid = yA.values - (a + b * yB.values)
    return a, b, resid


def ar1_half_life(resid: np.ndarray):
    r = resid - resid.mean()
    x = r[:-1]
    y = r[1:]
    rho = (x * y).sum() / (x * x).sum() if (x * x).sum() > 1e-12 else 0.0
    if 0 < rho < 1:
        hl = np.log(0.5) / np.log(rho)
    else:
        hl = float("inf")
    return rho, hl


def backtest_pair(spread: np.ndarray, z: np.ndarray, threshold: float, midA: np.ndarray, midB: np.ndarray, beta: float):
    """
    Open position when |z|>threshold, close when z crosses 0.
    Position is +1 long-spread (long A, short beta*B) when z<-threshold.
                -1 short-spread (short A, long beta*B) when z>+threshold.
    Uses mid-to-mid (no spread cost — optimistic). PnL: ds = d(spread).
    """
    pos = np.zeros(len(spread))
    cur = 0
    for i in range(1, len(spread)):
        if cur == 0:
            if z[i] > threshold:
                cur = -1
            elif z[i] < -threshold:
                cur = +1
        else:
            if cur > 0 and z[i] >= 0:
                cur = 0
            elif cur < 0 and z[i] <= 0:
                cur = 0
        pos[i] = cur
    # PnL = pos[i-1] * (spread[i] - spread[i-1])
    ds = np.diff(spread, prepend=spread[0])
    pnl = pos * ds
    cum = pnl.cumsum()
    n_trades = int((np.diff(pos) != 0).sum())
    sharpe = pnl.std() and (pnl.mean() / pnl.std() * np.sqrt(10000)) or 0
    return {
        "pnl_total": cum[-1] if len(cum) else 0,
        "n_trades": n_trades,
        "max_pos_held": int(np.abs(pos).max()),
        "frac_in_market": float((pos != 0).mean()),
        "sharpe_per_day": sharpe,
    }


def run():
    prices = load_prices()
    m = mid_panel(prices)

    rows = []
    for theme, members in THEMES.items():
        cols = [f"{theme}_{x}" for x in members]
        for a_name, b_name in itertools.combinations(cols, 2):
            yA = m[a_name].dropna()
            yB = m[b_name].dropna()
            common = yA.index.intersection(yB.index)
            if len(common) < 1000:
                continue
            yA = yA.loc[common].astype(float)
            yB = yB.loc[common].astype(float)

            alpha, beta, resid = fit_pair(yA, yB)
            mu = resid.mean()
            sd = resid.std()
            if sd < 1e-6:
                continue
            z = (resid - mu) / sd
            rho, hl = ar1_half_life(resid)

            backtest_15 = backtest_pair(resid, z, 1.5, yA.values, yB.values, beta)
            backtest_20 = backtest_pair(resid, z, 2.0, yA.values, yB.values, beta)
            backtest_25 = backtest_pair(resid, z, 2.5, yA.values, yB.values, beta)

            rows.append({
                "theme": theme,
                "A": a_name.replace(theme + "_", ""),
                "B": b_name.replace(theme + "_", ""),
                "beta": round(beta, 4),
                "corr": round(yA.corr(yB), 3),
                "ret_corr": round(yA.diff().corr(yB.diff()), 3),
                "ar1_rho": round(rho, 4),
                "half_life_ticks": round(hl, 1) if np.isfinite(hl) else "inf",
                "spread_std": round(sd, 1),
                "pnl_z15": round(backtest_15["pnl_total"], 0),
                "pnl_z20": round(backtest_20["pnl_total"], 0),
                "pnl_z25": round(backtest_25["pnl_total"], 0),
                "n_trades_z15": backtest_15["n_trades"],
                "frac_in_z15": round(backtest_15["frac_in_market"], 2),
            })

    df = pd.DataFrame(rows)
    df = df.sort_values("pnl_z20", ascending=False)
    out = os.path.join(os.path.dirname(__file__), "..", "experiments", "pairs_screen.csv")
    df.to_csv(out, index=False)
    print(f"Saved {len(df)} pairs to {out}")
    print()
    print("=" * 100)
    print("TOP 20 BY PnL @ z=2.0 (mid-to-mid, optimistic)")
    print("=" * 100)
    print(df.head(20).to_string(index=False))
    print()
    print("=" * 100)
    print("BOTTOM 5 (negative PnL — likely day-specific or anti-correlation flip)")
    print("=" * 100)
    print(df.tail(5).to_string(index=False))
    return df


if __name__ == "__main__":
    run()
