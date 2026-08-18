"""Unit tests for the v3.1 execution-correctness release.

Covers the fixes from POSTMORTEM.md:
  F1 — long-only clamp (never sell more than held; never open a short; reject price<=0)
  F2 — one-run rebalance budget (apply_risk_limits max_orders / max_sell_pct params)
  D4 — bot/rebalance_trueup.py plan (covers the JBL short; no oversell; sector cap respected)

Stdlib + mocks only — no live alpaca/anthropic packages required.
"""
# pylint: disable=protected-access,unused-argument,missing-class-docstring,missing-function-docstring
# Test-suite patterns, not defects: tests exercise module internals (_fmp_get); stub
# signatures must match the real API even when a test ignores a parameter; and the
# test method name is the documentation.
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Stub the packages that aren't installed in CI (mirrors test_gap_fixes.py).
for _pkg in ("alpaca", "alpaca.trading", "alpaca.trading.client",
             "alpaca.trading.requests", "alpaca.trading.enums", "anthropic"):
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

# The enums/requests modules need the names update.py / rebalance_trueup.py import.
_enums = sys.modules["alpaca.trading.enums"]
for _name in ("OrderSide", "TimeInForce", "QueryOrderStatus"):
    setattr(_enums, _name, MagicMock(name=_name))
_reqs = sys.modules["alpaca.trading.requests"]
for _name in ("MarketOrderRequest", "GetOrdersRequest"):
    setattr(_reqs, _name, MagicMock(name=_name))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
import update            # noqa: E402
import agent             # noqa: E402
import rebalance_trueup  # noqa: E402


def _order(oid, side, qty, price, ts, symbol="AAA"):
    o = MagicMock()
    o.id = oid
    o.filled_qty = qty
    o.filled_avg_price = price
    o.side = MagicMock(); o.side.value = side
    o.filled_at = None
    o.created_at = ts
    o.symbol = symbol
    o.qty = qty
    return o


def _pos(symbol, qty, price=100.0, sector="Technology", mv=None):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    p.current_price = price
    p.avg_entry_price = price
    p.market_value = qty * price if mv is None else mv
    p.unrealized_pl = 0.0
    return p


class _FakeClient:
    """Captures submit_order calls; serves positions + empty open-orders list."""
    def __init__(self, positions):
        self._positions = positions
        self.submitted = []      # list of (side, symbol, qty)

    def get_all_positions(self):
        return self._positions

    def get_orders(self, *_a, **_k):
        return []

    def submit_order(self, req):
        # MarketOrderRequest is a MagicMock — pull the kwargs we passed in.
        kwargs = req.call_args.kwargs if hasattr(req, "call_args") and req.call_args else {}
        self.submitted.append((kwargs.get("side"), kwargs.get("symbol"), kwargs.get("qty")))


# ════════════════════════════════════════════════════════════════════════════
# F1 — Long-only clamp in alpaca_place_orders
# ════════════════════════════════════════════════════════════════════════════
class TestLongOnlyClamp(unittest.TestCase):

    def test_sell_clamped_to_held(self):
        """Requesting to sell more than held must clamp to the held quantity."""
        client = _FakeClient([_pos("AAPL", 5)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("AAPL", 10)], to_buy=[],
            pv=10_000, cash=1_000, prices={"AAPL": 100.0},
        )
        sells = [p for p in placed if p[0] == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0][2], 5, "qty must clamp to the 5 shares held, not 10")

    def test_flat_position_never_sold(self):
        """A symbol not held (or flat) must never produce a SELL — no short."""
        client = _FakeClient([_pos("AAPL", 5)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("JBL", 1)], to_buy=[],
            pv=10_000, cash=1_000, prices={"JBL": 300.0},
        )
        self.assertEqual([p for p in placed if p[0] == "SELL"], [],
                         "JBL is not held → no SELL may be placed")

    def test_short_position_never_extended(self):
        """An already-short position (qty<0) must never be sold further."""
        client = _FakeClient([_pos("JBL", -22, price=320.0)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("JBL", 22)], to_buy=[],
            pv=10_000, cash=6_000, prices={"JBL": 320.0},
        )
        self.assertEqual([p for p in placed if p[0] == "SELL"], [],
                         "short JBL must not be sold deeper")

    def test_zero_price_sell_rejected(self):
        """A non-positive price must block the order (guards the $0 bad-fetch bug)."""
        client = _FakeClient([_pos("AAPL", 5)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("AAPL", 5)], to_buy=[],
            pv=10_000, cash=1_000, prices={"AAPL": 0.0},
        )
        self.assertEqual([p for p in placed if p[0] == "SELL"], [],
                         "price 0 → SELL must be skipped")

    def test_normal_sell_passes(self):
        """A valid SELL within held qty is placed unchanged."""
        client = _FakeClient([_pos("AAPL", 5)])
        placed = update.alpaca_place_orders(
            client, to_sell=[("AAPL", 3)], to_buy=[],
            pv=10_000, cash=1_000, prices={"AAPL": 100.0},
        )
        sells = [p for p in placed if p[0] == "SELL"]
        self.assertEqual(sells, [("SELL", "AAPL", 3)])


