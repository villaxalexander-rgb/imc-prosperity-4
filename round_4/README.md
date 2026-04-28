# IMC Prosperity 4 — Round 4 Voucher Alpha Bundle

This is the complete artifact set for the Round 4 (Black-Scholes Voucher)
strategy. Drop the contents of this folder into your repo at any location.

## Layout

```
trader/
  TraderBSV.py                    # ← THIS is the file to upload to IMC
research/
  scripts/                        # 22 numbered scripts: data loading,
                                  # TTE estimation, smile fitting, MR sweeps,
                                  # parameter tuning, multi-day replays, etc.
                                  # Run order: 00_load.py → 22_v6_test.py
  results/                        # Cached parquet/CSV/JSON artifacts produced
                                  # by the scripts — fast reload without
                                  # recomputing everything
  experiments/                    # Markdown writeups + strategy candidates
    round3_to_round4_transfer.md  # Transfer check (alphas that survived)
    round4_alpha_log.md           # Full experiment log (all 17 alphas tested)
    strategy_candidates.py        # Original strategy skeleton (pre-IMC format)
  data/                           # Source data (prices_round_4_day_{1,2,3}.csv,
                                  # trades_round_4_day_{1,2,3}.csv)
  backtest_logs/                  # All 6 IMC submission logs
    backtest_result/  → v1: -$35,328
    backtest_v2/      → v2: +$33,631
    backtest_v3/      → v3: +$61,764
    backtest_v4/      → v4: +$49,718  (HL=200 experiment, failed)
    backtest_v5/      → v5: +$62,612
    backtest_v6/      → v6: +$64,364  (current best)
```

## Submission History

| Version | IMC PnL    | Key change |
|---------|-----------:|------------|
| v1      | -$35,328   | EMA half-life 2,500 anchored at first observed price (broken in 1k test) |
| v2      | +$33,631   | Hardcoded REFERENCE_S=5248, REFERENCE_H=10000 |
| v3      | +$61,764   | Voucher size 200→300 (max), K=5400 added, REF_S=5250, EMA_HL=25, THR=8, EXIT=2 |
| v4      | +$49,718   | EMA_HL=200 (tried to optimize for 10k-iter final, hurt 1k-iter test) |
| v5      | +$62,612   | Reverted HL=25, kept K=5500 + HYD_EXIT=3 |
| **v6**  | **+$64,364** | THR 8→7, EXIT 2→1 |

Total improvement from v1 baseline: **+$99,692** ($-35k → +$64k).

## How the Strategy Works (v6)

Single core idea: **mean reversion to a hardcoded long-term reference**.

```
ema_S = EMA(VEV_mid, half_life=25)
dev_S = ema_S - REFERENCE_S          (REFERENCE_S = 5250)

if dev_S < -7  : LONG  VEV(200)  + LONG  vouchers K∈{5000-5500}(300 each)
if dev_S > +7  : SHORT VEV(200)  + SHORT vouchers K∈{5000-5500}(300 each)
if |dev_S| < 1 : flatten all positions
```

HYDROGEL_PACK is independent (uncorrelated with VEV), gets its own signal:
- ema_H, REFERENCE_H=10000, THR=15, EXIT=3, size=200.

## Key Calibration Numbers (from R4 training data)

- VEV underlying mean across all 3 training days: **5247.6**
- HYDROGEL_PACK mean: **9994.7**
- Per-tick implied volatility (joint smile fit): **σ = 0.000128**
- Per-tick realized log-vol: **0.000217**  → ratio 0.59 implies **strong mean
  reversion in the underlying** (variance ratio at 1000 ticks = 0.36).
  This is the source of the alpha.

## Why This Beats Naive Approaches

1. **Initial-anchor bug avoidance** — v1's EMA was initialized at the first
   observed price. In a 1k-iteration test, the EMA never converges to the
   true mean before the day ends. By hardcoding the reference, the deviation
   signal is meaningful from tick 0.

2. **Variance-ratio is the hidden lever** — most R3-style "BS fair value"
   approaches don't profit because the option chain is fairly priced. The
   alpha is in the underlying's mean-reverting nature, captured *through*
   the high-delta vouchers (which give 1.5× leverage on the same signal).

3. **Voucher position limits stack** — each of the 6 vouchers carries an
   independent 300-position limit. Trading them all on the same VEV signal
   is leverage, not diversification, but the per-product limits make this
   leverage free.

## Files Worth Reading

- `trader/TraderBSV.py` — production strategy (302 lines, well-commented).
- `research/experiments/round4_alpha_log.md` — every experiment log with verdict.
- `research/experiments/round3_to_round4_transfer.md` — what survived and why.
- `research/scripts/02_tte.py` — TTE re-estimation (yields T0=69500 ticks).
- `research/scripts/14_underlying_mr.py` — discovers the variance-ratio alpha.
- `research/scripts/16_size_test.py` — proves size=300 is the right call.
- `research/scripts/22_v6_test.py` — final parameter sweep.

---

## Status of this checkout (added when imported into the repo)

Only a subset of the bundle described above was available on this machine
at import time — namely:

- `trader/TraderBSV.py` (copied from `~/Desktop/Booth /IMC Prosperity/`)
- `research/data/*.csv` (extracted from `~/Downloads/ROUND_4.zip`)

The following sections referenced in the README are **not present locally**
and need to be added separately when the source bundle is supplied:

- `research/scripts/` (22 numbered scripts)
- `research/results/` (cached artifacts)
- `research/experiments/` (markdown writeups + `strategy_candidates.py`)
- `backtest_logs/` (v1–v6 submission logs)
