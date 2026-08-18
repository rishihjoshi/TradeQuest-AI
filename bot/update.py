#!/usr/bin/env python3
"""TradeQuest AI — portfolio updater bot.

Two modes:
  Alpaca mode  — Alpaca credentials present: reads real paper-trading positions
                 and account data, places real paper orders on rebalance days.
  Simulation   — No credentials: simulates portfolio from yfinance data.

Price & fundamentals data sources (in priority order):
  1. Yahoo Finance (yfinance) — primary; free, no API key, fast bulk download.
  2. Financial Modeling Prep (FMP) — fallback when yfinance returns None for
     eps_growth, revenue_growth, forward_pe, or ma_50d.  Uses FMP_API_KEY
     secret (250 free calls/day).  FMP is capped at FMP_FALLBACK_CAP symbols
     per run to stay within the free-tier daily quota.
"""

import io
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

REPO_ROOT        = Path(__file__).resolve().parent.parent
DATA_FILE        = REPO_ROOT / "data" / "portfolio.json"
LOG_FILE         = REPO_ROOT / "data" / "agent_log.json"
EXEC_SUMMARY_FILE = REPO_ROOT / "data" / "execution_summary.json"
INITIAL_CAPITAL = 100_000
TARGET_N = 10          # v2.2: top-conviction 10-12 positions only (was 17)
CANDIDATES_CAP = 60    # max tickers to fetch fundamentals for

# Alpaca credentials — read from env vars injected by GitHub Actions secrets
# Never hardcode these values here
ALPACA_ACCOUNT_NAME = os.environ.get("ALPACA_ACCOUNT_NAME", "TradeQuest Paper")
ALPACA_API_KEY      = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY   = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL     = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# ── FMP (Financial Modeling Prep) — fallback data source ──────
# Used when yfinance returns None for key fundamental fields.
# Free tier: 250 API calls/day.  Each symbol uses ≤2 calls (quote + income-stmt).
FMP_BASE          = "https://financialmodelingprep.com/stable"
FMP_API_KEY       = os.environ.get("FMP_API_KEY", "")
FMP_FALLBACK_CAP  = 30   # max symbols to query via FMP per run (= up to 60 API calls)

# ── Hardcoded risk limits — never read from external config ───
MAX_ORDERS_PER_RUN  = 5     # max total orders (sells + buys) placed in a DAILY (non-rebalance) run
MAX_SELL_VALUE_PCT  = 0.30  # daily runs: never liquidate more than 30% of portfolio in one run
CASH_FLOOR_PCT      = 0.05  # always keep ≥5% of portfolio value as cash
MAX_POSITION_PCT    = 0.10  # v2.2: single position cap raised to 10% (was 8%; 10-12 positions at 8-10% each)
MAX_SECTOR_PCT      = 0.30  # v2.2: no single GICS sector may exceed 30% of portfolio

# v3.1: a quarterly rebalance must complete the full rotation in ONE run — the daily 5-order /
# 30%-sell throttles turned the Jul-2026 rebalance into a 12-day grind that parked 40-60% in cash.
# Rebalance runs therefore use a wider budget; daily/sentinel runs keep the tight defaults.
REBALANCE_MAX_ORDERS   = 24    # enough for a full top-10-12 rotation (sells + buys) in one session
REBALANCE_MAX_SELL_PCT = 1.00  # a full quarterly rotation may sell the entire stale book
CASH_DEPLOY_BAND       = 0.03  # deploy idle cash when cash% exceeds (target + this band)

# v3.2: the screen re-ranks DAILY but the strategy rebalances QUARTERLY. Recomputing exits from a
# fresh top-N every run churned names oscillating around the boundary — FFIV was bought 2026-07-28
# at $412.75, sold 07-30 at $389.88 (-$45.74) and re-bought 07-31 at $401.62; CSX and WST did the
# same. That churn is the source of the 12.5% win rate. Three dampers, applied to RANK-BASED exits
# only (structural sell rules and sentinel exits are unaffected):
EXIT_RANK_MULTIPLE    = 1.5  # enter at rank ≤ TARGET_N, exit only at rank > TARGET_N × this
MIN_HOLD_DAYS         = 10   # a rank-based exit needs the position to be at least this old
REENTRY_COOLDOWN_DAYS = 10   # a symbol sold this recently cannot be re-bought

# v2.2 quarterly months — only these months allow new BUY orders and Tier 2+ SELL exits
QUARTERLY_MONTHS = {1, 4, 7, 10}  # Jan, Apr, Jul, Oct

# Pre-market execution mode flag (set via MARKET_OPEN_RUN=true env var)
MARKET_OPEN_RUN = os.environ.get("MARKET_OPEN_RUN", "").lower() == "true"

# Sentinel execution mode: reads sentinel_orders.json and places those sells immediately.
# Set via --sentinel CLI flag or SENTINEL_RUN=true env var.
SENTINEL_RUN = "--sentinel" in sys.argv or os.environ.get("SENTINEL_RUN", "").lower() == "true"

# Known dual-class share pairs: maps each ticker to a canonical issuer ID.
# Screener keeps only the highest-momentum ticker per issuer.
ISSUER_MAP: dict[str, str] = {
    "GOOG":  "ALPHABET",
    "GOOGL": "ALPHABET",
    "BRK-A": "BERKSHIRE",
    "BRK-B": "BERKSHIRE",
    "BF-A":  "BROWFORMAN",
    "BF-B":  "BROWFORMAN",
}


# ── v2.2 Helper utilities ─────────────────────────────────────

def is_quarterly_month(dt: datetime | None = None) -> bool:
    """Return True if the current month is a quarterly rebalance month (Jan/Apr/Jul/Oct).

    v2.2: Only quarterly months allow new BUY orders and Tier 2/3 SELL exits.
    Non-quarterly months are locked to Tier 1 (loss-harvest) sells only.
    """
    month = (dt or datetime.now()).month
    return month in QUARTERLY_MONTHS


def next_quarterly_date(dt: datetime | None = None) -> str:
    """First day of the next quarterly rebalance month (Jan/Apr/Jul/Oct), as YYYY-MM-DD.

    The old "1st of next month" value was wrong whenever the next month was not quarterly —
    portfolio.json advertised next_rebalance 2026-09-01 while new-entrant buys were in fact
    locked until 2026-10-01.
    """
    now = dt or datetime.now()
    year, month = now.year, now.month
    for _ in range(12):
        month += 1
        if month > 12:
            month, year = 1, year + 1
        if month in QUARTERLY_MONTHS:
            return f"{year:04d}-{month:02d}-01"
    return now.strftime("%Y-%m-%d")   # unreachable while QUARTERLY_MONTHS is non-empty


def has_unrealized_gain(holding: dict) -> bool:
    """Return True if the position has a positive unrealized PnL (the hold gate applies)."""
    pnl = holding.get("pnl") or holding.get("pnl_pct", 0)
    # Prefer dollar PnL for accuracy; fall back to pnl_pct if needed
    try:
        return float(pnl) > 0
    except (TypeError, ValueError):
        return False


# ── v3.2 churn dampers ────────────────────────────────────────

