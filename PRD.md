# TradeQuest AI — Product Requirements Document

**Date:** April 27, 2026  
**Status:** Draft — Pending Review  
**Approach:** Phased delivery, quick wins first

---

## 1. Current App State

TradeQuest AI is a production-ready, AI-powered momentum trading system running 100% on GitHub infrastructure. It implements a quantitative strategy stacking three documented market anomalies (momentum premium, quality premium, regime-aware allocation) across the S&P 500 universe.

**What works today:**
- Paper trading via Alpaca, orchestrated by GitHub Actions (3x daily)
- Claude Sonnet as an autonomous decision agent (day-start, day-end, monthly rebalance)
- Screening 500 S&P stocks through 4 sequential filters (momentum → quality → valuation → risk)
- News sentiment classification per holding via Claude Haiku + Alpaca News API
- Static GitHub Pages dashboard: equity curve, holdings cards, agent logs, news feed
- Prompt caching on STRATEGY.md (~50% token cost reduction)
- Git-as-database: immutable audit trail of every AI decision

**Current architecture:**
```
GitHub Actions → bot/update.py (yfinance + alpaca-py)
                → bot/agent.py (Claude Sonnet)
                → data/*.json (committed to repo)
                → GitHub Pages (HTML/CSS/JS dashboard)
```

---

## 2. Ecosystem Research

### 2.1 MCP Servers (Claude-Native Integrations)

| MCP Server | What It Provides | Relevance |
|---|---|---|
| **Alpaca MCP (Official v2)** | 61 endpoints — orders, account, market data, options, crypto; natural language trading; paper mode default | Direct drop-in for current alpaca-py calls; already supports paper trading |
| **Financial Datasets MCP** | Real-time prices, SEC filings, earnings, stock screener with 400+ metrics; OAuth 2.1 | Superior fundamental data vs yfinance for screening |
| **Alpha Vantage MCP** | Real-time + historical data, crypto, forex, technicals; clean structured format | yfinance alternative with MCP-native access |
| **EODHD MCP** | 77 read-only tools, global coverage, fundamentals, options chains | Best-in-class for international expansion |
| **TradingView MCP** | Real-time technical indicators, candlestick patterns, multi-exchange; Claude Desktop integration | Adds TA layer (RSI, MACD, Bollinger) to screening |

**Biggest find:** Alpaca has an official MCP Server v2 (released 2025, rewritten with FastMCP + OpenAPI auto-sync). This replaces all manual `alpaca-py` calls and exposes every Alpaca endpoint through natural language — including paper trading by default.

### 2.2 Python Libraries

| Library | Purpose | Relevance |
|---|---|---|
| **vectorbt** | NumPy/Numba vectorized backtesting — thousands of configs in seconds | Fastest way to validate 10-year strategy history |
| **quantstats** | 50+ performance metrics (Sharpe, Sortino, Calmar, VaR, CAGR); HTML tearsheet generator | One-call performance reports; benchmarking vs SPY |
| **backtrader** | Event-driven backtesting with live trading hooks; broker simulation | More realistic fills/slippage than vectorbt |
| **pandas-ta / TA-Lib** | 130+ technical indicators (RSI, MACD, ATR, Bollinger Bands) | Extends screening beyond price momentum |
| **finvizfinance** | Scrapes Finviz: fundamentals, insider activity, news, ratings | Fast screener data alternative to yfinance |
| **yfinance** | Already in use — free prices, fundamentals, volatility | Keep as primary; unreliable for earnings data |

### 2.3 Existing Open Source Projects

| Project | What It Does | Key Insight |
|---|---|---|
| **alpacahq/Momentum-Trading-Example** | Alpaca's own momentum day-trading example | Official reference implementation |
| **claude-trading-skills (tradermonty)** | Claude Code skills: portfolio analysis, rebalancing, screeners | Reusable skill patterns for our agent |
| **HKUDS/AI-Trader** | Agent-native trading framework; modular, Claude Code compatible | Architecture patterns for multi-agent orchestration |
| **wshobson/agents** | Multi-agent orchestration for Claude Code | Useful for Phase 3 specialist agent design |
| **marketcalls/vectorbt-backtesting-skills** | Agentic backtesting skill using vectorbt + TA-Lib + QuantStats | Ready-made S&P 500 momentum backtest templates |
| **nickmccullum/algorithmic-trading-python** | S&P 500 quantitative momentum strategy in Python (textbook reference) | Validation of our screening methodology |

### 2.4 CLIs

