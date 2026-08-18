"""Unit tests for the v4.0 clean-reset release.

Covers the machinery that makes account generation 2 safe to start:
  G1 — doc/code consistency: STRATEGY §8 constants must match update.py (drift killer)
  G2 — RESET_PORTFOLIO bootstrap seeds from real account equity and refuses a non-empty account
  G3 — DRY_RUN shadow mode submits nothing while still recording the plan
  G4 — assert_invariants + breach-streak kill switch
  G5 — STRATEGY.md Part I / Part II split
  G6 — the archive stays an executable regression fixture

Stdlib + mocks only — no live alpaca/anthropic packages required.
"""
# pylint: disable=protected-access,unused-argument,missing-class-docstring,missing-function-docstring
# Test-suite patterns, not defects: tests exercise module internals (_fmp_get); stub
# signatures must match the real API even when a test ignores a parameter; and the
# test method name is the documentation.
import ast
import json
import re
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

for _pkg in ("alpaca", "alpaca.trading", "alpaca.trading.client",
             "alpaca.trading.requests", "alpaca.trading.enums", "anthropic"):
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

_enums = sys.modules["alpaca.trading.enums"]
for _name in ("OrderSide", "TimeInForce", "QueryOrderStatus"):
    setattr(_enums, _name, MagicMock(name=_name))
_reqs = sys.modules["alpaca.trading.requests"]
for _name in ("MarketOrderRequest", "GetOrdersRequest"):
    setattr(_reqs, _name, MagicMock(name=_name))

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bot"))
import update   # noqa: E402
import agent    # noqa: E402

ARCHIVE = REPO / "archive" / "2026-04_2026-08-account-1"


def _pos(symbol, qty, price=100.0, mv=None):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    p.current_price = price
    p.avg_entry_price = price
    p.market_value = qty * price if mv is None else mv
    p.unrealized_pl = 0.0
    return p


class _FakeClient:
    def __init__(self, positions):
        self._positions = positions
        self.submitted = []

    def get_all_positions(self):
        return self._positions

    def get_orders(self, *_a, **_k):
        return []

    def submit_order(self, req):
        kwargs = req.call_args.kwargs if hasattr(req, "call_args") and req.call_args else {}
        self.submitted.append((kwargs.get("symbol"), kwargs.get("qty")))


# ════════════════════════════════════════════════════════════════════════════
# G1 — the drift killer: STRATEGY §8 constants vs the live code
# ════════════════════════════════════════════════════════════════════════════
class TestDocCodeConsistency(unittest.TestCase):
    """Every prior version of STRATEGY.md drifted from update.py.

    v3.0 documented MAX_POSITION_PCT = 8% while the code said 10%; meta.strategy read "v2.2"
    against v3.1 code. This test is the reason v4.0 should not repeat that.
    """

    SCALARS = [
        "TARGET_N", "MAX_ORDERS_PER_RUN", "MAX_SELL_VALUE_PCT", "CASH_FLOOR_PCT",
        "MAX_POSITION_PCT", "MAX_SECTOR_PCT", "REBALANCE_MAX_ORDERS", "REBALANCE_MAX_SELL_PCT",
        "CASH_DEPLOY_BAND", "EXIT_RANK_MULTIPLE", "MIN_HOLD_DAYS", "REENTRY_COOLDOWN_DAYS",
        "MAX_BREACH_RUNS",
    ]

    @classmethod
    def setUpClass(cls):
        cls.doc = (REPO / "STRATEGY.md").read_text(encoding="utf-8")

    def _documented(self, name):
        m = re.search(rf"^{name}\s*=\s*([^\s#]+)", self.doc, re.M)
        self.assertIsNotNone(m, f"{name} is not documented in STRATEGY.md §8")
        return m.group(1).strip()

    def test_every_constant_is_documented_and_matches(self):
        for name in self.SCALARS:
            with self.subTest(constant=name):
                self.assertEqual(
                    float(self._documented(name)), float(getattr(update, name)),
                    f"STRATEGY.md §8 disagrees with update.{name}",
                )

    def test_quarterly_months_match(self):
        # The scalar regex stops at the first space; rebuild the full set literal from the doc.
        m = re.search(r"QUARTERLY_MONTHS\s*=\s*(\{[^}]*\})", self.doc)
        self.assertIsNotNone(m, "QUARTERLY_MONTHS is not documented in STRATEGY.md §8")
        # literal_eval, not eval: this parses a file, and a doc should never be able to
        # execute code just because a test reads it.
        self.assertEqual(ast.literal_eval(m.group(1)), update.QUARTERLY_MONTHS)

    def test_strategy_version_is_v4(self):
        self.assertEqual(update.STRATEGY_VERSION, "4.0")
        self.assertIn("v4.0", self.doc.splitlines()[0])


