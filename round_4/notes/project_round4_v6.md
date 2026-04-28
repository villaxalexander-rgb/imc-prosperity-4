---
name: Round 4 v6 strategy summary
description: Current best Round 4 strategy is TraderBSV.py v6 (+$64,364), a mean-reversion alpha on VEV/HYDROGEL_PACK underlyings expressed via vouchers
type: project
originSessionId: 2170a43c-31aa-45ce-ad86-11c62fabcfe5
---
Round 4 (Black-Scholes Voucher) best submission is **v6: +$64,364**.

Core idea: **mean reversion of the underlying to a hardcoded reference**,
expressed through high-delta vouchers to stack per-product position limits.

v6 parameters (VEV leg):
- EMA half-life 25, REFERENCE_S = 5250, THR = 7 (entry), EXIT = 1
- Underlying VEV size 200; vouchers K ∈ {5000…5500}, size 300 each
- Sign of (ema_S − REFERENCE_S) drives long/short on all 7 instruments

HYDROGEL_PACK leg is independent: REFERENCE_H = 10000, THR = 15, EXIT = 3,
size 200. It is uncorrelated with VEV and gets its own signal.

Submission ladder (for context when comparing to future tweaks):
- v1 −$35,328 (EMA anchored at first observed price — broken on 1k-tick test)
- v2 +$33,631 (hardcoded references introduced)
- v3 +$61,764 (voucher size 200→300, K=5400 added, HL=25, THR=8, EXIT=2)
- v4 +$49,718 (HL=200 — failed on 1k-tick test, reverted)
- v5 +$62,612 (kept HL=25, added K=5500 + HYD_EXIT=3)
- v6 +$64,364 (THR 8→7, EXIT 2→1)

**Why:** alpha source is variance-ratio < 1 on the underlying (per-tick
realized log-vol 0.000217 vs joint-smile σ 0.000128, ratio 0.59 → variance
ratio 0.36 at 1000 ticks). Option chain itself is fairly priced; the edge
is the underlying's mean reversion, captured at leverage through the
vouchers' stacked position limits.

**How to apply:** when the user proposes a v7 or asks for tuning ideas,
respect the calibration constants (REFERENCE_S=5250, REFERENCE_H=10000,
σ=0.000128) and the lesson from v4 — optimize for the 1k-tick regime, not
just the 10k-tick final, since the EMA must be informative from tick 0.

**Repo location:** `round_4/trader/TraderBSV.py` (302 lines).
Data: `round_4/research/data/{prices,trades}_round_4_day_{1,2,3}.csv`.
Note: `research/scripts/`, `research/results/`, `research/experiments/`,
and `backtest_logs/` are described in the round_4 README but were not
present at import time — verify before referencing them.
