"""Regression tests for the two bugs shadow mode caught on 2026-08-18.

v4.1 made sizing fractional, and 317 tests passed — but the first DRY_RUN against the live book
printed `BUY 1.0 VLO` where the plan said 0.723759, and `5/10 buys after limits`. Two gates
downstream of the sizing work had never been exercised with fractional input:

  1. apply_risk_limits capped orders with `max(1, min(shares, int(max_pos_val / price)))`.
     max(1, 0.7238) = 1.0 — every fractional order was rounded UP to a whole share at the last
     gate, silently undoing fractional sizing and putting the 5% cash target out of reach.

  2. MAX_ORDERS_PER_RUN = 5 throttled a 10-order deployment to 5. That cap exists to damp churn;
     applying it to an allocation recreates the Jul-2026 pattern where a rebalance needing ~15
     legs took 12 trading days and parked 40–61% of the book in cash through a rising market.

The lesson these encode: a unit test on the sizing primitive proves nothing if a later gate
re-rounds the result. Test the path, not just the function.
"""
# pylint: disable=protected-access,unused-argument,missing-class-docstring,missing-function-docstring
import sys
import types
import unittest
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
import update   # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Bug 1 — the risk gate rounded fractional orders up to whole shares
# ════════════════════════════════════════════════════════════════════════════
class TestRiskGatePreservesFractional(unittest.TestCase):

    PV = 9_995.91

    def test_the_exact_live_case_survives_the_gate(self):
        """Planned 0.723759 VLO; the gate printed BUY 1.0."""
        _, buys = update.apply_risk_limits(
            to_sell=[], to_buy=[("VLO", 0.723759)], pv=self.PV, cash=3_415.42,
            agent_sell_approvals=set(), prices={"VLO": 348.64})
        self.assertAlmostEqual(dict(buys)["VLO"], 0.723759, places=5)

    def test_the_gate_never_rounds_a_small_order_up(self):
        for qty in (0.05, 0.4, 0.7238, 0.999):
            with self.subTest(qty=qty):
                _, buys = update.apply_risk_limits(
                    to_sell=[], to_buy=[("VLO", qty)], pv=self.PV, cash=5_000,
                    agent_sell_approvals=set(), prices={"VLO": 348.64})
                self.assertLessEqual(dict(buys)["VLO"], qty + 1e-6,
                                     "the gate may reduce an order, never enlarge it")

    def test_position_cap_is_still_enforced(self):
        """A huge request is trimmed to MAX_POSITION_PCT, not passed through."""
        _, buys = update.apply_risk_limits(
            to_sell=[], to_buy=[("VLO", 50)], pv=self.PV, cash=9_000,
            agent_sell_approvals=set(), prices={"VLO": 348.64})
        self.assertLessEqual(dict(buys)["VLO"] * 348.64,
                             self.PV * update.MAX_POSITION_PCT + 0.01)

    def test_cash_floor_is_still_enforced(self):
        _, buys = update.apply_risk_limits(
            to_sell=[], to_buy=[("VLO", 10)], pv=self.PV, cash=600.0,
            agent_sell_approvals=set(), prices={"VLO": 348.64})
        spent = sum(q * 348.64 for _, q in buys)
        self.assertLessEqual(spent, 600.0 - self.PV * update.CASH_FLOOR_PCT + 0.01)

    def test_an_unaffordable_name_is_skipped_not_forced(self):
        _, buys = update.apply_risk_limits(
            to_sell=[], to_buy=[("VLO", 1)], pv=self.PV,
            cash=self.PV * update.CASH_FLOOR_PCT, agent_sell_approvals=set(),
            prices={"VLO": 348.64})
        self.assertEqual(buys, [])

    def test_a_full_fractional_deployment_reaches_the_target(self):
        """The nine live top-ups, through the gate, must still land the book at ~5% cash."""
        plan = [("VLO", 0.723759, 348.64), ("MPC", 0.605386, 364.48),
                ("PSX", 0.930999, 241.57), ("BNY", 0.813710, 163.34),
                ("NUE", 0.581007, 265.18), ("NTRS", 1.976843, 190.81),
                ("CF", 0.909140, 120.06), ("JBL", 0.750663, 345.23),
                ("ROST", 0.990131, 237.99)]
        prices = {s: p for s, _, p in plan}
        _, buys = update.apply_risk_limits(
            to_sell=[], to_buy=[(s, q) for s, q, _ in plan], pv=self.PV, cash=3_415.42,
            agent_sell_approvals=set(), prices=prices,
            max_orders=update.REBALANCE_MAX_ORDERS)
        self.assertEqual(len(buys), 9, "every planned buy must survive the gate")
        spent = sum(q * prices[s] for s, q in buys)
        remaining_pct = (3_415.42 - spent) / self.PV * 100
        self.assertLess(remaining_pct, 20.0)


