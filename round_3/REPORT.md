# Round 3 — Trading Strategy Report

**Products traded**

| Symbol | Type | Notes |
| --- | --- | --- |
| `HYDROGEL_PACK` | Spot | Uncorrelated with Velvetfruit; mean-reverting; wide spread (~16 ticks) |
| `VELVETFRUIT_EXTRACT` (VE) | Spot | Tight spread (~5 ticks); upward drift across days; used as delta-hedge |
| `VEV_K`, K ∈ {4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500} | European call on VE | TTE = 7 Solvenarian days from day 1 |

## 1. Data reconnaissance

The three price files `prices_round_3_day_{0,1,2}.csv` each cover 10 000 ticks
at 100-unit spacing (1 000 000 timestamps/day × 3 days = 30 000 snapshots per product).

| Product | Mean mid | Mid std (per day) | Avg spread | Auto-correlation of Δmid |
| --- | ---: | ---: | ---: | ---: |
| HYDROGEL_PACK | 9 990.8 | ~30 | **15.7** | –0.13 |
| VELVETFRUIT_EXTRACT | 5 250.1 | ~15 | 5.0 | –0.16 |
| VEV_5400 | 15.9 | 3 | 1.4 | — |

Both spot products are **tick-to-tick mean-reverting** (negative lag-1 autocorrelation),
which is exactly the regime a patient liquidity provider wants. Velvetfruit has a
day-over-day drift (+10 on day 1, +28 on day 2), so an EWMA fair-value there suffers
from adverse selection — we therefore do **not** market-make VE.

## 2. Implied-vol analysis

Using Black-Scholes (r = 0, T in years with 250 trading days/year, TTE = 7 days on
day 1 i.e. `TTE(day, ts) = (8 − day − ts / 1e6) / 250`) we back out IV for every strike
at every tick.

### 2.1 Core strikes (5000–5500)

IV is remarkably stable — at-the-money vol hovers around **20 %** with a
cross-time standard deviation of only **~0.3 vol pts**.

| K | Mean IV | Std IV | Half-life of IV (ticks) |
| ---: | ---: | ---: | ---: |
| 5000 | 0.2001 | 0.006 | <1 |
| 5100 | 0.1983 | 0.004 | ~3 |
| 5200 | 0.2005 | 0.003 | ~4 |
| 5300 | 0.2027 | 0.004 | ~10 |
| **5400** | **0.1900** | **0.003** | ~8 |
| 5500 | 0.2062 | 0.004 | ~3 |

### 2.2 Wings (4000, 4500, 6000, 6500)

- **VEV_4000 / VEV_4500**: deep ITM, trade near intrinsic value. IV computed from the
  displayed mid is noisy (σ = 0.04–0.08) and often below intrinsic → we do **not**
  trade them.
- **VEV_6000 / VEV_6500**: permanently sit at the **0.5 price floor**; no usable
  signal → excluded.

### 2.3 The 5400 anomaly

Fitting a quadratic smile across strikes 5000–5500 at each tick **and excluding 5400
from the fit**, we measure how the 5400 market price compares to its smile-implied
fair:

| Statistic (out-of-sample) | Value |
| --- | ---: |
| Mean (market IV − smile fair IV) at K=5400 | **−0.0138** |
| Std | 0.0046 |
| Fraction of ticks where it is cheap | >99 % |
| Dollar edge per contract (vega × 1.4 vol pts) | ~$3.45 |

VEV_5400 is consistently and persistently underpriced by ~1.4 vol pts versus the rest
of the smile. That is the single cleanest edge in the dataset.

## 3. Strategy

### 3.1 Hydrogel Pack — inventory-skewed market making (biggest contributor)

- `fair = current book mid` (the EWMA approach lagged and lost money; instant mid is
  correct for mean-reverting returns)
- Inventory skew: `fair_skew = mid − 4 × position / 50` so our quotes drift
  against inventory as soon as we start leaning
- **Take**: any ask ≤ mid − 3, any bid ≥ mid + 3, up to position limit
- **Make**: post bid at `floor(fair_skew − 2)` and ask at `ceil(fair_skew + 2)`,
  inside the displayed spread, with 40-lot size per side

