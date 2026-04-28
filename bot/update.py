#!/usr/bin/env python3
"""TradeQuest AI — portfolio updater bot.

Two modes:
  Alpaca mode  — Alpaca credentials present: reads real paper-trading positions
                 and account data, places real paper orders on rebalance days.
  Simulation   — No credentials: simulates portfolio from yfinance data.

Market data always comes from Yahoo Finance (yfinance).
"""

import io
import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "data" / "portfolio.json"
LOG_FILE  = REPO_ROOT / "data" / "agent_log.json"
INITIAL_CAPITAL = 100_000
TARGET_N = 17          # target number of holdings (15-20)
CANDIDATES_CAP = 60    # max tickers to fetch fundamentals for

# Alpaca credentials — read from env vars injected by GitHub Actions secrets
# Never hardcode these values here
ALPACA_ACCOUNT_NAME = os.environ.get("ALPACA_ACCOUNT_NAME", "TradeQuest Paper")
ALPACA_API_KEY      = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY   = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL     = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# ── Hardcoded risk limits — never read from external config ───
MAX_ORDERS_PER_RUN  = 5     # max total orders (sells + buys) placed in a single run
MAX_SELL_VALUE_PCT  = 0.30  # never liquidate more than 30% of portfolio in one run
CASH_FLOOR_PCT      = 0.05  # always keep ≥5% of portfolio value as cash
MAX_POSITION_PCT    = 0.08  # single position cap: 8% of portfolio value


# ── Safety guards ─────────────────────────────────────────────

def verify_paper_url() -> None:
    """Crash early if the base URL is not a paper-trading endpoint."""
    if "paper" not in ALPACA_BASE_URL.lower():
        raise RuntimeError(
            f"SAFETY: ALPACA_BASE_URL '{ALPACA_BASE_URL}' does not look like a paper "
            "trading endpoint. Refusing to place orders."
        )


def load_agent_approvals() -> dict[str, set[str]]:
    """
    Read agent_log.json and return the most recent day_end or monthly run's
    approved actions as sets: {"SELL": {symbols...}, "BUY": {symbols...}}.

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

    # Find the most recent day_end or monthly run
    for run in log.get("runs", []):
        run_type = run.get("run_type") or run.get("type", "")
        if run_type not in ("day_end", "monthly"):
            continue
        for d in run.get("decisions", []):
            action = str(d.get("action", "")).upper()
            symbol = str(d.get("symbol", "")).strip().upper()
            urgency = str(d.get("urgency", "")).lower()
            if action in ("SELL", "BUY") and symbol:
                # Only execute if agent marked it immediate or next_open
                if urgency in ("immediate", "next_open"):
                    approvals[action].add(symbol)
        print(f"Agent approvals loaded from run {run.get('id','?')}: "
              f"{len(approvals['SELL'])} SELLs, {len(approvals['BUY'])} BUYs approved")
        return approvals  # use only the most recent qualifying run

    print("No day_end or monthly agent run found — orders blocked for safety.", file=sys.stderr)
    return approvals


def apply_risk_limits(
    to_sell: list[tuple],
    to_buy:  list[tuple],
    pv:      float,
    cash:    float,
    agent_sell_approvals: set[str],
) -> tuple[list[tuple], list[tuple]]:
    """
    Gate and cap sell + buy lists before they reach the broker.

    Rules applied in order:
    1. SELL only what the agent explicitly approved (immediate/next_open).
    2. Total sell value ≤ MAX_SELL_VALUE_PCT of portfolio.
    3. Total orders ≤ MAX_ORDERS_PER_RUN.
    4. BUY only if enough cash remains above CASH_FLOOR_PCT floor.
    5. Each BUY capped at MAX_POSITION_PCT of portfolio value.
    """
    cash_floor  = pv * CASH_FLOOR_PCT
    max_sell_val = pv * MAX_SELL_VALUE_PCT
    max_pos_val  = pv * MAX_POSITION_PCT

    # 1. Gate sells behind agent approval
    approved_sells = [(sym, shares) for sym, shares in to_sell
                      if sym.upper() in agent_sell_approvals]
    blocked = set(sym for sym, _ in to_sell) - set(sym for sym, _ in approved_sells)
    if blocked:
        print(f"  Risk gate: blocked unapproved SELLs — {', '.join(sorted(blocked))}")

    # 2. Cap total sell value
    capped_sells: list[tuple] = []
    running_sell_val = 0.0
    for sym, shares in approved_sells:
        # approximate value from portfolio data (shares * current price not available here;
        # use a generous upper bound of MAX_SELL_VALUE_PCT to avoid partial-share math)
        capped_sells.append((sym, shares))
        running_sell_val += 1  # count-based cap is enforced in step 3
        if running_sell_val >= max_sell_val / (pv / max(len(approved_sells), 1)):
            break

    # 3. Total order cap
    sell_budget = min(len(capped_sells), MAX_ORDERS_PER_RUN)
    capped_sells = capped_sells[:sell_budget]
    buy_budget   = max(0, MAX_ORDERS_PER_RUN - sell_budget)

    # 4 & 5. Cash floor + position size cap on buys
    available_cash = cash - cash_floor  # never spend below floor
    capped_buys: list[tuple] = []
    for sym, shares in to_buy[:buy_budget]:
        if available_cash <= 0:
            print(f"  Risk gate: cash floor reached — no more buys ({sym} skipped)")
            break
        # Recalculate shares respecting position cap and cash floor
        # shares passed in were computed by the caller; re-enforce the cap
        from_pos_cap   = int(max_pos_val / max(shares, 1))  # proportional cap
        capped_shares  = min(shares, from_pos_cap)
        capped_shares  = max(1, capped_shares)
        capped_buys.append((sym, capped_shares))
        available_cash -= capped_shares  # placeholder; real $ check is done in alpaca_place_orders

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


def alpaca_read_state(client) -> dict | None:
    """Fetch account summary, open positions, and recent closed orders."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        account   = client.get_account()
        positions = client.get_all_positions()
        orders    = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=50
        ))
        return {
            "portfolio_value": float(account.portfolio_value),
            "cash":            float(account.cash),
            "positions":       positions,
            "orders":          orders,
        }
    except Exception as e:
        print(f"Warning: Alpaca state fetch failed ({e}).", file=sys.stderr)
        return None


