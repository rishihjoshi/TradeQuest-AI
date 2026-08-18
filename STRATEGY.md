# TradeQuest AI — Master Strategy & System Document v3.0

**Version:** 3.0 | **Date:** June 2026 | **Status:** Live — Paper Trading (Alpaca)  
**Supersedes:** STRATEGY.md v2.2, PRD.md, HLD.md, LLD.md (all retired)

---

> **For the AI Agent:** Everything in §1–§8 is your operating rulebook. Read it completely before making any decision. The strategy rules in §3–§6 are non-negotiable constraints, not suggestions.  
> **For the Developer:** §9–§12 contain architecture, schemas, roadmap, and ops reference.

---

## §1 — Objective & Edge

**Mission:** Beat the S&P 500 on a risk-adjusted basis over rolling 12-month periods, targeting +3–7% annual excess return with lower drawdowns.

### Why This Should Work

Three independently documented market anomalies, stacked:

| Anomaly | Source | Expected Edge |
|---|---|---|
| **Momentum premium** | Jegadeesh & Titman (1993) | Top decile stocks outperform bottom by ~10%/yr |
| **Quality premium** | Novy-Marx (2013) | High-profit firms consistently outperform |
| **Regime-aware allocation** | Faber (2007) | Reducing equity in downtrends cuts drawdown 40–60% |

**Live evidence (May–Jun 2026):** Positions ranked 1–10 by momentum averaged +11.8% return; positions ranked 11–18 averaged -4.2%. This validates concentrating in the top tier and discarding the bottom.

### Target Performance

| Metric | Target | Current Status (Jul 2026) |
|---|---|---|
| Annual excess return vs SPY | +3% to +7% | 90-day: **−2.66pp** vs SPY (+0.72% vs +3.38%). Root cause = execution, not selection: a 40–61% cash drag from the throttled Jul-1 rebalance + a runaway JBL short. Fixed in v3.1. |
| Sharpe ratio | > 1.2 | 0.0 (paper trade PnL not fully calculated) |
| Max drawdown | < 15% | 0.09% (low — bull regime) |
| Annual turnover | ~20–35% (v3) | ~430 legs/yr historically — v3 rules now enforce quarterly lock |
| Win rate | 55–65% | 33.3% (stale data — realized PnL not populated in Alpaca mode) |

---

## §2 — Universe

**S&P 500 only** — large-cap, liquid, no micro-caps, no OTC, no ETFs.

- ~500 stocks screened daily via Yahoo Finance price data
- Wikipedia scrape for constituent list; 33-ticker fallback on failure
- Dual-class share dedup: `ISSUER_MAP` keeps highest-momentum ticker per issuer (GOOG/GOOGL → keep GOOG)
- Known limitation: S&P membership screens out failures (survivorship bias)

---

## §3 — The Four Filters (Applied in Order)

### Filter 1 — Momentum (Primary Driver)
- **6-month total return:** must rank in **top 30%** of universe
- **12-month total return:** must rank in **top 30%** of universe
- Combined momentum score = average of both percentile ranks → ranked candidate list
- **Skip-month rule:** use price from 21 trading days ago as "current" (avoids 1-month reversal effect per Jegadeesh & Titman)

### Filter 2 — Quality
- EPS growth > **10%** (trailing 12M or 3-year average)
- Revenue growth > **8%** (trailing 12M)
- If data is `None` (yfinance missing) → stock **fails** the filter conservatively
- Rationale: removes junk momentum — high-beta names with no earnings support that crash hardest

### Filter 3 — Valuation Guard Rail
- Forward P/E < **40**
- **OR** within the top 70% cheapest by sector (prevents excluding entire high-multiple sectors like tech during genuine growth)
- If forward P/E data is `None` → stock **passes** (relaxed fallback when data unavailable)

### Filter 4 — Risk
- 30-day annualised volatility must be **below the 90th percentile** of the universe
- Excludes the most volatile 10% — fat left tails destroy Sharpe ratio on equal-weighted portfolios

### Trend Gate (Entry Only)
- New entries must have price **above their 50-day MA** at time of purchase
- Prevents buying a stock that would immediately trigger the trend-break sell rule
- Lenient when MA data is `None` (missing data → allow entry)

---

## §4 — Portfolio Construction

