# IMC Prosperity 4 — Algorithmic Trading Bot

This repository contains my trading algorithms for the **IMC Prosperity 4**
competition. It is organized by round.

## Layout

```
imc-prosperity-4/
├── README.md                 ← this file
├── LICENSE
├── .gitignore
└── round_3/                  ← "Options Require Decisions"
    ├── trader.py             ← submitted algorithm (latest version)
    ├── backtest.py           ← event-driven backtester (3 historical days)
    ├── backtest_per_day.py   ← per-day breakdown
    ├── backtest_judge_day2.py← replay against the judge's exact day-2 book
    ├── REPORT.md             ← strategy write-up
    ├── data/                 ← prices_round_3_day_{0,1,2}.csv + trades CSVs
    ├── charts/               ← IV surface, equity curve, smile plots
    └── submission_logs/      ← every judge submission .log/.json/.py for forensics
```

## Round 3 — "Options Require Decisions"

**Products**
| Symbol | Type | Limit | Notes |
|---|---|---:|---|
| `HYDROGEL_PACK` | Spot | 200 | Wide ~16-tick spread, mean-reverting |
| `VELVETFRUIT_EXTRACT` | Spot | 200 | 5-tick spread, used for delta hedge + MM |
| `VEV_K`, K ∈ {4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500} | European call on VE | 300 each | TTE = 7d at start of round 1 |

**Strategy summary**

Three orthogonal alpha engines:

1. **HP market maker** — multi-layer make ladder (mid±2 / mid±4) with inventory
   skew. Captures the wide HP spread.
2. **VE market maker** — symmetric quotes inside the 5-tick VE spread, with skew
   anchored to the option delta-hedge target (so MM fills naturally drive
   inventory toward the right hedge).
3. **VEV vol-arb** — exploits the persistent zig-zag smile structural
   mispricing. Long cheap strikes (5100, 5400), short rich strikes (5200, 5300,
   5500). Pure passive MM on wide-spread strikes (5000, 5100). Vega-aware
   thresholds. Optional polynomial smile fit.

A delta hedge in VE uses a 500-tick cooldown and 100-lot deadband to avoid
spread-cost churn.

**Submission progression** (1000-tick judge preview window)

| # | ID | PnL | Notes |
|---:|---:|---:|---|
| 1 | 376585 | −$3,328 | Wrong VE limit (had 400, real 200) — 994/1000 ticks rejected |
| 2 | 377046 | +$262 | Limit fix, vol-level-agnostic smile |
| 3 | 377486 | +$456 | Added structural RV basket (long 5100/5400, short 5300/5500) |
| 4 | 378625 | +$665 | Bigger basket + passive top-ups |
| 5 | 387045 | +$595 | Added pure MM on wide-spread strikes |
| 6 | 387586 | +$1,038 | First crossing of $1k — VE delta-hedge accidentally captured spread |
| 7 | 387864 | +$1,043 | Stable |
| 8 | 387979 | +$976 | Regression: 66 VE-limit-overrun errors (two MM blocks) |
| 9 | (next) | target $20k | Single global VE budget, position-limit fix |

See `round_3/REPORT.md` for the full strategy write-up.

## Running locally

```bash
cd round_3
python3 backtest.py            # 3-day historical backtest
python3 backtest_per_day.py    # day-by-day breakdown
python3 backtest_judge_day2.py # replay against the live judge's day-2 book
```

Requires Python 3.9+ and `pandas`. The `trader.py` itself is pure-Python (no
numpy/scipy) so it runs inside the IMC Prosperity sandbox unchanged.

## License

MIT — see `LICENSE`.
