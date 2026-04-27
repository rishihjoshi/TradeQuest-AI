#!/usr/bin/env python3
"""Pre-agent enrichment: fetches calendar data and market breadth.

Writes data/enrichment.json before bot/agent.py runs so the Claude agent
has awareness of upcoming earnings and macro events for its holdings.

Data sources (all free, no paid subscription needed):
  - FMP API  — earnings calendar + economic calendar (250 calls/day free tier)
  - TraderMonty public CSV — market breadth index (no API key)

Requires env var: FMP_API_KEY
"""

import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT       = Path(__file__).resolve().parent.parent
PORTFOLIO_FILE  = REPO_ROOT / "data" / "portfolio.json"
ENRICHMENT_FILE = REPO_ROOT / "data" / "enrichment.json"

FMP_BASE        = "https://financialmodelingprep.com/stable"
BREADTH_CSV_URL = "https://tradermonty.github.io/market-breadth-analysis/market_breadth_data.csv"

# Only surface these macro events — keeps agent prompt token count low
HIGH_IMPACT_EVENTS = {
    "fomc rate decision", "federal funds rate",
    "cpi", "consumer price index",
    "ppi", "producer price index",
    "nonfarm payrolls", "unemployment rate",
    "gdp", "gross domestic product",
    "pce", "core pce",
    "retail sales",
    "jolts", "job openings",
    "initial jobless claims",
}

MAX_EARNINGS_EVENTS  = 7   # cap for prompt token budget
MAX_MACRO_EVENTS     = 7


# ── Portfolio helpers ─────────────────────────────────────────

def load_holding_symbols() -> list[str]:
    """Return ticker symbols currently held in the portfolio."""
    if not PORTFOLIO_FILE.exists():
        return []
    with open(PORTFOLIO_FILE, encoding="utf-8") as f:
        portfolio = json.load(f)
    return [h["symbol"] for h in portfolio.get("holdings", []) if h.get("symbol")]


# ── FMP helpers ───────────────────────────────────────────────

def _fmp_get(path: str, params: dict, api_key: str) -> list | dict | None:
    params["apikey"] = api_key
    url = f"{FMP_BASE}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        if e.code == 404 and body.strip() in ("[]", ""):
            return []
        print(f"FMP HTTP {e.code} on {path}: {body[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"FMP error on {path}: {e}", file=sys.stderr)
        return None


def fetch_earnings_for_holdings(holding_symbols: list[str], api_key: str) -> list[dict]:
    """Fetch earnings announcements for current holdings in the next 7 days."""
    if not holding_symbols or not api_key:
        return []

    today    = date.today()
    end_date = today + timedelta(days=7)
    data     = _fmp_get(
        "earnings-calendar",
        {"from": today.isoformat(), "to": end_date.isoformat()},
        api_key,
    )
    if not data:
        return []

    symbol_set = set(s.upper() for s in holding_symbols)
    results = []
    for item in data:
        sym = (item.get("symbol") or "").upper()
        if sym not in symbol_set:
            continue
        timing_raw = (item.get("time") or "").lower()
        if timing_raw in ("bmo", "pre-market", "before market open"):
            timing = "Before Market Open"
        elif timing_raw in ("amc", "after-market", "after market close"):
            timing = "After Market Close"
        else:
            timing = "During Hours"
        results.append({
            "symbol":        sym,
            "date":          item.get("date", ""),
            "timing":        timing,
            "eps_estimate":  item.get("epsEstimated"),
            "in_portfolio":  True,
        })

    results.sort(key=lambda x: x["date"])
    return results[:MAX_EARNINGS_EVENTS]


def fetch_macro_events(api_key: str) -> list[dict]:
    """Fetch high-impact US economic events for the next 14 days."""
    if not api_key:
        return []

    today    = date.today()
    end_date = today + timedelta(days=14)
    data     = _fmp_get(
        "economics-calendar",
        {"from": today.isoformat(), "to": end_date.isoformat()},
        api_key,
    )
    if not data:
        return []

    results = []
    for item in data:
        country = (item.get("country") or "").upper()
        impact  = (item.get("impact")  or "").lower()
        event   = (item.get("event")   or "").lower()
        if country != "US":
            continue
        if impact != "high":
            continue
        if not any(keyword in event for keyword in HIGH_IMPACT_EVENTS):
            continue
        results.append({
            "event":    item.get("event", ""),
            "date":     item.get("date", "")[:10],
            "impact":   "High",
            "previous": item.get("previous"),
            "estimate": item.get("estimate"),
        })

    results.sort(key=lambda x: x["date"])
    return results[:MAX_MACRO_EVENTS]


# ── Breadth helpers ───────────────────────────────────────────

def fetch_breadth_score() -> dict | None:
    """
    Fetch TraderMonty public breadth CSV and return the latest breadth reading.
    No API key required. Gracefully returns None on failure.
    """
    try:
        with urllib.request.urlopen(BREADTH_CSV_URL, timeout=15) as r:
            content = r.read().decode("utf-8")
    except Exception as e:
        print(f"Breadth CSV fetch failed: {e}", file=sys.stderr)
        return None

    try:
        reader = csv.DictReader(io.StringIO(content))
        rows   = [row for row in reader if row.get("Date")]
        if not rows:
            return None
        latest = rows[-1]
        raw_val  = float(latest.get("Breadth_Index_Raw",  0))
        ma8_val  = float(latest.get("Breadth_Index_8MA",  0))
        ma200_val = float(latest.get("Breadth_Index_200MA", 0))

        # Interpret breadth score (maps directly to STRATEGY.md regime thresholds)
        if raw_val > 0.60:
            interpretation = "HEALTHY — broad participation supports bull regime"
        elif raw_val > 0.40:
            interpretation = "NARROWING — selective rally, treat regime with caution"
        else:
            interpretation = "WEAK — thin participation, biases toward sideways/bear"

        above_200ma = ma8_val >= ma200_val

        return {
            "date":             latest.get("Date", ""),
            "breadth_raw":      round(raw_val, 4),
            "breadth_8ma":      round(ma8_val, 4),
            "breadth_200ma":    round(ma200_val, 4),
            "pct_above_200ma":  f"{raw_val * 100:.1f}%",
            "trend_above_200ma": above_200ma,
            "interpretation":   interpretation,
        }
    except Exception as e:
        print(f"Breadth CSV parse error: {e}", file=sys.stderr)
        return None


# ── Main ──────────────────────────────────────────────────────

def main():
    api_key  = os.environ.get("FMP_API_KEY", "")
    symbols  = load_holding_symbols()

    if not api_key:
        print("Warning: FMP_API_KEY not set — skipping earnings and macro calendar.", file=sys.stderr)

    print(f"Enrichment run — holdings: {symbols or 'none'}")

    earnings    = fetch_earnings_for_holdings(symbols, api_key)
    macro       = fetch_macro_events(api_key)
    breadth     = fetch_breadth_score()

    output = {
        "generated_at":       date.today().isoformat(),
        "earnings_this_week": earnings,
        "macro_events_14d":   macro,
        "market_breadth":     breadth,
    }

    ENRICHMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ENRICHMENT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {ENRICHMENT_FILE}")
    print(f"  Earnings events : {len(earnings)}")
    print(f"  Macro events    : {len(macro)}")
    print(f"  Breadth score   : {breadth['pct_above_200ma'] if breadth else 'N/A'} "
          f"({'above' if breadth and breadth['trend_above_200ma'] else 'below'} 200MA)"
          if breadth else "  Breadth score   : N/A")


if __name__ == "__main__":
    main()
