"""Unit tests for the v3.2 execution-resilience release.

Covers the failures found in the 2026-08-17 review (see POSTMORTEM.md "v3.1 → v3.2"):
  F-A — deployable cash excludes short proceeds (account.cash is inflated by a short)
  F-B — COVER path closes a short, bypassing the quarterly lock / cash floor / approval gate
  F-C — normalize_decisions preserves COVER in a non-quarterly month and on day_start
  F-D — rank hysteresis + minimum hold stop the boundary churn
  F-E — re-entry cooldown blocks buy → sell → re-buy round trips
  F-F — agent.py degrades to exit 0 (never freezes the pipeline) when the model API is down

Stdlib + mocks only — no live alpaca/anthropic packages required.
"""
# pylint: disable=protected-access,unused-argument,missing-class-docstring,missing-function-docstring
# Test-suite patterns, not defects: tests exercise module internals (_fmp_get); stub
# signatures must match the real API even when a test ignores a parameter; and the
# test method name is the documentation.
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

# Stub the packages that aren't installed in CI (mirrors test_v31_execution.py).
for _pkg in ("alpaca", "alpaca.trading", "alpaca.trading.client",
             "alpaca.trading.requests", "alpaca.trading.enums", "anthropic"):
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

_enums = sys.modules["alpaca.trading.enums"]
for _name in ("OrderSide", "TimeInForce", "QueryOrderStatus"):
    setattr(_enums, _name, MagicMock(name=_name))
_reqs = sys.modules["alpaca.trading.requests"]
for _name in ("MarketOrderRequest", "GetOrdersRequest"):
    setattr(_reqs, _name, MagicMock(name=_name))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
import update   # noqa: E402
import agent    # noqa: E402


def _pos(symbol, qty, price=100.0, mv=None):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    p.current_price = price
    p.avg_entry_price = price
    p.market_value = qty * price if mv is None else mv
    p.unrealized_pl = 0.0
    return p


def _holding(symbol, shares=10, price=100.0, entry_days_ago=60, status="above_ma", **kw):
    entry = (datetime.now() - timedelta(days=entry_days_ago)).strftime("%Y-%m-%d")
    h = {
        "symbol": symbol,
        "shares": shares,
        "current_price": price,
        "market_value": shares * price,
        "entry_date": entry,
        "status": status,
        "pnl": 0.0,
    }
    h.update(kw)
    return h


class _FakeClient:
    """Captures submit_order calls; serves positions + empty open-orders list."""
    def __init__(self, positions):
        self._positions = positions
        self.submitted = []

    def get_all_positions(self):
        return self._positions

    def get_orders(self, *_a, **_k):
        return []

    def submit_order(self, req):
        kwargs = req.call_args.kwargs if hasattr(req, "call_args") and req.call_args else {}
        self.submitted.append((kwargs.get("side"), kwargs.get("symbol"), kwargs.get("qty")))


# ════════════════════════════════════════════════════════════════════════════
# F-A — deployable cash excludes short proceeds
# ════════════════════════════════════════════════════════════════════════════
class TestDeployableCash(unittest.TestCase):

    def test_short_proceeds_excluded(self):
        """The real Aug-10 book: $7,566 reported cash was ~$0 of actual buying power."""
        positions = [_pos("JBL", -22, price=344.15, mv=-7571.30),
                     _pos("NTAP", 6, price=199.88)]
        deployable = update.compute_deployable_cash(7565.93, positions)
        self.assertLess(deployable, 1.0,
                        "cash minus the $7,571 short liability is ~$0, not $7,566")

    def test_no_shorts_leaves_cash_untouched(self):
        positions = [_pos("NTAP", 6, price=199.88)]
        self.assertAlmostEqual(update.compute_deployable_cash(1234.56, positions), 1234.56, 2)

    def test_never_negative(self):
        """A short worth more than cash floors deployable at zero, never below."""
        positions = [_pos("JBL", -22, price=344.15, mv=-7571.30)]
        self.assertEqual(update.compute_deployable_cash(100.0, positions), 0.0)

    def test_short_positions_detects_only_negatives(self):
        positions = [_pos("JBL", -22), _pos("NTAP", 6), _pos("VLO", 3)]
        self.assertEqual([str(p.symbol) for p in update.short_positions(positions)], ["JBL"])

    def test_summary_reports_deployable_and_breach(self):
        holdings = [
            {"symbol": "JBL",  "shares": -22, "market_value": -7571.30, "pnl": -557.41},
            {"symbol": "NTAP", "shares": 6,   "market_value": 1199.28,  "pnl": 149.94},
        ]
        s = update.compute_summary(holdings, cash=7565.93, existing_summary={}, all_trades=[])
        self.assertTrue(s["long_only_breach"])
        self.assertEqual(s["short_proceeds"], 7571.30)
        self.assertLess(s["deployable_cash"], 1.0)
        # Raw cash is preserved for the dashboard's short-proceeds IOU note.
        self.assertEqual(s["cash"], 7565.93)

    def test_risk_gate_cannot_spend_phantom_cash(self):
        """apply_risk_limits sized against deployable cash must authorize no buys."""
        _, buys = update.apply_risk_limits(
            to_sell=[], to_buy=[("VLO", 3)], pv=9533.72,
            cash=0.0,                       # deployable, not the $7,566 raw figure
            agent_sell_approvals=set(), prices={"VLO": 308.92},
        )
        self.assertEqual(buys, [], "no purchase may be funded by short proceeds")