| Tool | What It Is |
|---|---|
| **Alpaca CLI** | Official CLI for Alpaca Trading API — place orders, check account, stream data from terminal |
| **Claude Code CLI** | Already in use for agent execution; supports hooks and scheduled runs |

---

## 3. Gap Analysis

Comparing TradeQuest AI to the ecosystem, here is what's missing:

| Gap | Impact | Effort |
|---|---|---|
| **No backtesting** — can't validate the strategy has historical merit before trusting it with capital | Critical | Medium |
| **Fake sparklines** — holdings cards show seeded random walks, not real price data | High (trust) | Low |
| **No benchmark comparison** — equity curve doesn't show SPY/QQQ alongside portfolio | High (context) | Low |
| **No performance tearsheet** — Sharpe/Sortino/Calmar/CAGR only visible on dashboard, not exportable | Medium | Low |
| **Manual alpaca-py calls** — 61-endpoint Alpaca MCP v2 is now available; manual calls are fragile | Medium | Medium |
| **yfinance unreliable for fundamentals** — EPS/revenue data often stale or missing | Medium | Medium |
| **No real-time data** — dashboard refreshes only when GitHub Actions runs (3x/day) | Medium | High |
| **No notifications** — no alerts when agent flags a position or regime changes | Low-Medium | Medium |
| **Single strategy** — momentum only; no comparison to alternatives | Low | High |

---

## 4. Phased Roadmap

### Phase 1 — Quick Win (v1.0) ← Start here
**Theme:** Prove the strategy works. Make the dashboard trustworthy.  
**Effort:** 1–2 weeks  
**Goal:** Add backtesting + real sparklines + SPY benchmark so the dashboard shows verifiable historical performance rather than just paper trading results.

### Phase 2 — Enhanced Intelligence (v2.0)
**Theme:** Better data in, better decisions out.  
**Effort:** 3–4 weeks  
**Goal:** Replace fragile yfinance fundamentals with Financial Datasets MCP; integrate Alpaca MCP v2; add notification system; generate QuantStats tearsheet on demand.

### Phase 3 — Production-Ready (v3.0)
**Theme:** Live trading capability + multi-agent orchestration.  
**Effort:** 6–8 weeks  
**Goal:** Toggle from paper to live; specialist agents (screener, risk, execution); TradingView MCP for TA signals; real-time WebSocket data feed.

---

## 5. Phase 1 — Detailed Specification

### 5.1 Overview

Three targeted additions to the existing system — no rewrites, no new infrastructure:

1. **Strategy Backtester** — Run the momentum strategy on 10 years of S&P 500 data using vectorbt. Save results to `data/backtest.json`. Display on new "Backtest" tab in dashboard.
2. **Real Sparklines** — Replace seeded random walk sparklines with actual 30-day closing prices fetched from yfinance during `bot/update.py` runs.
3. **SPY Benchmark Overlay** — Add SPY daily close to `data/portfolio.json`; render as a second line on the equity chart.

### 5.2 Feature 1: Strategy Backtester

**New file:** `bot/backtest.py`

**Inputs:**
- S&P 500 constituent list (static snapshot, ~500 tickers)
- 10 years of daily price history via yfinance
- Strategy parameters from STRATEGY.md (6M/12M momentum ranking, top-30% cutoff, monthly rebalance, equal weight, 15–20 positions)

**Processing (vectorbt):**
- Vectorized momentum ranking across all 500 stocks for each monthly rebalance date
- Portfolio simulation: top-30% momentum, equal-weighted, monthly rebalance
- SPY buy-and-hold as benchmark
- QuantStats report generation: Sharpe, Sortino, Calmar, CAGR, max drawdown, win rate, alpha, beta

**Output:** `data/backtest.json`
```json
{
  "generated_at": "ISO timestamp",
  "period": { "start": "2015-01-01", "end": "2025-12-31" },
  "strategy": {
    "cagr": 0.142,
    "sharpe": 1.31,
    "sortino": 1.87,
    "calmar": 0.95,
    "max_drawdown": -0.187,
    "total_return": 2.84,
    "win_rate_monthly": 0.64
  },
  "benchmark": {
    "cagr": 0.112,
    "sharpe": 0.98,
    "total_return": 1.89
  },
  "equity_curve": [{ "date": "YYYY-MM-DD", "strategy": 100.0, "spy": 100.0 }],
  "annual_returns": [{ "year": 2015, "strategy": 0.08, "spy": 0.01 }]
}
```

