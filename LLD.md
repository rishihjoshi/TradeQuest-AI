# TradeQuest AI — Low-Level Design

**Version:** 2.0  
**Date:** May 2026  
**References:** HLD.md, STRATEGY.md, PRD.md

---

## 1. File and Directory Structure

```
TradeQuest-AI/
├── .github/
│   └── workflows/
│       ├── agent.yml            # Daily portfolio + AI agent pipeline
│       └── news-sentiment.yml   # Twice-weekly news sentiment pipeline
├── bot/
│   ├── update.py                # Portfolio updater (step 1 of agent.yml)
│   ├── enrich.py                # Enrichment fetcher (step 2 of agent.yml)
│   ├── agent.py                 # Claude AI decision agent (step 3 of agent.yml)
│   ├── news_sentiment.py        # News sentiment analyser (news-sentiment.yml)
│   └── requirements.txt         # Python dependencies
├── data/
│   ├── portfolio.json           # Portfolio state — written by update.py
│   ├── agent_log.json           # AI decision history — written by agent.py
│   ├── news.json                # News articles with sentiment — written by news_sentiment.py
│   ├── enrichment.json          # Calendar + breadth — written by enrich.py
│   ├── symbols.json             # S&P 500 universe for search — written by update.py
│   └── bars/
│       └── {SYMBOL}.json        # 1-year daily closes per holding — written by update.py
├── icons/
│   ├── icon.svg                 # PWA icon (any purpose)
│   └── icon-maskable.svg        # PWA icon (maskable — content in 80% safe zone)
├── index.html                   # PWA shell — single HTML file
├── app.js                       # Frontend single-page app (~1,900 lines)
├── style.css                    # All styles including responsive breakpoints
├── manifest.json                # PWA web app manifest
├── sw.js                        # Service worker — caching strategy
├── STRATEGY.md                  # Prompt-cached strategy document for Claude
├── HLD.md                       # High-level design document
└── LLD.md                       # This document
```

---

## 2. Data Schemas

### 2.1 `data/portfolio.json`

Written by `bot/update.py` on every agent workflow run.

```json
{
  "meta": {
    "strategy":        "TradeQuest AI Momentum Strategy v2.0",
    "universe":        "S&P 500",
    "account_name":    "TradeQuest Paper",
    "mode":            "alpaca | simulation",
    "initial_capital": 100000,
    "last_rebalance":  "YYYY-MM-DD",
    "next_rebalance":  "YYYY-MM-DD",
    "market_regime":   "bull | sideways | bear",
    "regime_confidence": 0.0,
    "regime_indicators": {
      "vix":            0.0,
      "ma200_trend":    "positive | negative",
      "breadth_pct":    0.0,
      "description":    "string"
    },
    "equity_exposure": 0.95,
    "cash_target":     0.05
  },
  "summary": {
    "initial_capital":   100000,
    "portfolio_value":   0.0,
    "cash":              0.0,
    "total_pnl":         0.0,
    "total_pnl_pct":     0.0,
    "unrealised_pnl":    0.0,
    "realised_pnl":      0.0,
    "win_rate":          0.0,
    "wins":              0,
    "losses":            0,
    "sharpe_ratio":      0.0,
    "max_drawdown":      0.0,
    "cash_pct":          0.0
  },
  "filter_status": {
    "momentum":  { "label": "Momentum",  "description": "...", "passing": 0, "total": 0, "threshold": "Top 30%" },
    "quality":   { "label": "Quality",   "description": "...", "passing": 0, "total": 0, "threshold": "EPS >10% & Rev >8%" },
    "valuation": { "label": "Valuation", "description": "...", "passing": 0, "total": 0, "threshold": "Fwd P/E <40" },
    "risk":      { "label": "Risk",      "description": "...", "passing": 0, "total": 0, "threshold": "Vol < 90th pct" }
  },
  "equity_curve": [
    { "date": "YYYY-MM-DD", "value": 0.0, "label": "Mon Jan 1" }
  ],
  "holdings": [ /* see §2.2 */ ],
  "trades":   [ /* see §2.3 */ ]
}
```

### 2.2 Holding Object (inside `portfolio.json → holdings[]`)

