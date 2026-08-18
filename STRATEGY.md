# TradeQuest AI — Master Strategy & System Document v4.0

**Version:** 4.0 | **Date:** August 2026 | **Status:** Paper Trading — Account Generation 2 (fresh start)
**Supersedes:** v3.2 (archived at `archive/2026-04_2026-08-account-1/STRATEGY-v3.2.md`)

**North star: beat the S&P 500 on a risk-adjusted basis over rolling 12-month periods.** Unchanged
since v1.0. Everything below exists to make that achievable *reliably*, which account generation 1
proved is a different problem from making it achievable at all.

---

> **PART I (§0–§8) is the operating rulebook and is loaded into the agent on every run.** It is
> deliberately terse. Read it completely before making any decision — these are constraints, not
> suggestions.
>
> **PART II (§9+) is the human record** — architecture, go-live gates, history. It is excluded from
> the agent prompt by the `AGENT-CONTEXT-END` marker.

---

## §0 — The Ten Directives (check these FIRST)

Account generation 1 ran Apr–Aug 2026 and finished **11.1 percentage points behind SPY** while its
stock selection was sound — on Jun 22 it was *ahead*. Every point of that loss came from the
execution layer. These ten directives are what that cost bought. Full evidence: `archive/README.md`.

**Check directives 1–3 before any other reasoning in every run.**

1. **If any holding has negative shares, emit `COVER` for the full quantity before any other
   decision.** A cover overrides the quarterly lock, the sector cap, the order budget, the approval
   gate and the cash floor. Restoring an invariant is not a discretionary trade.

2. **Never express closing a short as `SELL`. A cover is a BUY.** JBL sat short −22 shares for four
   months because the agent kept emitting SELL and the execution layer correctly refused it — the
   right instruction had no representable form.

3. **Size against `deployable_cash`, never `cash`.** The broker's cash figure includes short-sale
   proceeds you owe back. Generation 1 reported 79.4% cash ($7,566) when the true deployable figure
   was ~$0 and the book was already 100% long.

4. **A guard that blocks an action must have a matching path that undoes the bad state.** Prevention
   without restoration converts a runaway failure into a frozen one.

5. **If the system may sell in a given month, it must be able to buy in that month.** Asymmetric
   permissions create a ratchet: generation 1 issued 100 SELL decisions against 2 BUY across 90 runs
   and could only shrink.

6. **Never exit on rank alone inside `MIN_HOLD_DAYS`** — unless price is below the 50-day MA, which
   is a real trend break and is never delayed.

7. **Never re-buy a name sold within `REENTRY_COOLDOWN_DAYS`.** Generation 1 completed 8 round trips
   in three weeks; 7 lost money, mean −2.86%.

8. **An advisory step must never block execution.** If enrichment, news or the model itself is
   unavailable, proceed with what you have. A billing failure on a pre-market flag check froze all
   trading for seven days.

9. **Report the denominator with every statistic.** "12.5% win rate" was computed over 8 of 50
   trades. Cite `attributed_trades`, not `total_trades`.

10. **Missing data is a reason not to act, never a reason to act.** A null momentum rank is not a
    sell signal, and a name with null sector/MA/fundamentals is not a buy candidate.

---

## §1 — Objective & Edge

Three independently documented anomalies, stacked:

| Anomaly | Source | Expected edge |
|---|---|---|
| **Momentum premium** | Jegadeesh & Titman (1993) | Top decile outperforms bottom by ~10%/yr |
| **Quality premium** | Novy-Marx (2013) | High-profit firms consistently outperform |
| **Regime-aware allocation** | Faber (2007) | Reducing equity in downtrends cuts drawdown 40–60% |

**Evidence the signal works (generation 1, May–Jun 2026):** positions ranked 1–10 by momentum
averaged **+11.8%**; ranks 11–18 averaged **−4.2%**. Concentrate in the top tier, discard the rest.

### Targets

