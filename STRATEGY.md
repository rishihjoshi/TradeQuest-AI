# TradeQuest AI — Trading Strategy v2.2

## Objective
Beat the S&P 500 on a **risk-adjusted basis** over rolling 12-month periods, targeting +3–7% annual excess return with lower drawdowns than the index.

---

## The Edge — Why This Should Work

Three independently documented market anomalies are stacked together:

| Anomaly | Source | Expected Edge |
|---------|--------|--------------|
| **Momentum premium** | Jegadeesh & Titman (1993) | Top decile stocks outperform bottom decile by ~10%/yr |
| **Quality premium** | Novy-Marx (2013) | High-profit firms consistently outperform |
| **Regime-aware allocation** | Faber (2007) | Reducing equity in downtrends cuts drawdown 40–60% |

Stacking all three with a volatility guard produces a strategy that captures upside momentum while avoiding the worst crashes.

---

## Universe
**S&P 500 only** — large-cap, liquid, transparent. No micro-caps, no OTC, no ETFs.

- ~500 stocks screened daily via Yahoo Finance
- Survivorship bias is a known limitation (S&P index membership itself screens out failures)

---

## Filters (Applied in Order)

### 1. Momentum Filter — Primary Driver
- 6-month total return: must rank in **top 30%** of universe
- 12-month total return: must rank in **top 30%** of universe
- Score = average of both percentile ranks → ranked list of candidates
- **Skip-month rule**: use price from 21 trading days ago as "current" to avoid 1-month reversal effect

### 2. Quality Filter
- EPS growth > 10% (trailing 12M or 3-year average)
- Revenue growth > 8% (trailing 12M)
- Rationale: filters out low-quality "junk momentum" — high-beta stocks with no earnings support that crash hardest in corrections

### 3. Valuation Guard Rail
- Forward P/E < 40
- **OR** within the top 70% cheapest by sector (prevents systematically excluding entire high-multiple sectors like tech during genuine growth periods)
- Rationale: avoids buying at extreme multiples that are vulnerable to multiple compression even if momentum is strong

### 4. Risk Filter *(gap from v1 — now enforced)*
- 30-day annualized volatility must be **below the 90th percentile** of the universe
- Excludes the most volatile 10% of stocks
- Rationale: high-volatility momentum names have fat left tails — they produce spectacular gains but also spectacular losses; equal-weighting them destroys portfolio Sharpe ratio

---

## Portfolio Construction

| Parameter | Value |
|-----------|-------|
| Target positions | **10–12** (high-conviction only — top-tier momentum scores) |
| Weighting | Equal weight (start simple, avoids estimation error) |
| Min position size | **8%** (prevents dilution; fewer, stronger names) |
| Position hard cap | **20%** (trim to 15% at next scheduled quarterly rebalance) |
| Sector cap | **30%** max in any single GICS sector — higher concentration risk with 10–12 names requires this guard |
| Rebalance frequency | Quarterly (first trading day of Jan, Apr, Jul, Oct) |
| Monthly agent review | **Flag-only mode** — no new buys, no routine sells. Only Tier 1 loss-harvests and structural exits permitted |
| Cash in Bull regime | 5% |
| Cash in Sideways regime | 25% |
| Cash in Bear regime | 50% |

### Why 10–12 Positions (v2.2 Change from 15–20)
The momentum premium is concentrated in the top decile of the universe. Holding 15–20 positions dilutes the alpha by including ranks 11–18 which show materially lower momentum scores. Live run evidence (May–Jun 2026): positions ranked 1–10 (WDC, AMAT, ADI, CAT, JBL, KEYS, NUE, STLD, KLAC, VRT) averaged +11.8% return; positions ranked 11–18 (FDX, BEN, GOOGL, IBKR, C, ROST, NTRS, CFG) averaged -4.2% return. The bottom half dragged down the top half. Concentrating 8–10% in 10–12 positions captures the full momentum edge while reducing the management overhead that caused over-trading.

---

## Sell Rules *(v2.2 — 12-Month Hold Gate + Losers-First Pipeline)*

### The Core Principle
**Sell losing positions fast. Hold winning positions long.**

