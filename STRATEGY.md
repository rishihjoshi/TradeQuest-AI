# TradeQuest AI — Trading Strategy v2.0

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
| Target positions | 15–20 |
| Weighting | Equal weight (start simple, avoids estimation error) |
| Min position size | 5% (prevents over-diversification) |
| Position soft cap | 12% (review but do not force-trim if stock passes all filters) |
| Position hard cap | 20% (trim to 15% at next scheduled rebalance) |
| Rebalance frequency | Quarterly (first trading day of Jan, Apr, Jul, Oct) |
| Monthly agent review | Flag-only mode — no sells unless hard cap exceeded or critical sell rule fires |
| Cash in Bull regime | 5% |
| Cash in Sideways regime | 25% |
| Cash in Bear regime | 50% |

---

## Sell Rules *(v2.1 — tax-aware, tightened to reduce unnecessary realization)*

A position is **exited** when **any** of these trigger:

| Rule | Trigger | Rationale |
|------|---------|-----------|
| Momentum decay | Rank drops below **top 30%** confirmed over **5 trading days** | Stricter threshold (was 40%) prevents exiting on brief dips; confirmation window avoids single-day noise |
| Trend break | Price closes below **50-day MA** for **3 consecutive days** | Sustained break only (was 1 day); single-day violations are common in healthy uptrends |
| Quality deterioration | EPS growth negative for **2 consecutive quarters** | Requires persistent fundamental failure (was 1 quarter); single-quarter misses often reverse |
| Profit taking | Position up > 60% in < 60 days — **sell half, hold remainder** | Parabolic blow-off only; selling only half preserves continued upside while locking partial gains |
| Hard cap violation | Position weight exceeds **20%** — trim to 15% | Winners that grow large are a sign of success; only trim at extreme concentration |

**Tax-aware priority order for sells:**
1. Always prefer selling positions **at a loss** first (tax-loss harvesting) before trimming winners
2. Only sell a winning position if it violates a sell rule OR exceeds the 20% hard cap
3. Never sell a winner solely to rebalance to equal weight — wait for the quarterly cycle

**Key improvement over v2.0**: v2.0's 40% momentum threshold and 1-day MA rule generated excessive turnover (~100–150% annually) and unnecessary capital gains. v2.1 reduces turnover to ~40–60% by requiring stronger, confirmed signals before exiting.

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
- **On non-quarterly months** (Feb, Mar, May, Jun, Aug, Sep, Nov, Dec): output flags and WATCH decisions only — no sells unless a sell rule fires or hard cap exceeded
- **On quarterly months** (Jan, Apr, Jul, Oct): full rebalance — execute buys and sells, reset cash target to regime level
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
| Annual turnover | ~40–60% (v2.1, down from 100–150%) | N/A |
| Estimated annual capital gains | Low — primarily long-term holds >1 year | N/A |

---

## Decision Rules for the Agent

When the Claude agent reads this document and the current portfolio state, it uses the following decision tree:

```
FOR EACH holding (day_end and monthly review):
  IF momentum_rank < 30% confirmed ≥5 days  → SELL (rule 1)
  IF price < ma_50d for ≥3 consecutive days  → SELL (rule 2)
  IF eps_growth < 0 for 2 consecutive qtrs   → SELL (rule 3)
  IF pnl_pct > 60% in <60d                   → SELL half, HOLD remainder (rule 4)
  IF weight > 20%                             → TRIM to 15% at next quarterly (rule 5)
  ELSE                                        → HOLD

TAX-AWARE SELL PRIORITY (when sells are needed):
  1. Sell positions with unrealized losses first (tax-loss harvest)
  2. Only sell winners if a sell rule fires OR hard cap exceeded
  3. Never sell a winner to rebalance to equal weight

FOR quarterly rebalance (Jan/Apr/Jul/Oct first trading day):
  Screen full S&P 500 universe
  Apply filters 1–4 in order
  Take top TARGET_N by momentum score
  BUY new entrants not currently held (fund from cash first, then tax-loss sales)
  SELL positions not in new top-N AND at a loss (harvest losses)
  HOLD positions not in new top-N BUT with gains (avoid unnecessary realization)
  Adjust cash to match regime target

FOR non-quarterly monthly review (Feb/Mar/May/Jun/Aug/Sep/Nov/Dec):
  Run sell rules only — flag violations
  Output WATCH / SELL (critical rule only) / HOLD
  Do NOT initiate rebalance buys or routine sells
```

The agent must always cite which rule triggered a decision and explain its confidence level.
Whenever the agent issues a SELL decision on a position with an unrealized gain, it must explicitly note the estimated tax impact and confirm the rule justifies the gain realization.
