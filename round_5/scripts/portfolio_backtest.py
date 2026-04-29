"""
PORTFOLIO BACKTEST — run all top signals simultaneously with shared per-product
position accounting. Each signal contributes a *target* position per product;
we sum across signals and clip to limit, then walk each tick computing PnL.

Key design choice: every signal independently maintains its rolling z-score and
desired position weight, but we maintain ONE inventory per product. PnL is the
mark-to-market change of inventory plus realized cash.
"""
from __future__ import annotations
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np
import pandas as pd
from load_r5 import load_prices, mid_panel, THEMES


# Each signal: list of (product, weight) — sum-product gives the spread.
# Spread = sum_i (weight_i * mid_i)  + intercept_constant (we'll absorb intercept
# into rolling mean automatically — so "intercept" is unused)
SIGNALS = [
    # name, weights (dict prod->coef), z_enter, max_size_per_unit
    {"name": "PEB_XL_LM_tri",   "w": {"PEBBLES_XL": 1.0, "PEBBLES_L": +1.012, "PEBBLES_M": -1.212}, "z": 2.0, "size": 30},
    {"name": "PEB_S_ML_tri",    "w": {"PEBBLES_S": 1.0, "PEBBLES_M": +0.778, "PEBBLES_L": -0.106}, "z": 2.0, "size": 30},
    {"name": "PEB_XL_LXS_tri",  "w": {"PEBBLES_XL": 1.0, "PEBBLES_L": +1.291, "PEBBLES_XS": +1.163}, "z": 2.0, "size": 25},
    {"name": "PEB_XS_ML_tri",   "w": {"PEBBLES_XS": 1.0, "PEBBLES_M": +1.434, "PEBBLES_L": +0.094}, "z": 2.0, "size": 25},
    {"name": "PEB_M_SXL_tri",   "w": {"PEBBLES_M": 1.0, "PEBBLES_S": +0.878, "PEBBLES_XL": +0.208}, "z": 2.0, "size": 25},
    {"name": "MIC_OV_TRI_pair", "w": {"MICROCHIP_OVAL": 1.0, "MICROCHIP_TRIANGLE": -1.621}, "z": 2.0, "size": 30},
    {"name": "UV_AM_MAG_pair",  "w": {"UV_VISOR_AMBER": 1.0, "UV_VISOR_MAGENTA": +1.409}, "z": 2.0, "size": 30},
    {"name": "MIC_REC_SQ_pair", "w": {"MICROCHIP_RECTANGLE": 1.0, "MICROCHIP_SQUARE": +0.363}, "z": 2.0, "size": 30},
    {"name": "MIC_SQ_TRI_pair", "w": {"MICROCHIP_SQUARE": 1.0, "MICROCHIP_TRIANGLE": +0.989}, "z": 2.0, "size": 30},
]

POSITION_LIMIT = 200  # all R5 products

ZWIN = 500


