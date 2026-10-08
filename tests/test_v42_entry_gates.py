"""Unit tests for the v4.2 dust-deadlock and entry-gate release.

Generation 2 opened on 2026-08-18 and by 2026-09-01 sat at 44.28% cash against a 5% bull-regime
target, with 9 realised trades, 0 winners and -$276.05. The cause was a closed loop:

  1. The sell PLANNER truncated quantities with int(). Exiting 5.020361 shares sold 5.0 and left
     0.020361 behind. The execution clamp had been moved to float in v4.1 for exactly this reason;
     the planner feeding it was missed, so the fix never took effect.
  2. int(0.0865) == 0 then tripped the planner's "held <= 0: continue" guard, so the residue was
     unreachable by every sell rule -- flagged "Tier 1 SELL, unresolved across 3+ runs" forever.
  3. plan_slot_fill counted "shares > 0" as an occupied slot, so three stubs worth $60 made a
     7-name book report "10 of 10" and return no fills, stranding the cash.
  4. No buy path enforced the STRATEGY section 3 trend gate, and only one of five enforced the
     data-integrity gate. On 08-31 the redeployment bought KEYS ($322.03 vs a $329.87 MA), ROST
     ($228.13 vs $234.26) and JBL ($303.55 vs $334.08); Rule A sold all three the next morning.

Every test below fails on the pre-v4.2 code.
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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bot"))
import update   # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# The dust deadlock
# ════════════════════════════════════════════════════════════════════════════
class TestSlotMateriality(unittest.TestCase):
    """A fractional residue is a position to the broker and nothing to the strategy."""

    TARGET_PER = 944.19   # the live equal-weight target on 2026-09-01

    def test_dust_does_not_occupy_a_slot(self):
        for mv in (25.87, 8.00, 26.23):        # JBL, NTRS, ROST
            with self.subTest(market_value=mv):
                self.assertFalse(update.is_material(mv, self.TARGET_PER))

    def test_a_real_position_occupies_a_slot(self):
        for mv in (944.94, 1080.28, 291.98, 185.75):   # BNY, CF, KEYS, NUE
            with self.subTest(market_value=mv):
                self.assertTrue(update.is_material(mv, self.TARGET_PER))

    def test_a_halved_position_still_occupies_its_slot(self):
        """Materiality must not become a back-door exit for a name merely down a lot."""
        self.assertTrue(update.is_material(self.TARGET_PER * 0.5, self.TARGET_PER))

    def test_the_live_book_reports_three_open_slots(self):
        """7 real names + 3 stubs read as 10 of 10, which returned [] from plan_slot_fill."""
        book = [("BNY", 944.94), ("CF", 1080.28), ("JBL", 25.87), ("KEYS", 291.98),
                ("MPC", 1000.02), ("NTRS", 8.00), ("NUE", 185.75), ("PSX", 985.03),
                ("ROST", 26.23), ("VLO", 989.58)]
        occupied = sum(1 for _s, mv in book if update.is_material(mv, self.TARGET_PER))
        self.assertEqual(occupied, 7)
        self.assertEqual(update.TARGET_N - occupied, 3)


class TestSlotFillSeesThroughDust(unittest.TestCase):
    """plan_slot_fill returned [] on every run because dust filled its slots."""

    @staticmethod
    def _dust_book():
        return [{"symbol": "MPC", "shares": 2.61, "market_value": 1000.02, "sector": "Energy"},
                {"symbol": "JBL", "shares": 0.086496, "market_value": 25.87,
                 "sector": "Technology"},
                {"symbol": "NTRS", "shares": 0.043807, "market_value": 8.00,
                 "sector": "Financial Services"}]

    def test_dust_frees_the_slot_it_was_holding(self):
        funds = {"NUE": {"current_price": 251.92, "sector": "Basic Materials", "ma_50d": 247.25}}
        fills = update.plan_slot_fill(
            self._dust_book(), [{"symbol": "NUE", "momentum_rank": 1}], funds,
            4_401.20, 9_938.88, [], target_n=3)
        self.assertEqual([s for s, _ in fills], ["NUE"],
                         "two stubs were occupying slots a real name should fill")

    def test_a_book_of_real_positions_still_reports_full(self):
        real = [{"symbol": "MPC", "shares": 2.61, "market_value": 1000.02, "sector": "Energy"},
                {"symbol": "BNY", "shares": 5.88, "market_value": 944.94,
                 "sector": "Financial Services"}]
        funds = {"NUE": {"current_price": 251.92, "sector": "Basic Materials", "ma_50d": 247.25}}
        self.assertEqual(update.plan_slot_fill(
            real, [{"symbol": "NUE", "momentum_rank": 1}], funds,
            4_401.20, 9_938.88, [], target_n=2), [],
            "a genuinely full book must not be topped past target_n")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY section 3 entry gates
# ════════════════════════════════════════════════════════════════════════════
class TestEntryGate(unittest.TestCase):

    def test_a_name_below_its_50_day_ma_is_refused(self):
        """The three 2026-08-31 buys, each sold by Rule A the next morning."""
        for sym, price, ma in (("KEYS", 322.03, 329.87),
                               ("ROST", 228.13, 234.26),
                               ("JBL",  303.55, 334.08)):
            with self.subTest(symbol=sym):
                ok, why = update.is_eligible_entry(price, "Technology", 5, ma)
                self.assertFalse(ok, f"{sym} was below its 50-day MA at purchase")
                self.assertIn("50-day MA", why)

    def test_a_name_above_its_50_day_ma_is_allowed(self):
        ok, why = update.is_eligible_entry(383.12, "Energy", 4, 308.35)   # MPC
        self.assertTrue(ok, why)

    def test_price_exactly_at_the_ma_is_refused(self):
        self.assertFalse(update.is_eligible_entry(200.0, "Energy", 4, 200.0)[0])

    def test_unknown_sector_is_refused(self):
        """CF: the largest position in the book, entered with no evaluable data at all."""
        ok, why = update.is_eligible_entry(136.80, "Unknown", 0, None)
        self.assertFalse(ok)
        self.assertIn("sector", why)

    def test_null_momentum_rank_is_refused(self):
        self.assertFalse(update.is_eligible_entry(218.94, "Real Estate", 0, 210.0)[0])
        self.assertFalse(update.is_eligible_entry(218.94, "Real Estate", None, 210.0)[0])

    def test_null_ma_is_refused(self):
        ok, why = update.is_eligible_entry(155.0, "Technology", 5, None)
        self.assertFalse(ok)
        self.assertIn("50-day MA", why)

    def test_non_positive_price_is_refused(self):
        self.assertFalse(update.is_eligible_entry(0, "Energy", 4, 100.0)[0])


class TestGateAppliesToEveryBuyPath(unittest.TestCase):
    """Five paths can open or grow a position. Before v4.2 only one checked anything."""

    KEYS = {"symbol": "KEYS", "shares": 2.148903, "market_value": 692.01,
            "current_price": 322.03, "ma_50d": 329.87, "sector": "Technology"}

    def test_slot_fill_refuses_a_name_below_its_ma(self):
        funds = {"KEYS": {"current_price": 322.03, "sector": "Technology", "ma_50d": 329.87}}
        self.assertEqual(update.plan_slot_fill(
            [], [{"symbol": "KEYS", "momentum_rank": 5}], funds,
            9_000.0, 10_000.0, [], target_n=1), [],
            "slot fill bought a name already below its 50-day MA")

    def test_residual_sweep_refuses_a_name_below_its_ma(self):
        self.assertEqual(update.plan_residual_sweep(
            [dict(self.KEYS)], {"KEYS"}, 4_000.0, 10_000.0,
            {"KEYS": 322.03}, {"KEYS": 5}), [],
            "the sweep poured leftover cash into a name below its MA")

    def test_residual_sweep_refuses_a_name_with_null_data(self):
        cf = {"symbol": "CF", "shares": 7.896819, "market_value": 1080.28,
              "current_price": 136.80, "ma_50d": None, "sector": "Unknown"}
        self.assertEqual(update.plan_residual_sweep(
            [cf], {"CF"}, 4_000.0, 10_000.0, {"CF": 136.80}, {"CF": 0}), [])

    def test_the_sweep_respects_the_sector_cap(self):
        """v4.2: the sweep was the last buy path with no sector limit of any kind."""
        energy = [{"symbol": s, "shares": 3, "market_value": 950.0, "current_price": p,
                   "ma_50d": p * 0.85, "sector": "Energy"}
                  for s, p in (("MPC", 383.12), ("PSX", 252.00), ("VLO", 362.00))]
        sweep = update.plan_residual_sweep(
            energy, {"MPC", "PSX", "VLO"}, 6_000.0, 10_000.0,
            {"MPC": 383.12, "PSX": 252.00, "VLO": 362.00},
            {"MPC": 1, "PSX": 2, "VLO": 3})
        added = sum(q * {"MPC": 383.12, "PSX": 252.00, "VLO": 362.00}[s] for s, q in sweep)
        cap = 10_000.0 * (1 - update.CASH_FLOOR_PCT) * update.MAX_SECTOR_PCT
        self.assertLessEqual(2_850.0 + added, cap + 0.01,
                             "the sweep pushed Energy past the sector cap")

    def test_the_sweep_still_works_on_an_eligible_name(self):
        mpc = {"symbol": "MPC", "shares": 2.6, "market_value": 500.0,
               "current_price": 383.12, "ma_50d": 308.35, "sector": "Energy"}
        sweep = update.plan_residual_sweep(
            [mpc], {"MPC"}, 4_000.0, 10_000.0, {"MPC": 383.12}, {"MPC": 4})
        self.assertEqual(len(sweep), 1, "a qualifying name must still be swept into")
        self.assertEqual(sweep[0][0], "MPC")


# ════════════════════════════════════════════════════════════════════════════
# Sector concentration measured against money at risk
# ════════════════════════════════════════════════════════════════════════════
class TestSectorExposureOfInvested(unittest.TestCase):

    # The complete generation-2 book as of 2026-09-01 — invested $5,537.68 against a $9,938.88
    # portfolio value. The dust stubs are included because they are part of the denominator.
    LIVE = [{"symbol": "BNY", "shares": 5.887086, "market_value": 944.94,
             "sector": "Financial Services"},
            {"symbol": "CF", "shares": 7.896819, "market_value": 1080.28, "sector": "Unknown"},
            {"symbol": "JBL", "shares": 0.086496, "market_value": 25.87, "sector": "Technology"},
            {"symbol": "KEYS", "shares": 0.914514, "market_value": 291.98,
             "sector": "Technology"},
            {"symbol": "MPC", "shares": 2.610206, "market_value": 1000.02, "sector": "Energy"},
            {"symbol": "NTRS", "shares": 0.043807, "market_value": 8.00,
             "sector": "Financial Services"},
            {"symbol": "NUE", "shares": 0.737328, "market_value": 185.75,
             "sector": "Basic Materials"},
            {"symbol": "PSX", "shares": 3.908832, "market_value": 985.03, "sector": "Energy"},
            {"symbol": "ROST", "shares": 0.114058, "market_value": 26.23,
             "sector": "Consumer Cyclical"},
            {"symbol": "VLO", "shares": 2.733645, "market_value": 989.58, "sector": "Energy"}]

    def test_energy_breaches_on_invested_capital(self):
        """29.9% of portfolio value read as compliant; 53.7% of invested capital is the truth."""
        exposure = update.sector_exposure_of_invested(self.LIVE)
        self.assertGreater(exposure["Energy"], update.MAX_SECTOR_PCT)
        self.assertAlmostEqual(exposure["Energy"], 0.537, places=2)

    def test_shares_sum_to_one(self):
        self.assertAlmostEqual(sum(update.sector_exposure_of_invested(self.LIVE).values()), 1.0)

    def test_it_is_sorted_worst_first(self):
        vals = list(update.sector_exposure_of_invested(self.LIVE).values())
        self.assertEqual(vals, sorted(vals, reverse=True))

    def test_an_empty_book_is_not_a_breach(self):
        self.assertEqual(update.sector_exposure_of_invested([]), {})

    def test_dust_does_not_distort_the_denominator(self):
        self.assertEqual(update.sector_exposure_of_invested(
            [{"symbol": "X", "shares": 0, "market_value": 0, "sector": "Energy"}]), {})

    def test_concentration_is_advisory_and_never_halts(self):
        """Feeding this into assert_invariants would halt trading after MAX_BREACH_RUNS runs --
        stopping the very redeployment that dilutes the concentration (Directive 4)."""
        state = {"portfolio_value": 9938.88, "positions": [], "deployable_cash": 4401.20}
        self.assertEqual(update.assert_invariants(state, self.LIVE), [],
                         "a 53.7% invested-capital concentration must not halt trading")


class TestSectorTrimRestoration(unittest.TestCase):
    """v4.3: a sector over the cap is a breach with no discretionary cure.

    On 2026-10-07 the live book halted at breach_streak 6 (Technology 34.4%, Energy 33.0%).
    The overweight came from price drift, not buying, and the halt kill switch suppressed the
    only sells that could reduce it -- the same frozen-not-runaway shape as the JBL short.
    plan_sector_trims is the restoration path: it fires on every breached run and is routed
    past the halt, so the streak need never reach it.
    """

    # Tech 34.4% / Energy 33.0% of a $10,000 book — the live breach, rounded.
    def _breached_book(self):
        return [
            {"symbol": "NVDA", "shares": 1, "market_value": 2000, "sector": "Technology",
             "current_price": 2000, "momentum_rank": 1},
            {"symbol": "KEYS", "shares": 10, "market_value": 1440, "sector": "Technology",
             "current_price": 144, "momentum_rank": 40},
            {"symbol": "XOM", "shares": 10, "market_value": 2000, "sector": "Energy",
             "current_price": 200, "momentum_rank": 5},
            {"symbol": "VLO", "shares": 10, "market_value": 1300, "sector": "Energy",
             "current_price": 130, "momentum_rank": 60},
        ]

    def test_clears_the_breach(self):
        """After trimming, no sector may still exceed the cap."""
        pv = 10_000.0
        book = self._breached_book()
        trims = dict(update.plan_sector_trims(book, pv))
        # apply the trims and re-measure every sector
        sector_val: dict[str, float] = {}
        for h in book:
            sold = trims.get(h["symbol"], 0.0)
            sector_val[h["sector"]] = sector_val.get(h["sector"], 0.0) + \
                (h["market_value"] - sold * h["current_price"])
        for sec, val in sector_val.items():
            self.assertLessEqual(val, pv * update.MAX_SECTOR_PCT + 1e-6,
                                 f"{sec} still over cap after trim")

    def test_trims_the_worst_ranked_name_first(self):
        """The sector keeps its strongest name; the laggard is cut."""
        trims = dict(update.plan_sector_trims(self._breached_book(), 10_000.0))
        self.assertIn("KEYS", trims)     # rank 40 — the Tech laggard
        self.assertNotIn("NVDA", trims)  # rank 1  — the Tech leader is preserved
        self.assertIn("VLO", trims)      # rank 60 — the Energy laggard
        self.assertNotIn("XOM", trims)   # rank 5  — the Energy leader is preserved

    def test_no_trim_when_within_cap(self):
        book = [{"symbol": "A", "shares": 1, "market_value": 2000, "sector": "Technology",
                 "current_price": 2000, "momentum_rank": 1}]
        self.assertEqual(update.plan_sector_trims(book, 10_000.0), [])

    def test_never_sells_more_than_held(self):
        """A single oversized name is clamped to its own share count, not the raw excess."""
        book = [{"symbol": "BIG", "shares": 2, "market_value": 5000, "sector": "Technology",
                 "current_price": 2500, "momentum_rank": 9}]
        trims = dict(update.plan_sector_trims(book, 10_000.0))
        self.assertLessEqual(trims.get("BIG", 0.0), 2.0)


if __name__ == "__main__":
    unittest.main()