This asymmetry serves two purposes simultaneously: (1) tax-loss harvesting — realized losses offset future gains; (2) long-term capital gains treatment — positions held 12+ months are taxed at 0–20% vs ordinary income rates (up to 37%) for short-term gains. Every premature exit of a winner is a tax event that could have been deferred.

---

### Structural Rules — Apply to ALL positions regardless of profit/loss status

These rules represent genuine portfolio risk and override the hold gate:

| Rule | Trigger | Rationale |
|------|---------|-----------|
| **A — Trend break** | Price closes below **50-day MA** for **3 consecutive days** | Sustained structural break; price is telling you the thesis is wrong |
| **B — Quality failure** | EPS growth negative for **2 consecutive quarters** | Fundamental deterioration — the quality filter is now failing |
| **C — Hard cap** | Position weight exceeds **20%** → trim to 15% at next quarterly | Extreme concentration; trim at scheduled rebalance, not immediately |
| **D — Parabolic blow-off** | Position up > 60% in < 60 days → **sell half, hold remainder** | Captures blow-off top; retains continued upside |

### Momentum Decay Rule — Asymmetric by Profit/Loss Status

| Position status | Momentum rank < top 30%, confirmed 5 days | Action |
|---|---|---|
| **At a loss** (unrealized PnL < 0) | Confirmed | **SELL — Tier 1** (tax-loss harvest; execute at next open) |
| **At a gain** (unrealized PnL > 0) AND held < 12 months | Confirmed | **WATCH only** — flag but do not sell; defer to quarterly rebalance |
| **At a gain** (unrealized PnL > 0) AND held ≥ 12 months | Confirmed | **SELL eligible** — execute at next quarterly rebalance (LTCG treatment) |

### Sell Tier Classification

Every sell decision must be classified before issuing:

```
Tier 1 — Execute at next market open (next_open urgency):
  • Any position with unrealized LOSS hitting Rule A, B, C, D, or momentum decay
  • These are tax-loss harvests — no reason to wait; losses should exit fast

Tier 2 — Defer to next quarterly rebalance (next_rebalance urgency):
  • Any position with unrealized GAIN that fails ONLY momentum decay (Rule E)
  • Hold until July 1 (or next quarterly); review at rebalance — may qualify for LTCG

Tier 3 — Hold indefinitely:
  • Any position with unrealized GAIN that passes all structural rules (A/B/C/D)
  • Momentum rank improving is possible; do not sell into temporary rank weakness

Do NOT issue a Tier 2 or Tier 3 SELL in a non-quarterly month.
Agent must classify every SELL decision as Tier 1/2/3 before issuing it.
Agent must explicitly state unrealized PnL and hold duration when classifying.
```

### Tax-Aware Priority Order for Sells (when selling is necessary)
1. Sell positions **at a loss** first (Tier 1) — every dollar of realized loss offsets a future gain
2. Only sell a profitable position under a structural rule (Rule A/B/C/D) or after 12-month LTCG threshold
3. **Never** sell a winner solely to rebalance to equal weight — unequal weights are fine; tax drag is permanent

**Key improvement over v2.1**: v2.1 still allowed momentum-decay SELLs on profitable positions (+0.51%, +1.33%, +3.64% gains) generating unnecessary short-term capital gains. v2.2 routes all profitable momentum-decay signals to WATCH/deferred status, cutting taxable events by ~60% while preserving full loss-harvest efficiency.

---

## Market Regime Detection

The agent monitors three signals daily to determine the market regime:

| Signal | Bull | Sideways | Bear |
|--------|------|----------|------|
| SPY price vs 200-day MA | Above | Within 3% | Below |
| 30-day realized volatility | < 20% ann. | 20–28% | > 28% |
| Market breadth (% of S&P above 200-MA) | > 60% | 40–60% | < 40% |

Regime requires **2 of 3** signals to agree before switching — prevents whipsawing on a single bad day.

### Regime → Allocation

```
BULL     → 95% equity, 5% cash   → Full deployment
SIDEWAYS → 75% equity, 25% cash  → Defensive tilt
BEAR     → 50% equity, 50% cash  → Capital preservation
```