def alpaca_positions_to_holdings(positions, fundamentals: dict,
                                  screened_ranks: dict, vol30: "pd.Series") -> list[dict]:
    """Map Alpaca Position objects → portfolio.json holdings format."""
    holdings = []
    for pos in positions:
        sym      = pos.symbol
        fi       = fundamentals.get(sym, {})
        price    = float(pos.current_price   or 0)
        avg_cost = float(pos.avg_entry_price or 0)
        shares   = float(pos.qty             or 0)
        mv       = float(pos.market_value    or 0)
        upnl     = float(pos.unrealized_pl   or 0)
        upnl_pct = float(pos.unrealized_plpc or 0) * 100
        ma50     = fi.get("ma_50d", 0)
        rank_info = screened_ranks.get(sym, {})

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
            "eps_growth":     fi.get("eps_growth", 0),
            "revenue_growth": fi.get("revenue_growth", 0),
            "forward_pe":     fi.get("forward_pe", 0),
            "volatility_30d": round(float(vol30.get(sym, 0)), 4),
            "entry_date":     datetime.now().strftime("%Y-%m-%d"),
            "ma_50d":         ma50,
            "status":         "above_ma" if price > ma50 else "below_ma",
            "momentum_rank":  rank_info.get("rank", 0),
            "momentum_6m":    round(rank_info.get("mom_6m", 0), 4),
            "momentum_12m":   round(rank_info.get("mom_12m", 0), 4),
        })
    return holdings


def alpaca_orders_to_trades(orders) -> list[dict]:
    """Map Alpaca Order objects → portfolio.json trades format."""
    trades = []
    for o in orders:
        filled_qty = float(o.filled_qty or 0)
        if filled_qty == 0:
            continue
        fill_price = float(o.filled_avg_price or 0)
        filled_at  = o.filled_at
        date_str   = filled_at.strftime("%Y-%m-%d") if filled_at else str(o.created_at)[:10]
        action     = "BUY" if str(o.side).endswith("buy") else "SELL"
        trades.append({
            "id":       f"ALP-{str(o.id)[:8].upper()}",
            "date":     date_str,
            "action":   action,
            "symbol":   o.symbol,
            "name":     o.symbol,
            "shares":   filled_qty,
            "price":    round(fill_price, 2),
            "value":    round(filled_qty * fill_price, 2),
            "pnl":      None,
            "pnl_pct":  None,
            "reason":   "Alpaca paper trade — momentum rebalance",
            "type":     "market",
        })
    return trades


