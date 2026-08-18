"""Unit tests for the v4.1 cash-deployment release.

The generation-2 inception run deployed only 66% of a $10,000 book and left 34% in cash against
a 5% bull-regime target. No gate caused it. Three mechanical causes did:

  1. Integer share sizing always rounds DOWN, and the error scales with share price. Equal weight
     asked for $950 of VLO at $348.64 and got 2 shares -- $697.
  2. The redeployment carve-out could not repair it: it skipped any name whose remaining gap was
     smaller than one share, which was every name.
  3. The sector cap correctly dropped APA (rank 4) for breaching the Energy cap, but nothing
     backfilled the slot -- so the book ran 9 of 10 names and could not exceed 90% invested.

Covered here: fractional sizing, the float-safe clamp it requires, the residual sweep, and slot
fill. The clamp tests matter most -- that is the code the Jul-2026 post-mortem was about, and
moving it from int to float must not weaken "never sell more than held".
"""
# pylint: disable=protected-access,unused-argument,missing-class-docstring,missing-function-docstring
import json
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

LIVE_BOOK = REPO / "data" / "portfolio.json"


def _pos(symbol, qty, price=100.0):
    p = MagicMock()
    p.symbol, p.qty = symbol, qty
    p.current_price = p.avg_entry_price = price
    p.market_value = qty * price
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
# Fractional sizing
# ════════════════════════════════════════════════════════════════════════════
class TestFractionalSizing(unittest.TestCase):

    def test_fractional_hits_the_dollar_target(self):
        qty = update.size_shares(950.0, 348.64, fractional=True)
        self.assertAlmostEqual(qty * 348.64, 950.0, places=2)

    def test_integer_sizing_under_deploys(self):
        """The exact live VLO case: $950 requested, $697 deployed."""
        qty = update.size_shares(950.0, 348.64, fractional=False)
        self.assertEqual(qty, 2.0)
        self.assertLess(qty * 348.64, 700)

    def test_sizing_never_spends_more_than_its_budget(self):
        for dollars, price in ((950, 348.64), (100, 33.3), (5000, 7.77), (1, 0.99)):
            with self.subTest(dollars=dollars, price=price):
                qty = update.size_shares(dollars, price, fractional=True)
                self.assertLessEqual(qty * price, dollars + 1e-6)

    def test_dust_orders_are_rejected(self):
        self.assertEqual(update.size_shares(0.40, 348.64), 0.0)

    def test_non_positive_inputs_return_zero(self):
        self.assertEqual(update.size_shares(100, 0), 0.0)
        self.assertEqual(update.size_shares(0, 100), 0.0)
        self.assertEqual(update.size_shares(-50, 100), 0.0)