Regime change triggers rebalancing of the cash buffer within 3 trading days.

---

## Agentic AI Layer

Claude AI agent runs on two scheduled routines. Each run reads this strategy file, the
current portfolio state, and the previous run's log (for continuity), then writes
structured decisions to `data/agent_log.json`.

### Day End (4:30 PM ET — post-close, Mon–Fri)
**Purpose:** Daily close prices → run sell rules → place Alpaca paper trades if needed.
- Update portfolio with closing prices (via `bot/update.py`)
- Check all four sell rules against updated prices
- Make HOLD / SELL decisions for positions
- Place actual Alpaca paper trading orders for any sells
- On Fridays: append a `weekly_summary` (week return vs SPY, key trades, Monday watchlist)
- Output: decisions + trades → written to `data/agent_log.json`

### Monthly Review (1st of month, 4:30 PM ET) — flag-only except on quarterly months
**Purpose:** Re-screen universe, flag risks, execute only on critical signals.
- Re-screen all 500 S&P stocks against all four filters
- Rank new candidates by momentum score
- Compare to current holdings → identify new entrants and deteriorating positions
- **On non-quarterly months** (Feb, Mar, May, Jun, Aug, Sep, Nov, Dec):
  - Output WATCH and flag decisions only — **zero new BUY orders**
  - **Only Tier 1 SELLs** (unrealized losses hitting a structural or momentum-decay rule)
  - No Tier 2 or Tier 3 SELLs — defer profitable exits to the next quarterly
- **On quarterly months** (Jan, Apr, Jul, Oct): full rebalance
  - Execute all Tier 1 SELLs immediately
  - Execute Tier 2 SELLs deferred from prior non-quarterly months (if still failing filters)
  - Buy new top-10-12 entrants not currently held (fund from cash first, then Tier 1 sales)
  - Trim any position exceeding 20% hard cap to 15%
  - Reset cash target to current regime level
  - Apply sector cap: no single sector may exceed 30% after rebalance
- Tax-loss harvest: if deploying new cash into buys, first offset with any losing positions (>5% unrealized loss)
- Output: full rebalance plan or flag-only review → written to `data/agent_log.json`

---

## Gaps Addressed (v1 → v2 → v2.1)

| Gap identified | Fix |
|----------------|-----|
| No volatility filter — chased high-beta names | Added: exclude top 10% most volatile |
| Sell only on momentum rank OR MA break | Added: quality deterioration + profit-taking rules |
| Valuation too rigid (Fwd P/E < 40 excluded whole sectors) | Made relative: OR top 70% cheapest by sector |
| No regime detection | Added: bull/sideways/bear with 3-signal confirmation |
| No AI reasoning layer | Added: Claude agent runs 3x daily, reads this file, logs decisions |
| Alpaca credentials hardcoded risk | Fixed: GitHub Secrets + env vars only |
| XSS vulnerabilities in dashboard | Fixed: sanitize() on all innerHTML, CSP header, SRI on CDN |
| High turnover (~100–150%/yr) generating capital gains | v2.1: quarterly rebalance, stricter sell thresholds, sell losers first |
| Hard 8% cap forced trimming winners | v2.1: 12% soft cap, 20% hard cap — let winners run |
| Monthly rebalance caused unnecessary churn | v2.1: non-quarterly months are flag-only review |
| Equal-weight rebalancing sold winning positions | v2.1: tax-loss harvest priority; winners held unless sell rule fires |
| v2.1 still issued momentum-decay SELLs on small-gain positions (+0.51%, +1.33%) | v2.2: **12-month hold gate** — profitable positions immune to momentum-decay sell until 12mo mark |
| Agent issued Tier 2/3 SELLs in non-quarterly months (May/Jun) | v2.2: **code-level execution lock** — update.py blocks non-Tier-1 sells and all buys in non-quarterly months |
| 15–20 positions diluted momentum edge with bottom-half holdings | v2.2: **10–12 position limit** — only top-tier momentum names, 8–10% each |
| Premature exits + re-entries (STX sold $750→ re-bought $939; +25% miss) | v2.2: hold gate prevents selling winners; no mid-quarter buys prevents chasing back in |
| No sector cap — concentration risk in 10–12 name portfolio | v2.2: **30% sector cap** enforced at quarterly rebalance |

