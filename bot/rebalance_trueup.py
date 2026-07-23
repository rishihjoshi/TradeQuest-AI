#!/usr/bin/env python3
"""
rebalance_trueup.py — one-time portfolio true-up for TradeQuest AI (v3.1).

WHY THIS EXISTS
---------------
The Jul-2026 quarterly rebalance left the book in a broken state that the daily pipeline could
not self-heal (see POSTMORTEM.md):

  • JBL is a runaway NAKED SHORT (~-22 shares, ~-70% weight) — a long-only strategy carrying
    unbounded-loss risk. It was created by re-selling a trend-broken name every run.
  • Cash ballooned to ~61% (target 5%) because the 5-orders/run throttle stretched the
    rebalance over ~12 days.
  • Financial Services grew to ~49% of the long book (30% cap) because the sector cap only
    warned, never trimmed.

This script computes the exact set of orders to TRUE UP the account to the strategy target:

  • Cover (BUY to close) any short position — restores the long-only invariant.
  • Keep the TOP-N current holdings by momentum rank; exit the rest.
  • Equal-weight the survivors (~1/N each, hard-capped at MAX_POSITION_PCT).
  • Enforce the MAX_SECTOR_PCT sector cap (trim over-weight sectors).
  • Deploy idle cash down to the CASH_FLOOR_PCT target.

SAFETY
------
  • Default mode is --dry-run: it PRINTS the order plan and the resulting book, and places
    NOTHING. Review it first.
  • Live execution requires the explicit --execute flag.
  • Paper-trading guard (verify_paper_url) aborts against any non-paper endpoint.
  • Long-only clamp: SELLs never exceed the held quantity; the account can never be pushed short.

USAGE
-----
    python bot/rebalance_trueup.py                 # dry-run (default) — prints the plan
    python bot/rebalance_trueup.py --dry-run       # same as above, explicit
    python bot/rebalance_trueup.py --execute       # place the orders on Alpaca paper
    python bot/rebalance_trueup.py --target-n 10 --cash-target 0.05
"""

from __future__ import annotations

import argparse
import json
import sys

# Reuse the vetted broker helpers and risk constants — no duplicated broker logic.
from update import (
    _alpaca_client,
    alpaca_read_state,
    verify_paper_url,
    DATA_FILE,
    TARGET_N,
    MAX_POSITION_PCT,
    MAX_SECTOR_PCT,
    CASH_FLOOR_PCT,
)

# momentum_rank == 0 means "no rank / missing data" — treat as worst so those names exit first.
_NO_RANK = 10_000


def _load_reference() -> dict[str, dict]:
    """Read data/portfolio.json for per-symbol momentum rank + sector (best-effort context)."""
    ref: dict[str, dict] = {}
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for h in data.get("holdings", []):
            ref[h["symbol"].upper()] = {
                "rank":   h.get("momentum_rank") or _NO_RANK,
                "sector": h.get("sector") or "Unknown",
            }
    except (OSError, ValueError) as e:
        print(f"Note: could not read {DATA_FILE} for ranks/sectors ({e}); "
              f"positions with no rank sort last.", file=sys.stderr)
    return ref


def _positions_frame(state: dict, ref: dict[str, dict]) -> list[dict]:
    """Normalise Alpaca positions into plain dicts with rank/sector/price/value."""
    rows: list[dict] = []
    for pos in state.get("positions", []):
        sym   = str(pos.symbol).upper()
        qty   = float(pos.qty or 0)
        price = float(pos.current_price or pos.avg_entry_price or 0)
        rinfo = ref.get(sym, {})
        rank  = rinfo.get("rank") or _NO_RANK
        rows.append({
            "symbol": sym,
            "qty":    qty,
            "price":  price,
            "value":  float(pos.market_value or qty * price),
            "rank":   rank if rank and rank > 0 else _NO_RANK,
            "sector": rinfo.get("sector", "Unknown"),
            "upnl":   float(getattr(pos, "unrealized_pl", 0) or 0),
        })
    return rows