```json
{
  "symbol":         "TICKER",
  "name":           "Company Name",
  "sector":         "Technology",
  "shares":         1,
  "avg_cost":       0.0,
  "current_price":  0.0,
  "market_value":   0.0,
  "unrealised_pnl": 0.0,
  "pnl_pct":        0.0,
  "weight":         0.0,
  "momentum_rank":  1,
  "momentum_6m":    0.0,
  "momentum_12m":   0.0,
  "vol_30d":        0.0,
  "ma_50d":         0.0,
  "eps_growth":     0.0,
  "revenue_growth": 0.0,
  "forward_pe":     0.0,
  "entry_date":     "YYYY-MM-DD",
  "days_held":      0
}
```

### 2.3 Trade Object (inside `portfolio.json → trades[]`, max 50 entries)

```json
{
  "id":     "T001",
  "date":   "YYYY-MM-DD",
  "action": "BUY | SELL",
  "symbol": "TICKER",
  "shares": 1,
  "price":  0.0,
  "value":  0.0,
  "pnl":    0.0,
  "reason": "string"
}
```

### 2.4 `data/agent_log.json`

Written by `bot/agent.py`. Rolling 90-entry window (≈3 months at daily runs).

```json
{
  "last_run":  "ISO-8601 timestamp",
  "last_type": "day_end | monthly",
  "runs": [
    {
      "id":               "RUN-YYYYMMDD-HHMM",
      "timestamp":        "ISO-8601",
      "type":             "day_end | monthly",
      "model":            "claude-sonnet-4-6",
      "assessment":       "2–3 sentence overall assessment",
      "regime":           "bull | sideways | bear",
      "regime_confidence": 0.0,
      "flags":            [ "SYMBOL: reason" ],
      "decisions": [
        {
          "action":        "HOLD | SELL | BUY | WATCH",
          "symbol":        "TICKER",
          "reason":        "specific rule or rationale",
          "rule_triggered": "momentum_decay | trend_break | quality_drop | profit_take | new_entry | null",
          "urgency":       "immediate | next_open | next_rebalance"
        }
      ],
      "cash_action":    "increase | decrease | maintain",
      "cash_rationale": "string",
      "summary":        "one-sentence headline",
      "usage": {
        "input_tokens":        0,
        "output_tokens":       0,
        "cache_read_tokens":   0,
        "cache_write_tokens":  0
      }
    }
  ]
}
```

### 2.5 `data/news.json`

Written by `bot/news_sentiment.py` twice weekly.

```json
{
  "generated_at":  "ISO-8601",
  "article_count": 0,
  "articles": [
    {
      "id":         "string",
      "headline":   "string (max 200 chars, sanitised)",
      "summary":    "string (max 500 chars, sanitised)",
      "url":        "https://...",
      "author":     "string",
      "source":     "string",
      "symbols":    ["TICKER"],
      "created_at": "ISO-8601",
      "sentiment":  "bull | bear | neutral",
      "confidence": 0.0,
      "reason":     "string (max 200 chars)"
    }
  ]
}
```

### 2.6 `data/enrichment.json`

Written by `bot/enrich.py` before each agent run.

```json
{
  "generated_at": "YYYY-MM-DD",
  "earnings_this_week": [
    {
      "symbol":       "TICKER",
      "date":         "YYYY-MM-DD",
      "timing":       "BMO | AMC | TAS",
      "eps_estimate": 0.0
    }
  ],
  "macro_events_14d": [
    {
      "event":    "string (max 100 chars, sanitised)",
      "date":     "YYYY-MM-DD",
      "previous": "string | null",
      "estimate": "string | null"
    }
  ],
  "market_breadth": {
    "date":              "YYYY-MM-DD",
    "breadth_raw":       0.0,
    "breadth_8ma":       0.0,
    "breadth_200ma":     0.0,
    "pct_above_200ma":   "62.3%",
    "trend_above_200ma": true,
    "interpretation":    "HEALTHY | NARROWING | WEAK"
  }
}
```

### 2.7 `data/symbols.json`

Written by `update.py → write_symbols_json()`. Used by the frontend search.

```json
[
  { "symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology" }
]
```

### 2.8 `data/bars/{SYMBOL}.json`

Written by `update.py → write_holdings_bars()`. 1 file per holding, up to 365 daily bars.

```json
[
  { "t": "2025-01-02", "c": 185.50 }
]
```

---

## 3. Bot Module Design

