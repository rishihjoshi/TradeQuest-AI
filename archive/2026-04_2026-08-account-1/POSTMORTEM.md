# TradeQuest AI — 90-Day Post-Mortem (Apr 24 – Jul 23, 2026)

**Question asked:** Why are we not beating the S&P 500? What went wrong, and what are the lessons?

**Answer in one line:** The strategy's stock *selection* was fine — in mid-June the book was **ahead**
of SPY. The underperformance was **manufactured by the execution layer** after the July 1 rebalance:
a throttled rebalance parked 40–61% of the book in cash through a rising market, and a broken
sell-loop opened a **naked short in JBL**. Both are execution bugs, now fixed in v3.1.

---

## 1. Headline Result

| | Start (Apr 24) | Peak (Jun 22) | End (Jul 23) |
|---|---|---|---|
| **Portfolio** | $9,864 | **$10,699 (+8.5%)** | $9,963 (**+0.72%**) |
| **SPY (paper benchmark)** | $9,864 | $10,311 (+4.5%) | $10,197 (**+3.38%**) |
| **Excess vs SPY** | 0 | **+$388 ahead** | **−2.66pp behind** |

The portfolio **led SPY by ~$400 on Jun 22**, then gave back **−6.9% from its peak** into Jul 23 —
the entire round-trip happened *after* the July 1 quarterly rebalance. This was not a stock-picking
failure; it was an execution failure.

### Equity vs SPY (selected points)

```
date     portfolio    spy     gap        phase
Apr 24     9,864     9,864     0          inception
May 19     9,717    10,138   -421         over-trading / whipsaw drawdown
Jun 22    10,699    10,311   +388  <-- PEAK, ahead of SPY
Jul 01    10,389    10,330    +59         quarterly rebalance begins
Jul 10    10,525    10,458    +67         rebalance still grinding (5 orders/day)
Jul 23     9,963    10,197   -234  <-- END, 61% cash + JBL short
```

---

## 2. Findings (root causes, with code evidence)

### Finding 1 — CRITICAL: Runaway naked short in JBL (−22 shares, −$7,036, −70% weight)

`data/portfolio.json` holds `JBL: shares -22, market_value -$7,036, weight -0.7062`. A long-only
S&P momentum strategy was carrying a **naked short with unbounded loss risk**.

**Mechanism (confirmed in code):**
- JBL sat below its 50-day MA, so the agent emitted `SELL JBL (trend_break, next_open)` on **20 runs
  across 13 days** (Jul 6–22).
- Order build (`bot/update.py`, rebalance sell loop) appended `(sym, h["shares"])`; execution
  (`alpaca_place_orders`) did `qty = max(1, int(shares))`. Once JBL was flat/short, `h["shares"] ≤ 0`
  and `max(1, int(-22)) = 1` → **it sold one more share every run**, deepening the short daily.
- Nothing enforced "don't sell more than you hold." The idempotency guard only blocked a duplicate
  *same-day* order, not re-selling day after day.
- Two trades even printed at **price $0** (`S:JBL x1 @0`) — a bad price fetch was allowed to trade.

This is the same defect class flagged in the May red-team (no fill verification / ghost position),
now escalated into an actual short.

**Fix (v3.1):** long-only clamp in `alpaca_place_orders` — fetch held qty, `qty = min(requested,
held)`, skip when `held ≤ 0`, reject non-positive prices. Rebalance sell loop drops non-positive
holdings. A short is only ever closed by an explicit buy-to-cover (`bot/rebalance_trueup.py`).

### Finding 2 — CRITICAL: Cash drag (the entire −2.66pp); the 5-order cap throttles rebalances

Cash climbed **21.8% (Jun 1) → 44.1% (Jul 1) → 61.4% (Jul 23)** against a 5% bull-regime target.

`MAX_ORDERS_PER_RUN = 5` but the Jul 1 quarterly rebalance needed ~15 legs (sell ~6 stale names +
buy ~10 new top-10). It ground out over **Jul 7–22** — the entire last-50-trade window is 50 trades
in 12 days — leaving 40–61% of the book in cash through a rising market. Idle-cash × SPY's move ≈ the
whole shortfall. (The apparent 61% is also inflated by JBL short-sale proceeds — Findings 1 and 2
compound.) This was gap #13 from the May audit, never fixed.