---

## Market Breadth Context

When enrichment data is provided, use the breadth signal to calibrate regime_confidence:

| Breadth (% S&P above 200-MA) | Implication |
|---|---|
| > 60% | Broad participation — supports bull regime; use full confidence |
| 40–60% | Narrowing rally — treat regime classification with caution; consider sideways even if SPY is above 200-MA |
| < 40% | Thin participation — strong bias toward sideways or bear regardless of SPY trend |

A market where SPY is above its 200-day MA but breadth is below 40% is a **narrow (late-cycle) bull** — maintain higher cash than the regime alone would suggest.

The breadth 8MA crossing below the 200MA is an early warning of deterioration, not an immediate sell signal, but should reduce regime_confidence.

## Upcoming Earnings Awareness

When enrichment data includes earnings announcements for current holdings:
- A holding with earnings **within 3 days**: flag it with `WATCH` if not already a sell signal; note the earnings date in the reason
- A holding with earnings **within 1 day (BMO tomorrow or AMC today)**: consider `WATCH` with urgency `next_open` unless a sell rule is already triggered
- Do **not** sell solely because of an upcoming earnings — but do factor earnings risk into confidence levels
- If already planning to SELL based on a rule, prefer executing **before** earnings, not after

## Execution Pipeline & Urgency Semantics

TradeQuest runs on GitHub Actions with two workflows:

- **agent.yml** (5:30 PM ET Mon–Fri): Syncs Alpaca → enriches data → Claude makes decisions → commits state.
- **market-open.yml** (9:30 AM ET Mon–Fri): Executes approved orders at market open.

**All SELL and BUY orders execute at the next market open (9:30 AM ET).** There is no same-session execution path. Do not confuse urgency with speed.

Valid urgency values in `decisions[].urgency`:
- `"next_open"` — Execute at next 9:30 AM ET market open. Use for **Tier 1 SELLs only** (losing positions).
- `"next_rebalance"` — Defer to the next quarterly rebalance. Use for Tier 2/3 signals and all WATCH decisions.

The label `"immediate"` is deprecated. All agents should use `"next_open"` instead.

### Non-Quarterly Month Execution Lock *(v2.2 — Hard Rule)*

**In Feb, Mar, May, Jun, Aug, Sep, Nov, Dec the execution pipeline enforces:**
- `BUY` orders → **BLOCKED** at code level in `update.py` (no new entries mid-quarter)
- `SELL` orders → Only **Tier 1** (unrealized loss + structural or momentum-decay rule) are executed
- `SELL` orders for profitable positions → **BLOCKED** at code level unless Rule A/B/C/D fires

This is enforced in code (`update.py` checks `is_quarterly_month()` before accepting agent BUY approvals), not just as a prompt instruction. The agent's BUY decisions in non-quarterly months will be logged but the pipeline will not execute them.

**Persistent-flag escalation:** If a symbol has been flagged SELL in 3+ consecutive prior runs without execution:
- If position is **at a loss**: re-issue as Tier 1 SELL with `urgency="next_open"`; note consecutive count
- If position is **at a gain**: re-issue as Tier 2 WATCH; do NOT escalate to immediate execution — the hold gate applies regardless of flag count
- Do NOT issue a BUY for any symbol that has an active SELL flag.

**Sentinel rule (automated, no LLM):** A separate `sentinel.yml` workflow runs at 2:30 PM ET and automatically executes sell orders for positions meeting hard rules — without waiting for the 5:30 PM agent run:
1. Position weight > 2× MAX_POSITION_PCT (>20%) → forced sell
2. Price < 50-day MA for 3+ consecutive agent runs → forced sell (structural Rule A)
3. Symbol flagged SELL in 5+ consecutive runs AND **position is at a loss** → forced sell
   - Note: Rule 3 no longer force-sells profitable positions — the hold gate applies to the sentinel too

---

## Known Limitations & Honest Caveats