# ════════════════════════════════════════════════════════════════════════════
# The clamp — fractional must not weaken the long-only invariant
# ════════════════════════════════════════════════════════════════════════════
class TestFractionalSafety(unittest.TestCase):

    def test_full_exit_of_a_fractional_position_sells_everything(self):
        """int() truncation would strand 0.7278 shares no rule could ever reach."""
        client = _FakeClient([_pos("VLO", 2.7278, price=348.64)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("VLO", 2.7278)], to_buy=[], pv=10_000, cash=5_000,
            prices={"VLO": 348.64})
        self.assertAlmostEqual([p for p in placed if p[0] == "SELL"][0][2], 2.7278, places=4)

    def test_oversell_is_still_clamped_to_held(self):
        client = _FakeClient([_pos("VLO", 2.5, price=348.64)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("VLO", 99)], to_buy=[], pv=10_000, cash=5_000,
            prices={"VLO": 348.64})
        self.assertAlmostEqual([p for p in placed if p[0] == "SELL"][0][2], 2.5, places=4)

    def test_a_sell_still_cannot_open_a_short(self):
        client = _FakeClient([_pos("VLO", 2.5, price=348.64)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("JBL", 1.5)], to_buy=[], pv=10_000, cash=5_000,
            prices={"JBL": 345.0})
        self.assertEqual([p for p in placed if p[0] == "SELL"], [])

    def test_an_existing_short_is_still_never_extended(self):
        client = _FakeClient([_pos("JBL", -22, price=344.15)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("JBL", 1)], to_buy=[], pv=9_533, cash=0.0,
            prices={"JBL": 344.15})
        self.assertEqual([p for p in placed if p[0] == "SELL"], [])

    def test_cover_still_closes_the_whole_short(self):
        client = _FakeClient([_pos("JBL", -22, price=344.15)])
        placed = update.alpaca_place_orders(
            client, to_sell=[], to_buy=[], pv=9_533, cash=0.0,
            prices={"JBL": 344.15}, to_cover=[("JBL", 22)])
        self.assertEqual([p for p in placed if p[0] == "COVER"][0][2], 22.0)

    def test_fractional_buy_is_not_forced_up_to_a_whole_share(self):
        """max(1, int(shares)) turned a $250 top-up into a $348 share."""
        client = _FakeClient([])
        placed = update.alpaca_place_orders(
            client, to_sell=[], to_buy=[("VLO", 0.7278)], pv=10_000, cash=5_000,
            prices={"VLO": 348.64})
        self.assertAlmostEqual([p for p in placed if p[0] == "BUY"][0][2], 0.7278, places=4)

    def test_a_zero_quantity_buy_is_skipped(self):
        client = _FakeClient([])
        placed = update.alpaca_place_orders(
            client, to_sell=[], to_buy=[("VLO", 0.0)], pv=10_000, cash=5_000,
            prices={"VLO": 348.64})
        self.assertEqual(placed, [])


# ════════════════════════════════════════════════════════════════════════════
# Residual sweep
# ════════════════════════════════════════════════════════════════════════════
class TestResidualSweep(unittest.TestCase):

    @staticmethod
    def _h(sym, shares, price, mv, sector="Energy"):
        return {"symbol": sym, "shares": shares, "current_price": price,
                "market_value": mv, "sector": sector}

    def test_sweep_respects_the_position_cap(self):
        pv = 9_995.91
        holdings = [self._h("VLO", 2, 348.64, 697.28)]
        sweep = update.plan_residual_sweep(holdings, {"VLO"}, 3_415.42, pv, {"VLO": 348.64})
        qty = dict(sweep)["VLO"]
        self.assertLessEqual(697.28 + qty * 348.64, pv * update.MAX_POSITION_PCT + 0.01)

    def test_sweep_respects_the_cash_floor(self):
        pv = 9_995.91
        holdings = [self._h("VLO", 2, 348.64, 697.28)]
        sweep = update.plan_residual_sweep(holdings, {"VLO"}, 3_415.42, pv, {"VLO": 348.64})
        spent = sum(q * 348.64 for _, q in sweep)
        self.assertLessEqual(spent, 3_415.42 - pv * update.CASH_FLOOR_PCT + 0.01)

    def test_no_sweep_when_cash_is_already_at_the_floor(self):
        pv = 9_995.91
        holdings = [self._h("VLO", 2, 348.64, 697.28)]
        self.assertEqual(
            update.plan_residual_sweep(holdings, {"VLO"}, pv * update.CASH_FLOOR_PCT, pv,
                                       {"VLO": 348.64}), [])

    def test_names_outside_the_target_are_not_swept_into(self):
        holdings = [self._h("OLD", 2, 100, 200)]
        self.assertEqual(update.plan_residual_sweep(
            holdings, {"VLO"}, 5_000, 10_000, {"OLD": 100}), [])

    def test_shorts_are_never_swept_into(self):
        holdings = [self._h("JBL", -22, 344.15, -7_571)]
        self.assertEqual(update.plan_residual_sweep(
            holdings, {"JBL"}, 5_000, 10_000, {"JBL": 344.15}), [])

    def test_best_ranked_names_are_filled_first(self):
        holdings = [self._h("A", 1, 100, 100), self._h("B", 1, 100, 100)]
        sweep = update.plan_residual_sweep(holdings, {"A", "B"}, 4_000, 10_000,
                                           {"A": 100, "B": 100}, rank_of={"B": 1, "A": 9})
        self.assertEqual(sweep[0][0], "B")

    def test_a_position_already_at_cap_gets_nothing(self):
        pv = 10_000.0
        holdings = [self._h("VLO", 3, 333.33, pv * update.MAX_POSITION_PCT)]
        self.assertEqual(update.plan_residual_sweep(
            holdings, {"VLO"}, 5_000, pv, {"VLO": 333.33}), [])