| Parameter | Value | Notes |
|---|---|---|
| **Target positions** | **10–12** | High-conviction only — top-tier momentum ranks |
| **Weighting** | Equal weight | Avoids estimation error; rebalanced at quarterly |
| **Min position size** | **8%** | Prevents dilution from over-diversification |
| **Position hard cap** | **20%** | Trim to 15% at next quarterly rebalance |
| **Sector cap** | **30%** max per GICS sector | Concentration guard for 10–12 name portfolio |
| **Rebalance frequency** | Quarterly (Jan/Apr/Jul/Oct first trading day) | Full rotation, **completed in one run** (v3.1) |
| **Non-quarterly months** | Flag-only — no new buys, Tier 1 sells only | Feb/Mar/May/Jun/Aug/Sep/Nov/Dec |
| **Cash — Bull regime** | 5% | Fully deployed |
| **Cash — Sideways regime** | 25% | Defensive tilt |
| **Cash — Bear regime** | 50% | Capital preservation |

**Why 10–12 not 15–20:** The momentum premium concentrates in the top decile. Positions ranked 11–18 in the live run dragged down returns by ~16% relative to positions ranked 1–10. Fewer, stronger names held longer captures the full alpha without the management overhead that caused over-trading.

**Equal-weight sizing is dollar-based (v3.1):** each target position is sized to a **dollar** target (`min(deployable/N, 20% cap)`), never to a fixed share count divided by price. Price-driven sizing let a single expensive share (e.g. STX ~$913) become an oversized position while the best-ranked names stayed tiny — in the Jun-2026 book this cost ~1.5pp vs equal weight.

---

## §5 — Sell Rules v3.0 (Tax-Efficient Asymmetric Framework)

### Core Principle
**Sell losing positions fast. Hold winning positions long.**

- Losses exit immediately → tax-loss harvesting, offsets future gains
- Gains defer to 12+ months → long-term capital gains rates (0–20% vs 37% ordinary income)
- Every premature winner exit is a permanent tax cost that cannot be recovered

### Long-Only Invariant (v3.2 — Non-Negotiable)

**The system is long-only. It never sells more shares than it holds and never opens a short —
and when a short does exist, it closes it automatically.**

**Prevention (v3.1):**

- Every SELL is clamped at the execution layer to the quantity actually held; a position that is
  flat or already short is **never re-sold**.
- Orders with a non-positive price are rejected (a bad/zero price fetch must not place a trade).

**Restoration (v3.2) — an invariant needs a way back, not only a way to stop:**

- Any holding with negative shares generates a **COVER** order (buy-to-close the full short) on the
  next run, from `update.py` *and* from the sentinel.
- COVER **bypasses** the quarterly lock, the sector cap, the per-run order budget, the agent-approval
  gate, and the cash floor. Restoring an invariant is not a discretionary trade, and the cover is
  funded by short proceeds already in the account.
- COVER is a first-class agent action (`action: "COVER"`, `rule_triggered: "long_only_breach"`) and
  is exempt from the `day_start` flag-only rule and the non-quarterly `BUY → HOLD` rewrite.
- Any open short sets `long_only_breach: true` in `execution_summary.json` and prints a loud banner.
- `bot/rebalance_trueup.py` remains the manual path for remediating the whole book at once.

**Why:** In Jul 2026, JBL trend-broke below its 50-day MA and was re-sold on every run. With no
held-quantity clamp, Alpaca opened and then extended a naked short to −22 shares (−70% of the book,
unbounded loss risk). v3.1's clamp stopped the short growing but left it **unclosable**: the guard
skips SELL when held ≤ 0, a short symbol is never a buy candidate, and buys are blocked outside
quarterly months. From 2026-08-07 the agent flagged "BUY-TO-COVER MANDATORY" on every run with no
order it could produce, and the position stayed open. A prevention rule without a restoration rule
converts a runaway failure into a frozen one. See `POSTMORTEM.md` Findings 1 and 8.

---

### Structural Rules — Apply to ALL positions regardless of profit/loss

| Rule | Trigger | Action | Notes |
|---|---|---|---|
| **A — Trend break** | Price < 50-day MA, **3 consecutive days** | SELL Tier 1 | Sustained structural break — not a single-day dip |
| **B — Quality failure** | EPS growth negative, **2 consecutive quarters** | SELL Tier 1 | Persistent fundamental failure only |
| **C — Hard cap** | Position weight > **20%** | Flag — TRIM to 15% at next quarterly | Winners growing large = success; only trim at extreme concentration |
| **D — Parabolic blow-off** | Position up > **60% in < 60 days** | SELL **half**, hold remainder | Captures blow-off top; retains continued upside |

### Momentum Decay Rule (Rule E) — Asymmetric by Profit/Loss

| Position status | Momentum rank outside top 30%, confirmed ≥5 days | Action |
|---|---|---|
| **At a loss** (PnL < 0) | Confirmed | **SELL Tier 1** — `urgency: next_open` — tax-loss harvest |
| **At a gain** (PnL ≥ 0), held < 12 months | Confirmed | **WATCH only** — `urgency: next_rebalance` — hold gate active |
| **At a gain** (PnL ≥ 0), held ≥ 12 months | Confirmed | **SELL eligible** at next quarterly rebalance (LTCG treatment) |