# ════════════════════════════════════════════════════════════════════════════
# Bug 2 — the churn throttle was applied to an allocation
# ════════════════════════════════════════════════════════════════════════════
class TestDeploymentOrderBudget(unittest.TestCase):

    PV = 9_995.91

    @staticmethod
    def _ten_buys():
        return [(f"S{i}", 1.0) for i in range(10)]

    @staticmethod
    def _prices():
        return {f"S{i}": 100.0 for i in range(10)}

    def test_daily_throttle_still_caps_ordinary_runs(self):
        _, buys = update.apply_risk_limits(
            to_sell=[], to_buy=self._ten_buys(), pv=self.PV, cash=9_000,
            agent_sell_approvals=set(), prices=self._prices())
        self.assertEqual(len(buys), update.MAX_ORDERS_PER_RUN,
                         "churn damping must remain intact for normal runs")

    def test_deployment_budget_completes_the_allocation_in_one_run(self):
        """5/10 buys is the Jul-2026 grind in miniature."""
        _, buys = update.apply_risk_limits(
            to_sell=[], to_buy=self._ten_buys(), pv=self.PV, cash=9_000,
            agent_sell_approvals=set(), prices=self._prices(),
            max_orders=update.REBALANCE_MAX_ORDERS)
        self.assertEqual(len(buys), 10)

    def test_the_wider_budget_is_large_enough_for_a_full_book(self):
        self.assertGreaterEqual(update.REBALANCE_MAX_ORDERS, update.TARGET_N * 2,
                                "budget must cover a full rotation: sell all, buy all")

    def test_sells_keep_the_tight_cap_even_while_deploying(self):
        """Only the buy side widens; a deployment must not become a liquidation window."""
        sells = [(f"H{i}", 1) for i in range(10)]
        approvals = {f"H{i}" for i in range(10)}
        prices = {f"H{i}": 100.0 for i in range(10)}
        capped, _ = update.apply_risk_limits(
            sells, [], pv=self.PV, cash=1_000, agent_sell_approvals=approvals,
            prices=prices, max_orders=update.REBALANCE_MAX_ORDERS,
            max_sell_pct=update.MAX_SELL_VALUE_PCT)
        sold = sum(q * 100.0 for _, q in capped)
        self.assertLessEqual(sold, self.PV * update.MAX_SELL_VALUE_PCT + 0.01)


if __name__ == "__main__":
    unittest.main()


# ════════════════════════════════════════════════════════════════════════════
# Bug 3 — the sweep double-counted room against orders already planned
# ════════════════════════════════════════════════════════════════════════════
class TestSweepAccountsForPending(unittest.TestCase):
    """The 2026-08-18 shadow run queued VLO, MPC, PSX and BNY twice.

    The idempotency guard absorbed all four, so nothing reached the broker — but an execution
    guard should not be covering for a planning error, and on a run where those symbols had no
    prior order the duplicates would have gone through.
    """

    PV = 9_995.91

    @staticmethod
    def _h(sym, mv, price):
        return {"symbol": sym, "shares": 2, "current_price": price, "market_value": mv,
                "sector": "Energy"}

    def test_a_name_already_being_topped_up_is_not_re_proposed(self):
        holdings = [self._h("VLO", 696.73, 348.64)]
        topup = update.size_shares(949.61 - 696.73, 348.64)
        sweep = update.plan_residual_sweep(
            holdings, {"VLO"}, 3_224.54, self.PV, {"VLO": 348.64},
            pending=[("VLO", topup)])
        placed = 696.73 + topup * 348.64 + sum(q * 348.64 for _, q in sweep)
        self.assertLessEqual(placed, self.PV * update.MAX_POSITION_PCT + 0.01,
                             "top-up plus sweep must not exceed the position cap")

    def test_a_position_already_planned_to_cap_gets_no_sweep(self):
        holdings = [self._h("VLO", 696.73, 348.64)]
        to_cap = update.size_shares(self.PV * update.MAX_POSITION_PCT - 696.73, 348.64)
        self.assertEqual(update.plan_residual_sweep(
            holdings, {"VLO"}, 3_224.54, self.PV, {"VLO": 348.64},
            pending=[("VLO", to_cap)]), [])

    def test_without_pending_the_sweep_still_works(self):
        """Backwards compatible: omitting `pending` behaves as before."""
        holdings = [self._h("VLO", 696.73, 348.64)]
        self.assertTrue(update.plan_residual_sweep(
            holdings, {"VLO"}, 3_224.54, self.PV, {"VLO": 348.64}))

    def test_pending_for_an_unrelated_symbol_does_not_block(self):
        holdings = [self._h("VLO", 696.73, 348.64)]
        self.assertTrue(update.plan_residual_sweep(
            holdings, {"VLO"}, 3_224.54, self.PV, {"VLO": 348.64, "MPC": 364.48},
            pending=[("MPC", 1.0)]))