def main():
    prices = load_prices()
    book = (prices.set_index(["global_ts","product"])[["bid_price_1","ask_price_1"]].unstack("product"))
    bid = book["bid_price_1"]
    ask = book["ask_price_1"]
    mid = (bid + ask) / 2
    spd = ask - bid

    # All products used
    all_prods = set()
    for sig in SIGNALS:
        all_prods.update(sig["w"].keys())
    all_prods = sorted(all_prods)

    n_ticks = len(mid)
    pos = {p: 0 for p in all_prods}
    cash = {p: 0.0 for p in all_prods}
    sig_pos = {sig["name"]: 0 for sig in SIGNALS}  # signed position per signal in "spread units"

    # Pre-compute spread series per signal
    sig_spread = {}
    for sig in SIGNALS:
        s = pd.Series(0.0, index=mid.index)
        for p, w in sig["w"].items():
            s = s + w * mid[p]
        sig_spread[sig["name"]] = s
    # Rolling z-scores
    sig_z = {}
    for sig in SIGNALS:
        s = sig_spread[sig["name"]]
        rmu = s.rolling(ZWIN, min_periods=50).mean()
        rsd = s.rolling(ZWIN, min_periods=50).std()
        sig_z[sig["name"]] = ((s - rmu) / rsd).fillna(0).values

    pnl_curve = []
    daily_pnl = {2: 0.0, 3: 0.0, 4: 0.0}

    bid_arr = bid.values
    ask_arr = ask.values
    mid_arr = mid.values
    cols = list(mid.columns)
    col_idx = {c: i for i, c in enumerate(cols)}
    ts_arr = mid.index.values

    last_pnl = 0.0
    for i in range(50, n_ticks):
        # 1) Determine target signed position per signal
        target_per_sig = {}
        for sig in SIGNALS:
            z = sig_z[sig["name"]][i]
            cur = sig_pos[sig["name"]]
            if cur == 0:
                if z > sig["z"]:
                    target_per_sig[sig["name"]] = -1
                elif z < -sig["z"]:
                    target_per_sig[sig["name"]] = +1
                else:
                    target_per_sig[sig["name"]] = 0
            else:
                # Exit when z crosses 0
                if cur > 0 and z >= 0:
                    target_per_sig[sig["name"]] = 0
                elif cur < 0 and z <= 0:
                    target_per_sig[sig["name"]] = 0
                else:
                    target_per_sig[sig["name"]] = cur

        # 2) Aggregate desired product position
        desired = {p: 0 for p in all_prods}
        for sig in SIGNALS:
            cur = target_per_sig[sig["name"]]
            sz = sig["size"] * cur
            for p, w in sig["w"].items():
                desired[p] += int(round(w * sz))

        # Cap at limit
        for p in desired:
            if desired[p] > POSITION_LIMIT:
                desired[p] = POSITION_LIMIT
            if desired[p] < -POSITION_LIMIT:
                desired[p] = -POSITION_LIMIT

        # 3) Trade to desired (cross spread)
        for p in all_prods:
            j = col_idx[p]
            diff_pos = desired[p] - pos[p]
            if diff_pos == 0:
                continue
            if diff_pos > 0:
                # Buy at ask
                fill = diff_pos
                price = ask_arr[i, j]
                if not np.isnan(price):
                    cash[p] -= fill * price
                    pos[p] += fill
            else:
                fill = -diff_pos
                price = bid_arr[i, j]
                if not np.isnan(price):
                    cash[p] += fill * price
                    pos[p] -= fill

        # Update signal positions to reflect target (in absence of partial fills)
        for sig in SIGNALS:
            sig_pos[sig["name"]] = target_per_sig[sig["name"]]

        # 4) Mark to market
        mtm = sum(cash[p] + pos[p] * (mid_arr[i, col_idx[p]] if not np.isnan(mid_arr[i, col_idx[p]]) else 0)
                  for p in all_prods)
        pnl_curve.append(mtm)

        # Track per-day PnL
        gts = ts_arr[i]
        d = int(gts // 1_000_000)
        if d in daily_pnl:
            daily_pnl[d] = mtm

    # Compute per-day deltas
    p_curve = pd.Series(pnl_curve, index=mid.index[50:])
    d2_pnl = p_curve.loc[2_000_000:3_000_000].iloc[-1] if not p_curve.loc[2_000_000:3_000_000].empty else 0
    d3_pnl = (p_curve.loc[3_000_000:4_000_000].iloc[-1] - p_curve.loc[2_000_000:3_000_000].iloc[-1]) if not p_curve.loc[3_000_000:4_000_000].empty else 0
    d4_pnl = (p_curve.loc[4_000_000:5_000_000].iloc[-1] - p_curve.loc[3_000_000:4_000_000].iloc[-1]) if not p_curve.loc[4_000_000:5_000_000].empty else 0

    print()
    print("="*80)
    print("PORTFOLIO BACKTEST — all signals simultaneously")
    print("="*80)
    print(f"Final MTM PnL: ${pnl_curve[-1]:,.0f}")
    print(f"Day 2 final:   ${d2_pnl:,.0f}")
    print(f"Day 3 delta:   ${d3_pnl:,.0f}")
    print(f"Day 4 delta:   ${d4_pnl:,.0f}")
    print()
    print("Final positions (only those != 0):")
    for p, v in sorted(pos.items()):
        if v != 0:
            print(f"  {p:<26} {v:>5}  cash={cash[p]:>+10.0f}  mtm={cash[p] + v*mid_arr[-1, col_idx[p]]:>+10.0f}")
    print()
    print("Per-product final PnL contribution:")
    contrib = []
    for p in all_prods:
        j = col_idx[p]
        m = mid_arr[-1, j]
        if not np.isnan(m):
            contrib.append((p, cash[p] + pos[p] * m))
    for p, v in sorted(contrib, key=lambda x: -x[1]):
        print(f"  {p:<26} ${v:>+10,.0f}")

    # save curve
    out = os.path.join(os.path.dirname(__file__), "..", "experiments", "portfolio_curve.csv")
    pd.DataFrame({"global_ts": p_curve.index, "pnl": p_curve.values}).to_csv(out, index=False)
    print(f"\nSaved equity curve to {out}")


if __name__ == "__main__":
    main()
