#!/usr/bin/env python3
"""TradeQuest AI Agent — Claude-powered daily decision runner.

Run types (set via RUN_TYPE env var):
  day_start  — 9:00 AM ET, pre-market: flags, regime check, no trades placed
  day_end    — 4:30 PM ET, post-close: sell-rule checks, definitive decisions
  monthly    — 1st of month: full rebalance plan

Flow:
  1. Read STRATEGY.md  (prompt-cached — same doc every run, saves tokens)
  2. Read data/portfolio.json
  3. Send to Claude with run-type-specific task prompt
  4. Parse structured JSON response
  5. Append entry to data/agent_log.json
  6. Commit + push handled by GitHub Actions after this script exits
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

REPO_ROOT        = Path(__file__).resolve().parent.parent
DATA_FILE        = REPO_ROOT / "data" / "portfolio.json"
LOG_FILE         = REPO_ROOT / "data" / "agent_log.json"
STRATEGY_FILE    = REPO_ROOT / "STRATEGY.md"
ENRICHMENT_FILE  = REPO_ROOT / "data" / "enrichment.json"
RUN_TYPE         = os.environ.get("RUN_TYPE", "day_end")
MODEL            = "claude-sonnet-4-6"

# ── Task prompts per run type ─────────────────────────────────

TASK_DAY_START = """
## Your Task: DAY START — Pre-Market Check (9:00 AM ET)

You are running before the market opens. Do NOT recommend trades — flag only.

1. Review each holding against the four sell rules in the strategy
2. Flag any positions showing early warning signals (rank approaching 40%, price approaching MA50)
3. Assess the current market regime based on portfolio meta data
4. Note any concentration risk or sector imbalances
5. Provide a 1-sentence market outlook for the day

Be specific about symbols and numbers. If nothing is flagged, say so clearly.
"""

TASK_DAY_END = """
## Your Task: DAY END — Post-Close Review (4:30 PM ET)

The portfolio data has been updated with today's closing prices. Make definitive decisions.

1. Check each holding against ALL four sell rules:
   - Momentum rank > 40% of universe → SELL
   - Price < 50-day MA → SELL
   - EPS growth deteriorating → WATCH/SELL
   - Position up >60% in <60 days → consider profit taking (parabolic blow-off only)
2. For each flagged position, state the specific rule triggered
3. For HOLDs, briefly confirm the thesis still holds
4. Assess portfolio overall performance vs the strategy objectives
5. Note any regime changes that would affect cash allocation

Be decisive. Any clear sell-rule trigger should result in a SELL decision.

## Friday Close Addition
If today is Friday, include a `weekly_summary` object in your JSON response alongside
the standard fields:
{
  "weekly_summary": {
    "week_assessment": "<1-sentence portfolio health vs SPY this week>",
    "key_trades": ["<TICKER>", ...],
    "next_week_watch": ["<TICKER>", "<TICKER>"]
  }
}
"""

TASK_MONTHLY = """
## Your Task: MONTHLY REBALANCE (First Trading Day of Month)

This is the full monthly rebalance. Be thorough and decisive.

1. Assess each current holding — which should stay, which should go?
2. Based on the strategy filters, what types of stocks should be entering?
3. Review the current cash allocation vs the regime target
4. Make a complete rebalance plan:
   - List all SELL decisions (with rule that triggered each)
   - List all BUY candidates (with momentum/quality rationale)
   - State target position count and cash level
5. Identify any strategy drift or execution gaps from last month

This output will drive actual Alpaca paper trading orders. Be specific about actions.
"""

TASK_MAP = {
    "day_start": TASK_DAY_START,
    "day_end":   TASK_DAY_END,
    "monthly":   TASK_MONTHLY,
}

RESPONSE_SCHEMA = """
## Response Format

Respond ONLY with valid JSON — no markdown fences, no prose outside the JSON:

