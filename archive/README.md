# TradeQuest AI — Archive

Retired accounts, preserved whole. Each folder is a complete, self-contained record of one
generation of the system: the data it produced, the decisions it made, the failures it hit, and
the rules those failures produced.

**Nothing here is cleaned up.** The books are archived in their broken final state on purpose —
they are regression fixtures, not trophies. `2026-04_2026-08-account-1/data/portfolio.json` still
shows JBL at −22 shares. If a future refactor reopens that deadlock, replaying current code against
this file is what catches it.

---

## Generations

| Folder | Account | Period | Outcome |
|---|---|---|---|
| [`2026-04_2026-08-account-1/`](2026-04_2026-08-account-1/) | Alpaca paper #1 | Apr 24 – Aug 17, 2026 | **Retired.** −11.1pp vs SPY; unclosable short; 7-day silent outage |

---

## Generation 1 — what happened

The system ran for four months, targeting +3–7% annual excess return vs the S&P 500. It ended
**$9,534 against SPY's $10,724 — 11.1 percentage points behind** — carrying a −22 share naked short
in a long-only strategy, with the entire pipeline dead and unnoticed for a week.

**Stock selection was never the problem.** On Jun 22 the book was *ahead* of SPY by $388, and
positions ranked 1–10 by momentum averaged +11.8% while ranks 11–18 averaged −4.2%. Every dollar of
underperformance was manufactured by the execution layer after the July 1 rebalance.

Read [`evidence/timeline.md`](2026-04_2026-08-account-1/evidence/timeline.md) for the four-phase arc,
then [`POSTMORTEM.md`](2026-04_2026-08-account-1/POSTMORTEM.md) for all 13 findings with code
evidence.

## The ten lessons

These are the durable output of generation 1. They live on in `STRATEGY.md` §0 as operational
directives the agent reads on every run — this list is the reasoning behind them.

1. **Execution correctness dominates signal quality.** One missing `min(requested, held)` erased a
   good quarter. A long-only bot that can accidentally short has no business trading capital.
2. **Every guard needs a restoration path.** v3.1 stopped the short growing but left no way to close
   it — prevention without restoration converts a runaway failure into a frozen one.
3. **A limit that only warns is not a control.** The sector cap printed a warning while Financials
   reached 49% of the book.
4. **Never let an advisory component gate a critical one.** An LLM billing failure on a pre-market
   *flag check* stopped order execution for seven days.
5. **Trust no broker field you have not reconciled.** `account.cash` included short-sale proceeds:
   79.4% reported, ~$0 real, net exposure 20.6%.
6. **Asymmetric permissions create ratchets.** Sells allowed and buys blocked meant the book could
   only shrink — 100 SELL decisions against 2 BUY across 90 runs.
7. **Measurement cadence must match decision cadence.** A daily re-screen driving a quarterly
   strategy produced 8 round trips, 7 of them losses, mean −2.86%.
8. **A statistic without its denominator is not a statistic.** "12.5% win rate" was computed over 8
   of 50 trades.
9. **Silence is not success.** No alerting meant nobody knew the system was dead.
10. **Missing data is a reason not to act, never a reason to act.** Positions were bought with null
    sector, null MA and rank 0 — and a null rank then read as a sell signal.

## What proves each claim

| Claim | File |
|---|---|
| −11.1pp vs SPY; final book; JBL at −22 shares | [`data/portfolio.json`](2026-04_2026-08-account-1/data/portfolio.json) → `summary`, `holdings`, `equity_curve`, `spy_curve` |
| Four-phase arc with the gap at each turn | [`evidence/timeline.md`](2026-04_2026-08-account-1/evidence/timeline.md) |
| 8 round trips, 7 losses, mean −2.86% | [`evidence/round_trips.md`](2026-04_2026-08-account-1/evidence/round_trips.md) |
| 100 SELL vs 2 BUY; "BUY-TO-COVER MANDATORY" flagged every run from Aug 7 | [`data/agent_log.json`](2026-04_2026-08-account-1/data/agent_log.json) → 90 runs |
| All 13 findings with code-level mechanism | [`POSTMORTEM.md`](2026-04_2026-08-account-1/POSTMORTEM.md) |
| The doctrine as it stood when the account was retired | [`STRATEGY-v3.2.md`](2026-04_2026-08-account-1/STRATEGY-v3.2.md) |

## How to reload the lessons

The archive is executable, not decorative.

```bash
python -m pytest tests/test_v40_reset.py -k archive -q
```

That replays the archived book through current code and asserts it still produces `COVER JBL 22`
and $0 deployable cash. If either assertion fails, a regression has reopened a fixed failure.

To review the doctrine as it evolved, diff the superseded document against the live one:

```bash
git diff --no-index archive/2026-04_2026-08-account-1/STRATEGY-v3.2.md STRATEGY.md
```