def days_held(holding: dict, today: datetime | None = None) -> int | None:
    """Calendar days since entry_date, or None when the date is missing/unparseable."""
    raw = holding.get("entry_date")
    if not raw:
        return None
    try:
        entry = datetime.strptime(str(raw)[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return ((today or datetime.now()) - entry).days


def should_exit_on_rank(holding: dict, rank: int | None,
                        target_n: int = TARGET_N, today: datetime | None = None) -> tuple[bool, str]:
    """Decide a RANK-BASED exit under the v3.2 hysteresis band. Returns (exit?, reason).

    Entry happens at rank ≤ target_n but exit only past target_n × EXIT_RANK_MULTIPLE, so a
    name drifting around the boundary is held instead of round-tripped. A position younger
    than MIN_HOLD_DAYS is held regardless — UNLESS it has broken its 50-day MA, which is a
    genuine trend break (Rule A) and must not be delayed by a churn damper.
    """
    exit_threshold = int(target_n * EXIT_RANK_MULTIPLE)

    if rank is None or rank <= 0:
        reason = "unranked (dropped out of the screen universe)"
    elif rank > exit_threshold:
        reason = f"rank {rank} > exit threshold {exit_threshold}"
    else:
        return False, f"rank {rank} within hysteresis band (exit at >{exit_threshold})"

    if holding.get("status") == "below_ma":
        return True, f"{reason}; below 50-day MA — min-hold waived"

    held_days = days_held(holding, today)
    if held_days is not None and held_days < MIN_HOLD_DAYS:
        return False, (f"{reason} BUT held only {held_days}d "
                       f"(min {MIN_HOLD_DAYS}d) and above MA — exit deferred")
    return True, reason


def in_reentry_cooldown(symbol: str, trades: list[dict], today: datetime | None = None) -> int | None:
    """Days since the most recent SELL of `symbol`, if still inside the cooldown window.

    Returns None when the symbol is free to buy. Blocks the buy→sell→re-buy round trips
    (FFIV, CSX, WST in Jul 2026) that realized losses and re-entered at a worse price.
    """
    now = today or datetime.now()
    for t in trades or []:
        if str(t.get("action", "")).upper() != "SELL":
            continue
        if str(t.get("symbol", "")).upper() != symbol.upper():
            continue
        try:
            sold = datetime.strptime(str(t.get("date", ""))[:10], "%Y-%m-%d")
        except ValueError:
            continue
        age = (now - sold).days
        if age < REENTRY_COOLDOWN_DAYS:
            return age
    return None


def check_sector_concentration(holdings: list[dict]) -> dict[str, float]:
    """Return {sector: weight_sum} for sectors exceeding MAX_SECTOR_PCT.

    Used at quarterly rebalance to flag or block over-concentrated sectors.
    """
    from collections import defaultdict
    totals: dict[str, float] = defaultdict(float)
    for h in holdings:
        sector = h.get("sector", "Unknown")
        totals[sector] += float(h.get("weight", 0))
    return {s: w for s, w in totals.items() if w > MAX_SECTOR_PCT}


# ── FMP fallback — fundamentals ───────────────────────────────

def _fmp_get(path: str, params: dict) -> list | dict | None:
    """Thin HTTP helper for FMP stable API — mirrors enrich.py pattern.

    Returns parsed JSON (list or dict) or None on any error.
    Silently skips the call when FMP_API_KEY is not set.
    """
    if not FMP_API_KEY:
        return None
    try:
        r = requests.get(
            f"{FMP_BASE}/{path}",
            params={**params, "apikey": FMP_API_KEY},
            timeout=15,
        )
        if r.status_code == 429:
            print(f"  FMP rate-limit hit ({path}) — skipping fallback for this run.", file=sys.stderr)
            return None
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return data if data else None
    except Exception as e:
        print(f"  FMP {path} error: {e}", file=sys.stderr)
        return None


def fetch_fmp_fundamentals(symbol: str) -> dict:
    """Fetch fundamentals for ONE symbol from FMP as a yfinance fallback.

    Makes up to 2 FMP API calls:
      Call 1 — stable/quote
        → current_price  (quote.price)
        → ma_50d         (quote.priceAvg50)
        → forward_pe     (quote.pe — NOTE: FMP 'pe' is trailing P/E TTM,
                          not forward P/E. Stored with pe_source='fmp_trailing'
                          so the agent/filter knows it's a proxy, not analyst estimate)

      Call 2 — stable/income-statement?period=annual&limit=2
        → eps_growth     = (eps_year0 / eps_year1 - 1) × 100   (YoY)
        → revenue_growth = (revenue_year0 / revenue_year1 - 1) × 100 (YoY)

    Returns a dict with only the fields that were successfully retrieved.
    The caller merges this into the yfinance result, filling only None slots.
    """
    result: dict = {}

    # ── Call 1: Quote → price, 50-day MA, trailing P/E ────────
    quote_raw = _fmp_get("quote", {"symbol": symbol})
    if quote_raw:
        q = quote_raw[0] if isinstance(quote_raw, list) and quote_raw else quote_raw
        if isinstance(q, dict):
            price = q.get("price")
            avg50 = q.get("priceAvg50")
            pe    = q.get("pe")
            if price and float(price) > 0:
                result["current_price"] = round(float(price), 2)
            if avg50 and float(avg50) > 0:
                result["ma_50d"] = round(float(avg50), 2)
            if pe and float(pe) > 0:
                result["forward_pe"]  = round(float(pe), 1)
                result["pe_source"]   = "fmp_trailing"  # agent transparency flag

    # ── Call 2: Income statement → YoY EPS + revenue growth ───
    stmt_raw = _fmp_get(
        "income-statement",
        {"symbol": symbol, "period": "annual", "limit": "2"},
    )
    if stmt_raw and isinstance(stmt_raw, list) and len(stmt_raw) >= 2:
        curr = stmt_raw[0]   # most recent annual
        prev = stmt_raw[1]   # prior year

        # Revenue growth (YoY annual)
        rev_c = curr.get("revenue")
        rev_p = prev.get("revenue")
        if rev_c is not None and rev_p is not None:
            try:
                rev_c_f, rev_p_f = float(rev_c), float(rev_p)
                if rev_p_f != 0:
                    result["revenue_growth"] = round((rev_c_f / rev_p_f - 1) * 100, 1)
            except (TypeError, ValueError):
                pass

        # EPS growth (YoY annual) — prefer diluted EPS; fall back to net income per share
        eps_c = curr.get("epsdiluted") or curr.get("netIncomePerShare")
        eps_p = prev.get("epsdiluted") or prev.get("netIncomePerShare")
        if eps_c is not None and eps_p is not None:
            try:
                eps_c_f, eps_p_f = float(eps_c), float(eps_p)
                if eps_p_f != 0:
                    result["eps_growth"] = round((eps_c_f / eps_p_f - 1) * 100, 1)
            except (TypeError, ValueError):
                pass

    return result


# ── Safety guards ─────────────────────────────────────────────

def verify_paper_url() -> None:
    """Crash early if the base URL is not a paper-trading endpoint."""
    if "paper" not in ALPACA_BASE_URL.lower():
        raise RuntimeError(
            f"SAFETY: ALPACA_BASE_URL '{ALPACA_BASE_URL}' does not look like a paper "
            "trading endpoint. Refusing to place orders."
        )


def load_agent_approvals(target_urgency: str = "next_open") -> dict[str, set[str]]:
    """
    Read agent_log.json and return the most recent valid day_end or monthly run's
    approved actions as sets: {"SELL": {symbols...}, "BUY": {symbols...}}.

    target_urgency controls which urgency level is executed:
      "next_open"    — market-open run (default): actions marked next_open
      "next_rebalance" — monthly rebalance actions only

    All real sell decisions use "next_open" urgency and execute via market-open.yml
    at 9:30 AM ET. There is no intraday execution path for post-close decisions.

    Runs flagged with parse_failed=True are skipped so a bad Claude response
    never silently blocks all sells by returning empty approvals.

    Returns empty sets if no relevant run is found (no orders will be placed).
    """
    approvals: dict[str, set[str]] = {"SELL": set(), "BUY": set()}
    if not LOG_FILE.exists():
        print("Warning: agent_log.json not found — no agent approvals, orders blocked.", file=sys.stderr)
        return approvals

    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)
    except Exception as e:
        print(f"Warning: could not read agent_log.json ({e}) — orders blocked.", file=sys.stderr)
        return approvals

    # Find the most recent valid day_end or monthly run (skip parse-failed entries)
    for run in log.get("runs", []):
        run_type = run.get("run_type") or run.get("type", "")
        if run_type not in ("day_end", "monthly"):
            continue
        if run.get("parse_failed"):
            print(f"  Skipping parse-failed run {run.get('id','?')} — looking for earlier run")
            continue
        for d in run.get("decisions", []):
            action  = str(d.get("action", "")).upper()
            symbol  = str(d.get("symbol", "")).strip().upper()
            urgency = str(d.get("urgency", "")).lower()
            # Accept both "next_open" and legacy "immediate" labels — both execute at market open.
            is_match = (urgency == target_urgency) or (
                target_urgency == "next_open" and urgency == "immediate"
            )
            if action in ("SELL", "BUY") and symbol and is_match:
                approvals[action].add(symbol)
        print(f"Agent approvals loaded from run {run.get('id','?')} "
              f"(urgency={target_urgency}): "
              f"{len(approvals['SELL'])} SELLs, {len(approvals['BUY'])} BUYs approved")
        return approvals  # use only the most recent qualifying run

    print("No valid day_end or monthly agent run found — orders blocked for safety.", file=sys.stderr)
    return approvals


def apply_risk_limits(
    to_sell: list[tuple],
    to_buy:  list[tuple],
    pv:      float,
    cash:    float,
    agent_sell_approvals: set[str],
    prices:  dict[str, float] | None = None,
    max_orders:   int | None = None,
    max_sell_pct: float | None = None,
) -> tuple[list[tuple], list[tuple]]:
    """
    Gate and cap sell + buy lists before they reach the broker.

    Rules applied in order:
    1. SELL only what the agent explicitly approved (immediate/next_open).
    2. Total sell value ≤ max_sell_pct of portfolio (dollar-accurate).
    3. Total orders ≤ max_orders.
    4. BUY only if enough cash remains above CASH_FLOOR_PCT floor.
    5. Each BUY capped at MAX_POSITION_PCT of portfolio value (dollar-accurate).

    Daily/sentinel runs pass the tight defaults (MAX_ORDERS_PER_RUN / MAX_SELL_VALUE_PCT).
    Quarterly rebalance runs pass the wider REBALANCE_* budgets so the full rotation completes
    in a single session instead of grinding out over many days (v3.1).
    """
    prices       = prices or {}
    max_orders   = MAX_ORDERS_PER_RUN   if max_orders   is None else max_orders
    max_sell_pct = MAX_SELL_VALUE_PCT   if max_sell_pct is None else max_sell_pct
    cash_floor   = pv * CASH_FLOOR_PCT
    max_sell_val = pv * max_sell_pct
    max_pos_val  = pv * MAX_POSITION_PCT

    # 1. Gate sells behind agent approval.
    # Exception: positions that are >2× the max size are always approved — the risk limit
    # designed to prevent panic selling must not trap a confirmed overweight position.
    position_size_overrides = {
        sym.upper() for sym, shares in to_sell
        if prices.get(sym.upper(), 0.0) * shares > pv * MAX_POSITION_PCT * 2
    }
    if position_size_overrides:
        print(f"  Risk gate: position-size override applied for {', '.join(sorted(position_size_overrides))}"
              f" (>2× max position — bypassing agent approval gate)")
    approved_sells = [
        (sym, shares) for sym, shares in to_sell
        if sym.upper() in agent_sell_approvals or sym.upper() in position_size_overrides
    ]
    blocked = set(sym for sym, _ in to_sell) - set(sym for sym, _ in approved_sells)
    if blocked:
        print(f"  Risk gate: blocked unapproved SELLs — {', '.join(sorted(blocked))}")

    # 2. Cap total sell value in dollars.
    # Exception: confirmed position-size violations are not subject to the 30% cap —
    # an overweight position can only be corrected if it is actually allowed to sell.
    capped_sells: list[tuple] = []
    running_sell_val = 0.0
    for sym, shares in approved_sells:
        price = prices.get(sym.upper(), 0.0)
        order_val = shares * price if price > 0 else 0.0
        is_size_violation = sym.upper() in position_size_overrides
        if not is_size_violation and running_sell_val + order_val > max_sell_val:
            print(f"  Risk gate: sell cap ${max_sell_val:,.0f} reached — {sym} skipped")
            continue
        capped_sells.append((sym, shares))
        if not is_size_violation:
            running_sell_val += order_val

    # 3. Total order cap
    sell_budget = min(len(capped_sells), max_orders)
    capped_sells = capped_sells[:sell_budget]
    buy_budget   = max(0, max_orders - sell_budget)

    # 4 & 5. Cash floor + position size cap on buys (dollar-accurate)
    available_cash = cash - cash_floor  # never spend below floor
    capped_buys: list[tuple] = []
    for sym, shares in to_buy[:buy_budget]:
        if available_cash <= 0:
            print(f"  Risk gate: cash floor reached — no more buys ({sym} skipped)")
            break
        price = prices.get(sym.upper(), 0.0)
        # Cap shares so the position value stays within MAX_POSITION_PCT
        max_shares = int(max_pos_val / price) if price > 0 else shares
        capped_shares = max(1, min(shares, max_shares))
        order_cost = capped_shares * price if price > 0 else 0.0
        if order_cost > available_cash and price > 0:
            # Reduce to what cash allows; skip entirely if even 1 share exceeds budget
            affordable = int(available_cash / price)
            if affordable < 1:
                print(f"  Risk gate: can't afford even 1 share of {sym} "
                      f"(${price:,.0f}/share, available ${available_cash:,.0f}) — skipped")
                continue
            capped_shares = affordable
        capped_buys.append((sym, capped_shares))
        available_cash -= capped_shares * price if price > 0 else 0.0

    if len(capped_sells) < len(to_sell) or len(capped_buys) < len(to_buy):
        print(f"  Risk summary: {len(capped_sells)}/{len(to_sell)} sells, "
              f"{len(capped_buys)}/{len(to_buy)} buys after limits")

    return capped_sells, capped_buys


# ── Alpaca integration ────────────────────────────────────────

def _alpaca_client():
    """Return a TradingClient or None if credentials are missing."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("No Alpaca credentials — running in simulation mode.")
        return None
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        print(f"Alpaca connected — account: {ALPACA_ACCOUNT_NAME}")
        return client
    except Exception as e:
        print(f"Warning: Alpaca connection failed ({e}). Falling back to simulation.", file=sys.stderr)
        return None


def short_positions(positions) -> list:
    """Return positions with a negative quantity — a long-only invariant breach."""
    return [p for p in (positions or []) if float(getattr(p, "qty", 0) or 0) < 0]


def compute_deployable_cash(cash: float, positions) -> float:
    """Cash that is genuinely available to spend (v3.2).

    Alpaca's `account.cash` INCLUDES the proceeds of any short sale — money the account
    does not own and must give back when the short is covered. With the JBL short open
    (-22 sh, ~-$7,571) the account reported $7,566 cash / 79.4% on a $9,534 book while the
    true deployable figure was ~$0 and the book was already ~100% long. Sizing buys off
    the raw number would have authorized ~$7,090 of purchases against phantom money.

    Deployable = raw cash − Σ|market_value| of every short position. Never returns < 0.
    """
    short_liability = sum(
        abs(float(getattr(p, "market_value", 0) or 0)) for p in short_positions(positions)
    )
    return max(0.0, cash - short_liability)


def alpaca_read_state(client) -> dict | None:
    """Fetch account summary, open positions, closed orders, and pending open orders."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        account        = client.get_account()
        positions      = client.get_all_positions()
        # v3.2: paginate closed orders so compute_realized_pnl can pair full history.
        # The old flat limit=50 silently truncated it — 50 trades were recorded but only
        # 8 ever got a pnl attributed, which is why win_rate read 0.125 off 8 samples.
        orders_closed  = fetch_closed_orders(client)
        orders_open    = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=20))

        cash = float(account.cash)
        return {
            "portfolio_value": float(account.portfolio_value),
            "cash":            cash,
            "deployable_cash": compute_deployable_cash(cash, positions),
            "shorts":          short_positions(positions),
            "positions":       positions,
            "orders":          list(orders_closed) + list(orders_open),
        }
    except Exception as e:
        print(f"Warning: Alpaca state fetch failed ({e}).", file=sys.stderr)
        return None