{
  "assessment": "2-3 sentence overall assessment of portfolio and market",
  "regime": "bull|sideways|bear",
  "regime_confidence": 0.0,
  "flags": [
    "SYMBOL: reason this position needs attention"
  ],
  "decisions": [
    {
      "action": "HOLD|SELL|BUY|WATCH",
      "symbol": "TICKER",
      "reason": "specific rule or rationale",
      "rule_triggered": "momentum_decay|trend_break|quality_drop|profit_take|new_entry|null",
      "urgency": "immediate|next_open|next_rebalance"
    }
  ],
  "cash_action": "increase|decrease|maintain",
  "cash_rationale": "why cash level should change or stay",
  "summary": "one sentence for the activity log headline"
}
"""


# ── Data loading ──────────────────────────────────────────────

def load_strategy() -> str:
    if not STRATEGY_FILE.exists():
        return "Strategy file not found — operating on general momentum principles."
    return STRATEGY_FILE.read_text(encoding="utf-8")


def load_portfolio() -> dict:
    if not DATA_FILE.exists():
        return {}
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_log() -> dict:
    if not LOG_FILE.exists():
        return {"runs": [], "last_run": None, "last_type": None}
    with open(LOG_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_enrichment() -> dict:
    if not ENRICHMENT_FILE.exists():
        return {}
    with open(ENRICHMENT_FILE, encoding="utf-8") as f:
        return json.load(f)


def _safe(text: str | None, max_len: int = 100) -> str:
    """Truncate external API text; strips newlines and Markdown chars to prevent prompt injection."""
    cleaned = (str(text) if text is not None else "")[:max_len]
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    for ch in ("#", "*", "`", "\\"):
        cleaned = cleaned.replace(ch, "")
    return cleaned


def build_enrichment_section(enrichment: dict) -> str:
    """Build the Upcoming Catalysts section injected into the agent prompt."""
    if not enrichment:
        return ""

    earnings = enrichment.get("earnings_this_week", [])
    macro    = enrichment.get("macro_events_14d", [])
    breadth  = enrichment.get("market_breadth")

    if not earnings and not macro and not breadth:
        return ""

    lines = ["## Upcoming Market Catalysts\n"]

    if earnings:
        lines.append("### Earnings This Week (your holdings)")
        for e in earnings:
            eps = f", EPS est: {_safe(e['eps_estimate'])}" if e.get("eps_estimate") else ""
            lines.append(f"- **{_safe(e['symbol'], 10)}** — {_safe(e['date'], 10)} {_safe(e['timing'], 3)}{eps}")
        lines.append("")

    if macro:
        lines.append("### High-Impact Macro Events (next 14 days)")
        for m in macro:
            prev = f", prev: {_safe(m['previous'], 20)}" if m.get("previous") else ""
            est  = f", est: {_safe(m['estimate'], 20)}"  if m.get("estimate")  else ""
            lines.append(f"- **{_safe(m['date'], 10)}** {_safe(m['event'])}{prev}{est}")
        lines.append("")

    if breadth:
        trend = "above — bullish breadth trend" if breadth.get("trend_above_200ma") else "below — bearish breadth trend"
        lines.append("### Market Breadth Signal")
        lines.append(f"- % S&P 500 stocks above 200-day MA: **{_safe(breadth['pct_above_200ma'], 10)}**")
        lines.append(f"- Breadth 8MA vs 200MA: {trend}")
        lines.append(f"- Interpretation: {_safe(breadth['interpretation'], 80)}")
        lines.append(f"- Data as of: {_safe(breadth['date'], 10)}")
        lines.append("")

    return "\n".join(lines) + "\n"


def build_history_section(recent_history: list) -> str:
    """Summarise the last agent run so Claude has day-over-day continuity."""
    if not recent_history:
        return ""
    entry = recent_history[0]
    lines = [
        f"## Previous Run ({entry.get('type', '?')}, "
        f"{entry.get('timestamp', '')[:10]})\n"
    ]
    lines.append(
        f"Regime: {_safe(entry.get('regime', '?'), 20)} "
        f"({entry.get('regime_confidence', 0):.0%} confidence)"
    )
    flags = entry.get("flags", [])
    if flags:
        lines.append(f"Flags carried forward: {'; '.join(_safe(str(f), 80) for f in flags[:4])}")
    decisions = entry.get("decisions", [])
    sells   = [d["symbol"] for d in decisions if d.get("action") == "SELL"]
    watches = [d["symbol"] for d in decisions if d.get("action") == "WATCH"]
    if sells:
        lines.append(f"Sold last run: {', '.join(_safe(s, 10) for s in sells)}")
    if watches:
        lines.append(f"Watching from last run: {', '.join(_safe(s, 10) for s in watches)}")
    lines.append(f"Summary: {_safe(entry.get('summary', ''), 150)}")
    return "\n".join(lines) + "\n\n"


# ── Agent call ────────────────────────────────────────────────

def run_agent(
    run_type: str,
    portfolio: dict,
    strategy: str,
    enrichment: dict,
    recent_history: list | None = None,
) -> tuple[dict, dict]:
    """
    Call Claude with prompt-cached strategy + portfolio state + enrichment context.
    Returns (parsed_result, usage_info).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")

    client = anthropic.Anthropic(api_key=api_key)
    task   = TASK_MAP.get(run_type, TASK_DAY_END)

    # Compact portfolio for the prompt (drop equity_curve to save tokens)
    portfolio_slim = {
        k: v for k, v in portfolio.items()
        if k not in ("equity_curve",)
    }

    enrichment_section = build_enrichment_section(enrichment)
    history_section    = build_history_section(recent_history or [])

    message = client.messages.create(
        model=MODEL,
        max_tokens=2500,
        system=[
            {
                # Strategy doc is static — cache it (5-min TTL, saves ~2k tokens/run)
                "type": "text",
                "text": (
                    "You are TradeQuest AI, an autonomous momentum trading agent.\n"
                    "You strictly follow the strategy document below for all decisions.\n\n"
                    f"## STRATEGY DOCUMENT\n\n{strategy}"
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"{task}\n\n"
                    f"{enrichment_section}"
                    f"{history_section}"
                    f"## Current Portfolio State\n"
                    f"```json\n{json.dumps(portfolio_slim, indent=2)}\n```\n\n"
                    f"{RESPONSE_SCHEMA}"
                ),
            }
        ],
    )

    usage = {
        "input_tokens":      message.usage.input_tokens,
        "output_tokens":     message.usage.output_tokens,
        "cache_read_tokens": getattr(message.usage, "cache_read_input_tokens", 0),
        "cache_write_tokens": getattr(message.usage, "cache_creation_input_tokens", 0),
    }

    raw = message.content[0].text.strip()
    try:
        start  = raw.find("{")
        end    = raw.rfind("}") + 1
        result = json.loads(raw[start:end])
    except Exception as e:
        print(f"Warning: could not parse agent JSON response ({e}). Storing raw.", file=sys.stderr)
        result = {
            "assessment":  raw,
            "regime":      "unknown",
            "flags":       [],
            "decisions":   [],
            "cash_action": "maintain",
            "cash_rationale": "",
            "summary":     f"Agent ran ({run_type}) — response parse failed",
        }

    return result, usage