### 3.1 `bot/update.py` — Execution Flow

```
main()
 ├─ load existing portfolio.json (or initialise empty state)
 ├─ get_sp500_universe()            → list[{symbol, name, sector}]
 │    ├─ scrape Wikipedia table via requests + pd.read_html
 │    └─ fallback: 33-ticker hardcoded list on any error
 ├─ fetch_prices(tickers, "13mo")   → pd.DataFrame  (columns = tickers, index = dates)
 │    └─ yf.download(tickers, period, auto_adjust=True, threads=True)
 │         MultiIndex normalised: raw["Close"] → flat DataFrame
 ├─ calc_momentum(prices)           → (mom6: Series, mom12: Series)
 │    └─ (price[-1] / price[-126] - 1), (price[-1] / price[-252] - 1)
 ├─ calc_vol(prices, window=30)     → vol30: Series
 │    └─ pct_change().tail(30).std() * sqrt(252)
 ├─ Screen candidates:
 │    vol_90th = vol30.quantile(0.90)
 │    mom_score = (mom6.rank(pct) + mom12.rank(pct)) / 2
 │    candidates = top-CANDIDATES_CAP(60) by mom_score where vol < vol_90th
 ├─ fetch_fundamentals(candidates)  → dict[symbol, {name, sector, eps_growth, ...}]
 │    └─ yf.Ticker(sym).info  (sequential; errors silently skipped per symbol)
 ├─ Screen: quality (EPS>10%, Rev>8%) AND valuation (Fwd P/E<40) pass
 ├─ detect_regime(spy)              → dict with market_regime, confidence, indicators
 │
 ├─ [Alpaca mode — credentials present]
 │    ├─ alpaca_read_state(client)  → {positions, orders, cash, portfolio_value}
 │    ├─ alpaca_positions_to_holdings(positions, fundamentals, screened_ranks, vol30)
 │    ├─ compute delta: current_syms Δ target_syms → to_sell_syms, to_buy_syms
 │    ├─ load_agent_approvals()     → {SELL: set, BUY: set}  (reads agent_log.json)
 │    ├─ apply_risk_limits(raw_sells, raw_buys, pv, cash, approvals, price_map)
 │    │    Rules applied in order:
 │    │    1. Gate sells: agent must have approved symbol with immediate/next_open urgency
 │    │    2. Dollar sell cap: cumulative sell value ≤ 30% of portfolio per run
 │    │    3. Order count cap: total (sells + buys) ≤ 5 per run
 │    │    4. Cash floor: do not buy if cash would drop below 5% of portfolio
 │    │    5. Position size cap: each buy quantity ≤ 8% of portfolio value
 │    ├─ alpaca_place_orders(client, to_sell, to_buy, pv, cash)
 │    ├─ time.sleep(3)  → re-fetch refreshed state post-orders
 │    └─ handle_manual_order(client)   (ORDER_SYMBOL env var from workflow_dispatch)
 │
 ├─ [Simulation mode — no credentials]
 │    └─ reconcile(screened, fundamentals, existing_holdings, cash, current_pv)
 │         ├─ sell holdings not in new top-N → record sell trades
 │         └─ buy new top-N entrants proportionally from available cash
 │
 ├─ compute_summary(holdings, cash, existing_summary, trades)
 ├─ update_equity_curve(existing_curve, portfolio_value)  → append today's point
 ├─ write portfolio.json
 ├─ write_symbols_json(universe, data_dir)   → data/symbols.json
 └─ write_holdings_bars(new_holdings, prices_df, data_dir)  → data/bars/{SYM}.json
```

**Key constants:**

| Constant | Value | Purpose |
|----------|-------|---------|
| `INITIAL_CAPITAL` | 100,000 | Starting portfolio value |
| `TARGET_N` | 17 | Target position count |
| `CANDIDATES_CAP` | 60 | Max symbols for fundamental fetch |
| `MAX_ORDERS_PER_RUN` | 5 | Hard order count limit |
| `MAX_SELL_VALUE_PCT` | 0.30 | Max liquidation per run (30%) |
| `CASH_FLOOR_PCT` | 0.05 | Minimum cash reserve (5%) |
| `MAX_POSITION_PCT` | 0.08 | Max single position (8%) |

### 3.2 `bot/enrich.py` — Execution Flow