def fetch_closed_orders(client, page_size: int = 500, max_orders: int = 2000) -> list:
    """Page through closed orders newest-first so realized-P&L attribution sees real history."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    collected: list = []
    until = None
    while len(collected) < max_orders:
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=page_size, direction="desc", until=until
        )
        batch = list(client.get_orders(req))
        if not batch:
            break
        collected.extend(batch)
        if len(batch) < page_size:
            break
        # Walk backwards from the oldest order in this page.
        until = getattr(batch[-1], "submitted_at", None) or getattr(batch[-1], "created_at", None)
        if until is None:
            break
    return collected[:max_orders]


def alpaca_positions_to_holdings(
    positions,
    fundamentals: dict,
    screened_ranks: dict,
    vol30: "pd.Series",
    existing_map: dict | None = None,
) -> list[dict]:
    """Map Alpaca Position objects → portfolio.json holdings format.

    existing_map: {symbol: existing_holding_dict} from the previous portfolio.json.
    Used to preserve entry_date so Sell Rule 4 (profit >60% in <60 days) can fire.
    Fundamental fields are stored as None when data is unavailable so the agent
    can distinguish genuinely-missing data from a confirmed zero value.
    """
    existing_map = existing_map or {}
    holdings = []
    for pos in positions:
        sym       = pos.symbol
        fi        = fundamentals.get(sym, {})
        price     = float(pos.current_price   or 0)
        avg_cost  = float(pos.avg_entry_price or 0)
        shares    = float(pos.qty             or 0)
        mv        = float(pos.market_value    or 0)
        upnl      = float(pos.unrealized_pl   or 0)
        upnl_pct  = float(pos.unrealized_plpc or 0) * 100
        ma50      = fi.get("ma_50d")   # None means data is missing
        rank_info = screened_ranks.get(sym, {})

        # Preserve the original entry date so the profit-taking rule can be evaluated.
        # Only fall back to today when the position is genuinely new (not in prior portfolio).
        existing_entry = existing_map.get(sym, {}).get("entry_date")
        entry_date = existing_entry or datetime.now().strftime("%Y-%m-%d")

        holdings.append({
            "symbol":         sym,
            "name":           fi.get("name", sym),
            "sector":         fi.get("sector", "Unknown"),
            "shares":         shares,
            "avg_cost":       round(avg_cost, 2),
            "current_price":  round(price, 2),
            "market_value":   round(mv, 2),
            "weight":         0,   # recalculated below in reconcile/main
            "pnl":            round(upnl, 2),
            "pnl_pct":        round(upnl_pct, 2),
            "eps_growth":     fi.get("eps_growth"),     # None = data missing
            "revenue_growth": fi.get("revenue_growth"), # None = data missing
            "forward_pe":     fi.get("forward_pe"),     # None = data missing
            "volatility_30d": round(float(vol30.get(sym, 0)), 4),
            "entry_date":     entry_date,
            "ma_50d":         ma50,                     # None = data missing
            "status":         ("unknown_ma" if ma50 is None
                               else ("above_ma" if price > ma50 else "below_ma")),
            "momentum_rank":  rank_info.get("rank", 0),
            "momentum_6m":    round(rank_info.get("mom_6m", 0), 4),
            "momentum_12m":   round(rank_info.get("mom_12m", 0), 4),
        })
    return holdings


def compute_realized_pnl(orders) -> dict[str, tuple]:
    """Reconstruct realized P&L per SELL order via the average-cost method (F7).

    Alpaca order objects carry fills but no per-lot realized P&L, so we replay the filled
    orders chronologically, maintaining a running (qty, avg_cost) per symbol. On each SELL we
    realise (fill_price - avg_cost) × qty against the basis built from prior BUYs.

    Returns {order_id: (pnl, pnl_pct)}. A SELL whose basis isn't in the order window (the
    opening BUYs predate the fetched history) yields (None, None) rather than a misleading zero.

    Limitation: Alpaca returns a bounded order window (recent closed + open), so cost basis for
    very old lots may be unavailable. It self-heals as the window rolls forward.
    """
    filled = [o for o in orders if float(getattr(o, "filled_qty", 0) or 0) > 0]

    def _ts(o):
        return str(getattr(o, "filled_at", None) or getattr(o, "created_at", None) or "")

    basis: dict[str, dict] = {}          # symbol -> {"qty": float, "avg": float}
    realized: dict[str, tuple] = {}      # order_id -> (pnl, pnl_pct)
    for o in sorted(filled, key=_ts):
        sym   = o.symbol
        qty   = float(o.filled_qty or 0)
        price = float(o.filled_avg_price or 0)
        pos   = basis.setdefault(sym, {"qty": 0.0, "avg": 0.0})
        if o.side.value == "buy":
            new_qty = pos["qty"] + qty
            if new_qty > 0:
                pos["avg"] = (pos["qty"] * pos["avg"] + qty * price) / new_qty
            pos["qty"] = new_qty
        else:  # sell → realise against average cost
            avg = pos["avg"]
            if avg > 0 and price > 0:
                realized[str(o.id)] = (round((price - avg) * qty, 2),
                                       round((price / avg - 1) * 100, 2))
            else:
                realized[str(o.id)] = (None, None)
            pos["qty"] = max(0.0, pos["qty"] - qty)   # long-only: never track negative basis
    return realized


def alpaca_orders_to_trades(orders) -> list[dict]:
    """Map Alpaca Order objects → portfolio.json trades format.

    Includes both filled orders and pending (accepted/open) orders.
    Pending orders show price=0 and reason='Pending — awaiting market open'.
    Realized P&L on SELL trades is reconstructed via average-cost (compute_realized_pnl).
    """
    realized = compute_realized_pnl(orders)
    trades = []
    for o in orders:
        filled_qty = float(o.filled_qty or 0)
        fill_price = float(o.filled_avg_price or 0)
        filled_at  = o.filled_at
        created_at = getattr(o, "created_at", None)
        date_str   = (filled_at.strftime("%Y-%m-%d") if filled_at
                      else (str(created_at)[:10] if created_at else ""))
        action     = "BUY" if o.side.value == "buy" else "SELL"

        if filled_qty > 0:
            pnl, pnl_pct = realized.get(str(o.id), (None, None)) if action == "SELL" else (None, None)
            trades.append({
                "id":       f"ALP-{str(o.id)[:8].upper()}",
                "date":     date_str,
                "action":   action,
                "symbol":   o.symbol,
                "name":     o.symbol,
                "shares":   filled_qty,
                "price":    round(fill_price, 2),
                "value":    round(filled_qty * fill_price, 2),
                "pnl":      pnl,
                "pnl_pct":  pnl_pct,
                "reason":   "Alpaca paper trade — momentum rebalance",
                "type":     "market",
            })
        else:
            qty = float(o.qty or 0)
            if qty > 0:
                trades.append({
                    "id":       f"ALP-{str(o.id)[:8].upper()}",
                    "date":     date_str,
                    "action":   action,
                    "symbol":   o.symbol,
                    "name":     o.symbol,
                    "shares":   qty,
                    "price":    0.0,
                    "value":    0.0,
                    "pnl":      None,
                    "pnl_pct":  None,
                    "reason":   "Pending — awaiting market open",
                    "type":     "market",
                })
    return trades


def alpaca_place_orders(client, to_sell: list[tuple], to_buy: list[tuple],
                         pv: float, cash: float,
                         prices: dict[str, float] | None = None,
                         to_cover: list[tuple] | None = None) -> list[tuple]:
    """
    Submit market orders to Alpaca paper trading. Covers first, then sells, then buys.
    Enforces CASH_FLOOR_PCT: stops buying if remaining cash would fall below floor.
    Idempotency guard: skips symbols that already have an open order today to prevent
    duplicate submissions from concurrent workflow runs.

    Long-only invariant (v3.1): this is the last line of defence against the runaway-short
    bug — every SELL is clamped to the quantity actually held, and a symbol that is flat or
    already short is never sold. Orders with a non-positive price are also rejected. Without
    this, a symbol that trend-breaks every day (e.g. JBL, Jul 2026) is re-sold each run and
    Alpaca opens/extends a naked short with unbounded loss.

    Cover path (v3.2): `to_cover` closes existing shorts and RESTORES that invariant. Covers
    run before everything else and are exempt from the cash floor — a cover is funded by the
    short proceeds already sitting in the account, so blocking it on a cash test would leave
    the position permanently stuck (exactly what happened to JBL from Jul to Aug 2026).
    """
    from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

    prices     = prices or {}
    cash_floor = pv * CASH_FLOOR_PCT
    placed = []

    # Long-only guard: fetch current positions so we never sell more than we own.
    held_qty: dict[str, float] = {}
    try:
        for pos in client.get_all_positions():
            held_qty[str(getattr(pos, "symbol", "")).upper()] = float(getattr(pos, "qty", 0) or 0)
    except Exception as e:
        print(f"  Warning: could not fetch positions for long-only guard: {e}", file=sys.stderr)

    # Build set of symbols already having an open/pending order today (idempotency guard)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    already_ordered: set[str] = set()
    try:
        open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        for o in open_orders:
            order_date = str(getattr(o, "created_at", ""))[:10]
            if order_date == today_str:
                already_ordered.add(str(getattr(o, "symbol", "")).upper())
    except Exception as e:
        print(f"  Warning: could not fetch open orders for idempotency check: {e}", file=sys.stderr)

    # ── Covers first (v3.2): restore the long-only invariant before anything else ──
    for sym, shares in (to_cover or []):
        key = sym.upper()
        if key in already_ordered:
            print(f"  Idempotency: COVER {sym} already has an open order today — skipped")
            continue
        held = held_qty.get(key)
        if held is None or held >= 0:
            print(f"  Cover guard: {sym} is not short (qty {held}) — COVER skipped")
            continue
        # Never buy back more than the outstanding short, or the cover flips us long.
        qty = min(int(shares), int(abs(held)))
        if qty <= 0:
            print(f"  Cover guard: {sym} computed qty {qty} ≤ 0 — COVER skipped")
            continue
        try:
            client.submit_order(MarketOrderRequest(
                symbol=sym, qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            print(f"  ⇧ COVER {qty:>5} {sym} (buy-to-close short — long-only restored)")
            placed.append(("COVER", sym, qty))
            already_ordered.add(key)
        except Exception as e:
            print(f"  ✗ COVER {sym}: {e}", file=sys.stderr)

    for sym, shares in to_sell:
        if sym.upper() in already_ordered:
            print(f"  Idempotency: SELL {sym} already has an open order today — skipped")
            continue
        # Long-only clamp: never sell more than held; never open/extend a short.
        # A symbol we don't hold (held is None, or ≤ 0) is NEVER sold — selling it would
        # open a short. If the position fetch failed entirely (empty map), this conservatively
        # blocks all sells for the run (safe: they retry next run) rather than risk a short.
        held = held_qty.get(sym.upper())
        requested = int(shares)
        if held is None:
            print(f"  Long-only guard: {sym} has no position on record — SELL skipped (no short)")
            continue
        sellable = int(held)
        if sellable <= 0:
            print(f"  Long-only guard: {sym} not held (qty {held:g}) — SELL skipped (no short)")
            continue
        qty = sellable if requested <= 0 else min(requested, sellable)
        if qty <= 0:
            print(f"  Long-only guard: {sym} computed qty {qty} ≤ 0 — SELL skipped")
            continue
        price = prices.get(sym.upper(), 0.0)
        if price <= 0:
            print(f"  Price guard: {sym} has non-positive price ({price}) — SELL skipped")
            continue
        try:
            client.submit_order(MarketOrderRequest(
                symbol=sym, qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            print(f"  ↓ SELL {qty:>5} {sym}")
            placed.append(("SELL", sym, qty))
            already_ordered.add(sym.upper())
        except Exception as e:
            print(f"  ✗ SELL {sym}: {e}", file=sys.stderr)

    for sym, shares in to_buy:
        qty      = max(1, int(shares))
        if sym.upper() in already_ordered:
            print(f"  Idempotency: BUY {sym} already has an open order today — skipped")
            continue
        price    = prices.get(sym.upper(), 0.0)
        if price <= 0:
            print(f"  Price guard: {sym} has non-positive price ({price}) — BUY skipped")
            continue
        est_cost = qty * price
        if cash - est_cost < cash_floor:
            print(f"  Risk gate: cash floor — skipping BUY {sym} (cash ${cash:,.0f} near floor ${cash_floor:,.0f})")
            continue
        try:
            client.submit_order(MarketOrderRequest(
                symbol=sym, qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            print(f"  ↑ BUY  {qty:>5} {sym}")
            placed.append(("BUY", sym, qty))
            already_ordered.add(sym.upper())
        except Exception as e:
            print(f"  ✗ BUY  {sym}: {e}", file=sys.stderr)

    return placed


def write_execution_summary(
    placed: list[tuple],
    skipped: list[dict],
    errors: list[str],
    cash_pct_after: float | None,
    data_dir: Path,
    long_only_breach: bool = False,
) -> None:
    """Write data/execution_summary.json so agent.py can report what actually ran.

    This closes the execution-feedback gap: the agent sees exactly which orders
    were submitted, so it won't re-issue the same SELL next run assuming it was
    ignored (the KLAC problem).

    placed  — list of (action, symbol, qty) tuples from alpaca_place_orders()
    skipped — list of {"symbol": ..., "reason": ...} dicts for blocked orders
    errors  — list of plain-string error messages
    long_only_breach — True when a short was open at the start of this run (v3.2)
    """
    payload = {
        "timestamp":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode":           "market_open" if MARKET_OPEN_RUN else "post_close",
        "orders_placed":  [
            {"action": a, "symbol": s, "qty": q, "status": "submitted"}
            for a, s, q in placed
        ],
        "orders_skipped": skipped,
        "errors":         errors,
        "cash_pct_after": round(cash_pct_after, 2) if cash_pct_after is not None else None,
        "long_only_breach": bool(long_only_breach),
    }
    out_path = data_dir / "execution_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Execution summary written: {len(placed)} placed, {len(skipped)} skipped → {out_path}")


# ── Universe ─────────────────────────────────────────────────
def get_sp500_universe() -> list[dict]:
    """Return S&P 500 list as [{symbol, name, sector}]. Falls back to tickers-only list."""
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            timeout=30,
            headers={"User-Agent": "TradeQuestBot/2.0 (paper-trading; github-actions)"},
        )
        resp.raise_for_status()
        table = pd.read_html(io.StringIO(resp.text))[0]
        return [
            {
                "symbol": str(row["Symbol"]).replace(".", "-"),
                "name":   str(row.get("Security", row["Symbol"])),
                "sector": str(row.get("GICS Sector", "Unknown")),
            }
            for _, row in table.iterrows()
        ]
    except Exception as e:
        print(f"Warning: could not fetch S&P 500 list ({e}). Using fallback.", file=sys.stderr)
        return [{"symbol": s, "name": s, "sector": "Unknown"} for s in [
            "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "TSLA",
            "LLY", "JPM", "UNH", "XOM", "V", "MA", "COST", "HD", "PG",
            "ORCL", "JNJ", "ABBV", "CRM", "AMD", "MRK", "NFLX", "NOW",
            "PANW", "CRWD", "TSM", "CDNS", "GEV", "PLTR", "ARM", "ANET",
        ]]


def write_symbols_json(universe: list[dict], data_dir: Path) -> None:
    """Write data/symbols.json — [{symbol, name, sector}] for client-side search."""
    out_path = data_dir / "symbols.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(universe, f, separators=(",", ":"))
    print(f"Wrote {out_path} ({len(universe)} symbols)")


def write_holdings_bars(holdings: list[dict], prices: pd.DataFrame, data_dir: Path) -> None:
    """Write data/bars/{SYMBOL}.json for each current holding (1Y of daily closes)."""
    bars_dir = data_dir / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for h in holdings:
        sym = h["symbol"]
        if sym not in prices.columns:
            continue
        series = prices[sym].dropna().tail(365)
        bars = [
            {"t": ts.strftime("%Y-%m-%d"), "c": round(float(v), 2)}
            for ts, v in series.items()
        ]
        out_path = bars_dir / f"{sym}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(bars, f, separators=(",", ":"))
        written.append(sym)
    if written:
        print(f"Wrote bars for {len(written)} holdings: {', '.join(written)}")


def handle_manual_order(client) -> None:
    """If ORDER_SYMBOL env var is set, place a single paper order via Alpaca."""
    sym = os.environ.get("ORDER_SYMBOL", "").strip().upper()
    if not sym or not client:
        return

    # Validate symbol: only uppercase letters, digits, dot, hyphen (e.g. BRK-B, BF.B)
    import re as _re
    if not _re.fullmatch(r"[A-Z0-9.\-]{1,10}", sym):
        print(f"handle_manual_order: invalid ORDER_SYMBOL '{sym}' — skipping.", file=sys.stderr)
        return

    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, StopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    qty_str  = os.environ.get("ORDER_QTY",          "").strip()
    side_str = os.environ.get("ORDER_SIDE",   "buy").strip().lower()
    type_str = os.environ.get("ORDER_TYPE",  "market").strip().lower()
    tif_str  = os.environ.get("ORDER_TIF",    "day").strip().lower()
    lp_str   = os.environ.get("ORDER_LIMIT_PRICE",   "").strip()
    sp_str   = os.environ.get("ORDER_STOP_PRICE",    "").strip()

    qty = int(qty_str) if qty_str.isdigit() and int(qty_str) > 0 else 0
    if not qty:
        print("handle_manual_order: ORDER_QTY missing or invalid — skipping.", file=sys.stderr)
        return
    # Safety cap: never place a single order for more than 10,000 shares
    max_qty = 10_000
    if qty > max_qty:
        print(f"handle_manual_order: ORDER_QTY {qty} exceeds safety cap {max_qty} — skipping.", file=sys.stderr)
        return

    side = OrderSide.BUY  if side_str == "buy"  else OrderSide.SELL
    tif  = TimeInForce.DAY if tif_str == "day"  else TimeInForce.GTC

    try:
        verify_paper_url()
        if type_str == "limit" and lp_str:
            req = LimitOrderRequest(symbol=sym, qty=qty, side=side,
                                    time_in_force=tif, limit_price=float(lp_str))
        elif type_str == "stop" and sp_str:
            req = StopOrderRequest(symbol=sym, qty=qty, side=side,
                                   time_in_force=tif, stop_price=float(sp_str))
        else:
            req = MarketOrderRequest(symbol=sym, qty=qty, side=side, time_in_force=tif)

        order = client.submit_order(req)
        print(f"Manual order placed: {side_str.upper()} {qty} {sym} ({type_str}) → id={order.id}")
    except Exception as e:
        print(f"handle_manual_order error: {e}", file=sys.stderr)


# ── Price data ────────────────────────────────────────────────
def fetch_prices(tickers: list[str], period: str = "13mo") -> pd.DataFrame:
    print(f"Downloading price data for {len(tickers)} tickers…")
    raw = yf.download(tickers, period=period, auto_adjust=True,
                      progress=False, threads=True)
    # yfinance 1.x: single-ticker → flat columns; multi-ticker → MultiIndex (Price, Ticker)
    if len(tickers) == 1:
        return pd.DataFrame({tickers[0]: raw["Close"]})
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]].rename(columns={"Close": tickers[0]})
    return close.dropna(axis=1, how="all")


def calc_momentum(prices: pd.DataFrame):
    n = len(prices)
    d6  = min(126, n - 2)
    d12 = min(252, n - 2)
    # Skip the most recent 21 trading days (~1 month) to avoid the well-documented
    # short-term reversal effect (Jegadeesh & Titman, 1993).  Using the price from
    # 21 days ago as the "current" price removes the last-month return from the signal.
    skip = min(21, max(1, d6 - 1))
    mom6  = (prices.iloc[-skip] / prices.iloc[-d6  - 1] - 1).rename("mom_6m")
    mom12 = (prices.iloc[-skip] / prices.iloc[-d12 - 1] - 1).rename("mom_12m")
    return mom6, mom12


def calc_vol(prices: pd.DataFrame, window: int = 30) -> pd.Series:
    return (prices.pct_change().tail(window).std() * math.sqrt(252)).rename("vol_30d")


# ── Regime detection ──────────────────────────────────────────
def detect_regime(spy: pd.Series) -> dict:
    price   = float(spy.iloc[-1])
    ma200   = float(spy.tail(200).mean())
    vol     = float(spy.pct_change().tail(30).std() * math.sqrt(252))
    vix_est = round(vol * 100, 1)
    above   = price > ma200

    if above and vol < 0.20:
        regime, exposure = "bull", 0.95
        desc = f"Strong uptrend. SPY above 200-MA, realized vol ~{vix_est:.0f}."
    elif not above or vol > 0.28:
        regime, exposure = "bear", 0.50
        desc = f"Downtrend or elevated vol (~{vix_est:.0f}). Reducing to 50% equity."
    else:
        regime, exposure = "sideways", 0.75
        desc = f"Range-bound market. Neutral allocation, vol ~{vix_est:.0f}."

    confidence = round(min(0.95, 0.60 + abs(price / ma200 - 1) * 3), 2)
    breadth    = round(0.62 if above else 0.38, 2)

    return {
        "market_regime": regime,
        "regime_confidence": confidence,
        "regime_indicators": {
            "vix": vix_est,
            "ma200_trend": "positive" if above else "negative",
            "breadth_pct": breadth,
            "description": desc,
        },
        "equity_exposure": exposure,
        "cash_target": round(1 - exposure, 2),
    }


# ── Fundamentals ──────────────────────────────────────────────
def fetch_fundamentals(symbols: list[str]) -> dict:
    """Fetch fundamentals for all screened candidates.

    Two-tier data pipeline:
      Tier 1 (yfinance)  — primary source; free, no key, fast bulk via .info
      Tier 2 (FMP API)   — fallback for symbols where yfinance returned None
                           for any of: eps_growth, revenue_growth, forward_pe, ma_50d
                           Capped at FMP_FALLBACK_CAP symbols per run to stay within
                           the 250 free-tier daily API call limit.

    FMP note on forward_pe: FMP's 'pe' is trailing P/E (TTM), not analyst-estimated
    forward P/E. When FMP fills the forward_pe field, pe_source='fmp_trailing' is set
    so downstream filters can note the distinction.  The filter still applies fpe < 40
    — trailing P/E < 40 is a slightly more conservative cut than forward P/E < 40,
    which is acceptable as a fallback.
    """
    print(f"Fetching fundamentals for {len(symbols)} candidates (yfinance primary, FMP fallback)…")
    out: dict = {}

    # ── Tier 1: yfinance ─────────────────────────────────────
    for sym in symbols:
        try:
            info    = yf.Ticker(sym).info
            eg_raw  = info.get("earningsGrowth")
            rg_raw  = info.get("revenueGrowth")
            fpe_raw = info.get("forwardPE")
            ma_raw  = info.get("fiftyDayAverage")
            out[sym] = {
                "name":           info.get("longName") or info.get("shortName", sym),
                "sector":         info.get("sector", "Unknown"),
                # Store None when yfinance returns no data so the agent can distinguish
                # "data is missing" from "growth is genuinely zero or negative".
                "eps_growth":     round(eg_raw  * 100, 1) if eg_raw  is not None else None,
                "revenue_growth": round(rg_raw  * 100, 1) if rg_raw  is not None else None,
                "forward_pe":     round(fpe_raw,      1)  if fpe_raw is not None else None,
                "ma_50d":         round(ma_raw,        2)  if ma_raw  is not None else None,
                "current_price":  round(
                    info.get("currentPrice") or info.get("regularMarketPrice") or 0, 2
                ),
                "pe_source": "yfinance",
            }
        except Exception as e:
            print(f"  yfinance {sym}: {e}", file=sys.stderr)
            out[sym] = {
                "name": sym, "sector": "Unknown",
                "eps_growth": None, "revenue_growth": None,
                "forward_pe": None, "ma_50d": None, "current_price": 0,
                "pe_source": None,
            }

    # ── Tier 2: FMP fallback for symbols with missing key fields ─
    _fmp_key_fields = ("eps_growth", "revenue_growth", "forward_pe", "ma_50d")

    if FMP_API_KEY:
        missing = [
            sym for sym in symbols
            if any(out.get(sym, {}).get(f) is None for f in _fmp_key_fields)
        ]
        if missing:
            capped = missing[:FMP_FALLBACK_CAP]
            skipped = len(missing) - len(capped)
            print(
                f"FMP fallback: {len(capped)} symbol(s) missing yfinance data"
                + (f" ({skipped} skipped — FMP_FALLBACK_CAP={FMP_FALLBACK_CAP})" if skipped else "")
            )
            filled_count = 0
            for sym in capped:
                fmp_data = fetch_fmp_fundamentals(sym)
                if not fmp_data:
                    continue
                entry = out.setdefault(sym, {})
                filled_fields: list[str] = []
                for field in _fmp_key_fields:
                    if entry.get(field) is None and field in fmp_data:
                        entry[field] = fmp_data[field]
                        filled_fields.append(field)
                # Propagate FMP metadata flags
                if "pe_source" in fmp_data and entry.get("pe_source") in (None, "yfinance"):
                    entry["pe_source"] = fmp_data["pe_source"]
                # Fill current_price if yfinance returned 0
                if entry.get("current_price", 0) == 0 and fmp_data.get("current_price"):
                    entry["current_price"] = fmp_data["current_price"]
                if filled_fields:
                    print(f"  FMP filled {sym}: {', '.join(filled_fields)}")
                    filled_count += 1
            print(f"FMP fallback complete — enriched {filled_count}/{len(capped)} symbol(s).")
        else:
            print("FMP fallback: all symbols have complete yfinance data — no FMP calls needed.")
    else:
        missing_count = sum(
            1 for sym in symbols
            if any(out.get(sym, {}).get(f) is None for f in _fmp_key_fields)
        )
        if missing_count:
            print(
                f"Warning: {missing_count} symbol(s) have incomplete fundamentals "
                f"(FMP_API_KEY not set — cannot use fallback). "
                f"These will fail the quality filter conservatively.",
                file=sys.stderr,
            )

    return out


# ── Portfolio reconciliation ──────────────────────────────────
def reconcile(screened: list[dict], fundamentals: dict,
              existing: list[dict], cash: float,
              total_value: float) -> tuple[list[dict], list[dict], float]:
    today    = datetime.now().strftime("%Y-%m-%d")
    old      = {h["symbol"]: h for h in existing}
    new_syms = [s["symbol"] for s in screened[:TARGET_N]]
    to_sell  = set(old) - set(new_syms)
    to_buy   = set(new_syms) - set(old)
    trades   = []

    # Sell exits
    for sym in to_sell:
        h     = old[sym]
        price = fundamentals.get(sym, {}).get("current_price") or h.get("current_price", h["avg_cost"])
        pnl   = (price - h["avg_cost"]) * h["shares"]
        cash += h["shares"] * price
        ma50  = fundamentals.get(sym, {}).get("ma_50d", 0)
        reason = ("Price below 50-day MA — stop triggered"
                  if ma50 and price < ma50
                  else "Momentum rank dropped below top 40%")
        trades.append({
            "id": None, "date": today, "action": "SELL",
            "symbol": sym, "name": h["name"],
            "shares": h["shares"], "price": round(price, 2),
            "value": round(h["shares"] * price, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round((price / h["avg_cost"] - 1) * 100, 2),
            "reason": reason,
            "type": "stop" if "stop" in reason else "rebalance",
        })

    # Buy entries
    target_per = total_value / len(new_syms) if new_syms else 0
    for sym in to_buy:
        fi    = fundamentals.get(sym, {})
        price = fi.get("current_price", 0)
        if price <= 0 or cash < price:
            continue
        shares = max(1, int(min(target_per, cash) / price))
        cost   = shares * price
        cash  -= cost
        trades.append({
            "id": None, "date": today, "action": "BUY",
            "symbol": sym, "name": fi.get("name", sym),
            "shares": shares, "price": round(price, 2),
            "value": round(cost, 2),
            "pnl": None, "pnl_pct": None,
            "reason": "Entered top 30% momentum — monthly rebalance",
            "type": "rebalance",
        })

    # Build final holdings
    buy_map = {t["symbol"]: t for t in trades if t["action"] == "BUY"}
    holdings = []
    for item in screened[:TARGET_N]:
        sym = item["symbol"]
        fi  = fundamentals.get(sym, {})
        price = fi.get("current_price") or item.get("current_price", 0)

        if sym in old:
            h = {**old[sym]}
            h.update({
                "current_price": round(price, 2),
                "market_value":  round(h["shares"] * price, 2),
                "pnl":           round((price - h["avg_cost"]) * h["shares"], 2),
                "pnl_pct":       round((price / h["avg_cost"] - 1) * 100, 2),
                "ma_50d":        fi.get("ma_50d", h.get("ma_50d", 0)),
                "status":        "above_ma" if price > fi.get("ma_50d", 0) else "below_ma",
            })
        else:
            bt = buy_map.get(sym)
            shares = bt["shares"] if bt else max(1, int(target_per / price)) if price else 0
            h = {
                "symbol": sym, "name": fi.get("name", sym), "sector": fi.get("sector", "Unknown"),
                "shares": shares, "avg_cost": round(price, 2), "current_price": round(price, 2),
                "market_value": round(shares * price, 2), "weight": 0,
                "pnl": 0.0, "pnl_pct": 0.0,
                "eps_growth": fi.get("eps_growth", 0), "revenue_growth": fi.get("revenue_growth", 0),
                "forward_pe":  fi.get("forward_pe", 0), "volatility_30d": round(item.get("vol_30d", 0), 4),
                "entry_date": today, "ma_50d": fi.get("ma_50d", 0),
                "status": "above_ma" if price > fi.get("ma_50d", 0) else "below_ma",
            }

        h["momentum_rank"]  = item["momentum_rank"]
        h["momentum_6m"]    = round(item.get("momentum_6m", 0), 4)
        h["momentum_12m"]   = round(item.get("momentum_12m", 0), 4)
        for key in ("eps_growth", "revenue_growth", "forward_pe"):
            if fi.get(key):
                h[key] = fi[key]
        holdings.append(h)

    # Recalculate weights
    total_invested = sum(h["market_value"] for h in holdings)
    denom = total_invested + cash or 1
    for h in holdings:
        h["weight"] = round(h["market_value"] / denom, 4)

    return holdings, trades, cash


# ── Summary ───────────────────────────────────────────────────
def compute_summary(holdings, cash, existing_summary, all_trades,
                    deployable_cash: float | None = None) -> dict:
    invested  = sum(h["market_value"] for h in holdings)
    pv        = invested + cash
    initial   = (existing_summary or {}).get("initial_capital", INITIAL_CAPITAL)

    # v3.2: `cash` is the raw broker figure and includes short-sale proceeds (an IOU, not
    # spendable). The dashboard already annotates that. What was missing is an explicit
    # deployable figure for the risk gates and the manual-trade panel to size against.
    short_mv = sum(h["market_value"] for h in holdings if h.get("shares", 0) < 0)
    if deployable_cash is None:
        deployable_cash = max(0.0, cash + short_mv)   # short_mv is negative
    long_mv  = sum(h["market_value"] for h in holdings if h.get("shares", 0) > 0)

    unrealized = sum(h["pnl"] for h in holdings)
    realized   = sum(t["pnl"] for t in all_trades
                     if t["action"] == "SELL" and t.get("pnl") is not None)
    sells      = [t for t in all_trades if t["action"] == "SELL" and t.get("pnl") is not None]
    wins       = [t for t in sells if t["pnl"] > 0]
    losses     = [t for t in sells if t["pnl"] < 0]

    prev = existing_summary or {}
    return {
        "initial_capital":  initial,   # preserve across runs
        "portfolio_value":  round(pv, 2),
        "cash":             round(cash, 2),
        "cash_pct":         round(cash / pv * 100, 2) if pv else 0,
        # v3.2 — the figures the risk gates and the trade panel must size against.
        "deployable_cash":     round(deployable_cash, 2),
        "deployable_cash_pct": round(deployable_cash / pv * 100, 2) if pv else 0,
        "short_proceeds":      round(abs(short_mv), 2),
        "long_exposure_pct":   round(long_mv / pv * 100, 2) if pv else 0,
        "net_exposure_pct":    round(invested / pv * 100, 2) if pv else 0,
        "long_only_breach":    short_mv < 0,
        "invested":         round(invested, 2),
        "total_pnl":        round(unrealized + realized, 2),
        "total_pnl_pct":    round((unrealized + realized) / initial * 100, 2) if initial else 0,
        "realized_pnl":     round(realized, 2),
        "unrealized_pnl":   round(unrealized, 2),
        "win_rate":         round(len(wins) / len(sells), 3) if sells else prev.get("win_rate", 0),
        "total_trades":     len(all_trades),
        # v3.2: win_rate is computed over SELLs that carry an attributed P&L, not over
        # total_trades. Reporting only "50 trades / 12.5% win rate" implied a 50-trade
        # sample when the real denominator was 8. Surface the denominator explicitly.
        "attributed_trades": len(sells),
        "winning_trades":   len(wins),
        "losing_trades":    len(losses),
        "avg_win_pct":      round(sum(t["pnl_pct"] for t in wins)  / len(wins),   1) if wins   else prev.get("avg_win_pct", 0),
        "avg_loss_pct":     round(sum(t["pnl_pct"] for t in losses) / len(losses), 1) if losses else prev.get("avg_loss_pct", 0),
        "sharpe_ratio":     prev.get("sharpe_ratio", 0),    # updated separately if equity curve is long enough
        "max_drawdown_pct": prev.get("max_drawdown_pct", 0),
        "last_updated":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def update_equity_curve(curve: list, pv: float) -> list:
    _now  = datetime.now()
    label = f"{_now.strftime('%b')} {_now.day}"
    if curve and curve[-1]["date"] == label:
        curve[-1]["value"] = round(pv)
    else:
        curve.append({"date": label, "value": round(pv)})
    return curve[-90:]   # keep ~3 months of daily points


def compute_risk_metrics(curve: list) -> dict:
    """Sharpe ratio + max drawdown from the equity curve (F7).

    Sharpe = annualised mean daily return / annualised stdev (risk-free ≈ 0 for a paper account).
    Returns {} when the curve is too short to be meaningful (< 3 points) so callers keep the
    previous value rather than emitting a noisy or zero metric.
    """
    values = [float(p["value"]) for p in curve if p.get("value")]
    if len(values) < 3:
        return {}
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1]]
    if not rets:
        return {}
    mean = sum(rets) / len(rets)
    var  = sum((r - mean) ** 2 for r in rets) / len(rets)
    std  = var ** 0.5
    sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0.0

    peak = values[0]
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1)   # most-negative decline from a running peak

    return {
        "sharpe_ratio":     round(sharpe, 2),
        "max_drawdown_pct": round(abs(max_dd) * 100, 2),
    }


def build_spy_curve(equity_curve: list, spy: "pd.Series", initial_capital: float) -> list:
    """Build SPY benchmark curve normalized to the same start value as the portfolio.

    Reuses the SPY price series already fetched for regime detection — no extra API call.
    Each equity_curve date label ("May 7") is matched to the nearest prior trading day
    in the SPY index. The SPY value is then scaled so the first shared data point equals
    the portfolio's initial_capital, enabling an apples-to-apples visual comparison.
    """
    if not equity_curve or spy is None or spy.empty:
        return []

    try:
        # Build a lookup: "May 7" → close price
        spy_by_label: dict[str, float] = {}
        for ts, price in spy.items():
            label = f"{ts.strftime('%b')} {ts.day}"
            spy_by_label[label] = float(price)

        # Find the SPY price on the first equity_curve date
        first_label = equity_curve[0]["date"]
        spy_start = spy_by_label.get(first_label)
        if spy_start is None or spy_start <= 0:
            # Walk forward up to 5 days to find a trading day close to the start
            for point in equity_curve[1:6]:
                spy_start = spy_by_label.get(point["date"])
                if spy_start and spy_start > 0:
                    break
        if not spy_start or spy_start <= 0:
            return []

        result = []
        for point in equity_curve:
            price = spy_by_label.get(point["date"])
            if price is not None and price > 0:
                normalized = round(price / spy_start * initial_capital)
                result.append({"date": point["date"], "value": normalized})

        return result
    except Exception as e:
        print(f"Warning: build_spy_curve failed ({e})", file=sys.stderr)
        return []


# ── Main ──────────────────────────────────────────────────────
def run_sentinel() -> None:
    """Execute sell orders from sentinel_orders.json immediately via Alpaca.

    Called when update.py is invoked with --sentinel (or SENTINEL_RUN=true).
    Reads the sentinel_orders.json written by bot/sentinel.py, applies risk limits,
    and places Alpaca orders during market hours. Skips the full screener/enrichment
    pipeline — only sync + execute.
    """
    sentinel_file = REPO_ROOT / "data" / "sentinel_orders.json"
    if not sentinel_file.exists():
        print("sentinel mode: sentinel_orders.json not found — nothing to execute.")
        return

    with open(sentinel_file, encoding="utf-8") as f:
        sentinel_data = json.load(f)

    sells_raw = sentinel_data.get("sells", [])
    if not sells_raw:
        print("sentinel mode: no sells in sentinel_orders.json — nothing to execute.")
        return

    verify_paper_url()
    client = _alpaca_client()
    alpaca_state = alpaca_read_state(client)
    if not alpaca_state:
        print("sentinel mode: could not read Alpaca state — aborting.", file=sys.stderr)
        return

    pv              = alpaca_state["portfolio_value"]
    cash            = alpaca_state["cash"]
    deployable_cash = alpaca_state.get("deployable_cash", cash)

    # Build sell list from sentinel rules — bypass the agent approval gate (rules are hard)
    raw_sells = [(s["symbol"], s["shares"]) for s in sells_raw]
    price_map = {
        str(pos.symbol).upper(): float(pos.current_price or pos.avg_entry_price or 0)
        for pos in alpaca_state.get("positions", [])
    }

    # v3.2: the sentinel makes no LLM call, so it is the one path that stays alive through an
    # API outage (it was the only green workflow during the Aug 2026 credit exhaustion) — and
    # it runs during market hours. Covering shorts here means an open long-only breach gets
    # closed even when the agent and market-open pipelines are both down.
    to_cover: list[tuple] = [
        (str(p.symbol).upper(), int(abs(float(p.qty or 0))))
        for p in short_positions(alpaca_state.get("positions", []))
    ]
    for sym, qty in to_cover:
        print(f"sentinel mode: LONG-ONLY BREACH — covering {qty} {sym}")

    # Pass sentinel symbols as pre-approved; apply_risk_limits handles the 30% sell cap
    # and will internally detect overweight positions to bypass that cap for them.
    sentinel_syms = {s["symbol"].upper() for s in sells_raw}
    gated_sells, _ = apply_risk_limits(
        raw_sells, [], pv, deployable_cash, sentinel_syms, price_map
    )

    print(f"sentinel mode: placing {len(to_cover)} cover(s) + {len(gated_sells)} sell order(s) via Alpaca")
    placed = alpaca_place_orders(
        client, gated_sells, [], pv, deployable_cash, price_map, to_cover=to_cover
    )

    executed_syms = {sym.upper() for _, sym, _ in placed}
    exec_placed: list[tuple] = placed
    exec_skipped = [
        {"symbol": s["symbol"], "reason": "sentinel rule triggered but order blocked by risk gate"}
        for s in sells_raw
        if s["symbol"].upper() not in executed_syms
    ]
    cash_pct = round(cash / pv * 100, 2) if pv else None
    write_execution_summary(exec_placed, exec_skipped, [], cash_pct, REPO_ROOT / "data",
                            long_only_breach=bool(to_cover))
    print("sentinel mode: execution complete.")


def main():
    if SENTINEL_RUN:
        run_sentinel()
        return

    # Load existing state
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        print("No existing portfolio.json — starting fresh.")
        data = {
            "meta":         {"initial_capital": INITIAL_CAPITAL},
            "summary":      {"initial_capital": INITIAL_CAPITAL, "cash": INITIAL_CAPITAL, "portfolio_value": INITIAL_CAPITAL},
            "filter_status": {},
            "equity_curve": [],
            "holdings":     [],
            "trades":       [],
        }

    existing_holdings = data.get("holdings", [])
    existing_trades   = data.get("trades", [])
    cash              = data["summary"].get("cash", float(INITIAL_CAPITAL))

    # 1. Universe + prices
    universe = get_sp500_universe()
    tickers  = [u["symbol"] for u in universe]
    prices  = fetch_prices(tickers)
    available = list(prices.columns)
    print(f"Price data: {len(available)} tickers.")

    spy = prices.get("SPY") or yf.download("SPY", period="13mo", auto_adjust=True, progress=False)["Close"].squeeze()

    # 2. Signals
    mom6, mom12 = calc_momentum(prices)
    vol30       = calc_vol(prices)

    # 3. Screen
    vol_90th   = float(vol30.quantile(0.90))
    mom_score  = (mom6.rank(pct=True) + mom12.rank(pct=True)) / 2
    # Parentheses are required: & has higher precedence than >
    candidates = (mom_score[(mom_score > 0.70) & (vol30 < vol_90th)]
                  .sort_values(ascending=False)
                  .index.tolist()[:CANDIDATES_CAP])

    fundamentals = fetch_fundamentals(candidates)

    quality_pass = valuation_pass = 0
    screened = []
    for sym in candidates:
        fi  = fundamentals.get(sym, {})
        eg  = fi.get("eps_growth")     # None = data missing
        rg  = fi.get("revenue_growth") # None = data missing
        fpe = fi.get("forward_pe")     # None = data missing
        # Quality: require confirmed data — missing data fails conservatively.
        # STRATEGY.md: "filters out low-quality junk momentum"
        q_ok = eg is not None and rg is not None and eg > 10 and rg > 8
        # Valuation: STRATEGY.md says "falls back to relaxed rules when data unavailable"
        v_ok = fpe is None or fpe < 40
        # Trend gate: only enter a position that is already above its 50-day MA.
        # Prevents buying a stock that would immediately trigger the trend-break sell rule.
        # Lenient when MA data is unavailable (None) to avoid blocking new listings.
        ma50  = fi.get("ma_50d")
        price = fi.get("current_price", 0)
        t_ok  = ma50 is None or price <= 0 or price > ma50
        if q_ok:
            quality_pass += 1
        if v_ok:
            valuation_pass += 1
        if q_ok and v_ok and t_ok:
            screened.append({
                "symbol":        sym,
                "momentum_rank": len(screened) + 1,
                "momentum_6m":   round(float(mom6.get(sym, 0)), 4),
                "momentum_12m":  round(float(mom12.get(sym, 0)), 4),
                "vol_30d":       round(float(vol30.get(sym, 0)), 4),
                "current_price": fi.get("current_price", 0),
            })

    # Deduplicate dual-class share pairs: keep the highest-momentum ticker per issuer.
    # screened is already ordered by momentum, so the first occurrence wins.
    seen_issuers: dict[str, str] = {}
    deduped: list[dict] = []
    for item in screened:
        sym    = item["symbol"]
        issuer = ISSUER_MAP.get(sym, sym)
        if issuer not in seen_issuers:
            seen_issuers[issuer] = sym
            deduped.append(item)
        else:
            print(f"  Dedup: dropped {sym} (same issuer as {seen_issuers[issuer]})")
    screened = deduped

    print(f"Screened: {len(screened)} pass all filters. Targeting {TARGET_N}.")

    # Build a rank lookup so Alpaca positions can be annotated with momentum scores
    screened_ranks = {
        s["symbol"]: {
            "rank":    s["momentum_rank"],
            "mom_6m":  s.get("momentum_6m", 0),
            "mom_12m": s.get("momentum_12m", 0),
        }
        for s in screened
    }

    # 4. Regime
    regime_info = detect_regime(spy)

    # ── 5a. Alpaca mode (real paper-trading account) ───────────
    client      = _alpaca_client()
    alpaca_state = alpaca_read_state(client) if client else None

    if alpaca_state:
        print("Alpaca mode — reading live paper positions and placing orders.")

        # Guard: refuse to run against a live (non-paper) endpoint
        verify_paper_url()

        # Build existing holdings map for entry_date preservation
        existing_map = {h["symbol"]: h for h in existing_holdings}

        # Current Alpaca positions → holdings (preserves entry_date via existing_map)
        new_holdings = alpaca_positions_to_holdings(
            alpaca_state["positions"], fundamentals, screened_ranks, vol30, existing_map
        )

        cash            = alpaca_state["cash"]
        deployable_cash = alpaca_state.get("deployable_cash", cash)
        pv              = alpaca_state["portfolio_value"]

        # Trade history is needed BEFORE order placement so the v3.2 re-entry cooldown can
        # see recent sells. It is recomputed after the orders land.
        all_trades = alpaca_orders_to_trades(alpaca_state["orders"])

        # ── v3.2 long-only breach alarm ──────────────────────────
        # A long-only strategy holding a short is a system-down condition, not a data point.
        # This is the alarm that was missing while JBL sat at -22 shares for four months.
        shorts = alpaca_state.get("shorts") or []
        if shorts:
            print("\n" + "!" * 68)
            print("  LONG-ONLY INVARIANT BREACH — short position(s) open")
            for p in shorts:
                sym = str(getattr(p, "symbol", "?")).upper()
                q   = float(getattr(p, "qty", 0) or 0)
                mv  = float(getattr(p, "market_value", 0) or 0)
                pct = f" ({mv / pv * 100:.1f}% of portfolio)" if pv else ""
                print(f"    {sym}: {q:g} shares, market value ${mv:,.2f}{pct}")
            print(f"  Raw broker cash ${cash:,.2f} includes short proceeds; "
                  f"deployable is ${deployable_cash:,.2f}.")
            print("  A cover order is queued below. To remediate the whole book at once, run:")
            print("    python bot/rebalance_trueup.py --dry-run")
            print("!" * 68 + "\n")

        # ── v2.2 Quarterly lock + profit gate ────────────────────
        # Determine whether this run is in a quarterly rebalance month.
        # Non-quarterly months: only Tier 1 sells (unrealized loss positions) may execute.
        # Quarterly months: full rebalance — buys and all approved sells may execute.
        quarterly = is_quarterly_month()
        if quarterly:
            print("Quarter lock: QUARTERLY month — full rebalance allowed (buys + all Tier 1/2 sells).")
        else:
            print("Quarter lock: NON-QUARTERLY month — buys BLOCKED; only Tier 1 (loss) sells allowed.")

        # ── F3 (v3.2): explicit COVER path — the way out of the JBL deadlock ──────
        # v3.1 stopped the short from GROWING but left no way to CLOSE it: the long-only
        # guard skips SELL when held ≤ 0; a short symbol is in current_syms so it is never a
        # buy candidate; and buys are blocked outside quarterly months. The agent flagged
        # "BUY-TO-COVER MANDATORY" on every run from 2026-08-07 with no order it could produce.
        # Restoring an invariant is not a discretionary trade, so covers bypass the quarterly
        # lock, the sector cap, the agent-approval gate and the per-run order budget.
        to_cover: list[tuple] = []
        for h in new_holdings:
            held = int(h.get("shares", 0) or 0)
            if held < 0:
                to_cover.append((h["symbol"], abs(held)))
                print(f"  COVER: BUY {abs(held)} {h['symbol']} — restore long-only invariant "
                      f"(bypasses quarterly lock and order caps)")

        # Determine rebalance orders using the v3.2 hysteresis band rather than a symmetric
        # top-N diff. Shorts are excluded from both lists — they are handled by to_cover.
        current_syms = {h["symbol"] for h in new_holdings if int(h.get("shares", 0) or 0) > 0}
        target_syms  = {s["symbol"] for s in screened[:TARGET_N]}
        rank_of      = {s["symbol"]: s["momentum_rank"] for s in screened}

        # v3.1: equal-weight dollar target per position (deploy to CASH_FLOOR_PCT).
        # Sizing by dollars — not by shares/price — so an expensive single share (e.g. STX ~$913)
        # no longer becomes an oversized position while the best-ranked names stay tiny.
        n_target   = TARGET_N if TARGET_N else 1
        deployable = pv * (1 - CASH_FLOOR_PCT)
        target_per = min(deployable / n_target, pv * MAX_POSITION_PCT)

        # Build sell list. Two gates stack on top of the rank test:
        #   • hysteresis + min-hold (v3.2) — kills the boundary churn
        #   • profit gate (v2.2)          — outside quarterly months only losers may exit
        raw_sells: list[tuple] = []
        quarterly_deferred: list[str] = []
        churn_deferred: list[str] = []
        for h in new_holdings:
            held = int(h.get("shares", 0) or 0)
            if held <= 0:
                continue   # shorts and flats are handled by to_cover, never re-sold here
            do_exit, why = should_exit_on_rank(h, rank_of.get(h["symbol"]))
            if not do_exit:
                if h["symbol"] not in target_syms:
                    churn_deferred.append(f"{h['symbol']} ({why})")
                continue
            if quarterly or not has_unrealized_gain(h):
                # Quarterly month: all exits allowed.
                # Non-quarterly: only losing positions (Tier 1 loss-harvest).
                raw_sells.append((h["symbol"], held))
                print(f"  Exit: {h['symbol']} — {why}")
            else:
                quarterly_deferred.append(h["symbol"])

        if churn_deferred:
            print(f"  Churn damper: held despite being outside the top {TARGET_N} — "
                  f"{'; '.join(churn_deferred)}")
        if quarterly_deferred:
            print(
                f"  Hold gate: deferred profitable exit(s) to next quarterly — "
                f"{', '.join(quarterly_deferred)} (unrealized gain; non-quarterly month)"
            )

        # ── Buy list ──────────────────────────────────────────────────────────────
        # New ENTRANTS remain quarterly-only (v2.2 Execution Lock, unchanged).
        # v3.2 adds REDEPLOYMENT: topping up names already inside the top-N is allowed in any
        # month once deployable cash drifts above the floor + CASH_DEPLOY_BAND. Without this
        # the book could only shrink between quarterlies — Aug 2026 ran 4 sells and 0 buys,
        # which is the real cash-drag engine, not the per-run order throttle v3.1 addressed.
        to_buy_syms = target_syms - current_syms
        raw_buys: list[tuple] = []

        if quarterly:
            for sym in to_buy_syms:
                price = fundamentals.get(sym, {}).get("current_price", 0) or 0
                if price <= 0:
                    continue
                qty = int(target_per // price)
                if qty >= 1:
                    raw_buys.append((sym, qty))
        elif to_buy_syms:
            print(
                f"  Quarter lock: new-entrant BUYs BLOCKED in non-quarterly month — "
                f"{', '.join(sorted(to_buy_syms))} deferred to next quarterly rebalance."
            )

        deploy_trigger = CASH_FLOOR_PCT + CASH_DEPLOY_BAND
        deployable_pct = (deployable_cash / pv) if pv else 0.0
        if not quarterly and deployable_pct > deploy_trigger:
            selling_syms = {s for s, _ in raw_sells}
            topups: list[tuple] = []
            for h in sorted(new_holdings, key=lambda x: rank_of.get(x["symbol"], 9999)):
                sym = h["symbol"]
                if sym in selling_syms or sym not in target_syms:
                    continue
                price = h.get("current_price", 0) or 0
                gap   = target_per - float(h.get("market_value", 0) or 0)
                if price <= 0 or gap <= price:
                    continue
                qty = int(gap // price)
                if qty >= 1:
                    topups.append((sym, qty))
            if topups:
                print(f"  Cash deploy: {deployable_pct:.1%} deployable > trigger "
                      f"{deploy_trigger:.1%} — topping up {len(topups)} top-{TARGET_N} name(s) "
                      f"toward ${target_per:,.0f} each")
                raw_buys.extend(topups)

        # v3.2 re-entry cooldown: never re-buy a name sold within REENTRY_COOLDOWN_DAYS.
        if raw_buys:
            filtered: list[tuple] = []
            for sym, qty in raw_buys:
                age = in_reentry_cooldown(sym, all_trades)
                if age is not None:
                    print(f"  Cooldown: {sym} was sold {age}d ago "
                          f"(< {REENTRY_COOLDOWN_DAYS}d) — BUY skipped")
                    continue
                filtered.append((sym, qty))
            raw_buys = filtered

        # ── F4: enforce the sector cap at quarterly rebalance (v3.1 — was advisory-only) ──
        # Drop the lowest-ranked buy candidates from any sector that would breach MAX_SECTOR_PCT.
        if quarterly and raw_buys:
            rank_of = {s["symbol"]: s["momentum_rank"] for s in screened}
            sector_val: dict[str, float] = {}
            exiting_syms = {s for s, _ in raw_sells}
            for h in new_holdings:
                if int(h.get("shares", 0) or 0) > 0 and h["symbol"] not in exiting_syms:
                    sec = h.get("sector", "Unknown")
                    sector_val[sec] = sector_val.get(sec, 0.0) + float(h.get("market_value", 0))
            cap_val = pv * MAX_SECTOR_PCT
            kept_buys: list[tuple] = []
            # Add best-ranked buys first so a sector keeps its strongest names when trimming.
            for sym, qty in sorted(raw_buys, key=lambda sb: rank_of.get(sb[0], 9999)):
                sec   = fundamentals.get(sym, {}).get("sector", "Unknown")
                price = fundamentals.get(sym, {}).get("current_price", 0) or 0
                add_val = qty * price
                if sector_val.get(sec, 0.0) + add_val > cap_val:
                    print(f"  Sector cap: dropping BUY {sym} — {sec} would exceed {MAX_SECTOR_PCT:.0%}")
                    continue
                sector_val[sec] = sector_val.get(sec, 0.0) + add_val
                kept_buys.append((sym, qty))
            raw_buys = kept_buys

        # Load what Claude approved and apply all risk limits before touching the broker.
        # Both pre-market and post-close runs execute "next_open" decisions.
        # Legacy "immediate" labels are treated as next_open inside load_agent_approvals.
        target_urgency  = "next_open"
        agent_approvals = load_agent_approvals(target_urgency=target_urgency)
        price_map = {h["symbol"]: h.get("current_price", 0.0) for h in new_holdings}
        price_map.update({
            sym: fundamentals.get(sym, {}).get("current_price", 0.0)
            for sym in to_buy_syms
        })
        # v3.1: a quarterly rebalance completes the full rotation in one run (wide budget);
        # daily/non-quarterly runs keep the tight MAX_ORDERS_PER_RUN / 30%-sell throttles.
        if quarterly:
            run_max_orders, run_max_sell_pct = REBALANCE_MAX_ORDERS, REBALANCE_MAX_SELL_PCT
        else:
            run_max_orders, run_max_sell_pct = MAX_ORDERS_PER_RUN, MAX_SELL_VALUE_PCT
        # v3.2: risk gates size against DEPLOYABLE cash, not the raw broker figure — with a
        # short open, account.cash includes proceeds the account must give back.
        to_sell, to_buy = apply_risk_limits(
            raw_sells, raw_buys, pv, deployable_cash, agent_approvals["SELL"], price_map,
            max_orders=run_max_orders, max_sell_pct=run_max_sell_pct,
        )

        exec_placed:  list[tuple] = []
        exec_skipped: list[dict]  = []
        exec_errors:  list[str]   = []

        if to_cover or to_sell or to_buy:
            print(f"Rebalance: {len(to_cover)} covers, {len(to_sell)} sells, "
                  f"{len(to_buy)} buys (after risk gates)")
            exec_placed = alpaca_place_orders(
                client, to_sell, to_buy, pv, deployable_cash, price_map, to_cover=to_cover
            )
            # Brief pause then re-fetch: during-hours fills settle in <1s;
            # after-hours orders appear as pending in the open-orders list.
            time.sleep(3)
            refreshed = alpaca_read_state(client)
            if refreshed:
                new_holdings = alpaca_positions_to_holdings(
                    refreshed["positions"], fundamentals, screened_ranks, vol30, existing_map
                )
                cash            = refreshed["cash"]
                deployable_cash = refreshed.get("deployable_cash", cash)
                pv              = refreshed["portfolio_value"]
                all_trades      = alpaca_orders_to_trades(refreshed["orders"])
            else:
                all_trades = alpaca_orders_to_trades(alpaca_state["orders"])
        else:
            print("No orders placed — either no rebalance needed or risk gates blocked all orders.")

        # Collect symbols that were proposed but blocked by risk gates for skipped list
        proposed_sells = {sym for sym, _ in raw_sells}
        executed_sells = {sym for action, sym, _ in exec_placed if action == "SELL"}
        for sym in proposed_sells - executed_sells:
            exec_skipped.append({"symbol": sym, "reason": "blocked by agent approval or risk gate"})

        cash_pct_now = round(cash / pv * 100, 2) if pv else None
        write_execution_summary(
            exec_placed, exec_skipped, exec_errors,
            cash_pct_now, REPO_ROOT / "data",
            long_only_breach=bool(shorts),
        )

        # Handle manual order triggered via workflow_dispatch inputs
        handle_manual_order(client)

        # NOTE: all_trades is already set above — from the post-order refresh when one
        # succeeded, otherwise from the pre-order state. Recomputing it from the stale
        # alpaca_state here would discard orders placed moments ago.

        # Recalculate weights on current holdings
        total_mv = sum(h["market_value"] for h in new_holdings)
        denom    = total_mv + cash or 1
        for h in new_holdings:
            h["weight"] = round(h["market_value"] / denom, 4)

        summary = compute_summary(new_holdings, cash, data.get("summary"), all_trades,
                                  deployable_cash=deployable_cash)

    # ── 5b. Simulation mode (no Alpaca credentials) ────────────
    else:
        print("Simulation mode — estimating portfolio from yfinance data.")

        current_pv = sum(
            h["shares"] * (fundamentals.get(h["symbol"], {}).get("current_price")
                           or h.get("current_price", h["avg_cost"]))
            for h in existing_holdings
        ) + cash

        new_holdings, new_trades, cash = reconcile(
            screened, fundamentals, existing_holdings, cash, current_pv
        )

        base = len(existing_trades)
        for i, t in enumerate(new_trades):
            t["id"] = f"T{base + len(new_trades) - i:03d}"
        all_trades = new_trades + existing_trades

        pv      = sum(h["market_value"] for h in new_holdings) + cash
        summary = compute_summary(new_holdings, cash, data.get("summary"), all_trades)

    # ── 6. Equity curve + SPY benchmark curve ─────────────────
    curve     = update_equity_curve(data.get("equity_curve", []), pv)
    initial   = data.get("meta", {}).get("initial_capital", INITIAL_CAPITAL)
    spy_curve = build_spy_curve(curve, spy, initial)

    # F7: refresh Sharpe + max drawdown from the freshly-extended equity curve.
    summary.update(compute_risk_metrics(curve))

    # ── 7. Filter status ───────────────────────────────────────
    # In Alpaca mode holdings come from live positions (may include non-screened stocks),
    # so we validate the actual portfolio rather than reporting screened-candidate stats.
    nh = len(new_holdings)
    if alpaca_state:
        momentum_pass_held  = sum(1 for h in new_holdings if h.get("momentum_rank", 0) > 0)
        quality_pass_held   = sum(
            1 for h in new_holdings
            if h.get("eps_growth") is not None
            and h.get("revenue_growth") is not None
            and h["eps_growth"] > 10
            and h["revenue_growth"] > 8
        )
        valuation_pass_held = sum(
            1 for h in new_holdings
            if h.get("forward_pe") is None or h["forward_pe"] < 40
        )
        risk_pass_held = sum(
            1 for h in new_holdings if h.get("volatility_30d", 0) < vol_90th
        )
    else:
        momentum_pass_held  = nh
        quality_pass_held   = min(quality_pass, nh)
        valuation_pass_held = min(valuation_pass, nh)
        risk_pass_held      = nh

    filter_status = {
        "momentum":  {"label": "Momentum",  "description": "Top 30% by 6M & 12M return",          "passing": momentum_pass_held,  "total": nh, "threshold": "Top 30%"},
        "quality":   {"label": "Quality",   "description": "EPS growth >10%, Revenue growth >8%",  "passing": quality_pass_held,   "total": nh, "threshold": "EPS >10% & Rev >8%"},
        "valuation": {"label": "Valuation", "description": "Forward P/E <40 or top 70% by sector", "passing": valuation_pass_held, "total": nh, "threshold": "Fwd P/E <40"},
        "risk":      {"label": "Risk",      "description": "Volatility below 90th percentile",     "passing": risk_pass_held,      "total": nh, "threshold": "Vol < 90th pct"},
    }

    # ── 8. Write portfolio.json ────────────────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    output = {
        "meta": {
            **data.get("meta", {}),
            "strategy":         "TradeQuest AI Momentum Strategy v3.2",
            "strategy_version": "3.2",
            "universe":       "S&P 500",
            "account_name":   "TradeQuest Paper",
            "mode":           "alpaca" if alpaca_state else "simulation",
            "initial_capital": data.get("meta", {}).get("initial_capital", INITIAL_CAPITAL),
            "last_rebalance": today,
            # v3.2: was "1st of next month", which advertised 2026-09-01 while new entrants
            # actually stay locked until the next QUARTERLY month (Oct). Report the real date.
            "next_rebalance": next_quarterly_date(),
            **regime_info,
        },
        "summary":       summary,
        "filter_status": filter_status,
        "equity_curve":  curve,
        "spy_curve":     spy_curve,
        "benchmark":     spy_curve,
        "holdings":      new_holdings,
        "trades":        all_trades[:50],
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    mode_label = f"Alpaca ({ALPACA_ACCOUNT_NAME})" if alpaca_state else "Simulation"
    print(f"Done [{mode_label}]. Value: ${pv:,.2f} | Holdings: {nh} | Cash: ${cash:,.2f}")

    # ── 9. Write static data files for PWA ─────────────────────
    data_dir = REPO_ROOT / "data"
    write_symbols_json(universe, data_dir)
    write_holdings_bars(new_holdings, prices, data_dir)


if __name__ == "__main__":
    main()