def plan_trueup(state: dict, ref: dict[str, dict],
                target_n: int, cash_target: float) -> tuple[list[dict], dict]:
    """Return (orders, summary). Pure function — no side effects, safe to print/test."""
    pv   = float(state["portfolio_value"])
    rows = _positions_frame(state, ref)

    orders: list[dict] = []

    # 1) Cover every short first (restores long-only). A short has qty < 0 → BUY |qty| to close.
    for r in rows:
        if r["qty"] < 0:
            orders.append({
                "action": "BUY", "symbol": r["symbol"], "qty": int(round(-r["qty"])),
                "price": r["price"], "reason": "cover short (restore long-only)",
            })

    longs = [r for r in rows if r["qty"] > 0]

    # 2) Rank the current longs; keep the best target_n, exit the rest.
    longs_sorted = sorted(longs, key=lambda r: (r["rank"], -r["value"]))
    keep = longs_sorted[:target_n]
    exit_rows = longs_sorted[target_n:]

    for r in exit_rows:
        orders.append({
            "action": "SELL", "symbol": r["symbol"], "qty": int(r["qty"]),
            "price": r["price"], "reason": f"exit - rank {r['rank']} outside top {target_n}",
        })

    # 3) Equal-weight the survivors, hard-capped at MAX_POSITION_PCT, with a sector cap.
    per_target = min(pv * (1 - cash_target) / max(1, target_n), pv * MAX_POSITION_PCT)
    cap_val    = pv * MAX_SECTOR_PCT
    sector_val: dict[str, float] = {}
    target_shares: dict[str, int] = {}

    for r in keep:  # best-ranked first (keep already sorted)
        price = r["price"]
        if price <= 0:
            print(f"  Skip {r['symbol']}: non-positive price {price}", file=sys.stderr)
            target_shares[r["symbol"]] = int(r["qty"])  # leave as-is
            continue
        sec = r["sector"]
        room = max(0.0, cap_val - sector_val.get(sec, 0.0))
        dollars = min(per_target, room)
        shares = int(dollars // price)
        # Never force a name to zero purely on the sector cap if it's already held — keep ≥ current
        # only when within cap; otherwise trim toward the cap.
        target_shares[r["symbol"]] = shares
        sector_val[sec] = sector_val.get(sec, 0.0) + shares * price

    # 4) Convert target vs current into BUY/SELL deltas for the survivors.
    for r in keep:
        cur = int(r["qty"])
        tgt = target_shares.get(r["symbol"], cur)
        delta = tgt - cur
        if delta > 0:
            orders.append({
                "action": "BUY", "symbol": r["symbol"], "qty": delta,
                "price": r["price"], "reason": f"top-up to equal weight (~{per_target/pv:.1%})",
            })
        elif delta < 0:
            orders.append({
                "action": "SELL", "symbol": r["symbol"], "qty": -delta,
                "price": r["price"], "reason": "trim to equal weight / sector cap",
            })

    # ── Projected resulting book (for review) ──
    proj_cash = float(state["cash"])
    for o in orders:
        proj_cash += o["qty"] * o["price"] * (1 if o["action"] == "SELL" else -1)
    summary = {
        "pv": pv,
        "cash_before": float(state["cash"]),
        "cash_pct_before": round(float(state["cash"]) / pv * 100, 1) if pv else None,
        "proj_cash": round(proj_cash, 2),
        "proj_cash_pct": round(proj_cash / pv * 100, 1) if pv else None,
        "per_target_pct": round(per_target / pv * 100, 2) if pv else None,
        "n_keep": len(keep),
        "n_exit": len(exit_rows),
        "n_short_covers": sum(1 for o in orders if o["reason"].startswith("cover")),
        "kept_symbols": [r["symbol"] for r in keep],
        "sector_projection": {s: round(v / pv * 100, 1) for s, v in sorted(
            sector_val.items(), key=lambda kv: -kv[1])},
    }
    return orders, summary


def _print_plan(orders: list[dict], summary: dict) -> None:
    print("\n" + "=" * 68)
    print("  TradeQuest AI - REBALANCE TRUE-UP PLAN")
    print("=" * 68)
    print(f"  Portfolio value        : ${summary['pv']:,.0f}")
    print(f"  Cash before            : ${summary['cash_before']:,.0f} "
          f"({summary['cash_pct_before']}%)")
    print(f"  Projected cash after   : ${summary['proj_cash']:,.0f} "
          f"({summary['proj_cash_pct']}%)   [target ~{CASH_FLOOR_PCT:.0%}]")
    print(f"  Equal-weight per name  : ~{summary['per_target_pct']}% "
          f"(cap {MAX_POSITION_PCT:.0%})")
    print(f"  Keep / exit / covers   : {summary['n_keep']} / {summary['n_exit']} / "
          f"{summary['n_short_covers']}")
    print(f"  Target book            : {', '.join(summary['kept_symbols'])}")
    print(f"  Sector projection      : {summary['sector_projection']}  (cap {MAX_SECTOR_PCT:.0%})")
    print("-" * 68)
    if not orders:
        print("  No orders — book already at target.")
        return
    print(f"  {'ACTION':6} {'SYM':6} {'QTY':>5} {'PRICE':>9} {'VALUE':>10}  REASON")
    for o in orders:
        val = o["qty"] * o["price"]
        print(f"  {o['action']:6} {o['symbol']:6} {o['qty']:>5} "
              f"{o['price']:>9.2f} {val:>10,.0f}  {o['reason']}")
    gross = sum(o["qty"] * o["price"] for o in orders)
    print("-" * 68)
    print(f"  {len(orders)} orders, gross traded ~ ${gross:,.0f}")
    print("=" * 68 + "\n")


def _execute(client, orders: list[dict]) -> list[tuple]:
    """Submit the planned orders to Alpaca. Long-only clamp is re-applied defensively."""
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    # Re-fetch live held quantities for a final long-only clamp on SELLs.
    held: dict[str, float] = {}
    try:
        for pos in client.get_all_positions():
            held[str(pos.symbol).upper()] = float(pos.qty or 0)
    except Exception as e:
        print(f"  Warning: could not fetch positions for final clamp: {e}", file=sys.stderr)

    placed: list[tuple] = []
    # Sells/covers first to free (or settle) cash before top-up buys.
    ordered = sorted(orders, key=lambda o: 0 if o["action"] == "SELL" else 1)
    for o in ordered:
        sym, qty, action = o["symbol"], int(o["qty"]), o["action"]
        if qty <= 0 or o["price"] <= 0:
            print(f"  Guard: skip {action} {sym} (qty {qty}, price {o['price']})")
            continue
        if action == "SELL":
            have = int(held.get(sym, 0))
            if have <= 0:
                print(f"  Long-only guard: {sym} not held — SELL skipped")
                continue
            qty = min(qty, have)  # never sell more than held → never go short
        try:
            client.submit_order(MarketOrderRequest(
                symbol=sym, qty=qty,
                side=OrderSide.SELL if action == "SELL" else OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            arrow = "SELL" if action == "SELL" else "BUY "
            print(f"  {arrow} {qty:>5} {sym}")
            placed.append((action, sym, qty))
        except Exception as e:
            print(f"  ✗ {action} {sym}: {e}", file=sys.stderr)
    return placed


def main() -> int:
    ap = argparse.ArgumentParser(description="One-time TradeQuest portfolio true-up.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan only (default)")
    mode.add_argument("--execute", action="store_true", help="place the orders on Alpaca paper")
    ap.add_argument("--target-n", type=int, default=TARGET_N, help="number of names to keep")
    ap.add_argument("--cash-target", type=float, default=CASH_FLOOR_PCT,
                    help="target cash fraction (e.g. 0.05)")
    args = ap.parse_args()

    execute = args.execute  # dry-run is the default whenever --execute is absent

    client = _alpaca_client()
    if client is None:
        print("No Alpaca credentials — cannot read account state. Aborting.", file=sys.stderr)
        return 2
    verify_paper_url()  # hard stop against any non-paper endpoint

    state = alpaca_read_state(client)
    if not state:
        print("Could not read Alpaca account state. Aborting.", file=sys.stderr)
        return 2

    ref = _load_reference()
    orders, summary = plan_trueup(state, ref, args.target_n, args.cash_target)
    _print_plan(orders, summary)

    if not execute:
        print("DRY-RUN — no orders placed. Re-run with --execute to submit these orders.\n")
        return 0

    print("EXECUTE — submitting orders to Alpaca paper …\n")
    placed = _execute(client, orders)
    print(f"\nDone. {len(placed)} order(s) submitted. "
          f"Re-run the daily pipeline (update.py) to refresh portfolio.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
