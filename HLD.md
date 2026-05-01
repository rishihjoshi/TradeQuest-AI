# TradeQuest AI — High-Level Design

**Version:** 2.0  
**Date:** May 2026  
**Status:** Production (Paper Trading)

---

## 1. Purpose

TradeQuest AI is an autonomous momentum trading system that makes daily portfolio decisions using a Claude AI agent, executes them against an Alpaca paper-trading account, and publishes a real-time dashboard as a Progressive Web App on GitHub Pages — all without any dedicated server infrastructure.

**Objectives:**
- Beat S&P 500 on a risk-adjusted basis over rolling 12-month periods (+3–7% annual excess return)
- Demonstrate an end-to-end autonomous AI trading loop running at zero hosting cost
- Maintain a complete, immutable audit trail of every AI decision in version control

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    EXTERNAL DATA SOURCES                     │
│  Yahoo Finance · Wikipedia S&P 500 · Alpaca Markets News    │
│  Financial Modeling Prep (FMP) · TraderMonty Breadth CSV    │
└────────────────────────┬────────────────────────────────────┘
                         │ (fetched by GitHub Actions bots)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   GITHUB ACTIONS LAYER                       │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  agent.yml  (Mon–Fri 21:30 UTC / 5:30 PM ET)          │  │
│  │  ① bot/update.py   — fetch prices, screen S&P 500,    │  │
│  │                       place Alpaca orders, write       │  │
│  │                       portfolio.json + bars/*.json     │  │
│  │  ② bot/enrich.py   — earnings calendar, macro events, │  │
│  │                       market breadth → enrichment.json │  │
│  │  ③ bot/agent.py    — Claude Sonnet decision agent →   │  │
│  │                       agent_log.json                   │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  news-sentiment.yml  (Mon + Thu 13:30 UTC / 9:30 ET)  │  │
│  │  bot/news_sentiment.py  — Alpaca news + Claude Haiku  │  │
│  │                           sentiment → news.json        │  │
│  └───────────────────────────────────────────────────────┘  │
└────────────────────────┬────────────────────────────────────┘
                         │ git commit + push
                         ▼
┌─────────────────────────────────────────────────────────────┐
│             GIT REPOSITORY  (source of truth)               │
│                                                             │
│  data/portfolio.json     Holdings, trades, equity curve     │
│  data/agent_log.json     AI decisions (last 90 runs)        │
│  data/news.json          Sentiment-tagged articles          │
│  data/enrichment.json    Earnings + macro + breadth         │
│  data/symbols.json       Full S&P 500 universe (search)     │
│  data/bars/{SYM}.json    1-year daily closes per holding    │
└────────────────────────┬────────────────────────────────────┘
                         │ GitHub Pages serves static files
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           PWA DASHBOARD  (GitHub Pages / offline-capable)   │
│                                                             │
│  ◆ Portfolio   Equity curve · stat cards · holdings list    │
│  ⚡ Agent      AI run log · momentum heatmap · history      │
│  📰 News       Sentiment-tagged articles per holding        │
│  📋 Orders     Open + closed orders · activity feed         │
│                                                             │
│  Service worker: cache-first shell, network-first data      │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Key Components

### 3.1 Portfolio Updater — `bot/update.py`

Runs first in the daily pipeline. Responsibilities:

- **Universe fetch:** scrapes S&P 500 tickers from Wikipedia; falls back to 33-ticker list on failure
- **Price data:** downloads 13 months of daily OHLCV from Yahoo Finance for all ~500 tickers
- **Screening pipeline:** applies the 4-layer momentum strategy filters (see §5)
- **Alpaca mode:** when credentials present, reads live paper positions and places rebalance orders under AI + risk-limit approval
- **Simulation mode:** when credentials absent, reconciles positions and cash against screened candidates
- **Static data export:** writes `data/symbols.json` (search index) and `data/bars/{SYM}.json` (chart data per holding)

### 3.2 Enrichment Fetcher — `bot/enrich.py`

Runs second, populates context for the AI agent:

- **Earnings calendar:** FMP API — surfaces upcoming earnings for current holdings within 7 days
- **Macro events:** FMP API — high-impact US events (FOMC, CPI, NFP, GDP, PCE) within 14 days
- **Market breadth:** TraderMonty public CSV — % of S&P 500 above 200-day MA, 8MA vs 200MA trend

### 3.3 AI Agent — `bot/agent.py`

Core decision engine powered by Claude Sonnet:

- **Input:** strategy document (prompt-cached), portfolio state, enrichment context, last run history
- **Output:** structured JSON with holdings decisions (HOLD/SELL/BUY/WATCH), regime assessment, cash action
- **Run types:**
  - `day_end` — post-close sell-rule checks and definitive decisions (daily)
  - `monthly` — full rebalance plan (1st of each month, same schedule)
- **Prompt caching:** `STRATEGY.md` sent with `cache_control: ephemeral` — saves ~2,000 tokens per run

### 3.4 News Sentiment — `bot/news_sentiment.py`

Runs independently twice a week (Monday + Thursday):

- Fetches up to 30 recent articles from Alpaca News API for current holdings
- Classifies each as `bull / bear / neutral` using Claude Haiku (fast, low-cost)
- Writes sentiment results to `data/news.json`

### 3.5 PWA Dashboard — `index.html` + `app.js` + `style.css`

Single-page app served statically from GitHub Pages:

- **No backend:** reads `data/*.json` files directly via `fetch()`
- **Four tabs:** Portfolio, Agent, News, Orders
- **Offline support:** service worker caches shell (cache-first) and data files (network-first with stale fallback)
- **Installable:** full PWA manifest with icons, standalone display mode

---

## 4. Data Flow

### Daily Pipeline (21:30 UTC, Mon–Fri)

```
GitHub Actions trigger (cron)
  │
  ├─ update.py
  │     ├─ fetch S&P 500 universe (Wikipedia)
  │     ├─ download prices (Yahoo Finance, ~500 tickers)
  │     ├─ screen candidates (momentum → quality → valuation → risk)
  │     ├─ read Alpaca live positions + account state
  │     ├─ compute rebalance orders (delta vs top-N screened)
  │     ├─ gate orders through risk limits + AI approval
  │     ├─ place approved orders via Alpaca paper API
  │     └─ write portfolio.json, symbols.json, bars/*.json
  │
  ├─ enrich.py
  │     ├─ fetch earnings calendar (FMP)
  │     ├─ fetch macro events (FMP)
  │     ├─ fetch market breadth (TraderMonty CSV)
  │     └─ write enrichment.json
  │
  ├─ agent.py
  │     ├─ read strategy.md (prompt-cached)
  │     ├─ read portfolio.json + enrichment.json
  │     ├─ call Claude Sonnet → structured JSON decisions
  │     └─ write agent_log.json
  │
  └─ git commit + push
        └─ GitHub Pages redeploys (static files updated)
```

### News Pipeline (13:30 UTC, Mon + Thu)

```
GitHub Actions trigger (cron)
  │
  └─ news_sentiment.py
        ├─ read holdings from portfolio.json
        ├─ fetch articles from Alpaca News API
        ├─ classify each article via Claude Haiku
        └─ write news.json → git commit + push
```

### Frontend Read Path

```
User opens PWA
  ├─ service worker: serve shell from cache (instant)
  ├─ app.js: fetch portfolio.json (network-first, 15s timeout)
  ├─ app.js: fetch agent_log.json (network-first)
  ├─ (News tab) fetch news.json (network-first, 5-min memory TTL)
  └─ auto-refresh: 30s during market hours, 5 min otherwise
```

---

## 5. Trading Strategy Summary

The portfolio uses a 4-layer filter pipeline applied to the full S&P 500 universe:

| Layer | Filter | Threshold |
|-------|--------|-----------|
| 1. Momentum | Combined 6M + 12M return rank | Top 30% of universe |
| 2. Quality | EPS growth + revenue growth | EPS > 10%, Rev > 8% |
| 3. Valuation | Forward P/E | < 40, or top 70% by sector |
| 4. Risk | 30-day annualised volatility | Below 90th percentile |

**Portfolio construction:** 15–20 equal-weight positions, monthly rebalance, cash buffer sized to market regime (5% bull / 25% sideways / 50% bear).

**Sell rules:** any one triggers an exit — momentum rank > 40%, price < 50-day MA, EPS growth < 5% for 2 consecutive quarters, or position up > 60% in < 60 days (parabolic blow-off only).

**Regime detection:** SPY vs 200-day MA + 30-day realised vol + market breadth (% of S&P above 200-MA). Two-of-three signals must agree to switch regime.

---

## 6. Technology Stack

| Layer | Technology |
|-------|-----------|
| Compute | GitHub Actions (ubuntu-latest, Python 3.12) |
| Hosting | GitHub Pages (static, no server) |
| AI decisions | Anthropic Claude Sonnet 4.6 (with prompt caching) |
| News sentiment | Anthropic Claude Haiku 4.5 |
| Paper trading | Alpaca Markets paper trading API |
| Market data | Yahoo Finance via yfinance ≥1.0 |
| Enrichment data | Financial Modeling Prep (FMP) free tier |
| Market breadth | TraderMonty public CSV (no API key) |
| Frontend charting | Chart.js 4.4 (SRI-pinned CDN, pre-cached) |
| Data store | Git repository (JSON files) |
| Secret management | GitHub Actions Secrets (never in repo) |
| Offline support | Service Worker (Cache API) |

---

## 7. Security Design

- **No secrets in code:** all API keys (`ANTHROPIC_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `FMP_API_KEY`) are GitHub Actions Secrets, injected as env vars only at runtime
- **Paper-trading guard:** `verify_paper_url()` crashes the process if `ALPACA_BASE_URL` does not contain `"paper"` — prevents accidental live trades
- **Prompt injection defence:** all external data passed to Claude (news headlines, earnings events, agent history) is sanitised through `_safe()` — truncated, newlines stripped, Markdown chars removed
- **XSS defence:** frontend uses a `sanitize()` helper before every `innerHTML` assignment; CSP header blocks inline scripts and non-allowlisted sources
- **Risk limits enforced in code** (not by AI): hard limits on sell value (≤ 30% of portfolio per run), orders per run (≤ 5), position size (≤ 8%), and cash floor (≥ 5%) — these cannot be overridden by the agent
- **Workflow injection:** `run_type` input is allowlisted against `day_end | monthly` before being written to `$GITHUB_ENV`

---

## 8. Failure Modes and Mitigations

| Failure | Effect | Mitigation |
|---------|--------|-----------|
| Yahoo Finance 403 / timeout | update.py crashes, no portfolio update | Retry is implicit (next scheduled run); Wikipedia fallback for universe |
| Claude API timeout / parse failure | Soft: fallback JSON written to log; no trades placed | Structured fallback result; max_tokens guard at 8,000 |
| FMP API quota exhausted | Enrichment fields empty; agent runs without calendar context | `if not api_key: return []` graceful skip |
| TraderMonty CSV unreachable | Breadth field null in enrichment | `try/except` returns `None`; agent prompt omits breadth section |
| Git push conflict (parallel runs) | Push fails; current run's data not published | Cron stagger prevents overlap; next run overwrites cleanly |
| Alpaca API auth failure | Orders not placed; portfolio read fails | Alpaca mode skipped; falls through to simulation output |

---

## 9. Constraints and Limitations

1. **Data latency:** yfinance prices are end-of-day; dashboard shows "DELAYED" for intraday moves
2. **No backtesting:** strategy is validated on live paper trades only, not historical simulation
3. **Survivorship bias:** S&P 500 universe excludes failed companies by construction
4. **Momentum crash risk:** the factor crashes hard approximately once per decade; the bear regime target (50% cash) reduces but does not eliminate exposure
5. **GitHub Actions minutes:** free tier provides 2,000 minutes/month; each run consumes ~3–5 minutes
6. **yfinance reliability:** fundamental data (EPS, revenue growth) can be stale or missing; the bot applies relaxed valuation rules when data is absent
