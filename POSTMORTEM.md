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