# ════════════════════════════════════════════════════════════════════════════
# F2 — One-run rebalance budget in apply_risk_limits
# ════════════════════════════════════════════════════════════════════════════
class TestRebalanceBudget(unittest.TestCase):

    def setUp(self):
        # 8 approved sells, each small-value so the sell-value cap never binds.
        self.syms = [f"S{i}" for i in range(8)]
        self.to_sell = [(s, 1) for s in self.syms]
        self.approvals = {s.upper() for s in self.syms}
        self.prices = {s.upper(): 10.0 for s in self.syms}  # $10 each, pv $100k

    def test_daily_default_caps_at_five(self):
        """Default (daily) run limits to MAX_ORDERS_PER_RUN sells."""
        sells, _ = update.apply_risk_limits(
            self.to_sell, [], pv=100_000, cash=50_000,
            agent_sell_approvals=self.approvals, prices=self.prices,
        )
        self.assertEqual(len(sells), update.MAX_ORDERS_PER_RUN)

    def test_rebalance_budget_allows_full_rotation(self):
        """A rebalance run (wide budget) places all 8 sells in one pass."""
        sells, _ = update.apply_risk_limits(
            self.to_sell, [], pv=100_000, cash=50_000,
            agent_sell_approvals=self.approvals, prices=self.prices,
            max_orders=update.REBALANCE_MAX_ORDERS,
            max_sell_pct=update.REBALANCE_MAX_SELL_PCT,
        )
        self.assertEqual(len(sells), 8, "all 8 approved sells should pass in one rebalance run")


# ════════════════════════════════════════════════════════════════════════════
# D4 — rebalance_trueup.plan_trueup
# ════════════════════════════════════════════════════════════════════════════
class TestTrueUpPlan(unittest.TestCase):

    def _state(self):
        # A book resembling the live Jul-2026 state: a JBL short + several longs.
        positions = [
            _pos("JBL",  -22, price=320.0, sector="Technology"),
            _pos("BNY",    7, price=160.0, sector="Financial Services"),
            _pos("IBKR",  12, price=90.0,  sector="Financial Services"),
            _pos("MS",     4, price=213.0, sector="Financial Services"),
            _pos("NTRS",   4, price=178.0, sector="Financial Services"),
            _pos("MNST",   9, price=94.0,  sector="Consumer Defensive"),
            _pos("ROST",   2, price=235.0, sector="Consumer Cyclical"),
            _pos("ANET",   6, price=174.0, sector="Unknown"),
        ]
        return {"portfolio_value": 10_000.0, "cash": 6_000.0, "positions": positions}

    def _ref(self):
        return {
            "BNY":  {"rank": 8,  "sector": "Financial Services"},
            "IBKR": {"rank": 7,  "sector": "Financial Services"},
            "MS":   {"rank": 5,  "sector": "Financial Services"},
            "NTRS": {"rank": 11, "sector": "Financial Services"},
            "MNST": {"rank": 9,  "sector": "Consumer Defensive"},
            "ROST": {"rank": 6,  "sector": "Consumer Cyclical"},
            "ANET": {"rank": 0,  "sector": "Unknown"},   # missing data → sorts last
            "JBL":  {"rank": 0,  "sector": "Technology"},
        }

    def test_short_is_covered(self):
        orders, _ = rebalance_trueup.plan_trueup(self._state(), self._ref(), target_n=10, cash_target=0.05)
        covers = [o for o in orders if o["symbol"] == "JBL"]
        self.assertEqual(len(covers), 1)
        self.assertEqual(covers[0]["action"], "BUY")
        self.assertEqual(covers[0]["qty"], 22, "must buy back exactly the 22 shorted shares")

    def test_no_sell_exceeds_held(self):
        state = self._state()
        held = {p.symbol: p.qty for p in state["positions"]}
        orders, _ = rebalance_trueup.plan_trueup(state, self._ref(), target_n=10, cash_target=0.05)
        for o in orders:
            if o["action"] == "SELL":
                self.assertLessEqual(o["qty"], held.get(o["symbol"], 0) + 1e-6,
                                     f"{o['symbol']} SELL {o['qty']} exceeds held {held.get(o['symbol'])}")

    def test_sector_cap_respected(self):
        _, summary = rebalance_trueup.plan_trueup(self._state(), self._ref(), target_n=10, cash_target=0.05)
        for sector, pct in summary["sector_projection"].items():
            self.assertLessEqual(pct, update.MAX_SECTOR_PCT * 100 + 0.1,
                                 f"{sector} projected at {pct}% exceeds cap")

    def test_unranked_names_exit_first(self):
        """A missing-data name (rank 0 → ANET) must be exited, not kept."""
        _, summary = rebalance_trueup.plan_trueup(self._state(), self._ref(), target_n=6, cash_target=0.05)
        self.assertNotIn("ANET", summary["kept_symbols"])