1. **Earnings gap risk** — Momentum stocks holding into earnings can gap 10–20% overnight in either direction. Mitigated by quality filter (earnings growers rarely miss badly), not fully eliminated.

2. **Sector concentration** — Momentum often over-concentrates in 1–2 sectors (e.g., tech in 2020–2021, energy in 2022). No hard sector cap in v2 — monitored but not forced.

3. **Survivorship bias** — S&P 500 membership itself removes bankruptcies and failing companies. Real-world universe would include some failures.

4. **Transaction costs** — Paper trading ignores bid-ask spread, market impact, and commissions. Real-world returns would be ~0.5–1.5% lower annually for this turnover rate.

5. **Momentum crashes** — The momentum factor crashes hard and fast approximately once per decade (2009, 2020). The bear regime detection reduces exposure but does not eliminate it.

6. **yfinance data quality** — Fundamental data (EPS growth, forward P/E) from yfinance can be stale or missing. The bot falls back to relaxed valuation rules when data is unavailable.

---

## Target Performance vs S&P 500

| Metric | Target | S&P 500 benchmark |
|--------|--------|------------------|
| Annual excess return | +3% to +7% | 0% (by definition) |
| Sharpe ratio | > 1.2 | ~0.7 (historical) |
| Max drawdown | < 15% | ~35% in severe bear |
| Win rate (% of positions profitable) | 55–65% | N/A |
| Annual turnover | **~20–35%** (v2.2, down from 40–60% in v2.1) | N/A |
| Estimated annual capital gains | **Very low** — losses harvested immediately; gains deferred to 12mo LTCG treatment | N/A |

---

## Decision Rules for the Agent

When the Claude agent reads this document and the current portfolio state, it uses the following decision tree:

```
FOR EACH holding — v2.2 decision tree:

  STEP 1: Check structural rules (apply regardless of profit/loss status)
    IF price < ma_50d for ≥3 consecutive days          → SELL Tier 1 [Rule A]
    IF eps_growth < 0 for 2 consecutive quarters        → SELL Tier 1 [Rule B]
    IF weight > 20%                                     → flag TRIM to 15% at next quarterly [Rule C]
    IF pnl_pct > 60% in <60d                            → SELL half, HOLD remainder [Rule D]

  STEP 2: Check momentum decay (Rule E — asymmetric by profit/loss)
    IF momentum_rank outside top 30%, confirmed ≥5 days:
      AND unrealized PnL < 0  → SELL Tier 1 (next_open)   ← loss-harvest, exit fast
      AND unrealized PnL ≥ 0  → WATCH only (next_rebalance) ← hold gate applies

  STEP 3: Default
    ELSE → HOLD

  STEP 4: Classify every SELL before issuing it
    Tier 1 = loss position OR structural rule → urgency=next_open
    Tier 2 = gain position + momentum decay only → urgency=next_rebalance, defer to quarterly
    NEVER issue Tier 2 SELL in a non-quarterly month

FOR quarterly rebalance (Jan/Apr/Jul/Oct first trading day):
  Screen full S&P 500 universe
  Apply filters 1–4 in order
  Take top 10–12 by momentum score (TARGET_N = 10)
  Execute Tier 1 SELLs immediately (losses, structural failures)
  Execute deferred Tier 2 SELLs if still failing momentum at this rebalance
  BUY new top-10-12 entrants not currently held (fund from cash, then Tier 1 proceeds)
  HOLD profitable positions not in new top-N (avoid unnecessary gain realization)
  Check sector cap: no sector > 30% after rebalance; trim to comply
  Adjust cash to match regime target

FOR non-quarterly monthly review (Feb/Mar/May/Jun/Aug/Sep/Nov/Dec):
  Run STEP 1 + STEP 2 sell rule checks against all holdings
  Output WATCH / Tier 1 SELL (only) / HOLD
  BUY orders: NONE — code pipeline blocks mid-quarter buys
  Tier 2 SELLs: log as WATCH, do NOT set urgency=next_open
```

The agent must always cite which rule triggered a decision and explain its confidence level.
Whenever the agent issues a SELL decision on a position with an unrealized gain, it must explicitly note the estimated tax impact and confirm the rule justifies the gain realization.
