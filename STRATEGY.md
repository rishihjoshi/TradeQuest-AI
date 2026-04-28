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
| Max position size | 8% (prevents concentration risk) |
| Rebalance frequency | Monthly (first trading day) |
| Cash in Bull regime | 5% |
| Cash in Sideways regime | 25% |
| Cash in Bear regime | 50% |

---

## Sell Rules *(gap from v1 — significantly improved)*

A position is **exited** when **any** of these trigger:

| Rule | Trigger | Rationale |
|------|---------|-----------|
| Momentum decay | Rank drops below **top 40%** | Exit before full reversal, not after |
| Trend break | Price < **50-day MA** | Confirms momentum has ended |
| Quality deterioration | EPS growth < 5% for 2 consecutive quarters | Fundamental basis for holding is gone |
| Profit taking | Position up > 60% in < 60 days | Parabolic blow-off only — normal momentum continuation must not be cut |

**Key improvement over v1**: v1 sold only when rank dropped below 40% OR price < 50-day MA. v2 adds quality deterioration and profit-taking rules, which independently caught real failures (e.g., high-momentum stocks that reported earnings misses).

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

### Monthly Rebalance (1st of month, 4:30 PM ET)
**Purpose:** Full re-screening and portfolio reconstruction.
- Re-screen all 500 S&P stocks against all four filters
- Rank new candidates by momentum score
- Compare to current holdings → determine buys and sells
- Place all rebalance orders via Alpaca paper trading
- Reset equal weights
- Output: full rebalance plan → written to `data/agent_log.json`

---

## Gaps Addressed (v1 → v2)

| Gap identified in v1 | v2 fix |
|----------------------|--------|
| No volatility filter — chased high-beta names | Added: exclude top 10% most volatile |
| Sell only on momentum rank OR MA break | Added: quality deterioration + profit-taking rules |
| Valuation too rigid (Fwd P/E < 40 excluded whole sectors) | Made relative: OR top 70% cheapest by sector |
| No regime detection | Added: bull/sideways/bear with 3-signal confirmation |
| No AI reasoning layer | Added: Claude agent runs 3x daily, reads this file, logs decisions |
| Alpaca credentials hardcoded risk | Fixed: GitHub Secrets + env vars only |
| XSS vulnerabilities in dashboard | Fixed: sanitize() on all innerHTML, CSP header, SRI on CDN |

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
| Annual turnover | ~100–150% | N/A |

---

## Decision Rules for the Agent

When the Claude agent reads this document and the current portfolio state, it uses the following decision tree:

```
FOR EACH holding:
  IF momentum_rank > 40%  → SELL (rule 1)
  IF price < ma_50d       → SELL (rule 2)
  IF eps_growth < 5% x2   → SELL (rule 3)
  IF pnl_pct > 60% in <60d → SELL (rule 4, parabolic blow-off only)
  ELSE                    → HOLD

FOR monthly rebalance:
  Screen full S&P 500 universe
  Apply filters 1–4 in order
  Take top TARGET_N by momentum score
  BUY new entrants not currently held
  SELL positions not in new top-N
  Adjust cash to match regime target
```

The agent must always cite which rule triggered a decision and explain its confidence level.