# ════════════════════════════════════════════════════════════════════════════
# F7 — realized P&L reconstruction + risk metrics
# ════════════════════════════════════════════════════════════════════════════
class TestRealizedPnL(unittest.TestCase):

    def test_average_cost_realized_pnl(self):
        orders = [
            _order("b1", "buy",  10, 100, "2026-01-01"),
            _order("s1", "sell",  4, 120, "2026-01-05"),
            _order("b2", "buy",  10, 110, "2026-01-06"),
            _order("s2", "sell",  6, 130, "2026-01-10"),
        ]
        r = update.compute_realized_pnl(orders)
        self.assertEqual(r["s1"], (80.0, 20.0))            # (120-100)*4, +20%
        self.assertEqual(r["s2"][0], 142.5)                # basis 106.25 after b2

    def test_sell_without_basis_is_none(self):
        """A SELL whose opening BUYs aren't in the window yields (None, None), not a fake 0."""
        r = update.compute_realized_pnl([_order("s1", "sell", 5, 130, "2026-01-10")])
        self.assertEqual(r["s1"], (None, None))

    def test_trades_carry_pnl(self):
        orders = [
            _order("b1", "buy",  5, 100, "2026-01-01"),
            _order("s1", "sell", 5, 110, "2026-01-05"),
        ]
        trades = update.alpaca_orders_to_trades(orders)
        sell = next(t for t in trades if t["action"] == "SELL")
        self.assertEqual(sell["pnl"], 50.0)
        self.assertEqual(sell["pnl_pct"], 10.0)


class TestRiskMetrics(unittest.TestCase):

    def test_sharpe_and_drawdown(self):
        curve = [{"date": "a", "value": 100}, {"date": "b", "value": 110},
                 {"date": "c", "value": 99}, {"date": "d", "value": 120}]
        m = update.compute_risk_metrics(curve)
        self.assertIn("sharpe_ratio", m)
        self.assertAlmostEqual(m["max_drawdown_pct"], 10.0, places=1)  # 110 -> 99

    def test_short_curve_returns_empty(self):
        self.assertEqual(update.compute_risk_metrics([{"date": "a", "value": 100}]), {})


# ════════════════════════════════════════════════════════════════════════════
# F8 — agent decision normalization (spec conformance)
# ════════════════════════════════════════════════════════════════════════════
class TestNormalizeDecisions(unittest.TestCase):

    def test_day_start_is_flag_only(self):
        out = agent.normalize_decisions(
            [{"action": "SELL", "symbol": "X", "urgency": "next_open", "sell_tier": "tier1"}],
            "day_start")
        self.assertEqual(out[0]["action"], "WATCH")
        self.assertEqual(out[0]["_original_action"], "SELL")

    def test_immediate_urgency_normalized(self):
        out = agent.normalize_decisions(
            [{"action": "SELL", "symbol": "X", "urgency": "immediate"}],
            "day_end", quarterly=True)
        self.assertEqual(out[0]["urgency"], "next_open")
        self.assertEqual(out[0]["sell_tier"], "tier1")

    def test_non_quarterly_buy_blocked(self):
        out = agent.normalize_decisions(
            [{"action": "BUY", "symbol": "Y", "urgency": "next_rebalance"}],
            "monthly", quarterly=False)
        self.assertEqual(out[0]["action"], "HOLD")

    def test_quarterly_buy_preserved(self):
        out = agent.normalize_decisions(
            [{"action": "BUY", "symbol": "Y", "urgency": "next_rebalance"}],
            "monthly", quarterly=True)
        self.assertEqual(out[0]["action"], "BUY")

    def test_sell_tier_inferred(self):
        out = agent.normalize_decisions(
            [{"action": "SELL", "symbol": "Z", "urgency": "next_rebalance"}],
            "day_end", quarterly=True)
        self.assertEqual(out[0]["sell_tier"], "tier2")

    def test_every_sell_has_tier(self):
        out = agent.normalize_decisions(
            [{"action": "SELL", "symbol": "A", "urgency": "next_open"},
             {"action": "SELL", "symbol": "B"}],
            "day_end", quarterly=True)
        for d in out:
            self.assertIn(d["sell_tier"], ("tier1", "tier2"))


if __name__ == "__main__":
    unittest.main()