```
main()
 ├─ load_holding_symbols()          → list of current ticker symbols
 ├─ [ThreadPoolExecutor, max_workers=3]
 │    ├─ fetch_earnings_for_holdings(symbols, api_key)
 │    │    └─ GET /stable/earnings-calendar?from=today&to=today+7d
 │    │         filter: symbol in holdings, max 7 events
 │    ├─ fetch_macro_events(api_key)
 │    │    └─ GET /stable/economics-calendar?from=today&to=today+14d
 │    │         filter: country=US, impact=high, event in HIGH_IMPACT_EVENTS allowlist
 │    │         max 7 events
 │    └─ fetch_breadth_score()
 │         └─ GET TraderMonty CSV → last row → Breadth_Index, _8MA, _200MA
 └─ write enrichment.json (atomic: tempfile → shutil.move)
```

**Prompt injection defence in enrich.py:** all FMP text fields truncated to `PROMPT_FIELD_MAX = 100` chars, newlines stripped, before being written to `enrichment.json`.

### 3.3 `bot/agent.py` — Execution Flow

```
main()
 ├─ load_strategy()      → STRATEGY.md text (~7,000 tokens)
 ├─ load_portfolio()     → portfolio.json (equity_curve excluded from prompt)
 ├─ load_log()           → agent_log.json
 ├─ load_enrichment()    → enrichment.json
 ├─ recent_history = log["runs"][:1]   (last 1 run for day-over-day continuity)
 │
 └─ run_agent(run_type, portfolio, strategy, enrichment, recent_history)
      ├─ Build system message:
      │    { type: "text", text: "You are TradeQuest AI ... STRATEGY.md",
      │      cache_control: { type: "ephemeral" } }   ← 5-min prompt cache
      ├─ Build user message:
      │    TASK_PROMPT  (day_end | monthly | day_start)
      │    + enrichment_section  (earnings, macro, breadth)
      │    + history_section     (last run regime + decisions)
      │    + portfolio JSON (slim — equity_curve excluded)
      │    + RESPONSE_SCHEMA    (strict JSON format instruction)
      ├─ client.messages.create(model, max_tokens=8000, ...)
      ├─ Check stop_reason == "max_tokens" → print WARNING
      ├─ Parse: JSONDecoder.raw_decode(raw, raw.find("{"))
      │    On failure: store raw text as assessment, mark summary as "parse failed"
      └─ write_log(log, run_type, result, usage)
           └─ insert at index 0, trim to 90 entries, write agent_log.json
```

**Token budget (typical day_end with 17 holdings):**

| Section | Approx tokens |
|---------|--------------|
| System (STRATEGY.md) — cache hit | ~2,000 (charged once per 5 min) |
| Task prompt | ~300 |
| Enrichment section | ~200 |
| History section | ~150 |
| Portfolio JSON (17 holdings, no curve) | ~2,500 |
| Response schema | ~350 |
| **Total input** | **~5,500** |
| Typical output | ~2,000–4,000 |

### 3.4 `bot/news_sentiment.py` — Execution Flow

```
main()
 ├─ get_holdings()         → list of symbols from portfolio.json
 ├─ fetch_alpaca_news(symbols)
 │    └─ GET https://data.alpaca.markets/v1beta1/news
 │         headers: APCA-API-KEY-ID + APCA-API-SECRET-KEY
 │         params: limit=30, sort=desc, symbols=<up to 20>
 └─ for each article (max 30):
      ├─ analyze_sentiment(client, headline, summary)
      │    └─ claude-haiku-4-5 / max_tokens=120
      │         prompt: headline (≤200 chars) + summary (≤400 chars) → {sentiment, confidence, reason}
      │         parse: JSON.parse(raw[first_brace:last_brace+1])
      └─ time.sleep(0.5)   (burst throttle avoidance)
 └─ write news.json
```

**Model choice rationale:** Claude Haiku is used (not Sonnet) for news sentiment because each article requires only a single 120-token output — ~50x cheaper than Sonnet at comparable accuracy for simple classification tasks.

---

## 4. GitHub Actions Workflow Design

### 4.1 `agent.yml` — Daily Agent Pipeline