**Fix (v3.1):** rebalance runs use `REBALANCE_MAX_ORDERS = 24` / `REBALANCE_MAX_SELL_PCT = 1.00`;
the daily 5-order / 30%-sell throttles apply only to non-rebalance runs.

### Finding 3 — Sector cap was advisory-only; Financials reached 49% of the long book

`check_sector_concentration()` only **printed a warning** at rebalance; it never trimmed. The current
long book is **Financial Services 49.3%** (BNY, IBKR, GS, NTRS, MS, BEN) vs a 30% cap.

**Fix (v3.1):** the rebalance now **enforces** the cap — it drops the lowest-ranked buy candidates
from any sector that would breach `MAX_SECTOR_PCT`.

### Finding 4 — Position sizing was price-driven, not conviction-driven

Across holdings, `corr(current_price, weight) = +0.68` while `corr(momentum_rank, weight) = −0.07`
(should be strongly negative). Buys were sized `min(target_per, cap)/price`, so one expensive share
(EME $845, STX $913) became an oversized position while the best-ranked names stayed small. In the
June book this cost ~1.5pp vs equal weight.

**Fix (v3.1):** dollar-target equal-weight sizing — `target_per = min(deployable/N, pv×10%)`, shares
= `int(target_per // price)`.

### Finding 5 — Positions bought/held with missing screening data

4 current holdings have `momentum_rank = 0` and/or `sector = "Unknown"` (ANET, FRT, BEN, JBL). ANET
is `Unknown` sector + `unknown_ma` status yet a 10.5% position — bought without the data the four
filters and the trend gate require. **Proposed (F6):** fail the screen/trend gate when
`sector == "Unknown"` or `momentum_rank == 0`.

### Finding 6 — Churn / whipsaw destroyed the momentum edge

~100 trades in 90 days vs the strategy's ~20–35%/yr turnover target. Documented round-trips:
**STX sold $750 → rebought $939 (+25% self-inflicted)**, VRT round-trip −13%, GOOGL 2-day flip, plus
~13 sell→rebuy pairs. Driven by Finding 1's re-fire mechanism and the multi-day order-cap grind. The
v3.1 one-run rebalance + long-only clamp remove both drivers.

### Finding 7 — Instrumentation is broken, so the bot flies blind

- **Prompt cache: 0% hit** — `cache_read` totals 3,614 tokens across 90 runs. STRATEGY.md is marked
  cacheable but `update.py`+`enrich.py` take >5 min, so the 5-min ephemeral TTL expires every run.
  (Cost impact trivial; the "cached" design simply never functions.)
- **P&L tracking dead** — every trade has `pnl: null`, `realized_pnl: 0`, `winning_trades: 0`, yet
  `win_rate: 0.333`. Sharpe shows 0.0. The strategy's own scorecard is non-functional.
  **Fixed (F7):** `compute_realized_pnl` reconstructs per-SELL realized P&L by average cost from the
  order history; `compute_risk_metrics` computes Sharpe + max drawdown from the equity curve.
- **Agent spec drift vs STRATEGY §5/§7** — 149 SELL decisions omit `sell_tier`; 35 use the
  deprecated `urgency:"immediate"`; **11 `day_start` runs issued SELL/BUY** (spec: day_start = no
  trades); 4 BUYs proposed in non-quarterly months.
  **Fixed (F8):** `agent.normalize_decisions` enforces all four rules in code before logging —
  independent of the prompt.
- **News sentiment unused** — `bot/agent.py` has 0 references to `news.json`; every Haiku call is
  wasted.
- **UI:** holding cards render the JBL short's market value as positive `$7,036` (should be
  negative) — a display bug that hides the short from the dashboard.

---

## 3. Lessons Learnt

1. **Execution correctness dominates signal quality.** A long-only bot that can accidentally short
   has no business trading live capital. One missing `min(requested, held)` erased a good quarter.
2. **Throughput limits that can't complete a rebalance turn "quarterly rotation" into "quarter-long
   cash drag."** Size the budget to the job.
3. **A risk cap that only warns is not a risk control.** Sector and position caps must trim.
4. **Without working P&L/attribution the agent can't learn and neither can we** — every SELL reasoned
   from `pnl` fields that were partly null.