# ════════════════════════════════════════════════════════════════════════════
# F-B — COVER path (the way out of the JBL deadlock)
# ════════════════════════════════════════════════════════════════════════════
class TestCoverPath(unittest.TestCase):

    def test_cover_closes_the_short(self):
        client = _FakeClient([_pos("JBL", -22, price=344.15, mv=-7571.30)])
        placed = update.alpaca_place_orders(
            client, to_sell=[], to_buy=[], pv=9533.72, cash=0.0,
            prices={"JBL": 344.15}, to_cover=[("JBL", 22)],
        )
        covers = [p for p in placed if p[0] == "COVER"]
        self.assertEqual(len(covers), 1)
        self.assertEqual(covers[0][1:], ("JBL", 22))

    def test_cover_ignores_the_cash_floor(self):
        """A $7,571 cover must not be blocked by a cash test — it is funded by the short."""
        client = _FakeClient([_pos("JBL", -22, price=344.15, mv=-7571.30)])
        placed = update.alpaca_place_orders(
            client, to_sell=[], to_buy=[("VLO", 1)], pv=9533.72,
            cash=0.0,                                  # zero deployable cash
            prices={"JBL": 344.15, "VLO": 308.92}, to_cover=[("JBL", 22)],
        )
        self.assertEqual([p[0] for p in placed if p[0] == "COVER"], ["COVER"])
        self.assertEqual([p for p in placed if p[0] == "BUY"], [],
                         "the ordinary BUY is still cash-gated; only the cover is exempt")

    def test_cover_never_overshoots_into_a_long(self):
        """Covering more than the outstanding short would flip the position long."""
        client = _FakeClient([_pos("JBL", -5, price=344.15, mv=-1720.75)])
        placed = update.alpaca_place_orders(
            client, to_sell=[], to_buy=[], pv=9533.72, cash=0.0,
            prices={"JBL": 344.15}, to_cover=[("JBL", 22)],
        )
        self.assertEqual([p for p in placed if p[0] == "COVER"][0][2], 5)

    def test_cover_skipped_when_not_short(self):
        client = _FakeClient([_pos("JBL", 10, price=344.15)])
        placed = update.alpaca_place_orders(
            client, to_sell=[], to_buy=[], pv=9533.72, cash=1_000,
            prices={"JBL": 344.15}, to_cover=[("JBL", 22)],
        )
        self.assertEqual([p for p in placed if p[0] == "COVER"], [],
                         "a long position must never receive a cover order")

    def test_cover_runs_before_sells_and_buys(self):
        client = _FakeClient([_pos("JBL", -22, price=344.15, mv=-7571.30),
                              _pos("ROST", 2, price=255.05)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("ROST", 2)], to_buy=[], pv=9533.72, cash=5_000,
            prices={"JBL": 344.15, "ROST": 255.05}, to_cover=[("JBL", 22)],
        )
        self.assertEqual(placed[0][0], "COVER",
                         "the invariant must be restored before discretionary orders")


# ════════════════════════════════════════════════════════════════════════════
# F-C — COVER survives normalize_decisions
# ════════════════════════════════════════════════════════════════════════════
class TestNormalizeCover(unittest.TestCase):

    def test_cover_survives_non_quarterly_month(self):
        """BUY→HOLD must not swallow a COVER — that is what silenced JBL in Aug 2026."""
        out = agent.normalize_decisions(
            [{"action": "COVER", "symbol": "JBL", "reason": "long-only breach"}],
            run_type="day_end", quarterly=False,
        )
        self.assertEqual(out[0]["action"], "COVER")
        self.assertEqual(out[0]["urgency"], "next_open")

    def test_cover_survives_day_start(self):
        """day_start is flag-only for trades, but an invariant breach still executes."""
        out = agent.normalize_decisions(
            [{"action": "COVER", "symbol": "JBL", "reason": "long-only breach"}],
            run_type="day_start", quarterly=False,
        )
        self.assertEqual(out[0]["action"], "COVER")

    def test_plain_buy_is_still_blocked_off_quarter(self):
        """The v2.2 lock must remain intact for ordinary purchases."""
        out = agent.normalize_decisions(
            [{"action": "BUY", "symbol": "VLO", "reason": "new entrant"}],
            run_type="day_end", quarterly=False,
        )
        self.assertEqual(out[0]["action"], "HOLD")