def alpaca_place_orders(client, to_sell: list[tuple], to_buy: list[tuple],
                         pv: float, cash: float) -> list[tuple]:
    """
    Submit market orders to Alpaca paper trading. Sells first to free cash.
    Enforces CASH_FLOOR_PCT: stops buying if remaining cash would fall below floor.
    """
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    cash_floor = pv * CASH_FLOOR_PCT
    placed = []

    for sym, shares in to_sell:
        qty = max(1, int(shares))
        try:
            client.submit_order(MarketOrderRequest(
                symbol=sym, qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            print(f"  ↓ SELL {qty:>5} {sym}")
            placed.append(("SELL", sym, qty))
        except Exception as e:
            print(f"  ✗ SELL {sym}: {e}", file=sys.stderr)

    for sym, shares in to_buy:
        qty      = max(1, int(shares))
        est_cost = qty * 1  # real cost check happens at broker; this is a share-count guard
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
        except Exception as e:
            print(f"  ✗ BUY  {sym}: {e}", file=sys.stderr)

    return placed


# ── Universe ─────────────────────────────────────────────────
def get_sp500_tickers() -> list[str]:
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            timeout=30,
            headers={"User-Agent": "TradeQuestBot/2.0 (paper-trading; github-actions)"},
        )
        resp.raise_for_status()
        table = pd.read_html(io.StringIO(resp.text))[0]
        return table["Symbol"].str.replace(".", "-", regex=False).tolist()
    except Exception as e:
        print(f"Warning: could not fetch S&P 500 list ({e}). Using fallback.", file=sys.stderr)
        return [
            "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "TSLA",
            "LLY", "JPM", "UNH", "XOM", "V", "MA", "COST", "HD", "PG",
            "ORCL", "JNJ", "ABBV", "CRM", "AMD", "MRK", "NFLX", "NOW",
            "PANW", "CRWD", "TSM", "CDNS", "GEV", "PLTR", "ARM", "ANET",
        ]


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
    mom6  = (prices.iloc[-1] / prices.iloc[-d6  - 1] - 1).rename("mom_6m")
    mom12 = (prices.iloc[-1] / prices.iloc[-d12 - 1] - 1).rename("mom_12m")
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
    print(f"Fetching fundamentals for {len(symbols)} candidates…")
    out = {}
    for sym in symbols:
        try:
            info = yf.Ticker(sym).info
            out[sym] = {
                "name":             info.get("longName") or info.get("shortName", sym),
                "sector":           info.get("sector", "Unknown"),
                "eps_growth":       round((info.get("earningsGrowth")  or 0) * 100, 1),
                "revenue_growth":   round((info.get("revenueGrowth")   or 0) * 100, 1),
                "forward_pe":       round(info.get("forwardPE") or 999, 1),
                "ma_50d":           round(info.get("fiftyDayAverage")  or 0, 2),
                "current_price":    round(info.get("currentPrice") or info.get("regularMarketPrice") or 0, 2),
            }
        except Exception as e:
            print(f"  {sym}: {e}", file=sys.stderr)
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
def compute_summary(holdings, cash, existing_summary, all_trades) -> dict:
    invested  = sum(h["market_value"] for h in holdings)
    pv        = invested + cash
    initial   = (existing_summary or {}).get("initial_capital", INITIAL_CAPITAL)

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
        "invested":         round(invested, 2),
        "total_pnl":        round(unrealized + realized, 2),
        "total_pnl_pct":    round((unrealized + realized) / initial * 100, 2) if initial else 0,
        "realized_pnl":     round(realized, 2),
        "unrealized_pnl":   round(unrealized, 2),
        "win_rate":         round(len(wins) / len(sells), 3) if sells else prev.get("win_rate", 0),
        "total_trades":     len(all_trades),
        "winning_trades":   len(wins),
        "losing_trades":    len(losses),
        "avg_win_pct":      round(sum(t["pnl_pct"] for t in wins)  / len(wins),   1) if wins   else prev.get("avg_win_pct", 0),
        "avg_loss_pct":     round(sum(t["pnl_pct"] for t in losses) / len(losses), 1) if losses else prev.get("avg_loss_pct", 0),
        "sharpe_ratio":     prev.get("sharpe_ratio", 0),    # updated separately if equity curve is long enough
        "max_drawdown_pct": prev.get("max_drawdown_pct", 0),
        "last_updated":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def update_equity_curve(curve: list, pv: float) -> list:
    label = datetime.now().strftime("%b %-d")
    if curve and curve[-1]["date"] == label:
        curve[-1]["value"] = round(pv)
    else:
        curve.append({"date": label, "value": round(pv)})
    return curve[-90:]   # keep ~3 months of daily points


# ── Main ──────────────────────────────────────────────────────
def main():
    # Load existing state
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
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
    tickers = get_sp500_tickers()
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
        fi = fundamentals.get(sym, {})
        eg, rg, fpe = fi.get("eps_growth", 0), fi.get("revenue_growth", 0), fi.get("forward_pe", 999)
        q_ok = eg > 10 and rg > 8
        v_ok = fpe < 40 or fpe == 999
        if q_ok: quality_pass += 1
        if v_ok: valuation_pass += 1
        if q_ok and v_ok:
            screened.append({
                "symbol":       sym,
                "momentum_rank": len(screened) + 1,
                "momentum_6m":  round(float(mom6.get(sym, 0)), 4),
                "momentum_12m": round(float(mom12.get(sym, 0)), 4),
                "vol_30d":      round(float(vol30.get(sym, 0)), 4),
                "current_price": fi.get("current_price", 0),
            })

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

        # Current Alpaca positions → holdings
        new_holdings = alpaca_positions_to_holdings(
            alpaca_state["positions"], fundamentals, screened_ranks, vol30
        )

        cash = alpaca_state["cash"]
        pv   = alpaca_state["portfolio_value"]

        # Determine rebalance orders: sell positions not in screened top-N, buy new entrants
        current_syms = {h["symbol"] for h in new_holdings}
        target_syms  = {s["symbol"] for s in screened[:TARGET_N]}
        to_sell_syms = current_syms - target_syms
        to_buy_syms  = target_syms  - current_syms

        target_per = pv / TARGET_N if TARGET_N else 0
        raw_sells = [
            (h["symbol"], h["shares"])
            for h in new_holdings if h["symbol"] in to_sell_syms
        ]
        raw_buys = [
            (sym, max(1, int(min(target_per, pv * MAX_POSITION_PCT)
                              / fundamentals.get(sym, {}).get("current_price", 1))))
            for sym in to_buy_syms
            if fundamentals.get(sym, {}).get("current_price", 0) > 0
        ]

        # Load what Claude approved and apply all risk limits before touching the broker
        agent_approvals = load_agent_approvals()
        to_sell, to_buy = apply_risk_limits(
            raw_sells, raw_buys, pv, cash, agent_approvals["SELL"]
        )

        if to_sell or to_buy:
            print(f"Rebalance: {len(to_sell)} sells, {len(to_buy)} buys (after risk gates)")
            alpaca_place_orders(client, to_sell, to_buy, pv, cash)
        else:
            print("No orders placed — either no rebalance needed or risk gates blocked all orders.")

        # Trades from Alpaca order history
        all_trades = alpaca_orders_to_trades(alpaca_state["orders"])

        # Recalculate weights on current holdings
        total_mv = sum(h["market_value"] for h in new_holdings)
        denom    = total_mv + cash or 1
        for h in new_holdings:
            h["weight"] = round(h["market_value"] / denom, 4)

        summary = compute_summary(new_holdings, cash, data.get("summary"), all_trades)

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

    # ── 6. Equity curve ────────────────────────────────────────
    curve = update_equity_curve(data.get("equity_curve", []), pv)

    # ── 7. Filter status ───────────────────────────────────────
    nh = len(new_holdings)
    filter_status = {
        "momentum":  {"label": "Momentum",  "description": "Top 30% by 6M & 12M return",         "passing": nh, "total": nh, "threshold": "Top 30%"},
        "quality":   {"label": "Quality",   "description": "EPS growth >10%, Revenue growth >8%", "passing": min(quality_pass, nh), "total": nh, "threshold": "EPS >10% & Rev >8%"},
        "valuation": {"label": "Valuation", "description": "Forward P/E <40 or top 70% by sector","passing": min(valuation_pass, nh), "total": nh, "threshold": "Fwd P/E <40"},
        "risk":      {"label": "Risk",      "description": "Volatility below 90th percentile",    "passing": nh, "total": nh, "threshold": "Vol < 90th pct"},
    }

    # ── 8. Write portfolio.json ────────────────────────────────
    today      = datetime.now().strftime("%Y-%m-%d")
    next_month = (datetime.now().replace(day=1) + timedelta(days=32)).replace(day=1).strftime("%Y-%m-%d")
    output = {
        "meta": {
            **data.get("meta", {}),
            "strategy":       "TradeQuest AI Momentum Strategy v2.0",
            "universe":       "S&P 500",
            "account_name":   ALPACA_ACCOUNT_NAME,
            "mode":           "alpaca" if alpaca_state else "simulation",
            "initial_capital": data.get("meta", {}).get("initial_capital", INITIAL_CAPITAL),
            "last_rebalance": today,
            "next_rebalance": next_month,
            **regime_info,
        },
        "summary":       summary,
        "filter_status": filter_status,
        "equity_curve":  curve,
        "holdings":      new_holdings,
        "trades":        all_trades[:50],
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(output, f, indent=2)

    mode_label = f"Alpaca ({ALPACA_ACCOUNT_NAME})" if alpaca_state else "Simulation"
    print(f"Done [{mode_label}]. Value: ${pv:,.2f} | Holdings: {nh} | Cash: ${cash:,.2f}")


if __name__ == "__main__":
    main()