5. **6 weeks of bull-market paper trading is not validation.** The Phase-2 backtest (STRATEGY §11)
   remains the highest-value missing piece.

---

## 4. Remediation — Ranked & Costed

| ID | Priority | Fix | Status |
|----|----------|-----|--------|
| **F1** | P0 | Long-only clamp: SELL ≤ held, no short, reject price ≤ 0 (`update.py` `alpaca_place_orders` + rebalance sell loop) | ✅ v3.1 |
| **F2** | P0 | One-run rebalance budget (`REBALANCE_MAX_ORDERS`/`_SELL_PCT`); daily throttle unchanged | ✅ v3.1 |
| **F3** | P1 | Deploy idle cash to the 5% floor via dollar-target sizing on the full top-N | ✅ v3.1 |
| **F4** | P1 | Enforce sector cap at rebalance (trim), not advisory | ✅ v3.1 |
| **F5** | P1 | Dollar-target equal-weight sizing (kills price-driven over-sizing) | ✅ v3.1 |
| **F6** | P2 | Block entries with `sector=="Unknown"` / `momentum_rank==0` | ⬜ proposed |
| **F7** | P2 | Fix P&L / win-rate / Sharpe tracking from Alpaca fills | ✅ v3.1 |
| **F8** | P2 | Agent spec conformance: require `sell_tier`, drop `immediate`, day_start flag-only, block non-quarterly BUYs | ✅ v3.1 |
| **F9** | P2 | Prompt-cache within TTL, or drop the cache framing | ⬜ proposed |
| **F10** | P3 | vectorbt/quantstats 10-yr backtest before trusting live capital (STRATEGY §11) | ⬜ roadmap |
| **UI** | P2 | Render negative market value for short positions on holding cards | ⬜ proposed |

**One-time reconciliation:** `bot/rebalance_trueup.py` was added to true up the broken book — cover
the −22 JBL short, exit the 4 unranked names, trim Financials to ≤30%, equal-weight the survivors.
Run `python bot/rebalance_trueup.py --dry-run` to review the exact orders; `--execute` to place them.
(The dry-run lands the book at ~18.6% cash rather than 5% because the surviving top-10 is
Financials-heavy and the sector cap correctly blocks further concentration — the residual cash is
deployed into fresh non-financial names at the next scheduled quarterly rebalance, now that F1–F5 are
in place.)

---

## 5. How to Reproduce These Numbers

All figures come from `origin/main` data as of 2026-07-23:
- `data/portfolio.json` → `summary` (+0.72%, cash 61.4%, `total_pnl` $70.74), `holdings[JBL].shares`
  = −22, `spy_curve` last point $10,197, `equity_curve` peak $10,699 (Jun 22).
- `data/agent_log.json` → 90 runs (45 day_start, 43 day_end, 2 monthly); JBL = 20 SELL decisions
  Jul 6–22; 149 SELLs missing `sell_tier`; 35 `urgency:"immediate"`.

---

# Addendum — v3.1 → v3.2 Review (Aug 17, 2026)

**The gap widened after v3.1 shipped, and the system was dead for a week when this review ran.**

| Metric | Jul 23 (v3.1 shipped) | Aug 10 (last data) |
|---|---|---|
| Portfolio | $9,937 | **$9,533.72** |
| SPY benchmark | $10,225 | **$10,724** |
| Gap vs SPY | −$288 (−2.8pp) | **−$1,190 (−11.1pp)** |
| Reported cash | 61.4% | **79.36% ($7,565.93)** |
| JBL | −22 sh | **−22 sh, −$7,571 MV, −79.4% weight, −$557 unrealized** |
| Win rate | — | **12.5%** (1W / 7L attributed of 50 trades), Sharpe −0.33, max DD 11.96% |

v3.1's long-only clamp **did work** — JBL was not sold again after 2026-07-22. But the true-up was
never executed, and four deeper faults were never addressed.

### Finding 8 — CRITICAL: v3.1 converted a runaway short into a *deadlocked* short

Every path that could close JBL was blocked:

1. `update.py` long-only guard — `held ≤ 0` → SELL skipped (correct, but it means "do nothing").
2. `to_buy_syms = target_syms − current_syms` — JBL **is** in `current_syms` (a position, just
   negative), so it was structurally never a BUY candidate.