# ════════════════════════════════════════════════════════════════════════════
# F-D — rank hysteresis + minimum hold
# ════════════════════════════════════════════════════════════════════════════
class TestRankHysteresis(unittest.TestCase):

    def test_rank_inside_band_is_held(self):
        """Rank 12 with TARGET_N=10 sits inside the band (exit at >15) — hold, don't churn."""
        do_exit, why = update.should_exit_on_rank(_holding("CSX"), rank=12, target_n=10)
        self.assertFalse(do_exit, why)

    def test_rank_past_band_exits(self):
        do_exit, _ = update.should_exit_on_rank(_holding("CSX"), rank=16, target_n=10)
        self.assertTrue(do_exit)

    def test_unranked_position_exits(self):
        """A name that dropped out of the screen entirely cannot be evaluated — exit it."""
        do_exit, _ = update.should_exit_on_rank(_holding("SPG"), rank=None, target_n=10)
        self.assertTrue(do_exit)

    def test_min_hold_defers_a_young_exit(self):
        """FFIV was bought 07-28 and sold 07-30 for -$45.74. Two days is not a signal."""
        h = _holding("FFIV", entry_days_ago=2, status="above_ma")
        do_exit, why = update.should_exit_on_rank(h, rank=16, target_n=10)
        self.assertFalse(do_exit)
        self.assertIn("min", why.lower())

    def test_min_hold_waived_on_ma_break(self):
        """A genuine trend break (Rule A) must not be delayed by a churn damper."""
        h = _holding("FFIV", entry_days_ago=2, status="below_ma")
        do_exit, why = update.should_exit_on_rank(h, rank=16, target_n=10)
        self.assertTrue(do_exit, why)

    def test_old_position_past_band_still_exits(self):
        h = _holding("CSX", entry_days_ago=45)
        self.assertTrue(update.should_exit_on_rank(h, rank=21, target_n=10)[0])

    def test_days_held_handles_missing_and_bad_dates(self):
        self.assertIsNone(update.days_held({}))
        self.assertIsNone(update.days_held({"entry_date": "not-a-date"}))
        self.assertGreaterEqual(update.days_held(_holding("X", entry_days_ago=5)), 4)


# ════════════════════════════════════════════════════════════════════════════
# F-E — re-entry cooldown
# ════════════════════════════════════════════════════════════════════════════
class TestReentryCooldown(unittest.TestCase):

    @staticmethod
    def _sell(symbol, days_ago):
        return {"action": "SELL", "symbol": symbol,
                "date": (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")}

    def test_recent_sell_blocks_rebuy(self):
        """WST: sold 07-27 at $326.28, re-bought 07-28 at $338.14 — $11.86/sh worse."""
        age = update.in_reentry_cooldown("WST", [self._sell("WST", 1)])
        self.assertEqual(age, 1)

    def test_old_sell_allows_rebuy(self):
        self.assertIsNone(update.in_reentry_cooldown("WST", [self._sell("WST", 30)]))

    def test_other_symbols_unaffected(self):
        self.assertIsNone(update.in_reentry_cooldown("VLO", [self._sell("WST", 1)]))

    def test_buys_are_ignored(self):
        trades = [{"action": "BUY", "symbol": "WST",
                   "date": datetime.now().strftime("%Y-%m-%d")}]
        self.assertIsNone(update.in_reentry_cooldown("WST", trades))

    def test_malformed_trade_rows_do_not_crash(self):
        trades = [{"action": "SELL", "symbol": "WST", "date": "garbage"},
                  {"action": "SELL"}, {}]
        self.assertIsNone(update.in_reentry_cooldown("WST", trades))


# ════════════════════════════════════════════════════════════════════════════
# F-F — resilience: the agent degrades, it does not block
# ════════════════════════════════════════════════════════════════════════════
class TestAgentDegradation(unittest.TestCase):

    def test_degraded_entry_is_skipped_by_approvals(self):
        """A degraded run carries parse_failed so approvals fall back to the last good run."""
        log = {"runs": []}
        original = agent.LOG_FILE
        try:
            agent.LOG_FILE = Path(__file__).resolve().parent / "_tmp_agent_log.json"
            entry = agent.write_degraded_log(log, "day_end", RuntimeError("credit balance too low"))
            self.assertTrue(entry["parse_failed"])
            self.assertTrue(entry["agent_unavailable"])
            self.assertEqual(entry["decisions"], [])
            self.assertIn("credit balance too low", entry["assessment"])
        finally:
            agent.LOG_FILE.unlink(missing_ok=True)
            agent.LOG_FILE = original


# ════════════════════════════════════════════════════════════════════════════
# Housekeeping — next_rebalance must name a real quarterly month
# ════════════════════════════════════════════════════════════════════════════
class TestNextQuarterly(unittest.TestCase):

    def test_august_points_at_october_not_september(self):
        """portfolio.json advertised 2026-09-01 while buys were locked until Oct."""
        self.assertEqual(update.next_quarterly_date(datetime(2026, 8, 17)), "2026-10-01")

    def test_wraps_into_next_year(self):
        self.assertEqual(update.next_quarterly_date(datetime(2026, 11, 3)), "2027-01-01")

    def test_inside_a_quarterly_month_points_at_the_next_one(self):
        self.assertEqual(update.next_quarterly_date(datetime(2026, 7, 15)), "2026-10-01")


if __name__ == "__main__":
    unittest.main()