**GitHub Actions:** Add `backtest` workflow — runs monthly (1st of month, before the rebalance run). Can also be triggered manually. Commits updated `data/backtest.json`.

**Dashboard — new "Backtest" tab:**
- Stat cards: CAGR, Sharpe, Max Drawdown, Alpha vs SPY (4 cards)
- Dual-line Chart.js chart: strategy equity curve vs SPY (indexed to 100)
- Annual returns bar chart (strategy vs SPY side-by-side per year)
- Strategy parameters summary card

### 5.3 Feature 2: Real Sparklines

**Modified file:** `bot/update.py`

During the existing daily update run, fetch 30-day closing price history for each holding:
```python
# For each holding in portfolio
hist = yf.Ticker(symbol).history(period="30d")["Close"]
holding["sparkline"] = hist.round(2).tolist()
holding["sparkline_dates"] = [d.strftime("%Y-%m-%d") for d in hist.index]
```

Store in `data/portfolio.json` under each holding object.

**Modified file:** `app.js`

Replace the deterministic sparkline generator with a real canvas renderer reading `holding.sparkline`. Same canvas element, same visual size — just real data. Graceful fallback to the existing seeded approach if `sparkline` key is absent (backwards compatibility).

### 5.4 Feature 3: SPY Benchmark Overlay

**Modified file:** `bot/update.py`

Fetch SPY daily closes going back to `portfolio.meta.start_date`:
```python
spy = yf.Ticker("SPY").history(start=portfolio_start)["Close"]
```

Store as `portfolio.benchmark = [{ "date": "YYYY-MM-DD", "spy_value": float }]` — indexed to the same initial capital as the portfolio.

**Modified file:** `app.js`

Add a second dataset to the existing Chart.js equity chart:
- Portfolio line: gold (#D4AF37) — existing
- SPY line: gray (#888888), dashed — new
- Legend labels: "TradeQuest Strategy" / "SPY Buy & Hold"

### 5.5 Dependencies to Add

```
# bot/requirements.txt additions
vectorbt>=0.26
quantstats>=0.0.62
```

No new API keys. No new secrets. No new services. Uses existing yfinance + GitHub Actions.

### 5.6 New GitHub Actions Workflow

**File:** `.github/workflows/backtest.yml`

```yaml
name: Monthly Backtest
on:
  schedule:
    - cron: '0 20 1 * *'   # 1st of month, 8 PM ET
  workflow_dispatch:
jobs:
  backtest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r bot/requirements.txt
      - run: python bot/backtest.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      - run: |
          git config user.name "TradeQuest Bot"
          git config user.email "bot@tradequest"
          git add data/backtest.json
          git commit -m "chore: update backtest results [skip ci]"
          git push
```

### 5.7 Success Metrics for Phase 1

| Metric | Target |
|---|---|
| Backtester produces 10-year results | ✅ / ❌ |
| Strategy CAGR > SPY CAGR in backtest | Target: +2–4% annually |
| Strategy Sharpe > 1.0 | Target: >1.2 |
| Sparklines show real 30-day price data | Visual verification |
| SPY benchmark visible on equity chart | Visual verification |
| Backtest workflow runs successfully on Actions | ✅ / ❌ |
| No regressions to existing portfolio/agent/news tabs | Manual QA pass |

---

## 6. Phase 2 Preview (Not Scoping Now)

- **Alpaca MCP v2 integration** — replace manual alpaca-py calls with official MCP server (61 endpoints, auto-sync with Alpaca spec changes)
- **Financial Datasets MCP** — replace yfinance fundamentals for EPS/revenue screening (more reliable, SEC-sourced)
- **QuantStats HTML tearsheet** — generate full HTML report on each monthly rebalance; commit to `data/tearsheet.html`; link from dashboard
- **Notification system** — GitHub Actions sends webhook/email when agent flags a position or regime changes
- **Finviz screener integration** — `finvizfinance` library as a secondary data source for valuation filters

## 7. Phase 3 Preview (Not Scoping Now)

- **Live trading toggle** — switch from paper to live Alpaca account via env var; circuit breakers required
- **Multi-agent orchestration** — specialist agents: screener agent, risk agent, execution agent, each with narrow scope
- **TradingView MCP** — add RSI/MACD/Bollinger signals to screening filter
- **Real-time WebSocket feed** — replace static JSON polling with Alpaca WebSocket stream for live price updates in dashboard
- **Options overlays** — protective puts on holdings during bear regime

---

*End of PRD — Phase 1 ready for implementation upon approval.*