3. Buys blocked entirely in non-quarterly months.
4. `agent.normalize_decisions` rewrote `BUY → HOLD` off-quarter, so the agent could not even
   *express* a cover.

From 2026-08-07 the agent emitted `JBL / SELL / tier1 / "BUY-TO-COVER MANDATORY — long-only
invariant breach"` on **every run**. Because a cover had to be encoded as `action=SELL`, the
execution layer read it as a sell and skipped it. The right instruction was untranslatable into an
order — the May TSLA deadlock repeating with new plumbing.

**Lesson: a prevention rule without a restoration rule converts a runaway failure into a frozen one.**

**Fixed (v3.2):** first-class `COVER` action in the agent schema, exempt from the quarterly lock and
the day_start flag-only rule; an explicit cover path in `update.py` **and** in the sentinel, bypassing
the quarterly lock, sector cap, order budget, approval gate and cash floor. See STRATEGY §5.

### Finding 9 — CRITICAL: an LLM billing failure froze order execution for 7 days

Every `TradeQuest Agent` and `TradeQuest Market Open` run failed from 2026-08-10 to 2026-08-17:

```
anthropic.BadRequestError: 400 — 'Your credit balance is too low to access the Anthropic API.'
```

`market-open.yml` ran `agent.py` (an **advisory** day_start flag check) as step 1 and `update.py`
(actual order execution) as step 2. `agent.py` exited 1 → the job aborted → orders never executed.
`TradeQuest Sentinel` was the only green workflow — the only one that makes no LLM call. Nothing
alerted, so the book sat unmanaged for a week with an open naked short.

**Fixed (v3.2):** `continue-on-error` on the advisory step; `agent.py` catches API errors, writes a
degraded log entry and exits 0; `if: always()` on both commit steps (a later failure was discarding
a perfectly good `portfolio.json` refresh); failure-notification issues via
`.github/scripts/notify_failure.sh`.

### Finding 10 — CRITICAL: `account.cash` includes short proceeds — the real performance story

`alpaca_read_state` read `float(account.cash)` verbatim. That figure includes ~$7,571 of JBL
short-sale proceeds the account does not own.

- Reported cash: **$7,565.93 (79.36%)** · True deployable: **~$0**
- Long $9,539 − short $7,571 = **$1,968 net = 20.6% of equity**

A book at ~21% net exposure cannot track a market that rallied ~5% since Jul 23. The dashboard's
"79% cash drag" was not idle capital; it was a short cancelling out the long book. It was also a
live bomb: `apply_risk_limits` computed `available_cash = cash − pv·0.05 ≈ $7,090`, which would have
been deployed at the next quarterly against money that isn't there → ~180% gross exposure.

**Fixed (v3.2):** `compute_deployable_cash()` = `cash − Σ|market_value of shorts|`, used by every
risk gate, by the sentinel, and by the dashboard trade panel. `summary` now also reports
`deployable_cash`, `short_proceeds`, `long_exposure_pct`, `net_exposure_pct`, `long_only_breach`.

### Finding 11 — The asymmetric ratchet: sells allowed daily, buys blocked for 3 months

Non-quarterly months permitted Tier-1 loss-harvest SELLs but blocked **all** BUYs, so the book could
only shrink between quarterly rebalances. August is the clean demonstration — 4 sells, 0 buys:

```
Aug 4  SELL MNST 9      Aug 7  SELL FRT 8
Aug 5  SELL HUM 2       Aug 10 SELL CSX 19
```

With the profit gate (losers sold now, winners deferred) this is a systematic
*sell-losers / never-redeploy* ratchet — the inverse of a momentum strategy. **This**, not the
per-run order throttle v3.1 addressed, is the real cash-drag engine.

**Fixed (v3.2):** redeployment carve-out — top-ups into names already inside the top-N are allowed in
any month once deployable cash exceeds `CASH_FLOOR_PCT + CASH_DEPLOY_BAND` (8%). New entrants remain
quarterly-only. `CASH_DEPLOY_BAND` had been defined since v3.1 and never used; this is what it was
declared for.

### Finding 12 — Daily screen driving a quarterly strategy (Finding 6, quantified)

`to_sell_syms` was recomputed every run against a freshly-ranked top-10 with no hysteresis, no
minimum hold and no re-entry cooldown:

| Symbol | Bought | Sold | Re-bought | Damage |
|---|---|---|---|---|
| FFIV | Jul 28 @ $412.75 | Jul 30 @ $389.88 | Jul 31 @ $401.62 | −$45.74 realized, re-entered higher |
| CSX | Jul 24 @ $53.16 (18) | Jul 28 @ $51.75 | Jul 29 @ $50.61 (19) | −$25.38 realized |
| WST | Jul 24 | Jul 27 @ $326.28 | Jul 28 @ $338.14 | re-entered $11.86/sh worse |
| NTRS | Jul 22 @ $179.52 | Jul 30 @ $179.77 | — | round trip for ~$0 |

The agent's own ranks show the instability: CSX was rank 18 on Aug 7 and rank 21 on Aug 10; STLD was
rank 10 then 17. That is the origin of `win_rate 0.125` / `avg_loss_pct −3.3%`.

**Fixed (v3.2):** rank hysteresis (`EXIT_RANK_MULTIPLE = 1.5` — enter at ≤10, exit at >15),
`MIN_HOLD_DAYS = 10` (waived on a 50-day MA break so Rule A is never delayed), and
`REENTRY_COOLDOWN_DAYS = 10`. Replayed against the Aug-10 book, NUE (rank 12) and STLD (rank 13) are
now held where the old symmetric diff would have sold both.

### Finding 13 — Metrics understated their own sample size

`total_trades: 50` but only 8 carried an attributed P&L (1 win / 7 losses) — `win_rate` was computed
over those 8. `alpaca_read_state` fetched closed orders with a flat `limit=50`, silently truncating
the history `compute_realized_pnl` needs to pair fills. Separately, `meta.next_rebalance` advertised
`2026-09-01` while new-entrant buys were actually locked until Oct 1, and `meta.strategy` still read
"v2.2" / `strategy_version: "2.1"` against v3.1 code.

**Fixed (v3.2):** paginated `fetch_closed_orders()`; `summary.attributed_trades` reports the real
denominator; `next_quarterly_date()` reports the true unlock date; version strings bumped to v3.2.

---

## Remediation Status (v3.2)

| ID | Finding | Status |
|---|---|---|
| **F11** | COVER path — restore the long-only invariant automatically (update.py + sentinel + agent schema) | ✅ v3.2 |
| **F12** | Decouple advisory LLM step from order execution; degrade to exit 0; `if: always()` commits; failure alerts | ✅ v3.2 |
| **F13** | `deployable_cash` excludes short proceeds across all risk gates + dashboard | ✅ v3.2 |
| **F14** | Long-only breach alarm (`long_only_breach` in execution_summary + console banner) | ✅ v3.2 |
| **F15** | Redeployment carve-out for top-N top-ups in non-quarterly months | ✅ v3.2 |
| **F16** | Churn dampers: rank hysteresis, minimum hold, re-entry cooldown | ✅ v3.2 |
| **F17** | Paginated order history + `attributed_trades`; correct `next_rebalance`; version strings | ✅ v3.2 |
| **F6** | Missing-data entry gate (APH/SPG hold null sector/MA/rank and passed the screen) | ⬜ still open |
| **F9** | Prompt-cache hit rate | ⬜ still open |
| **F10** | Backtest harness | ⬜ still open |

**Still requires a human:** top up the Anthropic API credit balance (nothing runs until then), then
run `python bot/rebalance_trueup.py --dry-run` during market hours and `--execute` to remediate the
whole book at once. The v3.2 COVER path will otherwise close JBL on the first successful run.

## How to Reproduce These Numbers

All figures come from `origin/main` data as of 2026-08-17:
- `data/portfolio.json` → `summary` (pv $9,533.72, cash $7,565.93 / 79.36%, win_rate 0.125,
  sharpe −0.33, max_drawdown 11.96%), `holdings[JBL]` = −22 sh / −$7,571.30 / weight −0.7942,
  `equity_curve` and `spy_curve` last points $9,534 vs $10,724.
- `data/agent_log.json` → JBL flagged `SELL/tier1` "BUY-TO-COVER MANDATORY" on every day_end run
  from 2026-08-07; `WATCH` with the same text on every day_start run.
- `gh run list` → every Agent and Market Open run failed from 2026-08-10 onward; Sentinel green.
