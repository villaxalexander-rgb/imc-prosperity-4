# Round 5 Submission History

| Version | Submission ID | Live PnL | Notes |
|---|---|---|---|
| v21 | 551211 | $81,xxx | Original baseline (friend's reference) |
| v22 | 553162 | $85,113.88 | Friend's $85k subset (best single-toggle removals) |
| v27 | — | — | ALL-IN size-up push — mixed (3 winners, 4 losers) |
| v28 | — | — | Winners-only cleanup |
| v30 | — | $85,210 | v22 + TRANSLATOR_SPACE_GRAY MR add |
| v31 | 560265 | **$85,887.88** | v30 + 3 v27-validated MM size-ups (+$678) |
| v32 | 560448 | $85,887.88 | + UV_VISOR_AMBER re-tune (no-op — z never crosses 2.5) |
| v33 | TBD | TBD | + TRANSLATOR_VOID_BLUE MM q=2→3 |

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
- 33 mean-reversion (SOLO) products with rolling z-score signal
- 16 market-making products with passive bid/ask quoting
- LIMIT = 10 per product (IMC R5 spec)
- All signals share state via traderData (JSON encoded, capped at 44k chars)

## Iteration Discipline

Every change is a SINGLE-ATTRIBUTION test on top of the LIVE-confirmed baseline.
Multi-day historical backtests overfit; only LIVE attribution is trusted.

