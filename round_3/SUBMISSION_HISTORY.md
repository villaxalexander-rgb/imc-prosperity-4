# Round 3 Submission History

Each submission is a separate run on the IMC judge's 1000-tick preview window
(day 2 of the historical sample). Live final round runs the full 10,000-tick
day; observed PnL multiplies roughly 10× on a full run.

## Timeline

| # | ID | PnL | Δ vs prev | What changed |
|---:|---:|---:|---:|---|
| 1 | 376585 | **−$3,328** | — | Initial submission. Wrong VE position limit (had 400 in code, real 200) ⇒ 994/1000 ticks rejected. Fair-IV table was historical mean ⇒ all VEVs maxed long when live vol shifted. |
| 2 | 377046 | **+$262** | +$3,590 | Fixed VE limit. Switched to vol-level-agnostic smile residual signal. Removed hardcoded 5400 buy bias. Added position-gate for hedge capacity. |
| 3 | 377486 | **+$456** | +$194 | Added structural RV basket. Discovered persistent zig-zag mispricing: long 5100/5400 (cheap), short 5300/5500 (rich). Modest sizes (5100 +60, 5400 +100, 5300 −60, 5500 −60). |
| 4 | 378625 | **+$665** | +$209 | Bigger basket (5300 to −120). Two-layer passive top-ups inside spread. |
| 5 | 387045 | **+$595** | −$70 | Tried two-layer HP at mid±1 (got adverse-selected, lost $75 on HP). Added VEV_5200 short (lost $158 — bad day for it). Net regression. |
| 6 | 387586 | **+$1,038** | +$443 | First crossing of $1k. Reverted HP to single layer mid±2. Dropped 5200. Added pure MM on wide-spread strikes 5000/5100. **Surprise $503 on VE** from delta-hedge passive orders. |
| 7 | 387864 | **+$1,043** | +$5 | Stable — same configuration. |
| 8 | 387979 | **+$976** | −$67 | Regression caused by **66 "exceeded VE limit of 200" errors** — two MM blocks competed for the same budget. |
| 9 | (in progress) | target $20k | | Single global VE budget tracker. Position limits corrected (HP=200, VEV=300 from Round 3 brief — was 50/200 in earlier code). HP+VEV sizes scaled. |

## What works (proven across submissions)

1. **HP MM at mid±2** — consistently prints **~$288/run** regardless of basket
   tweaks. Counterparty volume per tick is the bottleneck.
2. **VE delta-hedge passive orders** — accidentally captured $503 in sub 6.
   Promoting this to explicit always-on VE MM is the biggest unlock.
3. **VEV_5300 short** — scales linearly with target size:
   - −60 → +$72
   - −120 → +$256
   - −160 → +$433
   - −180 → +$135 (varied with day-2 noise)
   - −270 → +$156 (sub 8, with limit-error losses)
4. **VEV_5500 short** at −60 to −90: small but reliably positive.
5. **VEV_5100 long**: modest passive fills, +$77 to +$135.

## What hurts (don't repeat)

1. **Aggressive 5400 lifts**. Spread is 1-2 ticks; lifting offers pays full
   spread that erodes the structural-cheap edge. **EXEC_FAIR_BLEND[5400] = 0.0**.
2. **Tight HP MM at mid±1**. Adversely selected by 1-tick noise traders.
3. **Smaller-than-200 VEV limits**. Hardcoded 200 in early code → gave up 1.5×
   capacity on the basket.
4. **Multiple VE MM blocks without shared budget**. Caused 66 "exceeded limit"
   errors in sub 8 — orders dropped, fills lost.
5. **Hedge cooldown stored in raw timestamps**. 1200 ts = 12 ticks (not 1200).
   Real cooldown should be **50,000 ts = 500 ticks**.
6. **Scaling 5500 short to −120**. Lost $63 — its low option price (~$6) means
   each tick of MTM noise is a big % of notional. Cap at −60 to −100.

## Path to $20k

The 1000-tick preview window caps PnL at roughly **$1.5–3k** even with perfect
execution. A full 10,000-tick day naturally scales to **$15–30k** for the same
strategy. Optimization beyond v12 is about:

1. Eliminating remaining order-rejection waste
2. Capturing more of the VE 5-tick spread (it's now the biggest single alpha)
3. Trying a small mid±1 inner HP layer guarded by inventory brake
4. Per-strike pure MM on 5200 (spread 3, currently uncovered)