```yaml
trigger:   cron '30 21 * * 1-5'   # 21:30 UTC = 5:30 PM ET Mon–Fri
           workflow_dispatch (run_type: day_end | monthly)

permissions: contents: write       # Required to push data/*.json commits

steps:
  1. checkout
  2. setup-python 3.12 with pip cache
  3. pip install -r bot/requirements.txt
  4. Detect run type:
       - workflow_dispatch input allowlisted against ^(day_end|monthly)$
       - day=01 → monthly, else → day_end
       - Written to $GITHUB_ENV as RUN_TYPE
  5. python bot/update.py    (env: ALPACA_* secrets)
  6. python bot/enrich.py    (env: FMP_API_KEY secret)
  7. python bot/agent.py     (env: RUN_TYPE, ANTHROPIC_API_KEY, ALPACA_* secrets)
  8. git add data/portfolio.json data/agent_log.json data/enrichment.json
     git commit -m "agent[{RUN_TYPE}]: {date}"   (if staged changes exist)
     git push -u origin HEAD
```

**Note:** `data/symbols.json` and `data/bars/*.json` are NOT committed in this workflow. They are updated by `update.py` and pushed separately, or seeded manually. The agent commit only tracks the three core data files.

### 4.2 `news-sentiment.yml` — News Pipeline

```yaml
trigger:   cron '30 13 * * 1'   # Monday  09:30 ET
           cron '30 13 * * 4'   # Thursday 09:30 ET
           workflow_dispatch

steps:
  1–4. Same checkout/setup/install as agent.yml
  5.   python bot/news_sentiment.py   (env: ANTHROPIC_API_KEY, ALPACA_* secrets)
  6.   git add data/news.json
       git commit -m "news: sentiment update {date} [skip ci]"   (if changed)
       git push
```

**`[skip ci]` tag** prevents the news commit from re-triggering any CI workflows.

---

## 5. Frontend Architecture — `app.js`

The frontend is a single JavaScript file (~1,900 lines) with no build step and no framework.

### 5.1 Module Structure

```
Constants + Helpers
  ├─ fmt$, fmtK, fmtPct, fmtDate   (number/date formatters)
  ├─ sanitize(str)                  (XSS defence — all innerHTML goes through this)
  ├─ colorClass(v) / signedHtml()   (profit/loss CSS class helpers)
  └─ isMarketHours()                (America/New_York timezone detection)

State modules (pure objects)
  ├─ AgentHistory     (localStorage — non-sensitive run metadata, max 10 entries)
  └─ tryRecoverRun()  (attempts to extract usable fields from parse-failed agent runs)

Renderers (canvas / SVG — no library)
  ├─ drawSparkline(canvas, holding)  (8-point deterministic sparkline from momentum)
  └─ buildSectorDonut(holdings)      (SVG donut chart from sector weights)

Main App IIFE
  ├─ State: portfolio, agentLog, newsCache, symbolChart, equityChart
  ├─ init()
  │    ├─ setupInstallPrompt()       (PWA A2HS — Android beforeinstallprompt + iOS hint)
  │    ├─ setupTabBar()
  │    ├─ setupSearch()
  │    ├─ loadData()                 (first fetch + start refresh loop)
  │    └─ setupTradeTicket()
  │
  ├─ loadData()
  │    ├─ fetchWithTimeout(DATA_URL, 15s)
  │    ├─ fetchWithTimeout(AGENT_LOG_URL, 15s)
  │    ├─ render(portfolio, agentLog)
  │    └─ schedule next refresh (30s market hours / 5min otherwise)
  │
  ├─ render(portfolio, agentLog)
  │    ├─ renderHeader(meta)              update regime badge, last-updated, rebalance date
  │    ├─ renderStatCards(summary)        portfolio value, P&L, win rate, Sharpe, drawdown
  │    ├─ renderCatalystsBanner(enrich)   upcoming earnings chip strip
  │    ├─ renderEquityChart(curve)        Chart.js line chart, gold colour
  │    ├─ renderHoldings(holdings)        Robinhood-style cards with sparklines + expand
  │    ├─ renderTrades(trades)            filterable/sortable table
  │    ├─ renderStrategy(filter_status)   4-filter progress bars + regime panel
  │    ├─ renderMomentumHeatmap(holdings) colour-coded rank grid (green/yellow/red)
  │    └─ renderAgentLog(runs)            collapsible cards, newest first
  │
  ├─ Symbol Screen
  │    ├─ openSymbolScreen(symbol)        full-screen price + chart view
  │    ├─ loadSymbolQuote(symbol)         reads from portfolio holdings (no live API)
  │    └─ loadSymbolBars(symbol, tf)      fetches data/bars/{SYM}.json + slices by TF
  │
  ├─ Trade Ticket (2-step modal)
  │    ├─ showTradeTicket(symbol, side)   step 1: form (qty, type, TIF)
  │    ├─ updateTradePreview()            est. cost, buying power impact
  │    └─ submitOrder()                   shows GitHub Actions workflow_dispatch deep-link
  │                                        (no direct API call — order placed via Actions)
  │
  ├─ Orders Tab
  │    ├─ loadOrdersIfStale()             maps trades[] → order shape
  │    └─ renderOrdersTab(open, closed)
  │
  └─ News Tab
       ├─ loadNewsIfStale()               5-min memory TTL (NEWS_TTL_MS)
       └─ renderNewsTab(articles, page)   paginated, 25 articles per page
```