Intuition: the book's wide 16-tick spread means the resting quotes from the
counterparty are a poor fair value estimate. By always quoting tighter than the
book and leaning on inventory we harvest ~2.5 ticks per filled round-trip.

### 3.2 VEV options — per-strike IV mean-reversion + 5400 bias

For each traded strike (5000–5500):
1. Compute market IV from book mid.
2. Smooth it with an EWMA (α = 0.25 ≈ 2-tick half-life) to match the series'
   natural mean-reversion half-life.
3. Compare to the calibrated fair IV (`FAIR_IV[K]`); for K = 5400 we add +1.0 vol pt
   to the fair to capture the chronic underpricing.
4. Size the target position linearly in the z-score: 0 below ±1 σ, full-size at ±3 σ.
5. Take orders only when the book price crosses through our BS fair price.

### 3.3 Delta hedging in Velvetfruit Extract

After the options loop, sum `position × BS-delta` across all held VEVs and rebalance
the VE position to neutralise it, but **only when net delta is off by ≥ 20 lots**
(a dead-band — hedging every tick costs more than it's worth when VEV positions are
small).

### 3.4 What we explicitly do **not** do

- No market-making of VE (tight 5-tick spread + intraday drift ⇒ adverse-selected)
- No trading of VEV_4000/4500 (below intrinsic) or VEV_6000/6500 (floor-locked)

## 4. Backtest results

Using `backtest.py` against the 30 000-tick historical window, with a conservative
passive-fill model (we can take at most 25 % of the displayed opposite-side size per
tick, only when our order is strictly inside the spread):

```
==== Round 3 backtest ====
Ticks processed: 30,000
Total PnL:       380,686
```

| Product | PnL | Final position | Fills |
| --- | ---: | ---: | ---: |
| HYDROGEL_PACK | **+388 537** | −31 | 158 543 |
| VEV_5100 | +6 811 | 116 | 532 |
| VEV_5000 | +522 | 48 | 290 |
| VEV_5200 | +453 | −48 | 1 024 |
| VEV_5500 | +411 | 13 | 789 |
| VEV_5400 | +304 | 200 | 200 |
| VEV_5300 | −361 | −36 | 420 |
| VELVETFRUIT_EXTRACT (hedge) | −15 990 | −151 | 2 833 |
| VEV_4000/4500/6000/6500 | 0 | 0 | 0 |
| **Total** | **+380 686** | | |

### Per-day breakdown (`backtest_per_day.py`)

| Day | Total | HP | VE hedge | VEV net |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 128 520 | 130 013 | −1 950 | +457 |
| 1 | 130 308 | 130 978 | −21 | −649 |
| 2 | 128 326 | 129 435 | −7 223 | +6 114 |

Consistency is strong — the strategy yields roughly **$128 k / day** with tight
day-to-day variance.

## 5. Key risks and how the code mitigates them

| Risk | Mitigation |
| --- | --- |
| Stale fair value → adverse fills on HP | Use instant book mid, not EWMA |
| Runaway inventory on HP | Inventory skew of up to 4 ticks pushes quotes against our book |
| Hedge churn on VE | 20-lot dead-band on delta before rebalancing |
| Stuck-at-floor options | Whitelist of tradable strikes (`VEV_TRADED_STRIKES`) |
| Smile calibration stale | Per-strike IV uses a fast EWMA (α = 0.25) so changes track reality |
| Wing option positions during expiry | None taken; deep ITM / OTM strikes excluded |

## 6. Files

- `trader.py` — The `Trader` class. Exposes `decide(...)` for the backtester and
  `run(state)` for the IMC SDK.
- `backtest.py` — Event-driven backtester, aggregate over all 3 days.
- `backtest_per_day.py` — Per-day breakdown to check day-to-day robustness.
- `iv_grid.csv` — Precomputed IV surface (used for calibration).
- `equity_curve.csv` — Time series of mark-to-market equity.
- `charts/01_equity.png` — Equity curve.
- `charts/02_iv_timeseries.png` — IV(t) per strike.
- `charts/03_smile.png` — Cross-strike smile on each day.
- `charts/04_vev5400_anomaly.png` — The persistent 5400 mispricing.
- `charts/05_hp_mid.png` — HP spot time series.
- `charts/06_ve_mid.png` — VE spot time series.