# ════════════════════════════════════════════════════════════════════════════
# Slot fill
# ════════════════════════════════════════════════════════════════════════════
class TestSlotFill(unittest.TestCase):

    @staticmethod
    def _screened(*syms):
        return [{"symbol": s, "momentum_rank": i + 1} for i, s in enumerate(syms)]

    @staticmethod
    def _funds(**kw):
        return {s: {"current_price": p, "sector": sec, "ma_50d": 100.0}
                for s, (p, sec) in kw.items()}

    def test_an_empty_slot_is_filled(self):
        holdings = [{"symbol": "VLO", "shares": 2, "market_value": 700, "sector": "Energy"}]
        fills = update.plan_slot_fill(
            holdings, self._screened("VLO", "ROST"),
            self._funds(VLO=(348.64, "Energy"), ROST=(237.99, "Consumer Cyclical")),
            9_000.0, 10_000.0, [], target_n=2)
        self.assertEqual([s for s, _ in fills], ["ROST"])

    def test_a_full_book_gets_no_fill(self):
        holdings = [{"symbol": "VLO", "shares": 2, "market_value": 700, "sector": "Energy"}]
        self.assertEqual(update.plan_slot_fill(
            holdings, self._screened("VLO", "ROST"),
            self._funds(VLO=(348.64, "Energy"), ROST=(237.99, "Consumer Cyclical")),
            9_000.0, 10_000.0, [], target_n=1), [])

    def test_a_held_name_is_never_re_added(self):
        holdings = [{"symbol": "VLO", "shares": 2, "market_value": 700, "sector": "Energy"}]
        fills = update.plan_slot_fill(
            holdings, self._screened("VLO"), self._funds(VLO=(348.64, "Energy")),
            9_000.0, 10_000.0, [], target_n=5)
        self.assertEqual(fills, [])

    def test_the_sector_cap_still_blocks_a_fill(self):
        holdings = [{"symbol": "VLO", "shares": 8, "market_value": 2_900, "sector": "Energy"}]
        self.assertEqual(update.plan_slot_fill(
            holdings, self._screened("VLO", "MPC"),
            self._funds(VLO=(348.64, "Energy"), MPC=(364.48, "Energy")),
            7_000.0, 10_000.0, [], target_n=2), [],
            "filling MPC would push Energy past the 30% cap")

    def test_missing_data_blocks_a_fill(self):
        """Directive 10: never enter on data we could not evaluate."""
        funds = {"APH": {"current_price": 155.0, "sector": "Unknown", "ma_50d": None}}
        self.assertEqual(update.plan_slot_fill(
            [], self._screened("APH"), funds, 9_000.0, 10_000.0, [], target_n=1), [])

    def test_a_null_momentum_rank_blocks_a_fill(self):
        funds = {"SPG": {"current_price": 218.94, "sector": "Real Estate", "ma_50d": 210.0}}
        screened = [{"symbol": "SPG", "momentum_rank": 0}]
        self.assertEqual(update.plan_slot_fill(
            [], screened, funds, 9_000.0, 10_000.0, [], target_n=1), [])

    def test_the_reentry_cooldown_blocks_a_fill(self):
        trades = [{"action": "SELL", "symbol": "ROST",
                   "date": datetime.now().strftime("%Y-%m-%d")}]
        self.assertEqual(update.plan_slot_fill(
            [], self._screened("ROST"), self._funds(ROST=(237.99, "Consumer Cyclical")),
            9_000.0, 10_000.0, trades, target_n=1), [])

    def test_no_fill_below_the_cash_floor(self):
        self.assertEqual(update.plan_slot_fill(
            [], self._screened("ROST"), self._funds(ROST=(237.99, "Consumer Cyclical")),
            10_000.0 * update.CASH_FLOOR_PCT, 10_000.0, [], target_n=1), [])

    def test_fills_never_exceed_the_open_slot_count(self):
        holdings = [{"symbol": "VLO", "shares": 2, "market_value": 700, "sector": "Energy"}]
        fills = update.plan_slot_fill(
            holdings, self._screened("VLO", "ROST", "CF", "NUE"),
            self._funds(VLO=(348.64, "Energy"), ROST=(237.99, "Consumer Cyclical"),
                        CF=(120.06, "Basic Materials"), NUE=(265.18, "Industrials")),
            9_000.0, 10_000.0, [], target_n=3)
        self.assertEqual(len(fills), 2)