### 5.2 Refresh Strategy

```
isMarketHours()  →  true   →  refresh every 30 seconds
                 →  false  →  refresh every 5 minutes
```

"Market hours" defined as 9:30–16:00 ET on weekdays. Refresh fetches both `portfolio.json` and `agent_log.json`. The news tab has a separate 5-minute memory TTL to avoid redundant fetches when switching tabs.

### 5.3 Equity Chart (Chart.js)

- Type: `line` with `tension: 0.3` (smooth curve)
- Dataset: `equity_curve[]` from `portfolio.json`
- X axis: `{ type: 'category' }` — dates as string labels
- Y axis: dollar-formatted ticks, grid lines at `#222`
- Colour: gold (`#D4AF37`) with gradient fill
- Tooltip: shows date + value formatted as `$XX,XXX`
- Responsive: `maintainAspectRatio: false`, fills `.chart-container`

### 5.4 Holdings Cards

Each holding renders as a `.pos-card` with:

- **Collapsed view:** symbol + sector | deterministic sparkline canvas | price + P&L
- **Expanded view:** 6-stat detail grid (avg cost, return %, momentum rank, vol, weight, days held) + Close Position button
- **Sparkline:** 8-point path drawn on `<canvas>` via `drawSparkline()`. Deterministic seed from symbol chars — does not change on re-render. Colour: green if 6M momentum positive, red if negative.
- **Real bar data:** if `data/bars/{SYM}.json` exists, the symbol detail screen chart uses real OHLCV instead of the deterministic sparkline.

---

## 6. Service Worker Caching Strategy — `sw.js`

Cache name: `tradequest-v10` (bump version to force cache invalidation on deploy).

| Resource type | Strategy | Rationale |
|--------------|----------|-----------|
| App shell (HTML, CSS, JS, manifest, icons) | Cache-first | Instant load; shell doesn't change between deploys |
| Chart.js CDN | Network-first → cache fallback | Gets updates; app renders offline |
| `data/*.json` files | Network-first → cache fallback | Always tries fresh data; serves stale on offline |
| All other same-origin | Cache-first | |

**Install:** pre-caches all SHELL URLs including Chart.js CDN. `skipWaiting()` called inside `waitUntil` — SW only activates after cache is fully committed.

**Activate:** deletes all caches whose key ≠ current `CACHE` constant.

---

## 7. Security Controls

### 7.1 Prompt Injection Defence

Every external string passed to Claude is processed through `_safe()`:

```python
def _safe(text: str | None, max_len: int = 100) -> str:
    cleaned = (str(text) if text is not None else "")[:max_len]
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    for ch in ("#", "*", "`", "\\"):
        cleaned = cleaned.replace(ch, "")
    return cleaned
```

Applied to: news headlines, FMP event names, company names, agent history fields.

### 7.2 XSS Defence (Frontend)

```javascript
function sanitize(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#x27;');
}
```

All `innerHTML` assignments use either `sanitize()` on untrusted text, or construct safe HTML via template literals with sanitized fields. News article URLs are validated against `https?://` before rendering as links.

Content Security Policy (index.html):
```
default-src 'none'
script-src  'self' https://cdn.jsdelivr.net
style-src   'self' 'unsafe-inline' https://fonts.googleapis.com
img-src     'self' data:
connect-src 'self'
worker-src  'self'
manifest-src 'self'
```