# ── Log writing ───────────────────────────────────────────────

def write_log(log: dict, run_type: str, result: dict, usage: dict) -> dict:
    entry = {
        "id":               f"RUN-{datetime.now().strftime('%Y%m%d-%H%M')}",
        "timestamp":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type":             run_type,
        "model":            MODEL,
        "assessment":       result.get("assessment", ""),
        "regime":           result.get("regime", ""),
        "regime_confidence": result.get("regime_confidence", 0),
        "flags":            result.get("flags", []),
        "decisions":        result.get("decisions", []),
        "cash_action":      result.get("cash_action", "maintain"),
        "cash_rationale":   result.get("cash_rationale", ""),
        "summary":          result.get("summary", ""),
        "usage":            usage,
    }

    log.setdefault("runs", []).insert(0, entry)
    log["runs"]      = log["runs"][:90]   # keep ~3 months of history
    log["last_run"]  = entry["timestamp"]
    log["last_type"] = run_type

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)

    return entry


# ── Main ──────────────────────────────────────────────────────

def main():
    run_type = RUN_TYPE
    print(f"TradeQuest Agent — {run_type.upper()} | {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC")

    strategy       = load_strategy()
    portfolio      = load_portfolio()
    log            = load_log()
    enrichment     = load_enrichment()
    recent_history = log.get("runs", [])[:1]   # last 1 run for continuity

    if not portfolio:
        print("Warning: portfolio.json not found — agent running with empty state.", file=sys.stderr)
    if enrichment:
        earnings_count = len(enrichment.get("earnings_this_week", []))
        macro_count    = len(enrichment.get("macro_events_14d", []))
        breadth_pct    = (enrichment.get("market_breadth") or {}).get("pct_above_200ma", "N/A")
        print(f"Enrichment : {earnings_count} earnings | {macro_count} macro events | breadth {breadth_pct}")
    else:
        print("Enrichment : none (run bot/enrich.py first for calendar context)")
    if recent_history:
        print(f"History    : last run was {recent_history[0].get('type','?')} "
              f"({recent_history[0].get('timestamp','')[:10]})")

    result, usage = run_agent(run_type, portfolio, strategy, enrichment, recent_history)
    entry = write_log(log, run_type, result, usage)

    # Console summary
    print(f"\n{'='*60}")
    print(f"Summary : {entry['summary']}")
    print(f"Regime  : {entry['regime']} (confidence {entry['regime_confidence']:.0%})")
    print(f"Flags   : {len(entry['flags'])} position(s)")
    for flag in entry["flags"]:
        print(f"  ⚑ {flag}")
    print(f"Decisions: {len(entry['decisions'])}")
    for d in entry["decisions"]:
        marker = {"SELL": "↓", "BUY": "↑", "HOLD": "·", "WATCH": "⚠"}.get(d["action"], "?")
        print(f"  {marker} {d['action']:5} {d.get('symbol','?'):6} — {d.get('reason','')}")
    cached = usage.get("cache_read_tokens", 0)
    print(f"\nTokens  : {usage['input_tokens']} in / {usage['output_tokens']} out"
          + (f" / {cached} cached" if cached else ""))
    print(f"Log     : {LOG_FILE}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