### Tier Classification (Required on Every SELL Decision)

```
Tier 1 → urgency: next_open
  • Loss position hitting ANY rule (A, B, C, D, or E)
  • These are tax-loss harvests — execute at next market open

Tier 2 → urgency: next_rebalance
  • Gain position failing ONLY Rule E (momentum decay)
  • Defer to next quarterly rebalance — may qualify for LTCG by then
  • Do NOT issue Tier 2 SELLs in non-quarterly months

Tier 3 → HOLD
  • Gain position passing all structural rules
  • Momentum rank improvement is possible — do not sell into temporary weakness
```

**Agent must state unrealized PnL and entry date in every SELL or WATCH decision.**  
**Agent must never sell a profitable position due to momentum decay alone.**

### Tax-Aware Priority Order
1. Sell positions **at a loss** first (Tier 1) — every realized loss offsets a future gain
2. Only sell profitable positions under a structural rule (A/B/C/D) or after 12-month LTCG threshold
3. **Never** sell a winner to rebalance to equal weight — unequal weights are acceptable; tax drag is permanent

---

## §6 — Market Regime Detection

Three signals, **2-of-3 must agree** before switching regime (prevents whipsawing):

| Signal | Bull | Sideways | Bear |
|---|---|---|---|
| SPY vs 200-day MA | Above | Within 3% | Below |
| 30-day realized vol | < 20% ann. | 20–28% | > 28% |
| Market breadth (% S&P above 200-MA) | > 60% | 40–60% | < 40% |

### Breadth Calibration

| Breadth reading | Implication |
|---|---|
| > 60% | Broad participation — full bull confidence |
| 40–60% | **Narrowing rally** — treat regime with caution; consider sideways even if SPY above 200-MA |
| < 40% | Thin participation — strong bias toward sideways/bear regardless of SPY headline |

A market where SPY is above its 200-day MA but breadth < 40% is a **narrow late-cycle bull** — maintain higher cash than the regime alone suggests. The breadth 8MA crossing below the 200MA is an early warning signal (reduce confidence, not an immediate sell).

### Regime → Cash Allocation

```
BULL     → 95% equity, 5% cash   (fully deployed)
SIDEWAYS → 75% equity, 25% cash  (defensive tilt)
BEAR     → 50% equity, 50% cash  (capital preservation)
```

Regime change triggers cash rebalancing within 3 trading days.

---

## §7 — Agent Decision Framework

### Run Types

| Run | When | Purpose | Trades Allowed |
|---|---|---|---|
| `day_end` | 5:30 PM ET Mon–Fri | Post-close sell-rule checks; definitive decisions | Tier 1 SELLs only (non-quarterly months) |
| `day_start` | 9:00 AM ET Mon–Fri | Pre-market flag check; no trades | None |
| `monthly` | 1st of month, 5:30 PM ET | Full rebalance on quarterly months; flag-only on others | Full (quarterly) or Tier 1 only (non-quarterly) |

### Decision Tree (Execute in Order for Each Holding)

```
STEP 1 — Structural rules (ALL positions, regardless of profit/loss):
  IF price < ma_50d for ≥3 consecutive days           → SELL Tier 1 [Rule A]
  IF eps_growth < 0 for 2 consecutive quarters         → SELL Tier 1 [Rule B]
  IF weight > 20%                                      → flag TRIM to 15% at quarterly [Rule C]
  IF pnl_pct > 60% in <60d                             → SELL half, HOLD remainder [Rule D]

STEP 2 — Momentum decay (Rule E — asymmetric):
  IF momentum_rank outside top 30%, confirmed ≥5 days:
    AND pnl < 0   → SELL Tier 1 (next_open)      ← loss-harvest, exit fast
    AND pnl ≥ 0   → WATCH only (next_rebalance)  ← hold gate — do NOT sell

STEP 3 — Default:
  ELSE → HOLD

STEP 4 — Classify every SELL before issuing:
  sell_tier: "tier1" → urgency: next_open
  sell_tier: "tier2" → urgency: next_rebalance, ONLY in quarterly months
  NEVER issue tier2 in a non-quarterly month (Feb/Mar/May/Jun/Aug/Sep/Nov/Dec)

FOR quarterly rebalance (Jan/Apr/Jul/Oct):
  Screen full S&P 500 → apply all 4 filters → take top 10–12 by momentum score
  Execute all Tier 1 SELLs immediately
  Execute deferred Tier 2 SELLs if still failing filters at this rebalance date
  BUY new top-10-12 entrants not currently held (cash first, then Tier 1 proceeds)
  HOLD profitable positions not in new top-10 (avoid unnecessary gain realization)
  Check sector cap: no sector > 30% after rebalance; trim to comply
  Adjust cash to match regime target

FOR non-quarterly monthly review (Feb/Mar/May/Jun/Aug/Sep/Nov/Dec):
  Run STEPS 1–3 sell rules only
  Issue Tier 1 SELLs (losses hitting any rule)
  Issue WATCH for Tier 2 signals — set urgency: next_rebalance
  BUY orders: NONE — pipeline blocks mid-quarter buys at code level
```