# ════════════════════════════════════════════════════════════════════════════
# G2 — RESET_PORTFOLIO bootstrap
# ════════════════════════════════════════════════════════════════════════════
class TestBootstrap(unittest.TestCase):

    STALE = 9864.11   # generation 1's initial_capital — must never be inherited

    def test_seeds_from_real_account_equity(self):
        state = {"portfolio_value": 100_000.0, "positions": [], "orders": []}
        book = update.bootstrap_fresh_portfolio(state, "TradeQuest Paper 2",
                                                dt=datetime(2026, 8, 18))
        self.assertEqual(book["meta"]["initial_capital"], 100_000.0)
        self.assertEqual(book["summary"]["initial_capital"], 100_000.0)
        self.assertNotEqual(book["meta"]["initial_capital"], self.STALE)
        self.assertEqual(book["meta"]["inception_date"], "2026-08-18")
        self.assertEqual(book["meta"]["account_generation"], 2)
        self.assertEqual(book["meta"]["strategy_version"], "4.0")
        self.assertEqual(book["meta"]["account_name"], "TradeQuest Paper 2")

    def test_book_starts_empty(self):
        state = {"portfolio_value": 50_000.0, "positions": [], "orders": []}
        book = update.bootstrap_fresh_portfolio(state, "acct")
        for key in ("holdings", "trades", "equity_curve", "spy_curve", "benchmark"):
            self.assertEqual(book[key], [], f"{key} must start empty")

    def test_refuses_account_holding_positions(self):
        state = {"portfolio_value": 100_000.0, "positions": [_pos("JBL", -22)], "orders": []}
        with self.assertRaises(update.InvariantBreach) as ctx:
            update.bootstrap_fresh_portfolio(state, "acct")
        self.assertIn("JBL", str(ctx.exception))

    def test_refuses_account_with_order_history(self):
        state = {"portfolio_value": 100_000.0, "positions": [], "orders": [MagicMock()]}
        with self.assertRaises(update.InvariantBreach):
            update.bootstrap_fresh_portfolio(state, "acct")

    def test_refuses_unfunded_account(self):
        with self.assertRaises(update.InvariantBreach):
            update.bootstrap_fresh_portfolio(
                {"portfolio_value": 0.0, "positions": [], "orders": []}, "acct")