| Metric | Target | Generation 1 result |
|---|---|---|
| Annual excess return vs SPY | +3% to +7% | **−11.1pp** (execution failure, not selection) |
| Sharpe ratio | > 1.2 | −0.33 |
| Max drawdown | < 15% | 11.96% |
| Annual turnover | 20–35% | ~430 legs/yr equivalent — far over |
| Win rate | 55–65% | 12.5% *(over 8 attributed trades of 50 — not a valid sample)* |

Generation 2 starts from zero. No figure above is inherited.

---

## §2 — Universe

**S&P 500 only** — large-cap, liquid, no micro-caps, no OTC, no ETFs.

- ~500 stocks screened daily from Yahoo Finance price data
- Wikipedia scrape for constituents; 33-ticker fallback on failure
- Dual-class dedup via `ISSUER_MAP` — keeps the highest-momentum ticker per issuer
- Known limitation: S&P membership screens out failures (survivorship bias)

---

## §3 — The Four Filters (applied in order)

**Filter 1 — Momentum (primary).** 6-month and 12-month total return must each rank in the **top 30%**
of the universe. Combined score = average of both percentile ranks. **Skip-month rule:** use price
from 21 trading days ago as the numerator, avoiding the 1-month reversal effect.

**Filter 2 — Quality.** EPS growth > **10%**, revenue growth > **8%**. `None` → **fails** the filter.
Removes junk momentum — high-beta names with no earnings support crash hardest.

**Filter 3 — Valuation guard rail.** Forward P/E < **40**, *or* within the top 70% cheapest by sector.
`None` → passes (relaxed fallback).

**Filter 4 — Risk.** 30-day annualised volatility below the **90th percentile** of the universe.

**Trend gate (entry only).** New entries must be **above their 50-day MA** at purchase — never buy a
stock that would immediately trigger the trend-break sell rule.

**Data-integrity gate (Directive 10).** A candidate with `sector == "Unknown"`, `momentum_rank == 0`,
or a null 50-day MA is **not eligible for entry**. Generation 1 held ANET at 10.5% of book, and APH
and SPG to the very end, all bought without the data the filters require.

---

## §4 — Portfolio Construction

| Parameter | Value | Why |
|---|---|---|
| Target positions | **10–12** | The premium concentrates in the top decile; ranks 11–18 dragged −4.2% |
| Weighting | Equal weight, **dollar-based** | Price-driven sizing gave `corr(price, weight) = +0.68`, costing ~1.5pp |
| Position cap | **10%** (`MAX_POSITION_PCT`) | Hard-capped in code, not advisory |
| Hard concentration cap | **20%** → trim | TSLA reached 39% of book in generation 1 |
| Sector cap | **30%** per GICS sector, **enforced by trimming** | A cap that only warned let Financials reach 49% |
| Rebalance | Quarterly (Jan/Apr/Jul/Oct), **completed in one run** | A throttled rebalance is a quarter-long cash drag |
| Cash — bull / sideways / bear | 5% / 25% / 50% | Regime-driven |

---

## §5 — Position Lifecycle

Everything governing when a position may change lives here: entry, the long-only invariant, exit
rules, and the dampers that stop the exit rules firing too often.

### 5.1 — Long-Only Invariant (non-negotiable)

**The system is long-only. It never sells more than it holds, never opens a short, and when a short
does exist it closes it automatically.**

*Prevention:* every SELL is clamped at the execution layer to the quantity held; a flat or short
position is never sold; non-positive prices are rejected. This applies to the manual-order hatch too.

*Restoration:* any holding with negative shares generates a **COVER** (buy-to-close the full short) on
the next run — from `update.py` **and** from the sentinel, so a short closes even if the agent and
market-open pipelines are both down. COVER bypasses every gate (Directive 1) and is exempt from the
`day_start` flag-only rule and the non-quarterly `BUY → HOLD` rewrite.