### Earnings Awareness

- Holding with earnings **within 3 days**: flag as WATCH; note the date
- Holding with earnings **within 1 day**: WATCH with urgency `next_open`
- Do **not** sell solely because of upcoming earnings — factor earnings risk into confidence
- If already planning to SELL for another reason, prefer executing **before** earnings

### Persistent-Flag Escalation

- Symbol flagged Tier 1 SELL in 3+ consecutive runs without execution → re-issue with `urgency: next_open`, note consecutive count in reason
- Symbol flagged SELL but position is **at a gain** → escalate to WATCH, do NOT force to `next_open`
- Do NOT issue a BUY for any symbol with an active SELL flag

---

## §8 — Execution Pipeline

### Workflow Schedule

| Workflow | Trigger | Action |
|---|---|---|
| `market-open.yml` | 9:30 AM ET Mon–Fri | Execute `next_open` orders from prior evening's agent run |
| `agent.yml` | 5:30 PM ET Mon–Fri | Sync Alpaca → enrich → Claude decides → commit |
| `sentinel.yml` | 2:30 PM ET Mon–Fri | Automated hard-rule sells (no LLM) |
| `news-sentiment.yml` | 9:30 AM ET Mon + Thu | Claude Haiku news sentiment classification |

**All orders execute at the next market open (9:30 AM ET).** There is no same-session execution path.

### Urgency Values

- `"next_open"` — Execute at next 9:30 AM via `market-open.yml`. Use for all Tier 1 SELLs.
- `"next_rebalance"` — Defer to next monthly/quarterly. Use for Tier 2 WATCH and non-urgent signals.
- `"immediate"` — **Deprecated.** Treated as `next_open` for backward compatibility.

### Non-Quarterly Month Execution Lock (Code-Enforced)

In Feb, Mar, May, Jun, Aug, Sep, Nov, Dec — `update.py` enforces at code level:
- `BUY` orders for **new entrants** → **BLOCKED** (not just the agent prompt — the pipeline rejects them)
- `BUY` orders that **redeploy into existing top-N holdings** → **ALLOWED** (v3.2, see below)
- `COVER` orders → **ALWAYS ALLOWED** (long-only invariant restoration, see §5)
- Tier 2 SELL orders → **BLOCKED** (profitable positions with only momentum decay)
- Tier 1 SELL orders → **ALLOWED** (loss positions with any rule trigger)

This is enforced by `is_quarterly_month()` in `update.py`, not just agent instructions.

**Redeployment carve-out (v3.2 — fixes the ratchet).** Blocking *all* buys while allowing Tier 1
sells made the book able only to shrink for up to three months at a time. Aug 2026 is the clean
demonstration: 4 sells (MNST, HUM, FRT, CSX) and 0 buys, with the proceeds idle in a rising market.
Combined with the profit gate — losers sold now, winners deferred — that is a systematic
*sell-losers / never-redeploy* ratchet, the inverse of a momentum strategy.

So in any month, when **deployable** cash exceeds `CASH_FLOOR_PCT + CASH_DEPLOY_BAND` (5% + 3% = 8%),
positions already inside the top-N are topped up toward their equal-weight dollar target. New
entrants still wait for the quarterly. This preserves the v2.2 intent (no discretionary new bets
between rebalances) without letting the book bleed exposure.

### Churn Dampers (v3.2)

The screen re-ranks **daily** while the strategy rebalances **quarterly**. Recomputing exits from a
fresh top-N every run churned names oscillating around the rank boundary — FFIV was bought
2026-07-28 at \$412.75, sold 07-30 at \$389.88 (−\$45.74) and re-bought 07-31 at \$401.62; CSX and
WST did the same. Three dampers now apply to **rank-based exits only** (structural Rules A–E and
sentinel exits are unaffected):

| Damper | Constant | Behaviour |
|---|---|---|
| **Rank hysteresis** | `EXIT_RANK_MULTIPLE = 1.5` | Enter at rank ≤ `TARGET_N` (10); exit only at rank > 15 |
| **Minimum hold** | `MIN_HOLD_DAYS = 10` | A rank-based exit needs the position to be ≥10 days old — **waived** if price is below the 50-day MA (a real trend break must not be delayed) |
| **Re-entry cooldown** | `REENTRY_COOLDOWN_DAYS = 10` | A symbol sold within 10 days cannot be re-bought |