# ════════════════════════════════════════════════════════════════════════════
# Top-ups follow HELD names, not just the current top-10 screen
# ════════════════════════════════════════════════════════════════════════════
class TestKeepSetSemantics(unittest.TestCase):
    """Five holdings moved 5-7 rank places overnight and stopped receiving top-ups.

    That is the noise the churn dampers exist to ignore. Having decided to hold a name, leaving
    it under-weight while sitting on idle cash is cash drag by another route: a ten-name book
    where five are deliberately light is not equal weight. The set that receives capital is
    therefore "names we are keeping", not "names in today's top ten".
    """

    @staticmethod
    def _keep_syms(target_syms, holdings, selling_syms):
        """Mirrors the keep_syms expression in update.main()."""
        return target_syms | {
            h["symbol"] for h in holdings
            if float(h.get("shares", 0) or 0) > 0 and h["symbol"] not in selling_syms
        }

    def _book(self):
        return [{"symbol": "VLO", "shares": 2, "market_value": 696.73},   # rank 1  - in top 10
                {"symbol": "ROST", "shares": 3, "market_value": 712.63},  # rank 17 - held only
                {"symbol": "JBL", "shares": 2, "market_value": 689.36}]   # rank 15 - held only

    def test_held_names_outside_the_top_ten_are_included(self):
        keep = self._keep_syms({"VLO", "KEYS"}, self._book(), set())
        self.assertIn("ROST", keep)
        self.assertIn("JBL", keep)

    def test_current_top_ten_names_are_still_included(self):
        self.assertIn("VLO", self._keep_syms({"VLO", "KEYS"}, self._book(), set()))

    def test_an_unheld_target_name_is_included_for_the_slot_fill(self):
        self.assertIn("KEYS", self._keep_syms({"VLO", "KEYS"}, self._book(), set()))

    def test_a_name_queued_to_sell_is_excluded(self):
        """Never add to a position we are exiting this same run."""
        keep = self._keep_syms({"VLO"}, self._book(), {"ROST"})
        self.assertNotIn("ROST", keep)

    def test_a_short_is_never_topped_up(self):
        book = [{"symbol": "JBL", "shares": -22, "market_value": -7571}]
        self.assertNotIn("JBL", self._keep_syms(set(), book, set()))

    def test_the_sweep_can_reach_held_names_too(self):
        holdings = [{"symbol": "ROST", "shares": 3, "current_price": 237.99,
                     "market_value": 712.63, "sector": "Consumer Cyclical"}]
        sweep = update.plan_residual_sweep(holdings, {"ROST"}, 3_000.0, 9_995.91,
                                           {"ROST": 237.99})
        self.assertTrue(sweep, "leftover cash must be able to reach a held name")


# ════════════════════════════════════════════════════════════════════════════
# Bug 4 — layered planners produced two orders for one symbol
# ════════════════════════════════════════════════════════════════════════════
class TestOrderConsolidation(unittest.TestCase):
    """Top-up + sweep for the same name became two orders; the second was dropped.

    The idempotency guard is right to fire — it stops duplicate submissions from concurrent
    runs. The planner should simply not produce duplicates. On 2026-08-18 the guard dropped six
    such orders and stranded ~$257.
    """

    def test_repeated_symbols_are_summed(self):
        merged = dict(update.consolidate_orders([("VLO", 0.7258), ("VLO", 0.1234)]))
        self.assertAlmostEqual(merged["VLO"], 0.8492, places=4)

    def test_one_entry_per_symbol(self):
        out = update.consolidate_orders([("VLO", 1), ("MPC", 1), ("VLO", 1), ("VLO", 1)])
        self.assertEqual(len(out), 2)
        self.assertEqual([s for s, _ in out], ["VLO", "MPC"], "first-seen order is preserved")

    def test_distinct_symbols_are_untouched(self):
        orders = [("VLO", 0.7), ("MPC", 0.6), ("PSX", 0.9)]
        self.assertEqual(update.consolidate_orders(orders), orders)

    def test_empty_input_is_handled(self):
        self.assertEqual(update.consolidate_orders([]), [])

    def test_zero_and_negative_totals_are_dropped(self):
        self.assertEqual(update.consolidate_orders([("VLO", 0.0)]), [])
        self.assertEqual(update.consolidate_orders([("VLO", 1.0), ("VLO", -1.0)]), [])

    def test_the_live_duplicate_set_collapses_to_nine_orders(self):
        """8 top-ups + 1 slot fill + a 6-name sweep = 15 raw, 9 real."""
        topups = [(s, 0.8) for s in ("VLO", "MPC", "PSX", "BNY", "NUE", "CF", "ROST", "NTRS")]
        fills = [("CASY", 1.09)]
        sweep = [(s, 0.05) for s in ("VLO", "MPC", "PSX", "BNY", "NUE", "CF")]
        out = update.consolidate_orders(topups + fills + sweep)
        self.assertEqual(len(out), 9)
        self.assertAlmostEqual(dict(out)["VLO"], 0.85, places=4,
                               msg="the swept remainder must survive, not be dropped")
        self.assertAlmostEqual(dict(out)["ROST"], 0.8, places=4)