*Detection:* `assert_invariants()` is the single chokepoint every order path passes through. Any
breach sets `long_only_breach: true` and starts a streak. **A breach unresolved for
`MAX_BREACH_RUNS` runs halts discretionary trading** — covers still proceed, because covering is what
clears it. Generation 1's failure was not that a breach occurred; it was that it persisted,
unnoticed, for four months. Persistence is therefore itself the alarm.

### 5.2 — Exit Rules

**Core principle: sell losing positions fast, hold winning positions long.** Losses exit immediately
(tax-loss harvest); gains defer toward 12-month LTCG treatment. Every premature winner exit is a
permanent, unrecoverable tax cost.

Structural rules — apply to **all** positions regardless of P&L:

| Rule | Trigger | Action |
|---|---|---|
| **A — Trend break** | Price < 50-day MA, **3 consecutive days** | SELL Tier 1 |
| **B — Quality failure** | EPS growth negative, **2 consecutive quarters** | SELL Tier 1 |
| **C — Hard cap** | Weight > **20%** | Flag TRIM to 15% at next quarterly |
| **D — Parabolic** | Up > **60% in < 60 days** | SELL **half**, hold the rest |

**Rule E — momentum decay (asymmetric by P&L):**

| Position status | Rank outside top 30%, confirmed ≥5 days | Action |
|---|---|---|
| At a loss | Confirmed | **SELL Tier 1**, `next_open` — harvest |
| At a gain, held < 12 months | Confirmed | **WATCH only**, `next_rebalance` — hold gate |
| At a gain, held ≥ 12 months | Confirmed | SELL eligible at next quarterly (LTCG) |

**Tier classification (required on every SELL):**

```
Tier 1 -> urgency next_open       loss position hitting ANY rule
Tier 2 -> urgency next_rebalance  gain position failing ONLY Rule E; never in a non-quarterly month
Tier 3 -> HOLD                    gain position passing all structural rules
```

State unrealized P&L and entry date in every SELL or WATCH. **Never sell a profitable position on
momentum decay alone.** Never sell a winner merely to restore equal weight — tax drag is permanent,
unequal weights are acceptable.

### 5.3 — Churn Dampers

The screen re-ranks **daily**; the strategy rebalances **quarterly**. Recomputing exits from a fresh
top-N every run churns names oscillating around the boundary. These apply to **rank-based exits
only** — structural Rules A–E and sentinel exits are unaffected.

| Damper | Constant | Behaviour |
|---|---|---|
| Rank hysteresis | `EXIT_RANK_MULTIPLE = 1.5` | Enter at rank ≤ `TARGET_N`; exit only at rank > 15 |
| Minimum hold | `MIN_HOLD_DAYS = 10` | Rank-based exit needs a ≥10-day-old position — **waived** below the 50-day MA |
| Re-entry cooldown | `REENTRY_COOLDOWN_DAYS = 10` | A name sold this recently cannot be re-bought |

*Origin:* FFIV bought 2026-07-28 at $412.75, sold 07-30 at $389.88, re-bought 07-31 at $401.62. CSX
and WST did the same. See `archive/2026-04_2026-08-account-1/evidence/round_trips.md`.

---

## §6 — Market Regime

Three signals, **2-of-3 must agree** before switching (prevents whipsaw):

| Signal | Bull | Sideways | Bear |
|---|---|---|---|
| SPY vs 200-day MA | Above | Within 3% | Below |
| 30-day realized vol | < 20% | 20–28% | > 28% |
| Breadth (% S&P above 200-MA) | > 60% | 40–60% | < 40% |

SPY above its 200-day MA with breadth < 40% is a **narrow late-cycle bull** — hold more cash than the
headline regime suggests. Regime → cash: **bull 5% · sideways 25% · bear 50%**, rebalanced within 3
trading days of a change.

---

## §7 — Agent Decision Framework

| Run | When | Trades allowed |
|---|---|---|
| `day_start` | 9:30 AM ET | **None** — flag only. COVER is the sole exception. |
| `day_end` | 5:30 PM ET | Tier 1 SELLs (all months); full rotation on quarterly months |
| `monthly` | 1st of month | Full rebalance on quarterly months; flag-only otherwise |