### Sentinel Hard Rules (Automated — No LLM)

Runs at 2:30 PM ET without waiting for the agent:

| Rule | Trigger | Action |
|---|---|---|
| **Rule 1 — Concentration** | Position weight > 20% (2× `MAX_POSITION_PCT`) | Forced SELL |
| **Rule 2 — Trend break** | Price < 50-day MA for ≥3 consecutive agent runs | Forced SELL |
| **Rule 3 — Persistent flag** | SELL flagged ≥5 consecutive runs **AND position at a loss** | Forced SELL |

**Rule 3 hold gate (v3):** The sentinel does NOT force-sell profitable positions. A winning position with persistent SELL flags defers to the next quarterly rebalance. Only loss positions are force-sold by Rule 3.

### Hard Risk Limits (Code-Enforced in `update.py`)

These cannot be overridden by the agent or any config:

```python
# Daily / sentinel (non-rebalance) runs — tight throttles against panic churn
MAX_ORDERS_PER_RUN  = 5      # max total orders per DAILY run
MAX_SELL_VALUE_PCT  = 0.30   # max 30% of portfolio liquidated per DAILY run
CASH_FLOOR_PCT      = 0.05   # always keep ≥5% cash
MAX_POSITION_PCT    = 0.10   # max 10% per position (v3, was 8%)
MAX_SECTOR_PCT      = 0.30   # max 30% per GICS sector — ENFORCED (trim), not advisory (v3.1)
QUARTERLY_MONTHS    = {1,4,7,10}  # months where full rebalance is permitted

# Quarterly rebalance runs — wide budget so the full rotation finishes in ONE session (v3.1)
REBALANCE_MAX_ORDERS   = 24    # a full top-10-12 rotation (sells + buys) in one run
REBALANCE_MAX_SELL_PCT = 1.00  # a quarterly rotation may sell the entire stale book

# v3.2 — churn dampers and the redeployment trigger
CASH_DEPLOY_BAND      = 0.03   # redeploy once deployable cash > CASH_FLOOR_PCT + this
EXIT_RANK_MULTIPLE    = 1.5    # enter at rank ≤ TARGET_N, exit only at rank > TARGET_N × 1.5
MIN_HOLD_DAYS         = 10     # min age for a rank-based exit (waived on an MA break)
REENTRY_COOLDOWN_DAYS = 10     # a symbol sold this recently cannot be re-bought
```

**Cash is measured as `deployable_cash`, not `account.cash` (v3.2).** Alpaca's `cash` field includes
short-sale proceeds — money the account must give back. With the JBL short open the account reported
\$7,566 cash / 79.4% on a \$9,534 book while the true deployable figure was ~\$0 and the book was
already ~100% long. Every risk gate and the dashboard trade panel now size against
`cash − Σ|market_value of shorts|`.

**One-run rebalance rule (v3.1):** a quarterly rebalance uses `REBALANCE_MAX_ORDERS` /
`REBALANCE_MAX_SELL_PCT`, not the daily 5-order / 30%-sell throttles. The daily throttles applied to
the Jul-2026 quarterly rebalance stretched it over ~12 trading days and parked **40–61% of the book
in cash** through a rising market — that cash drag, not stock selection, was the entire quarter's
underperformance. The sector cap (`MAX_SECTOR_PCT`) is now **enforced at rebalance** — over-weight
sectors are trimmed by dropping the lowest-ranked buys — rather than only printing a warning.

---

## §9 — System Architecture

### Technology Stack

| Layer | Technology |
|---|---|
| Compute | GitHub Actions (ubuntu-latest, Python 3.14) |
| Hosting | GitHub Pages (static, zero server cost) |
| AI decisions | Anthropic Claude Sonnet 4.6 (prompt-cached) |
| News sentiment | Anthropic Claude Haiku 4.5 |
| Paper trading | Alpaca Markets paper API |
| Market data | Yahoo Finance (`yfinance ≥1.0`) |
| Enrichment | Financial Modeling Prep (FMP) free tier |
| Market breadth | TraderMonty public CSV (no API key) |
| Frontend charting | Chart.js 4.4 (SRI-pinned CDN) |
| Data store | Git repository (JSON files — immutable audit trail) |
| Secrets | GitHub Actions Secrets (never in repo or logs) |
| Offline support | Service Worker (Cache API) |

