"""Unit tests for safety-critical functions that had no coverage.

A coverage audit during the dev pipeline found 31 public functions in bot/ with no test
reference at all — including the two that stand between this bot and real money:

  * verify_paper_url()     — the only thing preventing orders hitting a LIVE endpoint
  * handle_manual_order()  — the ORDER_SYMBOL escape hatch, which bypasses the rebalance path

The audit also surfaced a live defect: sentinel.count_consecutive_sell_flags() filtered on a
`run_type` key that agent.py never writes (it writes `type`), so the filter skipped every run,
the count was always 0, and sentinel Rule 3 never fired once between Apr and Aug 2026. Replayed
against the archived log, ROST had 21 consecutive flags against a threshold of 5. That
regression is locked down here.

Stdlib + mocks only.
"""
# pylint: disable=protected-access,unused-argument,missing-class-docstring,missing-function-docstring
import json
import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

for _pkg in ("alpaca", "alpaca.trading", "alpaca.trading.client",
             "alpaca.trading.requests", "alpaca.trading.enums", "anthropic"):
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

_enums = sys.modules["alpaca.trading.enums"]
for _name in ("TimeInForce", "QueryOrderStatus"):
    setattr(_enums, _name, MagicMock(name=_name))


class _Side:
    """OrderSide needs real identity so `side == OrderSide.SELL` works in the code under test."""
    BUY = "buy"
    SELL = "sell"


_enums.OrderSide = _Side
_reqs = sys.modules["alpaca.trading.requests"]
for _name in ("MarketOrderRequest", "GetOrdersRequest", "LimitOrderRequest", "StopOrderRequest"):
    setattr(_reqs, _name, MagicMock(name=_name))

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bot"))
import update     # noqa: E402
import sentinel   # noqa: E402

ARCHIVE_LOG = REPO / "archive" / "2026-04_2026-08-account-1" / "data" / "agent_log.json"


def _pos(symbol, qty, price=100.0):
    p = MagicMock()
    p.symbol, p.qty = symbol, qty
    p.current_price = p.avg_entry_price = price
    p.market_value = qty * price
    p.unrealized_pl = 0.0
    return p


# ════════════════════════════════════════════════════════════════════════════
# The live-trading guard — untested until now
# ════════════════════════════════════════════════════════════════════════════
class TestPaperTradingGuard(unittest.TestCase):

    def _with_url(self, url):
        orig = update.ALPACA_BASE_URL
        update.ALPACA_BASE_URL = url
        self.addCleanup(lambda: setattr(update, "ALPACA_BASE_URL", orig))

    def test_paper_endpoint_is_allowed(self):
        self._with_url("https://paper-api.alpaca.markets")
        update.verify_paper_url()   # must not raise

    def test_live_endpoint_is_refused(self):
        """This is the last line between a paper bot and real money."""
        self._with_url("https://api.alpaca.markets")
        with self.assertRaises(RuntimeError) as ctx:
            update.verify_paper_url()
        self.assertIn("Refusing to place orders", str(ctx.exception))

    def test_empty_url_is_refused(self):
        self._with_url("")
        with self.assertRaises(RuntimeError):
            update.verify_paper_url()

    def test_match_is_case_insensitive(self):
        self._with_url("https://PAPER-API.ALPACA.MARKETS")
        update.verify_paper_url()