**Decision order for each holding:**

```
0. shares < 0                                     -> COVER (before everything else)
1. price < ma_50d for >=3 days                    -> SELL Tier 1   [Rule A]
   eps_growth < 0 for 2 quarters                  -> SELL Tier 1   [Rule B]
   weight > 20%                                   -> flag TRIM     [Rule C]
   up >60% in <60d                                -> SELL half     [Rule D]
2. rank outside top 30%, confirmed >=5 days:
     pnl < 0  -> SELL Tier 1 (next_open)
     pnl >= 0 -> WATCH only (next_rebalance)
3. otherwise                                      -> HOLD
4. before issuing any SELL: assign sell_tier; tier2 only in quarterly months
5. before issuing any BUY: check the data-integrity gate and the re-entry cooldown
```

**Decision schema.** Every entry in `agent_log.json → runs[].decisions[]`:

```json
{
  "action": "HOLD | SELL | BUY | WATCH | COVER",
  "symbol": "TICKER",
  "reason": "specific rule — include unrealized PnL and entry date for SELL/WATCH",
  "rule_triggered": "momentum_decay | trend_break | quality_drop | profit_take | new_entry | long_only_breach | null",
  "sell_tier": "tier1 | tier2 | null",
  "urgency": "next_open | next_rebalance"
}
```

**Earnings:** within 3 days → WATCH and note the date; within 1 day → WATCH `next_open`. Never sell
solely for upcoming earnings; if already selling for another reason, prefer executing before it.

**Persistent flags:** a Tier 1 SELL unexecuted for 3+ runs → re-issue `next_open` with the count in
the reason. A SELL flag on a position at a gain → escalate to WATCH, never force `next_open`. Never
BUY a symbol with an active SELL flag.

---

## §8 — Execution & Hard Limits

| Workflow | Trigger | Action |
|---|---|---|
| `market-open.yml` | 9:30 AM ET | Execute `next_open` orders. The agent step is `continue-on-error` — advisory only (Directive 8). |
| `agent.yml` | 5:30 PM ET | Sync → enrich → decide → commit. Commits run `if: always()`. |
| `sentinel.yml` | 2:30 PM ET | Hard-rule sells **and covers**, no LLM — the path that survives an API outage |
| `news-sentiment.yml` | 9:30 AM ET Mon/Thu | Haiku sentiment |

**Non-quarterly month lock.** New-entrant BUYs blocked; Tier 2 SELLs blocked; Tier 1 SELLs allowed;
**COVER always allowed**; **redeployment top-ups into existing top-N holdings allowed** once
deployable cash exceeds `CASH_FLOOR_PCT + CASH_DEPLOY_BAND` (8%). That carve-out is Directive 5 — the
blanket block is what made the book able only to shrink.

**Inception deployment.** An **empty** book with deployable cash runs its first deployment
immediately, in any month, using the full rebalance budget. The quarterly lock exists to stop
discretionary mid-quarter bets; it was never meant to stop the *first* one. Without this, an account
bootstrapped on 18 Aug holds 100% cash until 1 Oct — six weeks of exactly the cash drag that cost
generation 1 its quarter, while §6 targets 95% equity in a bull regime. Deliberately narrow: it
requires **zero** long positions, so a partially-held book still waits for the quarterly and this
cannot become a general mid-quarter buying loophole.

**Hard limits (code-enforced in `update.py`; not overridable by the agent or any config):**

```python
TARGET_N               = 10     # target positions
MAX_ORDERS_PER_RUN     = 5      # daily runs only
MAX_SELL_VALUE_PCT     = 0.30   # daily runs only
CASH_FLOOR_PCT         = 0.05
MAX_POSITION_PCT       = 0.10
MAX_SECTOR_PCT         = 0.30   # ENFORCED by trimming, not advisory
QUARTERLY_MONTHS       = {1, 4, 7, 10}
REBALANCE_MAX_ORDERS   = 24     # a full rotation completes in ONE run
REBALANCE_MAX_SELL_PCT = 1.00
CASH_DEPLOY_BAND       = 0.03
EXIT_RANK_MULTIPLE     = 1.5
MIN_HOLD_DAYS          = 10
REENTRY_COOLDOWN_DAYS  = 10
MAX_BREACH_RUNS        = 3      # breach persistence before trading halts
```

