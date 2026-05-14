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

REPO_ROOT           = Path(__file__).resolve().parent.parent
DATA_FILE           = REPO_ROOT / "data" / "portfolio.json"
LOG_FILE            = REPO_ROOT / "data" / "agent_log.json"
STRATEGY_FILE       = REPO_ROOT / "STRATEGY.md"
ENRICHMENT_FILE     = REPO_ROOT / "data" / "enrichment.json"
EXEC_SUMMARY_FILE   = REPO_ROOT / "data" / "execution_summary.json"
RUN_TYPE            = os.environ.get("RUN_TYPE", "day_end")
MODEL               = "claude-sonnet-4-6"
HISTORY_RUNS        = 5   # number of prior runs injected for continuity (was 1)

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


def load_execution_summary() -> dict:
    """Load last execution summary written by update.py after Alpaca order placement."""
    if not EXEC_SUMMARY_FILE.exists():
        return {}
    with open(EXEC_SUMMARY_FILE, encoding="utf-8") as f:
        return json.load(f)


def build_execution_section(exec_summary: dict) -> str:
    """Inject market-open execution results so Claude knows which orders actually ran.

    This closes the 'KLAC never sold' feedback gap: without this the agent
    re-issues the same SELL every day because it can't distinguish an unfilled
    order from one that was never placed.
    """
    if not exec_summary:
        return ""

    ts       = _safe(exec_summary.get("timestamp", "")[:16], 16)
    placed   = exec_summary.get("orders_placed", [])
    skipped  = exec_summary.get("orders_skipped", [])
    errors   = exec_summary.get("errors", [])
    cash_pct = exec_summary.get("cash_pct_after")

    if not placed and not skipped and not errors:
        return ""

    lines = [f"## Last Market-Open Execution ({ts} UTC)\n"]

    if placed:
        lines.append("### Orders Placed at Market Open")
        for o in placed[:20]:          # guard against huge lists
            sym    = _safe(o.get("symbol", "?"), 10)
            action = _safe(o.get("side", o.get("action", "?")), 10)
            qty    = o.get("qty", o.get("shares", "?"))
            status = _safe(o.get("status", "submitted"), 20)
            lines.append(f"  - {action.upper()} {qty} {sym} → {status}")
        lines.append("")

    if skipped:
        lines.append("### Orders Skipped (no approval or market closed)")
        for o in skipped[:10]:
            sym    = _safe(o.get("symbol", "?"), 10)
            reason = _safe(o.get("reason", "unknown"), 80)
            lines.append(f"  - {sym}: {reason}")
        lines.append("")

    if errors:
        lines.append("### Execution Errors")
        for e in errors[:10]:
            lines.append(f"  - {_safe(str(e), 100)}")
        lines.append("")

    if cash_pct is not None:
        lines.append(f"Cash after execution: {float(cash_pct):.1f}%\n")

    lines.append(
        "> If a symbol appears above as PLACED/submitted but is still in the portfolio, "
        "the order may be pending fill — do NOT re-issue the same order today."
    )
    lines.append("")

    return "\n".join(lines) + "\n"


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
    """Summarise the last N agent runs so Claude has multi-day continuity.

    Key improvements over the original single-run version:
    - Surfaces the most-recent run in full detail
    - Detects symbols flagged/sold across multiple runs (persistent problems)
    - Explicitly calls out SELL orders that appear unresolved (still held)
    """
    if not recent_history:
        return ""

    lines = ["## Agent History (last runs)\n"]

    # ── Most recent run — full detail ─────────────────────────────
    entry = recent_history[0]
    lines.append(
        f"### Latest Run ({_safe(entry.get('type', '?'), 20)}, "
        f"{_safe(entry.get('timestamp', '')[:10], 10)})"
    )
    lines.append(
        f"Regime: {_safe(entry.get('regime', '?'), 20)} "
        f"({entry.get('regime_confidence', 0):.0%} confidence)"
    )
    flags = entry.get("flags", [])
    if flags:
        lines.append("Flags: " + "; ".join(_safe(str(f), 80) for f in flags[:5]))
    decisions = entry.get("decisions", [])
    for action in ("SELL", "BUY", "WATCH"):
        syms = [d.get("symbol", "?") for d in decisions if d.get("action") == action]
        if syms:
            lines.append(f"{action}: {', '.join(_safe(s, 10) for s in syms)}")
    lines.append(f"Summary: {_safe(entry.get('summary', ''), 150)}")
    lines.append("")

    # ── Prior runs — abbreviated ───────────────────────────────────
    if len(recent_history) > 1:
        lines.append("### Prior Runs")
        for run in recent_history[1:]:
            rtype = _safe(run.get("type", "?"), 15)
            rdate = _safe(run.get("timestamp", "")[:10], 10)
            regime = _safe(run.get("regime", "?"), 12)
            sells = [d.get("symbol", "?") for d in run.get("decisions", []) if d.get("action") == "SELL"]
            watches = [d.get("symbol", "?") for d in run.get("decisions", []) if d.get("action") == "WATCH"]
            parts = [f"[{rdate} {rtype}] regime={regime}"]
            if sells:
                parts.append(f"sold={','.join(_safe(s, 10) for s in sells)}")
            if watches:
                parts.append(f"watch={','.join(_safe(s, 10) for s in watches)}")
            lines.append("  " + "  ".join(parts))
        lines.append("")

    # ── Persistent-problem detection ───────────────────────────────
    # Count how many runs each symbol appeared in as SELL or flag
    symbol_sell_runs: dict[str, list[str]] = {}
    symbol_flag_runs: dict[str, list[str]] = {}
    for run in recent_history:
        rdate = run.get("timestamp", "")[:10]
        for d in run.get("decisions", []):
            if d.get("action") == "SELL":
                sym = _safe(d.get("symbol", ""), 10)
                symbol_sell_runs.setdefault(sym, []).append(rdate)
        for f in run.get("flags", []):
            fstr = str(f)
            # Flags are typically "SYMBOL: reason" format
            sym = _safe(fstr.split(":", maxsplit=1)[0].strip(), 10) if ":" in fstr else ""
            if sym:
                symbol_flag_runs.setdefault(sym, []).append(rdate)

    persistent_sells = {s: dates for s, dates in symbol_sell_runs.items() if len(dates) >= 2}
    persistent_flags = {s: dates for s, dates in symbol_flag_runs.items() if len(dates) >= 2}

    if persistent_sells:
        lines.append("### ⚠ UNRESOLVED SELL ORDERS (repeated across multiple runs)")
        lines.append("These symbols have been flagged SELL in 2+ consecutive runs but may still be held.")
        lines.append("If still in portfolio, treat as URGENT — investigate why execution did not occur.")
        for sym, dates in persistent_sells.items():
            lines.append(f"  - **{sym}**: SELL issued on {', '.join(dates)}")
        lines.append("")

    if persistent_flags:
        lines.append("### Persistently Flagged Positions")
        for sym, dates in persistent_flags.items():
            lines.append(f"  - {sym}: flagged on {', '.join(dates)}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ── Agent call ────────────────────────────────────────────────

def run_agent(
    run_type: str,
    portfolio: dict,
    strategy: str,
    enrichment: dict,
    recent_history: list | None = None,
    exec_summary: dict | None = None,
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

    # Compact portfolio for the prompt (drop equity_curve + spy_curve to save tokens)
    portfolio_slim = {
        k: v for k, v in portfolio.items()
        if k not in ("equity_curve", "spy_curve")
    }

    enrichment_section = build_enrichment_section(enrichment)
    history_section    = build_history_section(recent_history or [])
    execution_section  = build_execution_section(exec_summary or {})

    message = client.messages.create(
        model=MODEL,
        max_tokens=8000,  # 4000 was still risky — monthly rebalance with 20 positions needs ~4-6k tokens
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
                    f"{execution_section}"
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

    raw         = message.content[0].text.strip()
    stop_reason = message.stop_reason

    # Explicit truncation guard — max_tokens hit means JSON is incomplete
    if stop_reason == "max_tokens":
        print(
            f"WARNING: Response truncated at token limit — increase max_tokens. "
            f"output_tokens={message.usage.output_tokens}",
            file=sys.stderr,
        )

    try:
        # decoder.raw_decode walks forward from the first '{' and stops at the
        # exact matching '}' — handles nested objects correctly and never
        # breaks on trailing text or a truncated response (unlike rfind('}'))
        decoder = json.JSONDecoder()
        start   = raw.find("{")
        if start == -1:
            raise ValueError("No JSON object found in Claude's response")
        result, _ = decoder.raw_decode(raw, start)
    except Exception as e:
        print(
            f"Warning: could not parse agent JSON response "
            f"(stop_reason={stop_reason}, error={e}). Storing raw.",
            file=sys.stderr,
        )
        # parse_failed=True tells load_agent_approvals() to skip this entry and look
        # further back in history, so a single bad Claude response never silently
        # blocks all sells by returning an empty decisions list.
        result = {
            "assessment":        raw,
            "regime":            "unknown",
            "regime_confidence": 0,
            "flags":             [],
            "decisions":         [],
            "cash_action":       "maintain",
            "cash_rationale":    "",
            "summary":           f"Agent ran ({run_type}) — response parse failed",
            "parse_failed":      True,
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
    # Propagate parse_failed flag so load_agent_approvals() can skip this entry
    if result.get("parse_failed"):
        entry["parse_failed"] = True

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
    exec_summary   = load_execution_summary()
    recent_history = log.get("runs", [])[:HISTORY_RUNS]   # last N runs for continuity

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
        print(f"History    : {len(recent_history)} prior runs loaded "
              f"(latest: {recent_history[0].get('type','?')} "
              f"{recent_history[0].get('timestamp','')[:10]})")
    if exec_summary:
        n_placed = len(exec_summary.get("orders_placed", []))
        print(f"Execution  : {n_placed} orders from last market-open "
              f"({exec_summary.get('timestamp', '')[:10]})")
    else:
        print("Execution  : no execution_summary.json found (run update.py first)")

    result, usage = run_agent(
        run_type, portfolio, strategy, enrichment,
        recent_history, exec_summary,
    )
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