# ════════════════════════════════════════════════════════════════════════════
# The manual-order escape hatch — a second path to the broker
# ════════════════════════════════════════════════════════════════════════════
class TestManualOrderHatch(unittest.TestCase):

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("ORDER_SYMBOL", "ORDER_QTY", "ORDER_SIDE", "ORDER_TYPE")}
        orig_url, orig_dry = update.ALPACA_BASE_URL, update.DRY_RUN
        update.ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

        def restore():
            update.ALPACA_BASE_URL, update.DRY_RUN = orig_url, orig_dry
            for k, v in self._env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)

    @staticmethod
    def _env_order(symbol, qty, side):
        os.environ["ORDER_SYMBOL"] = symbol
        os.environ["ORDER_QTY"] = str(qty)
        os.environ["ORDER_SIDE"] = side
        os.environ["ORDER_TYPE"] = "market"

    @staticmethod
    def _client(positions):
        c = MagicMock()
        c.get_all_positions.return_value = positions
        return c

    def test_sell_beyond_held_is_clamped(self):
        """Selling more than held is how the JBL short was opened in the first place."""
        self._env_order("ROST", 50, "sell")
        client = self._client([_pos("ROST", 5)])
        update.handle_manual_order(client)
        self.assertEqual(client.submit_order.call_count, 1)

    def test_sell_of_unheld_symbol_is_blocked(self):
        self._env_order("JBL", 22, "sell")
        client = self._client([_pos("ROST", 5)])
        update.handle_manual_order(client)
        client.submit_order.assert_not_called()

    def test_sell_of_short_position_is_blocked(self):
        """A short must never be deepened by hand."""
        self._env_order("JBL", 1, "sell")
        client = self._client([_pos("JBL", -22)])
        update.handle_manual_order(client)
        client.submit_order.assert_not_called()

    def test_dry_run_submits_nothing(self):
        self._env_order("ROST", 2, "sell")
        update.DRY_RUN = True
        client = self._client([_pos("ROST", 5)])
        update.handle_manual_order(client)
        client.submit_order.assert_not_called()

    def test_invalid_symbol_is_rejected(self):
        self._env_order("../../etc/passwd", 1, "buy")
        client = self._client([])
        update.handle_manual_order(client)
        client.submit_order.assert_not_called()

    def test_quantity_above_safety_cap_is_rejected(self):
        self._env_order("ROST", 10_001, "buy")
        client = self._client([])
        update.handle_manual_order(client)
        client.submit_order.assert_not_called()

    def test_no_symbol_is_a_no_op(self):
        os.environ.pop("ORDER_SYMBOL", None)
        client = self._client([])
        update.handle_manual_order(client)
        client.submit_order.assert_not_called()


# ════════════════════════════════════════════════════════════════════════════
# REGRESSION — sentinel Rule 3 never fired for four months
# ════════════════════════════════════════════════════════════════════════════
class TestSentinelFlagCounter(unittest.TestCase):

    @staticmethod
    def _run(key, value, symbol, action="SELL"):
        return {key: value, "decisions": [{"action": action, "symbol": symbol}]}

    def test_counts_runs_keyed_by_type(self):
        """agent.py writes `type`. Reading only `run_type` skipped every run -> always 0."""
        runs = [self._run("type", "day_end", "ROST") for _ in range(6)]
        self.assertEqual(sentinel.count_consecutive_sell_flags(runs, "ROST"), 6)

    def test_still_counts_runs_keyed_by_run_type(self):
        runs = [self._run("run_type", "day_end", "ROST") for _ in range(4)]
        self.assertEqual(sentinel.count_consecutive_sell_flags(runs, "ROST"), 4)

    def test_streak_breaks_on_a_clean_run(self):
        runs = [self._run("type", "day_end", "ROST"),
                self._run("type", "day_end", "ROST"),
                self._run("type", "day_end", "VLO"),
                self._run("type", "day_end", "ROST")]
        self.assertEqual(sentinel.count_consecutive_sell_flags(runs, "ROST"), 2)

    def test_watch_counts_toward_the_streak(self):
        runs = [self._run("type", "day_end", "ROST", action="WATCH") for _ in range(3)]
        self.assertEqual(sentinel.count_consecutive_sell_flags(runs, "ROST"), 3)

    def test_hold_does_not_count(self):
        runs = [self._run("type", "day_end", "ROST", action="HOLD")]
        self.assertEqual(sentinel.count_consecutive_sell_flags(runs, "ROST"), 0)

    def test_unrelated_run_types_are_skipped(self):
        runs = [self._run("type", "sentinel", "ROST")]
        self.assertEqual(sentinel.count_consecutive_sell_flags(runs, "ROST"), 0)

    @unittest.skipUnless(ARCHIVE_LOG.exists(), "archive not present")
    def test_archived_log_proves_the_bug_was_real(self):
        """ROST was flagged 21 consecutive times against a threshold of 5, and never sold."""
        runs = json.loads(ARCHIVE_LOG.read_text(encoding="utf-8"))["runs"]
        streak = sentinel.count_consecutive_sell_flags(runs, "ROST")
        self.assertGreaterEqual(streak, sentinel.PERSISTENT_FLAG_RUNS,
                                "Rule 3 should have fired on ROST and never did")

    def test_below_ma_counter_breaks_streak_correctly(self):
        below = {"type": "day_end", "decisions": [
            {"symbol": "FFIV", "reason": "price below 50-day MA", "rule_triggered": "trend_break"}]}
        clean = {"type": "day_end", "decisions": [
            {"symbol": "FFIV", "reason": "holding", "rule_triggered": "null"}]}
        self.assertEqual(sentinel.count_consecutive_below_ma([below, below, clean], "FFIV"), 2)
        self.assertEqual(sentinel.count_consecutive_below_ma([clean, below], "FFIV"), 0)


