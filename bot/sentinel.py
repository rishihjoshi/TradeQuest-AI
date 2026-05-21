#!/usr/bin/env python3
"""TradeQuest Sentinel — rule-based intraday sell checker (no LLM).

Runs at 2:30 PM ET via sentinel.yml while the market is still open.
Applies three hard rules autonomously, without calling Claude:

  Rule 1 — Concentration: position weight > 2× MAX_POSITION_PCT (>16%) → SELL
  Rule 2 — Trend break: price < 50-day MA for ≥ 3 consecutive agent runs → SELL
  Rule 3 — Persistent flag: symbol flagged SELL in ≥ 5 consecutive runs → SELL

Writes data/sentinel_orders.json with the sell list.
update.py --sentinel reads that file and places Alpaca orders immediately.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT       = Path(__file__).resolve().parent.parent
DATA_FILE       = REPO_ROOT / "data" / "portfolio.json"
LOG_FILE        = REPO_ROOT / "data" / "agent_log.json"
SENTINEL_FILE   = REPO_ROOT / "data" / "sentinel_orders.json"

MAX_POSITION_PCT        = 0.08   # must match update.py
CONCENTRATION_THRESHOLD = MAX_POSITION_PCT * 2   # 16%
TREND_BREAK_RUNS        = 3      # consecutive below-MA runs → sell
PERSISTENT_FLAG_RUNS    = 5      # consecutive SELL flags without execution → override


def load_portfolio() -> dict:
    if not DATA_FILE.exists():
        print("sentinel: portfolio.json not found — nothing to check.", file=sys.stderr)
        return {}
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_log() -> list:
    """Return runs list (newest-first order as written by agent.py)."""
    if not LOG_FILE.exists():
        return []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f).get("runs", [])
    except Exception as e:
        print(f"sentinel: could not read agent_log.json: {e}", file=sys.stderr)
        return []


def count_consecutive_sell_flags(runs: list, symbol: str) -> int:
    """Count how many consecutive recent runs flagged `symbol` as SELL."""
    count = 0
    for run in runs:
        if run.get("run_type") not in ("day_end", "monthly", "day_start"):
            continue
        decisions = run.get("decisions", [])
        flagged = any(
            str(d.get("action", "")).upper() in ("SELL", "WATCH")
            and str(d.get("symbol", "")).upper() == symbol.upper()
            for d in decisions
        )
        if flagged:
            count += 1
        else:
            break   # streak broken
    return count


def count_consecutive_below_ma(runs: list, symbol: str) -> int:
    """Count consecutive recent runs where the symbol's price was below its MA50."""
    count = 0
    for run in runs:
        decisions = run.get("decisions", [])
        below_ma = any(
            str(d.get("symbol", "")).upper() == symbol.upper()
            and "below" in str(d.get("reason", "")).lower()
            and "ma" in str(d.get("reason", "")).lower()
            and str(d.get("rule_triggered", "")).lower() == "trend_break"
            for d in decisions
        )
        if below_ma:
            count += 1
        else:
            break
    return count


def check_rules(portfolio: dict, runs: list) -> list[dict]:
    """
    Apply the three sentinel rules to current holdings.
    Returns a list of sell candidates:
      [{"symbol": "KLAC", "shares": 1, "rule": "concentration", "reason": "..."}]
    """
    holdings = portfolio.get("holdings", [])
    sells: list[dict] = []
    seen: set[str] = set()

    for h in holdings:
        sym    = str(h.get("symbol", "")).upper()
        weight = float(h.get("weight", 0))
        shares = int(h.get("shares", 0))

        if not sym or shares <= 0 or sym in seen:
            continue

        # Rule 1 — Concentration: weight > 2× MAX_POSITION_PCT
        if weight > CONCENTRATION_THRESHOLD:
            sells.append({
                "symbol": sym,
                "shares": shares,
                "rule": "concentration",
                "reason": (
                    f"Position weight {weight:.1%} exceeds 2× MAX_POSITION_PCT "
                    f"({CONCENTRATION_THRESHOLD:.0%}). Forced exit — sentinel Rule 1."
                ),
            })
            seen.add(sym)
            continue

        # Rule 2 — Trend break: below MA50 for ≥ TREND_BREAK_RUNS consecutive runs
        below_ma_count = count_consecutive_below_ma(runs, sym)
        if below_ma_count >= TREND_BREAK_RUNS:
            sells.append({
                "symbol": sym,
                "shares": shares,
                "rule": "trend_break",
                "reason": (
                    f"Price below 50-day MA for {below_ma_count} consecutive agent runs "
                    f"(≥ {TREND_BREAK_RUNS} threshold). Forced exit — sentinel Rule 2."
                ),
            })
            seen.add(sym)
            continue

        # Rule 3 — Persistent flag: SELL flagged ≥ PERSISTENT_FLAG_RUNS consecutive runs
        sell_flag_count = count_consecutive_sell_flags(runs, sym)
        if sell_flag_count >= PERSISTENT_FLAG_RUNS:
            sells.append({
                "symbol": sym,
                "shares": shares,
                "rule": "persistent_flag",
                "reason": (
                    f"Symbol flagged SELL/WATCH in {sell_flag_count} consecutive agent runs "
                    f"without execution (≥ {PERSISTENT_FLAG_RUNS} threshold). "
                    f"Forced exit — sentinel Rule 3."
                ),
            })
            seen.add(sym)

    return sells


def write_sentinel_orders(sells: list[dict]) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sells": sells,
        "count": len(sells),
    }
    with open(SENTINEL_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"sentinel: wrote {len(sells)} sell order(s) to sentinel_orders.json")


def main() -> None:
    portfolio = load_portfolio()
    if not portfolio:
        sys.exit(0)

    runs = load_log()

    sells = check_rules(portfolio, runs)

    if not sells:
        print("sentinel: no hard rules triggered — no orders to place.")
        write_sentinel_orders([])
        sys.exit(0)

    print(f"sentinel: {len(sells)} rule(s) triggered:")
    for s in sells:
        print(f"  [{s['rule'].upper()}] {s['symbol']} ({s['shares']} shares): {s['reason']}")

    write_sentinel_orders(sells)


if __name__ == "__main__":
    main()