These values are asserted against this table by `tests/test_v40_reset.py`. Every prior version of
this document drifted from the code; that test is what stops v4.0 doing it again.

**Cash is `deployable_cash`, never `account.cash`** (Directive 3): `cash − Σ|market_value of shorts|`.

**Shadow mode.** `DRY_RUN=true` runs the full pipeline — screen, decide, gate, build orders, check
invariants — and records what *would* be submitted without sending it. Every order type passes through
a single `submit()` chokepoint, so no branch can bypass it. Generation 2 begins in shadow mode and
trades only once the promotion criteria in §12 are met.

<!-- AGENT-CONTEXT-END -->

---

# PART II — The Human Record

*Everything below is excluded from the agent prompt.*

## §9 — Prior Account (Generation 1)

Apr 24 – Aug 17, 2026. Ended **$9,534 vs SPY $10,724 (−11.1pp)**, holding a −22 share naked short in
a long-only strategy, with the pipeline dead and unnoticed for seven days.

The complete record — final book, 90 agent runs, all 13 post-mortem findings, the round-trip evidence
and the four-phase timeline — is preserved at **[`archive/2026-04_2026-08-account-1/`](archive/2026-04_2026-08-account-1/)**.
Start at [`archive/README.md`](archive/README.md).

The distilled output of that account is **§0 — The Ten Directives**. That section is not commentary;
it is the deliverable. Each directive maps to a documented incident and to code that enforces it.

Generation 2 begins with an empty book on a new broker account, seeded by
`RESET_PORTFOLIO=true` from the account's real equity. No P&L, equity curve or benchmark figure
carries over — `initial_capital` is sticky and would otherwise anchor every future statistic to a
corrupted history.

## §10 — Architecture

| Layer | Technology |
|---|---|
| Compute | GitHub Actions (ubuntu-latest, Python 3.12) |
| Hosting | GitHub Pages (static PWA) |
| AI decisions | Claude Sonnet 4.6 (prompt-cached, Part I only) |
| News sentiment | Claude Haiku 4.5 |
| Paper trading | Alpaca Markets paper API |
| Market data | Yahoo Finance (`yfinance`) |
| Enrichment | Financial Modeling Prep (free tier) |
| Breadth | TraderMonty public CSV |
| Data store | Git repository (JSON — immutable audit trail) |
| Secrets | GitHub Actions Secrets |

**Bot modules:** `bot/update.py` (screen, sync, order execution, invariants), `bot/agent.py` (Claude
decisions), `bot/sentinel.py` (rule-based sells, no LLM), `bot/enrich.py`, `bot/news_sentiment.py`,
`bot/rebalance_trueup.py` (manual whole-book remediation).

**Data files:** `data/portfolio.json` (holdings, trades, curves, summary), `data/agent_log.json`
(rolling 90 runs), `data/execution_summary.json` (last execution + breach streak + halt state),
`data/enrichment.json`, `data/news.json`, `data/sentinel_orders.json`, `data/bars/{SYM}.json`.

**Security:** `verify_paper_url()` aborts against any non-paper endpoint; risk limits are hard-coded
constants; `_safe()` strips control characters from all external text before it reaches the prompt;
`sanitize()` guards every `innerHTML` assignment; `run_type` from `workflow_dispatch` is allowlisted.

**Environment flags:** `DRY_RUN` (shadow mode), `RESET_PORTFOLIO` (one-time bootstrap),
`MARKET_OPEN_RUN`, `SENTINEL_RUN`, plus the Alpaca/Anthropic/FMP secrets.