### System Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                     EXTERNAL DATA SOURCES                        │
│   Yahoo Finance · Wikipedia S&P 500 · Alpaca News API            │
│   Financial Modeling Prep (FMP) · TraderMonty Breadth CSV        │
└───────────────────────────┬──────────────────────────────────────┘
                            │ fetched by GitHub Actions bots
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                    GITHUB ACTIONS LAYER                          │
│                                                                  │
│  market-open.yml  9:30 AM ET                                     │
│    ① agent.py (day_start)  — pre-market flag check               │
│    ② update.py             — execute next_open orders            │
│                                                                  │
│  agent.yml  5:30 PM ET                                           │
│    ① update.py  — prices, screen, Alpaca sync, portfolio.json   │
│    ② enrich.py  — earnings, macro, breadth → enrichment.json    │
│    ③ agent.py   — Claude Sonnet decisions → agent_log.json       │
│                                                                  │
│  sentinel.yml  2:30 PM ET                                        │
│    ① sentinel.py — hard-rule sells (no LLM)                      │
│    ② update.py --sentinel — place sentinel orders                │
│                                                                  │
│  news-sentiment.yml  9:30 AM ET Mon + Thu                        │
│    ① news_sentiment.py — Haiku sentiment → news.json             │
└───────────────────────────┬──────────────────────────────────────┘
                            │ git commit + push
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│              GIT REPOSITORY  (source of truth)                   │
│                                                                  │
│  data/portfolio.json         Holdings, equity curve, trades      │
│  data/agent_log.json         AI decision history (90 runs)       │
│  data/news.json              Sentiment-tagged articles           │
│  data/enrichment.json        Earnings + macro + breadth          │
│  data/symbols.json           S&P 500 universe (search index)     │
│  data/bars/{SYM}.json        1-year daily closes per holding     │
│  data/sentinel_orders.json   Sentinel sell queue                 │
│  data/execution_summary.json Last market-open execution result   │
└───────────────────────────┬──────────────────────────────────────┘
                            │ GitHub Pages serves static files
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│        PWA DASHBOARD  (GitHub Pages / offline-capable)           │
│                                                                  │
│  ◆ Portfolio   Equity curve · stat cards · holdings cards        │
│  ⚡ Agent      AI run log · momentum heatmap · decision history  │
│  📰 News       Sentiment-tagged articles per holding             │
│  📋 Orders     Open + closed Alpaca orders · activity feed       │
└──────────────────────────────────────────────────────────────────┘
```

### Bot Modules

| File | Role | Runs in |
|---|---|---|
| `bot/update.py` | Price fetch, screening, Alpaca sync, order execution | `market-open.yml`, `agent.yml` |
| `bot/enrich.py` | FMP earnings + macro events, TraderMonty breadth | `agent.yml` |
| `bot/agent.py` | Claude Sonnet decision engine (reads this file prompt-cached) | `agent.yml`, `market-open.yml` |
| `bot/sentinel.py` | Rule-based hard-sell checker (no LLM) | `sentinel.yml` |
| `bot/news_sentiment.py` | Claude Haiku news classification | `news-sentiment.yml` |

### Security Controls

| Control | Implementation |
|---|---|
| No secrets in code | All API keys in GitHub Secrets, injected at runtime only |
| Paper trading guard | `verify_paper_url()` crashes if `ALPACA_BASE_URL` doesn't contain `"paper"` |
| Prompt injection defence | `_safe()` truncates + strips `\n`, `#`, `*`, `` ` ``, `\` from all external text |
| XSS defence | `sanitize()` applied to all `innerHTML` assignments in frontend; CSP header set |
| Risk limits | Hard-coded constants in `update.py`; cannot be overridden by agent or config |
| Workflow injection | `run_type` from `workflow_dispatch` allowlisted against `^(day_end|monthly)$` |

---

## §10 — Data Reference

### Key Data Files

| File | Written by | Purpose |
|---|---|---|
| `data/portfolio.json` | `update.py` | Holdings, trades (last 50), equity curve, SPY benchmark, summary stats |
| `data/agent_log.json` | `agent.py` | AI decisions, rolling 90-run window |
| `data/enrichment.json` | `enrich.py` | Earnings calendar, macro events, market breadth |
| `data/news.json` | `news_sentiment.py` | Sentiment-tagged articles for current holdings |
| `data/bars/{SYM}.json` | `update.py` | 1-year daily closes per holding (chart data) |
| `data/sentinel_orders.json` | `sentinel.py` | Sell queue for automated rule-based exits |
| `data/execution_summary.json` | `update.py` | Last market-open execution feedback for agent continuity |

### Agent Log Decision Schema

Every decision in `agent_log.json → runs[].decisions[]`:

```json
{
  "action": "HOLD | SELL | BUY | WATCH",
  "symbol": "TICKER",
  "reason": "specific rule — must include unrealized PnL and entry date for SELL/WATCH",
  "rule_triggered": "momentum_decay | trend_break | quality_drop | profit_take | new_entry | null",
  "sell_tier": "tier1 | tier2 | null",
  "urgency": "next_open | next_rebalance"
}
```

**Validation rules:**
- `sell_tier: "tier1"` → `urgency` must be `"next_open"`
- `sell_tier: "tier2"` → `urgency` must be `"next_rebalance"`; only valid in quarterly months
- `action: "BUY"` → only valid in quarterly months (Feb/Mar/May/Jun/Aug/Sep/Nov/Dec blocked)

### Holding Object Schema (inside `portfolio.json → holdings[]`)

```json
{
  "symbol":         "TICKER",
  "name":           "Company Name",
  "sector":         "Technology",
  "shares":         1,
  "avg_cost":       0.0,
  "current_price":  0.0,
  "market_value":   0.0,
  "weight":         0.042,
  "pnl":            0.0,
  "pnl_pct":        0.0,
  "eps_growth":     null,
  "revenue_growth": null,
  "forward_pe":     null,
  "volatility_30d": 0.0,
  "entry_date":     "YYYY-MM-DD",
  "ma_50d":         null,
  "status":         "above_ma | below_ma | unknown_ma",
  "momentum_rank":  1,
  "momentum_6m":    0.0,
  "momentum_12m":   0.0
}
```

`null` means data was unavailable from yfinance — distinct from `0.0`. Agent uses `null` to distinguish missing data from confirmed-zero values.

### Environment Variables (GitHub Secrets)

| Variable | Used by | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `agent.py`, `news_sentiment.py` | Claude API authentication |
| `ALPACA_API_KEY` | `update.py`, `news_sentiment.py` | Alpaca account key |
| `ALPACA_SECRET_KEY` | `update.py`, `news_sentiment.py` | Alpaca account secret |
| `ALPACA_BASE_URL` | `update.py` | Must contain `"paper"` — live trading guard |
| `ALPACA_ACCOUNT_NAME` | `update.py` | Display label only |
| `FMP_API_KEY` | `enrich.py` | Financial Modeling Prep free tier |

---

## §11 — Roadmap

### Phase 1 Status (Phase 1 = v1.0 → v2.2 era) — Largely Complete ✅

| Feature | Status | Notes |
|---|---|---|
| Paper trading via Alpaca | ✅ Done | Full Alpaca py integration |
| Claude Sonnet decision agent | ✅ Done | 3 run types, prompt-cached |
| 4-filter screening pipeline | ✅ Done | Momentum → quality → valuation → risk |
| Regime detection | ✅ Done | 3-signal 2-of-3 rule |
| SPY benchmark overlay | ✅ Done | `spy_curve` in `portfolio.json` |
| Real price bars per holding | ✅ Done | `data/bars/{SYM}.json` |
| Market breadth enrichment | ✅ Done | TraderMonty CSV integration |
| Earnings + macro calendar | ✅ Done | FMP API integration |
| News sentiment | ✅ Done | Claude Haiku on Alpaca News |
| Sentinel hard-rule engine | ✅ Done | `sentinel.py` + `sentinel.yml` |
| Tax-efficient sell framework | ✅ Done | v3 Tier 1/2 + 12-month hold gate |
| Quarterly execution lock | ✅ Done | `is_quarterly_month()` code-enforced |
| **Strategy backtesting** | ❌ Pending | vectorbt 10-year backtest — highest priority |

### Phase 2 — Enhanced Intelligence (Next Quarter)

**Theme:** Better data in, better decisions out.

| Feature | Tool | Impact |
|---|---|---|
| Strategy backtester | `vectorbt` + `quantstats` | Validate 10-year historical merit before trusting live capital |
| Better fundamental data | Financial Datasets MCP or `finvizfinance` | Replace unreliable yfinance EPS/revenue data |
| QuantStats HTML tearsheet | `quantstats` | Full performance report on each monthly rebalance |
| Notification system | GitHub Actions webhook/email | Alert when agent flags a position or regime changes |
| Alpaca MCP v2 | Official Alpaca MCP Server | Replace manual `alpaca-py` calls with 61-endpoint MCP |

**Backtesting is the single highest-priority item.** Without it, the strategy is validated only on ~6 weeks of live paper trading during a strong bull market — not enough to claim statistical confidence. Target: S&P 500 10-year backtest with monthly rebalance, equal weight, top-30% momentum.

### Phase 3 — Production-Ready (6–12 Months Out)