# ════════════════════════════════════════════════════════════════════════════
# G3 — DRY_RUN shadow mode
# ════════════════════════════════════════════════════════════════════════════
class TestDryRun(unittest.TestCase):

    def setUp(self):
        self._orig = update.DRY_RUN
        update.DRY_RUN = True

    def tearDown(self):
        update.DRY_RUN = self._orig

    def test_submits_nothing_but_still_reports_the_plan(self):
        """Covers, sells and buys together — none may reach the broker in shadow mode."""
        client = _FakeClient([_pos("JBL", -22, price=344.15, mv=-7571.30),
                              _pos("ROST", 2, price=255.05)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("ROST", 2)], to_buy=[("VLO", 1)],
            pv=100_000, cash=50_000,
            prices={"JBL": 344.15, "ROST": 255.05, "VLO": 308.92},
            to_cover=[("JBL", 22)],
        )
        self.assertEqual(client.submitted, [], "DRY_RUN must submit zero orders")
        actions = sorted(p[0] for p in placed)
        self.assertEqual(actions, ["BUY", "COVER", "SELL"],
                         "the plan must still be recorded for review")

    def test_live_mode_still_submits(self):
        update.DRY_RUN = False
        client = _FakeClient([_pos("ROST", 2, price=255.05)])
        update.alpaca_place_orders(client, to_sell=[("ROST", 2)], to_buy=[],
                                   pv=100_000, cash=50_000, prices={"ROST": 255.05})
        self.assertEqual(len(client.submitted), 1)

    def test_execution_summary_marks_dry_run(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            update.write_execution_summary(
                [("COVER", "JBL", 22)], [], [], 5.0, Path(d), dry_run=True)
            payload = json.loads((Path(d) / "execution_summary.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["orders_placed"][0]["status"], "dry_run")


# ════════════════════════════════════════════════════════════════════════════
# G4 — invariant gate + kill switch
# ════════════════════════════════════════════════════════════════════════════
class TestInvariants(unittest.TestCase):

    def test_short_position_is_a_breach(self):
        state = {"portfolio_value": 9533.72, "deployable_cash": 0.0,
                 "positions": [_pos("JBL", -22, price=344.15, mv=-7571.30)]}
        breaches = update.assert_invariants(state)
        self.assertTrue(any("JBL" in b and "short" in b for b in breaches), breaches)

    def test_clean_book_has_no_breaches(self):
        state = {"portfolio_value": 100_000.0, "deployable_cash": 5_000.0,
                 "positions": [_pos("VLO", 30, price=308.92)]}
        self.assertEqual(update.assert_invariants(state), [])

    def test_oversized_position_is_a_breach(self):
        state = {"portfolio_value": 10_000.0, "deployable_cash": 100.0,
                 "positions": [_pos("TSLA", 10, price=390.0)]}   # 39% of a 10% cap book
        self.assertTrue(any("TSLA" in b for b in update.assert_invariants(state)))

    def test_negative_deployable_cash_is_a_breach(self):
        state = {"portfolio_value": 10_000.0, "deployable_cash": -5.0, "positions": []}
        self.assertTrue(any("deployable" in b for b in update.assert_invariants(state)))

    def test_sector_concentration_is_a_breach(self):
        state = {"portfolio_value": 10_000.0, "deployable_cash": 0.0, "positions": []}
        holdings = [
            {"symbol": "BNY", "shares": 1, "market_value": 3000, "sector": "Financial Services"},
            {"symbol": "BEN", "shares": 1, "market_value": 2000, "sector": "Financial Services"},
        ]
        self.assertTrue(any("Financial Services" in b
                            for b in update.assert_invariants(state, holdings)))

    def test_kill_switch_only_fires_on_persistence(self):
        """A single bad run must not halt trading; a persistent one must."""
        b = ["JBL: short position"]
        self.assertFalse(update.trading_halted(b, 1))
        self.assertFalse(update.trading_halted(b, 2))
        self.assertTrue(update.trading_halted(b, update.MAX_BREACH_RUNS))
        self.assertFalse(update.trading_halted([], 99), "no breach = no halt")

    def test_breach_streak_round_trips_through_execution_summary(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            update.write_execution_summary([], [], [], None, Path(d),
                                           breaches=["JBL: short"], breach_streak=2)
            self.assertEqual(update.read_breach_streak(Path(d)), 2)

    def test_missing_summary_reads_zero_streak(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(update.read_breach_streak(Path(d)), 0)

    def test_reconciliation_detects_divergence(self):
        state = {"positions": [_pos("VLO", 3), _pos("NTAP", 6)]}
        holdings = [{"symbol": "VLO", "shares": 3}, {"symbol": "ROST", "shares": 2}]
        diffs = update.reconcile_positions(state, holdings)
        self.assertTrue(any("NTAP" in d for d in diffs))
        self.assertTrue(any("ROST" in d for d in diffs))
        self.assertFalse(any("VLO" in d for d in diffs), "matching positions must not be flagged")


# ════════════════════════════════════════════════════════════════════════════
# G5 — STRATEGY.md split
# ════════════════════════════════════════════════════════════════════════════
class TestStrategySplit(unittest.TestCase):

    def test_agent_gets_part_one_only(self):
        sliced = agent.load_strategy()
        self.assertIn("§0 — The Ten Directives", sliced)
        self.assertIn("§5 — Position Lifecycle", sliced)
        self.assertIn("§8 — Execution & Hard Limits", sliced)
        self.assertNotIn("PART II — The Human Record", sliced)
        self.assertNotIn("Go-Live Gates", sliced)
        self.assertNotIn("Evolution Log", sliced)

    def test_split_actually_removes_content(self):
        full = (REPO / "STRATEGY.md").read_text(encoding="utf-8")
        self.assertLess(len(agent.load_strategy()), len(full))

    def test_missing_marker_falls_back_to_full_document(self):
        """Degrading to the old behaviour is safe; sending an empty rulebook is not."""
        import tempfile
        orig = agent.STRATEGY_FILE
        try:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "STRATEGY.md"
                p.write_text("# No marker here\n\nRules follow.\n", encoding="utf-8")
                agent.STRATEGY_FILE = p
                self.assertIn("Rules follow.", agent.load_strategy())
        finally:
            agent.STRATEGY_FILE = orig

    def test_preamble_no_longer_contradicts_the_document(self):
        """The v2.2 preamble asserted a blanket non-quarterly BUY block that v3.2 made false."""
        src = (REPO / "bot" / "agent.py").read_text(encoding="utf-8")
        self.assertNotIn("strategy v2.2", src)
        self.assertNotIn("no BUY decisions", src)
        self.assertIn("COVER is", src)

    def test_cover_is_in_the_agent_response_schema(self):
        self.assertIn("COVER", agent.RESPONSE_SCHEMA)


# ════════════════════════════════════════════════════════════════════════════
# G6 — the archive is an executable regression fixture
# ════════════════════════════════════════════════════════════════════════════
@unittest.skipUnless(ARCHIVE.exists(), "archive not present")
class TestArchive(unittest.TestCase):

    def test_referenced_files_exist(self):
        for rel in ("POSTMORTEM.md", "STRATEGY-v3.2.md", "data/portfolio.json",
                    "data/agent_log.json", "data/news.json", "data/enrichment.json",
                    "evidence/round_trips.md", "evidence/timeline.md"):
            with self.subTest(path=rel):
                self.assertTrue((ARCHIVE / rel).exists(), f"missing {rel}")
        self.assertTrue((REPO / "archive" / "README.md").exists())

    def test_failure_is_preserved_not_cleaned(self):
        """The archived book must still show the break. A tidied archive proves nothing."""
        book = json.loads((ARCHIVE / "data" / "portfolio.json").read_text(encoding="utf-8"))
        jbl = next(h for h in book["holdings"] if h["symbol"] == "JBL")
        self.assertEqual(jbl["shares"], -22)

    def test_archived_book_still_replays_to_a_cover(self):
        """Current code, replayed against generation 1's book, must still fix it.

        If a future refactor reopens the deadlock, this fails instead of a quarter.
        """
        book = json.loads((ARCHIVE / "data" / "portfolio.json").read_text(encoding="utf-8"))
        positions = [_pos(h["symbol"], h["shares"], h["current_price"], h["market_value"])
                     for h in book["holdings"]]

        deployable = update.compute_deployable_cash(book["summary"]["cash"], positions)
        self.assertLess(deployable, 1.0, "the $7,566 reported cash was ~$0 deployable")

        state = {"portfolio_value": book["summary"]["portfolio_value"],
                 "deployable_cash": deployable, "positions": positions}
        self.assertTrue(any("JBL" in b for b in update.assert_invariants(state)))

        was_dry, update.DRY_RUN = update.DRY_RUN, False
        try:
            client = _FakeClient(positions)
            placed = update.alpaca_place_orders(
                client, to_sell=[], to_buy=[],
                pv=book["summary"]["portfolio_value"], cash=deployable,
                prices={h["symbol"]: h["current_price"] for h in book["holdings"]},
                to_cover=[("JBL", 22)],
            )
        finally:
            update.DRY_RUN = was_dry
        self.assertIn(("COVER", "JBL", 22), placed)


if __name__ == "__main__":
    unittest.main()