Chart.js CDN is pinned with SRI hash (`integrity="sha384-..."` on the `<script>` tag).

### 7.3 Trading Risk Limits (Hard-coded in `update.py`)

These constants cannot be overridden by the AI agent or any config file:

```python
MAX_ORDERS_PER_RUN  = 5     # hard order count ceiling
MAX_SELL_VALUE_PCT  = 0.30  # max 30% of portfolio liquidated per run
CASH_FLOOR_PCT      = 0.05  # always keep ≥5% cash
MAX_POSITION_PCT    = 0.08  # no single position > 8% of portfolio value
```

### 7.4 Paper Trading Guard

```python
def verify_paper_url() -> None:
    if "paper" not in ALPACA_BASE_URL.lower():
        raise RuntimeError("SAFETY: not a paper trading endpoint. Refusing.")
```

Called before any order placement. Crashes the process immediately if the URL is not a paper endpoint.

### 7.5 Workflow Injection Guard

`run_type` from `workflow_dispatch` is allowlisted before being written to `$GITHUB_ENV`:

```bash
if [[ "$OVERRIDE" =~ ^(day_end|monthly)$ ]]; then
  echo "RUN_TYPE=$OVERRIDE" >> $GITHUB_ENV
fi
```

---

## 8. Error Handling Summary

| Module | Error | Handling |
|--------|-------|----------|
| `update.py` | Wikipedia 403 | Catch all → fallback 33-ticker list |
| `update.py` | yfinance download failure (per ticker) | Logged to stderr; ticker dropped from analysis |
| `update.py` | `yf.Ticker.info` exception | `try/except Exception` per symbol; symbol excluded from fundamentals |
| `update.py` | `calc_momentum()` with empty DataFrame | `IndexError` propagates → workflow step fails → no commit |
| `enrich.py` | FMP HTTP error or timeout | Returns `[]` for that section; empty enrichment still written |
| `enrich.py` | TraderMonty CSV parse error | `except Exception → return None`; agent prompt omits breadth section |
| `enrich.py` | `tempfile → shutil.move` failure | `os.unlink(tmp); raise` — no partial write |
| `agent.py` | Claude `max_tokens` hit | Warning printed; `raw_decode` extracts partial JSON |
| `agent.py` | JSON parse completely fails | Fallback dict with `summary: "...parse failed"` written to log |
| `agent.py` | `ANTHROPIC_API_KEY` missing | `RuntimeError` — workflow step fails immediately |
| `news_sentiment.py` | Alpaca News API error | `raise_for_status()` propagates → workflow fails |
| `news_sentiment.py` | Claude sentiment parse | Fallback: `{"sentiment": "neutral", "confidence": 0.0}` |

---

## 9. Python Dependencies

```
yfinance>=1.0,<2.0       # price data + fundamentals; pinned to 1.x API
pandas>=3.0,<4.0         # data manipulation; 3.x required for io.StringIO fix
numpy>=1.26,<3.0         # numerical operations; <3.0 for compatibility
lxml>=5.0                # HTML parsing for pd.read_html (Wikipedia scrape)
requests>=2.32           # HTTP for Wikipedia, FMP, TraderMonty, Alpaca News
alpaca-py>=0.29          # Alpaca Markets paper trading SDK
anthropic>=0.40          # Claude API SDK with prompt caching support
```

---

## 10. Environment Variables (GitHub Secrets)

| Variable | Used by | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | `agent.py`, `news_sentiment.py` | Claude API authentication |
| `ALPACA_API_KEY` | `update.py`, `news_sentiment.py` | Alpaca account authentication |
| `ALPACA_SECRET_KEY` | `update.py`, `news_sentiment.py` | Alpaca account authentication |
| `ALPACA_BASE_URL` | `update.py` | Alpaca endpoint (must contain "paper") |
| `ALPACA_ACCOUNT_NAME` | `update.py` | Display label only (not used for auth) |
| `FMP_API_KEY` | `enrich.py` | Financial Modeling Prep API (free tier) |

All variables are injected as environment variables in the GitHub Actions `env:` block. No variable is written to any file or log. `portfolio.json` uses a static display label (`"TradeQuest Paper"`) instead of the real account ID.