| Feature | Notes |
|---|---|
| Live trading toggle | `LIVE_TRADING=true` env var; circuit breakers required before enabling |
| Multi-agent orchestration | Specialist agents: screener, risk monitor, execution — each narrow scope |
| TradingView MCP | Add RSI/MACD/Bollinger signals as an additional screening layer |
| Real-time WebSocket feed | Alpaca WebSocket → replace static 30s polling with live price stream |
| Options overlay | Protective puts during bear regime on top holdings |

---

## §12 — Known Limitations & Honest Caveats

1. **Earnings gap risk** — Momentum stocks holding into earnings can gap ±10–20% overnight. Mitigated by quality filter (earnings growers rarely miss badly) but not eliminated. Agent flags earnings within 3 days as WATCH.

2. **Sector concentration** — Momentum over-concentrates in 1–2 sectors (tech 2020–21, energy 2022). v3 adds a 30% sector cap at quarterly rebalance.

3. **Survivorship bias** — S&P 500 membership removes bankruptcies and failing companies. Real-world universe would include some failures not captured in our screen.

4. **yfinance data quality** — EPS growth and forward P/E from yfinance are frequently stale or missing (returned as `None`). The bot applies conservative fallbacks (`None` → fail quality filter; `None` forward P/E → pass valuation filter).

5. **Transaction costs** — Paper trading ignores bid-ask spread, market impact, and commissions. Real-world returns would be ~0.5–1.5% lower annually at this turnover rate.

6. **Momentum crash risk** — The momentum factor crashes hard approximately once per decade (2009, 2020). The bear regime reduces exposure to 50% equity but does not eliminate it. The hold gate and sector cap reduce concentration into the crash but cannot prevent it.

7. **No backtesting yet** — All performance claims are based on ~6 weeks of live paper trading in a strong bull market (VIX ~9.6, SPY above 200-MA, breadth 62%). This is not a statistically valid validation period. Backtesting is Phase 2 priority #1.

8. **Cash drag observed** — Live run showed 22.9% cash vs 5% bull target due to execution failures and excessive turnover generating re-investable cash faster than it was deployed. v3 quarterly lock and hold gate address the root causes.

---

## §13 — Evolution Log (v1 → v2 → v3)

| Version | Key Changes |
|---|---|
| **v1.0** | Initial: momentum + quality filters, monthly rebalance, basic Alpaca integration |
| **v2.0** | Added: volatility filter, regime detection, quality deterioration sell rule, profit-taking rule, AI agent with Claude Sonnet |
| **v2.1** | Added: quarterly rebalance (was monthly), stricter sell thresholds (40%→30% momentum rank), 1-day→3-day MA break confirmation, tax-loss harvest priority, hard cap raised 8%→20% |
| **v2.2** | Added: 12-month hold gate (profitable positions immune to momentum-decay sell), Tier 1/2 sell pipeline, code-level non-quarterly execution lock, TARGET_N 17→10, MAX_POSITION_PCT 8%→10%, 30% sector cap |
| **v3.0** | **This document** — Consolidated all docs (PRD + HLD + LLD + STRATEGY) into single source of truth; updated all constants and rules to match live code; added live performance observations; updated Phase 2 roadmap (backtesting = #1 priority); retired stale separate documents |
| **v3.1** | **Execution-correctness release** (see `POSTMORTEM.md`). Added: (1) **long-only invariant** — SELLs clamped to held qty, no shorts, reject non-positive prices (fixes the JBL −22 runaway short); (2) **one-run quarterly rebalance** — `REBALANCE_MAX_ORDERS`/`REBALANCE_MAX_SELL_PCT` replace the 5-order/30% throttle for rebalance runs (fixes the 40–61% cash drag); (3) **sector cap enforced** (trim) instead of advisory; (4) **dollar-target equal-weight sizing** (fixes price-driven over-sizing); (5) one-time `bot/rebalance_trueup.py` to reconcile the broken book; (6) dedicated **Cash card** on the dashboard; (7) **P&L/Sharpe tracking fixed** — `compute_realized_pnl` (average-cost) + `compute_risk_metrics` populate realized P&L, win rate, Sharpe, max drawdown; (8) **agent spec conformance in code** — `normalize_decisions` requires `sell_tier`, drops `immediate`, makes day_start flag-only, blocks non-quarterly BUYs. |

**Critical learnings from May–June 2026 live run that drove v3:**
- Positions ranked 1–10 averaged +11.8% vs positions 11–18 at -4.2% → concentrate in top tier
- STX sold at $750, re-entered at $939 (25% miss) → hold gate prevents this
- 50 trades in 6 weeks (target: ~35 total for the year) → quarterly lock enforced in code
- 22.9% cash vs 5% bull target → sell flags not clearing + no buy-in mechanism mid-quarter
- GOOGL sold May 27 → re-bought May 29 (2-day round trip, no gain) → no mid-quarter buys
