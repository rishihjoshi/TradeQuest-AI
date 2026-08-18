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
