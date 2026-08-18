# Evidence — Round-Trip Churn (Jul 16 – Aug 7, 2026)

Source: `../data/portfolio.json` → `trades[]` (the retained last-50 window).

A *round trip* is a symbol bought and then sold inside the window. The strategy targets
20–35% annual turnover; this is what a daily re-screen driving a quarterly strategy
actually produced.

**8 completed round trips. 7 were losses. Mean -2.86% per trip.**

| Symbol | Bought | Sold | Held | Result |
|---|---|---|---|---|
| **JBHT** | 2026-07-16 @ $293.18 | 2026-07-29 @ $271.02 | ~13d | **-7.56%** |
| **FFIV** | 2026-07-28 @ $412.75 | 2026-07-30 @ $389.88 | ~2d | **-5.54%** |
| **GWW** | 2026-07-20 @ $1,386.33 | 2026-07-22 @ $1,348.22 | ~2d | **-2.75%** |
| **CSX** | 2026-07-24 @ $53.16 | 2026-07-28 @ $51.75 | ~4d | **-2.65%** |
| **HUM** | 2026-07-30 @ $371.32 | 2026-08-05 @ $361.82 | ~5d | **-2.56%** |
| **LUV** | 2026-07-28 @ $45.81 | 2026-07-30 @ $44.93 | ~2d | **-1.92%** |
| **GE** | 2026-07-24 @ $356.73 | 2026-07-31 @ $356.48 | ~7d | **-0.07%** |
| **NTRS** | 2026-07-22 @ $179.52 | 2026-07-30 @ $179.77 | ~8d | **+0.14%** |

## Sell → re-buy pairs (worse than a round trip — the position was re-entered higher)

| Symbol | Sold | Re-bought | Re-entry cost |
|---|---|---|---|
| **BEN** | 2026-07-24 @ $32.63 | 2026-07-27 @ $33.22 | **$+0.59/sh** |
| **CSX** | 2026-07-28 @ $51.75 | 2026-07-29 @ $50.61 | **$-1.14/sh** |
| **FFIV** | 2026-07-30 @ $389.88 | 2026-07-31 @ $401.62 | **$+11.74/sh** |
| **NTRS** | 2026-07-21 @ $183.97 | 2026-07-22 @ $179.52 | **$-4.45/sh** |
| **WST** | 2026-07-27 @ $326.28 | 2026-07-28 @ $338.14 | **$+11.86/sh** |

## The rule this produced

STRATEGY.md §5 churn dampers — rank hysteresis (`EXIT_RANK_MULTIPLE = 1.5`),
`MIN_HOLD_DAYS = 10` (waived on a 50-day MA break), `REENTRY_COOLDOWN_DAYS = 10`.
Replayed against the final book, NUE (rank 12) and STLD (rank 13) are held where the
old symmetric top-N diff sold both.