# ════════════════════════════════════════════════════════════════════════════
# Replay against the ACTUAL generation-2 book
# ════════════════════════════════════════════════════════════════════════════
@unittest.skipUnless(LIVE_BOOK.exists(), "no live book")
class TestLiveBookReachesTarget(unittest.TestCase):
    """9 positions, 34.17% cash. Prove the v4.1 planners close the gap to the 5% target."""

    @classmethod
    def setUpClass(cls):
        cls.book = json.loads(LIVE_BOOK.read_text(encoding="utf-8"))
        cls.summary = cls.book["summary"]
        if cls.summary.get("cash_pct", 0) < 15:
            raise unittest.SkipTest("book has already been re-deployed")

    def _plan_spend(self):
        pv, cash = self.summary["portfolio_value"], self.summary["cash"]
        per = min(pv * (1 - update.CASH_FLOOR_PCT) / update.TARGET_N,
                  pv * update.MAX_POSITION_PCT)
        spend = 0.0
        for h in self.book["holdings"]:
            gap = per - h["market_value"]
            if gap > 0:
                spend += update.size_shares(gap, h["current_price"]) * h["current_price"]
        open_slots = update.TARGET_N - len(self.book["holdings"])
        headroom = cash - spend - pv * update.CASH_FLOOR_PCT
        spend += max(0.0, min(per * open_slots, headroom))
        return pv, cash, spend

    def test_topups_plus_slot_fill_reach_the_target(self):
        pv, cash, spend = self._plan_spend()
        remaining_pct = (cash - spend) / pv * 100
        self.assertLess(remaining_pct, 8.0,
                        f"cash should fall from {self.summary['cash_pct']}% "
                        f"to under 8%, got {remaining_pct:.1f}%")

    def test_the_plan_never_breaches_the_cash_floor(self):
        pv, cash, spend = self._plan_spend()
        self.assertGreaterEqual(cash - spend, pv * update.CASH_FLOOR_PCT - 0.01)

    def test_no_position_would_exceed_the_cap(self):
        pv = self.summary["portfolio_value"]
        per = min(pv * (1 - update.CASH_FLOOR_PCT) / update.TARGET_N,
                  pv * update.MAX_POSITION_PCT)
        for h in self.book["holdings"]:
            with self.subTest(symbol=h["symbol"]):
                self.assertLessEqual(max(per, h["market_value"]),
                                     pv * update.MAX_POSITION_PCT + 0.01)

    def test_integer_sizing_would_not_have_reached_the_target(self):
        """Confirms fractional is doing the work, not the sweep alone."""
        pv, cash = self.summary["portfolio_value"], self.summary["cash"]
        per = min(pv * (1 - update.CASH_FLOOR_PCT) / update.TARGET_N,
                  pv * update.MAX_POSITION_PCT)
        spend = 0.0
        for h in self.book["holdings"]:
            gap = per - h["market_value"]
            if gap > 0:
                spend += update.size_shares(gap, h["current_price"],
                                            fractional=False) * h["current_price"]
        self.assertGreater((cash - spend) / pv * 100, 15.0,
                           "integer sizing alone should still leave the book well above target")


if __name__ == "__main__":
    unittest.main()
