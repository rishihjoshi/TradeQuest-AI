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
import shutil
import sys
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import requests

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

# Stored as abbreviation in JSON — no round-trip conversion needed in the dashboard
_TIMING_MAP = {
    "bmo": "BMO", "pre-market": "BMO", "before market open": "BMO",
    "amc": "AMC", "after-market": "AMC", "after market close": "AMC",
}

MAX_EARNINGS_EVENTS = 7
MAX_MACRO_EVENTS    = 7
PROMPT_FIELD_MAX    = 100  # chars; prevents prompt-injection via FMP event names


def _safe(text: str | None, max_len: int = PROMPT_FIELD_MAX) -> str:
    """Truncate and strip newlines from external API text before use in prompts."""
    return (text or "")[:max_len].replace("\n", " ").replace("\r", " ")


def load_holding_symbols() -> list[str]:
    if not PORTFOLIO_FILE.exists():
        return []
    with open(PORTFOLIO_FILE, encoding="utf-8") as f:
        portfolio = json.load(f)
    return [h["symbol"] for h in portfolio.get("holdings", []) if h.get("symbol")]


def _fmp_get(path: str, params: dict, api_key: str) -> list | dict | None:
    try:
        r = requests.get(f"{FMP_BASE}/{path}", params={**params, "apikey": api_key}, timeout=20)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        print(f"FMP HTTP {e.response.status_code} on {path}: {e.response.text[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"FMP error on {path}: {e}", file=sys.stderr)
        return None


def fetch_earnings_for_holdings(holding_symbols: list[str], api_key: str) -> list[dict]:
    if not holding_symbols or not api_key:
        return []

    today    = date.today()
    data     = _fmp_get("earnings-calendar", {"from": today.isoformat(), "to": (today + timedelta(days=7)).isoformat()}, api_key)
    if not data:
        return []

    symbol_set = {s.upper() for s in holding_symbols}
    results = []
    for item in data:
        sym = (item.get("symbol") or "").upper()
        if sym not in symbol_set:
            continue
        timing = _TIMING_MAP.get((item.get("time") or "").lower(), "TAS")
        results.append({
            "symbol":       sym,
            "date":         item.get("date", ""),
            "timing":       timing,
            "eps_estimate": item.get("epsEstimated"),
        })

    results.sort(key=lambda x: x["date"])
    return results[:MAX_EARNINGS_EVENTS]


def fetch_macro_events(api_key: str) -> list[dict]:
    if not api_key:
        return []

    today = date.today()
    data  = _fmp_get("economics-calendar", {"from": today.isoformat(), "to": (today + timedelta(days=14)).isoformat()}, api_key)
    if not data:
        return []

    results = []
    for item in data:
        if (item.get("country") or "").upper() != "US":
            continue
        if (item.get("impact") or "").lower() != "high":
            continue
        event_lower = (item.get("event") or "").lower()
        if not any(kw in event_lower for kw in HIGH_IMPACT_EVENTS):
            continue
        results.append({
            "event":    _safe(item.get("event")),
            "date":     (item.get("date") or "")[:10],
            "previous": _safe(str(item["previous"])) if item.get("previous") is not None else None,
            "estimate": _safe(str(item["estimate"])) if item.get("estimate") is not None else None,
        })

    results.sort(key=lambda x: x["date"])
    return results[:MAX_MACRO_EVENTS]


def fetch_breadth_score() -> dict | None:
    try:
        r = requests.get(BREADTH_CSV_URL, timeout=15)
        r.raise_for_status()
        content = r.text
    except Exception as e:
        print(f"Breadth CSV fetch failed: {e}", file=sys.stderr)
        return None

    try:
        reader = csv.DictReader(io.StringIO(content))
        last   = deque((row for row in reader if row.get("Date")), maxlen=1)
        if not last:
            return None
        row      = last[0]
        raw_val  = float(row.get("Breadth_Index_Raw",  0))
        ma8_val  = float(row.get("Breadth_Index_8MA",  0))
        ma200_val = float(row.get("Breadth_Index_200MA", 0))

        if raw_val > 0.60:
            interpretation = "HEALTHY — broad participation supports bull regime"
        elif raw_val > 0.40:
            interpretation = "NARROWING — selective rally, treat regime with caution"
        else:
            interpretation = "WEAK — thin participation, biases toward sideways/bear"

        return {
            "date":              row.get("Date", ""),
            "breadth_raw":       round(raw_val, 4),
            "breadth_8ma":       round(ma8_val, 4),
            "breadth_200ma":     round(ma200_val, 4),
            "pct_above_200ma":   f"{raw_val * 100:.1f}%",
            "trend_above_200ma": ma8_val >= ma200_val,
            "interpretation":    interpretation,
        }
    except Exception as e:
        print(f"Breadth CSV parse error: {e}", file=sys.stderr)
        return None


def main():
    api_key = os.environ.get("FMP_API_KEY", "")
    symbols = load_holding_symbols()

    if not api_key:
        print("Warning: FMP_API_KEY not set — skipping earnings and macro calendar.", file=sys.stderr)

    print(f"Enrichment run — holdings: {symbols or 'none'}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_earnings = pool.submit(fetch_earnings_for_holdings, symbols, api_key)
        f_macro    = pool.submit(fetch_macro_events, api_key)
        f_breadth  = pool.submit(fetch_breadth_score)
        earnings = f_earnings.result()
        macro    = f_macro.result()
        breadth  = f_breadth.result()

    output = {
        "generated_at":       date.today().isoformat(),
        "earnings_this_week": earnings,
        "macro_events_14d":   macro,
        "market_breadth":     breadth,
    }

    ENRICHMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ENRICHMENT_FILE.parent, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
        shutil.move(tmp, ENRICHMENT_FILE)
    except Exception:
        os.unlink(tmp)
        raise

    breadth_info = f"{breadth['pct_above_200ma']} ({'above' if breadth['trend_above_200ma'] else 'below'} 200MA)" if breadth else "N/A"
    print(f"Wrote {ENRICHMENT_FILE}")
    print(f"  Earnings events : {len(earnings)}")
    print(f"  Macro events    : {len(macro)}")
    print(f"  Breadth score   : {breadth_info}")


if __name__ == "__main__":
    main()