# ════════════════════════════════════════════════════════════════════════════
# Risk caps, regime, equity curve — all previously untested
# ════════════════════════════════════════════════════════════════════════════
class TestSectorConcentration(unittest.TestCase):

    def test_breach_is_reported(self):
        holdings = [{"symbol": "BNY", "sector": "Financial Services", "weight": 0.20},
                    {"symbol": "BEN", "sector": "Financial Services", "weight": 0.25},
                    {"symbol": "VLO", "sector": "Energy", "weight": 0.10}]
        over = update.check_sector_concentration(holdings)
        self.assertIn("Financial Services", over)
        self.assertAlmostEqual(over["Financial Services"], 0.45, 2)
        self.assertNotIn("Energy", over)

    def test_exactly_at_the_cap_is_not_a_breach(self):
        holdings = [{"symbol": "A", "sector": "Tech", "weight": update.MAX_SECTOR_PCT}]
        self.assertEqual(update.check_sector_concentration(holdings), {})

    def test_missing_sector_buckets_as_unknown(self):
        holdings = [{"symbol": "APH", "weight": 0.40}]
        self.assertIn("Unknown", update.check_sector_concentration(holdings))


class TestEquityCurve(unittest.TestCase):

    def test_same_day_updates_in_place(self):
        curve = update.update_equity_curve([], 10_000)
        curve = update.update_equity_curve(curve, 10_500)
        self.assertEqual(len(curve), 1, "two runs on one day must not create two points")
        self.assertEqual(curve[0]["value"], 10_500)

    def test_new_day_appends(self):
        curve = update.update_equity_curve([{"date": "Jan 1", "value": 9_000}], 10_000)
        self.assertEqual(len(curve), 2)

    def test_history_is_capped_at_90_points(self):
        curve = [{"date": f"D{i}", "value": i} for i in range(120)]
        self.assertEqual(len(update.update_equity_curve(curve, 999)), 90)

    def test_fresh_start_produces_a_single_point(self):
        """Generation 2 begins with an empty curve; the first run seeds exactly one point."""
        curve = update.update_equity_curve([], 100_000)
        self.assertEqual(len(curve), 1)
        self.assertEqual(curve[0]["value"], 100_000)


class TestRiskMetrics(unittest.TestCase):

    def test_short_curve_returns_nothing(self):
        """Too few points must yield {} so callers keep the prior value, not emit a fake 0."""
        self.assertEqual(update.compute_risk_metrics([{"date": "A", "value": 1}]), {})

    def test_drawdown_is_measured_from_the_peak(self):
        curve = [{"date": "A", "value": 10_000}, {"date": "B", "value": 11_000},
                 {"date": "C", "value": 9_000},  {"date": "D", "value": 9_500}]
        m = update.compute_risk_metrics(curve)
        self.assertAlmostEqual(m["max_drawdown_pct"], 18.18, 1)   # 11,000 -> 9,000

    def test_flat_curve_has_no_drawdown(self):
        curve = [{"date": c, "value": 10_000} for c in "ABCD"]
        self.assertEqual(update.compute_risk_metrics(curve)["max_drawdown_pct"], 0.0)