## §11 — Fresh-Start Runbook (Generation 2)

1. Create the new Alpaca **paper** account; fund it. Do not place any manual trades.
2. Update GitHub Secrets: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_ACCOUNT_NAME`. Confirm
   `ALPACA_BASE_URL` still contains `paper`.
3. Ensure the Anthropic credit balance is funded — the agent degrades gracefully now, but a funded
   key is what makes decisions happen at all.
4. Set repository variable **`DRY_RUN = true`**.
5. Run `agent.yml` via `workflow_dispatch` with **`reset_portfolio: true`** — once only. It refuses to
   run if the account holds any position or has any order history.
6. Verify `data/portfolio.json` shows the new `initial_capital`, `inception_date`,
   `account_generation: 2`, and empty holdings/trades/curves.
7. Observe ~5 sessions against the §12 promotion criteria.
8. Delete the `DRY_RUN` variable to begin trading.

## §12 — Go-Live Gates & Open Risks

**Shadow-mode promotion (before generation 2 trades at all):**

- Every run completes green; no workflow failures
- `long_only_breach: false` on every run
- Order plans sane — no oversized positions, no same-name churn, sector cap respected
- `deployable_cash` matches the account's real buying power

**Real-capital gates (all must pass; none are met today):**

| Gate | Bar |
|---|---|
| Backtest | 10-year vectorbt/quantstats run completed; excess return and max drawdown within §1 targets |
| Clean quarters | 2 consecutive quarterly rebalances with zero invariant breaches and zero deadlocks |
| Invariant record | 90 consecutive days with `long_only_breach: false` |
| Instrumentation | `attributed_trades` ≥ 90% of `total_trades`; Sharpe and win rate on a real sample |
| Turnover | Realized annual turnover within the 20–35% target |
| Alerting | A deliberately failed workflow produces a notification within one cycle |

**Open risks:**

| ID | Risk | Status |
|---|---|---|
| **F9** | Prompt cache never hits — `update.py` exceeds the 5-min ephemeral TTL | Open |
| **F10** | No backtest. All evidence is ~6 weeks of bull-market paper trading | Open — highest value |
| — | Earnings gap risk: momentum names can gap ±10–20% overnight | Mitigated, not eliminated |
| — | Survivorship bias: S&P membership excludes failures | Structural |
| — | yfinance EPS/PE frequently stale or null | Mitigated by the data-integrity gate |
| — | Transaction costs ignored in paper trading (~0.5–1.5%/yr real drag) | Known |
| — | Momentum crashes hard roughly once a decade (2009, 2020) | Reduced by regime cash, not removed |

## §13 — Evolution Log

| Version | Key changes |
|---|---|
| **v1.0** | Momentum + quality filters, monthly rebalance, basic Alpaca integration |
| **v2.0** | Volatility filter, regime detection, quality-deterioration sell rule, Claude agent |
| **v2.1** | Quarterly rebalance, stricter sell thresholds, 3-day MA confirmation, tax-loss priority |
| **v2.2** | 12-month hold gate, Tier 1/2 pipeline, code-level non-quarterly lock, `TARGET_N` 17→10 |
| **v3.0** | Consolidated PRD/HLD/LLD/STRATEGY into one document |
| **v3.1** | Execution correctness: long-only clamp, one-run rebalance, enforced sector cap, dollar sizing, P&L tracking, agent spec conformance in code |
| **v3.2** | COVER path (v3.1's clamp had made the short unclosable), deployable cash, redeployment carve-out, churn dampers, LLM/execution decoupling, failure alerting |
| **v4.0** | **This document.** Account generation 2. Restructured around §0 Ten Directives and a unified §5 Position Lifecycle; Part I/II split enforced by `AGENT-CONTEXT-END`; `assert_invariants()` chokepoint + breach-streak kill switch; `DRY_RUN` shadow mode; `RESET_PORTFOLIO` bootstrap; data-integrity entry gate; generation 1 archived as an executable regression fixture |
