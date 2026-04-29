# Round 5 Submission History

| Version | Submission ID | Live PnL | Δ vs prev | Notes |
|---|---|---|---|---|
| v21 | 551211 | $81,xxx | — | Original baseline (friend's reference) |
| v22 | 553162 | $85,113.88 | +$4k | Friend's $85k subset (best single-toggle removals) |
| v30 | — | $85,210 | +$96 | v22 + TRANSLATOR_SPACE_GRAY MR add |
| **v31** | **560265** | **$85,887.88** | **+$678** | **HIGH-WATER MARK** — v30 + 3 v27-validated MM size-ups |
| v32 | 560448 | $85,887.88 | $0 | + UV_VISOR_AMBER re-tune (no-op — z never fires) |
| v33 | 560660 | $85,841.07 | −$47 | + TRANSLATOR_VOID_BLUE q=2→3 (37% per-unit penalty; reverted) |
| v34 | TBD | TBD | TBD | **Architectural pivot**: concurrent MM on 5 idle SOLO products |

## High-water mark: v31 ($85,887.88)

Saved as `trader_v31_85888_REFERENCE.py` for quick rollback if v34+ regress.

Stack:
1. friend's $85,113 baseline subset (33 SOLO + 16 MM products)
2. + TRANSLATOR_SPACE_GRAY as 34th SOLO (+$96)
3. + 3 MM size-ups (TRANSLATOR_ECLIPSE_CHARCOAL q=3→5, PANEL_4X4 q=3→5, TRANSLATOR_GRAPHITE_MIST q=3→5) for +$678

## v34 — concurrent MM architecture (toward $150k+)

Added `MM_ON_SOLO_IDLE` dict for 5 SOLO products (PEBBLES_S, PEBBLES_M, TRANSLATOR_SPACE_GRAY, MICROCHIP_CIRCLE, ROBOT_DISHES) that earn extra spread during their MR-idle ticks. Gating: skip if SOLO produced any orders OR |position| > q.

Expected uplift: +$2-8k → ~$88-94k.
Path to $150k: stack v35 (pairs trading) + v36 (microprice MM skew) + v37+.

## Per-product PnL @ v32 LIVE (top 10)

| Product | PnL |
|---|---|
| PEBBLES_XL | $7,010 |
| SLEEP_POD_COTTON | $6,004 |
| PANEL_4X4 | $5,806 |
| TRANSLATOR_GRAPHITE_MIST | $4,761 |
| OXYGEN_SHAKE_GARLIC | $4,570 |
| TRANSLATOR_ECLIPSE_CHARCOAL | $4,215 |
| UV_VISOR_ORANGE | $4,124 |
| PEBBLES_L | $3,940 |
| PANEL_2X4 | $3,616 |
| MICROCHIP_TRIANGLE | $3,036 |

## Architecture

- 50 products total (5 × 10 themes)
- 33-34 mean-reversion (SOLO) products with rolling z-score signal
- 16 market-making products with passive bid/ask quoting
- v34: 5 NEW concurrent-MM-on-idle hooks (gated by SOLO activity)
- LIMIT = 10 per product (IMC R5 spec)
- All signals share state via traderData (JSON encoded, capped at 44k chars)

## Iteration discipline

Every change is a SINGLE-ATTRIBUTION test on top of the LIVE-confirmed baseline.
Multi-day historical backtests overfit; only LIVE attribution is trusted.