class TestRegimeDetection(unittest.TestCase):

    @staticmethod
    def _spy(values):
        import pandas as pd
        return pd.Series([float(v) for v in values])

    def test_uptrend_low_vol_is_bull(self):
        r = update.detect_regime(self._spy([100 + i * 0.1 for i in range(250)]))
        self.assertEqual(r["market_regime"], "bull")
        self.assertEqual(r["equity_exposure"], 0.95)
        self.assertAlmostEqual(r["cash_target"], 0.05, 2)

    def test_downtrend_is_bear(self):
        r = update.detect_regime(self._spy([200 - i * 0.3 for i in range(250)]))
        self.assertEqual(r["market_regime"], "bear")
        self.assertEqual(r["equity_exposure"], 0.50)

    def test_cash_target_always_complements_exposure(self):
        for series in ([100 + i * 0.1 for i in range(250)], [200 - i * 0.3 for i in range(250)]):
            r = update.detect_regime(self._spy(series))
            self.assertAlmostEqual(r["equity_exposure"] + r["cash_target"], 1.0, 2)


class TestClosedOrderPagination(unittest.TestCase):
    """P&L attribution was computed over 8 of 50 trades because of a flat limit=50 fetch."""

    def test_pages_until_a_short_batch(self):
        client = MagicMock()
        page = [MagicMock(submitted_at=f"t{i}", created_at=f"t{i}") for i in range(500)]
        client.get_orders.side_effect = [page, page[:10]]
        self.assertEqual(len(update.fetch_closed_orders(client)), 510)

    def test_stops_at_the_hard_ceiling(self):
        client = MagicMock()
        page = [MagicMock(submitted_at="t", created_at="t") for _ in range(500)]
        client.get_orders.return_value = page
        self.assertLessEqual(len(update.fetch_closed_orders(client, max_orders=1000)), 1000)

    def test_empty_history_is_handled(self):
        client = MagicMock()
        client.get_orders.return_value = []
        self.assertEqual(update.fetch_closed_orders(client), [])


class TestAlpacaStateShape(unittest.TestCase):

    def test_state_exposes_deployable_cash_and_shorts(self):
        client = MagicMock()
        account = MagicMock()
        account.portfolio_value, account.cash = "9533.72", "7565.93"
        client.get_account.return_value = account
        client.get_all_positions.return_value = [_pos("JBL", -22, 344.15)]
        client.get_orders.return_value = []

        state = update.alpaca_read_state(client)
        self.assertIn("deployable_cash", state)
        self.assertIn("shorts", state)
        self.assertEqual(len(state["shorts"]), 1)
        self.assertLess(state["deployable_cash"], state["cash"],
                        "short proceeds must be excluded from deployable cash")

    def test_fetch_failure_returns_none_rather_than_raising(self):
        """A broker outage must degrade, not crash the run (Directive 8)."""
        client = MagicMock()
        client.get_account.side_effect = RuntimeError("broker down")
        self.assertIsNone(update.alpaca_read_state(client))


class TestExecutionSummaryRoundTrip(unittest.TestCase):

    def test_written_summary_is_readable_and_complete(self):
        with TemporaryDirectory() as d:
            update.write_execution_summary(
                [("COVER", "JBL", 22)], [{"symbol": "VLO", "reason": "gated"}], ["boom"],
                12.5, Path(d), long_only_breach=True, breaches=["JBL: short"],
                breach_streak=2, halted=False, dry_run=False)
            payload = json.loads((Path(d) / "execution_summary.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["long_only_breach"])
        self.assertEqual(payload["breach_streak"], 2)
        self.assertFalse(payload["trading_halted"])
        self.assertEqual(payload["orders_placed"][0]["action"], "COVER")
        self.assertEqual(payload["orders_skipped"][0]["symbol"], "VLO")
        self.assertEqual(payload["errors"], ["boom"])


if __name__ == "__main__":
    unittest.main()
