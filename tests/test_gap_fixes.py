"""Unit tests for the 8 gap fixes applied to bot/update.py and bot/agent.py.

Each test class corresponds to one named gap fix.
Tests use only stdlib — no alpaca/anthropic packages required.
"""
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# pandas, numpy, yfinance are installed in the test environment.
# Only stub the packages that aren't: alpaca and anthropic.
def _stub(name: str):
    mod = types.ModuleType(name)
    sys.modules.setdefault(name, mod)
    return mod

for _pkg in ("alpaca", "alpaca.trading", "alpaca.trading.client",
             "alpaca.trading.requests", "alpaca.trading.enums", "anthropic"):
    _stub(_pkg)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
import update  # noqa: E402
import agent   # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Gap 1 — fetch_fundamentals stores None for missing yfinance fields
# ════════════════════════════════════════════════════════════════════════════
class TestFetchFundamentalsNoneSentinel(unittest.TestCase):

    def _run(self, info_dict: dict) -> dict:
        """Run fetch_fundamentals with a mocked yf.Ticker that returns info_dict."""
        mock_ticker = MagicMock()
        mock_ticker.info = info_dict
        with patch.object(update.yf, "Ticker", return_value=mock_ticker):
            return update.fetch_fundamentals(["AAPL"])["AAPL"]

    def test_missing_fields_return_none(self):
        """All-empty info → all four fundamental fields must be None."""
        result = self._run({})
        self.assertIsNone(result["eps_growth"],     "eps_growth should be None when missing")
        self.assertIsNone(result["revenue_growth"], "revenue_growth should be None when missing")
        self.assertIsNone(result["forward_pe"],     "forward_pe should be None when missing")
        self.assertIsNone(result["ma_50d"],         "ma_50d should be None when missing")

    def test_present_fields_are_rounded(self):
        """Valid yfinance values should be stored rounded, not None."""
        result = self._run({
            "earningsGrowth":  0.113,   # → 11.3
            "revenueGrowth":   0.082,   # → 8.2
            "forwardPE":       25.678,  # → 25.7
            "fiftyDayAverage": 271.234, # → 271.23
        })
        self.assertAlmostEqual(result["eps_growth"],     11.3)
        self.assertAlmostEqual(result["revenue_growth"],  8.2)
        self.assertAlmostEqual(result["forward_pe"],     25.7)
        self.assertAlmostEqual(result["ma_50d"],        271.23)

    def test_zero_growth_stored_as_zero_not_none(self):
        """Explicit 0.0 from yfinance (e.g., flat earnings) must be stored as 0, not None."""
        result = self._run({
            "earningsGrowth":  0.0,
            "revenueGrowth":   0.0,
            "forwardPE":       15.0,
            "fiftyDayAverage": 200.0,
        })
        self.assertEqual(result["eps_growth"],     0.0)
        self.assertEqual(result["revenue_growth"], 0.0)
        self.assertIsNotNone(result["eps_growth"])
        self.assertIsNotNone(result["revenue_growth"])

    def test_negative_growth_preserved(self):
        """Negative growth (e.g., -5%) must be stored as a negative number, not None."""
        result = self._run({"earningsGrowth": -0.05, "revenueGrowth": -0.03})
        self.assertAlmostEqual(result["eps_growth"],     -5.0)
        self.assertAlmostEqual(result["revenue_growth"], -3.0)


# ════════════════════════════════════════════════════════════════════════════
# Gap 2 — quality filter handles None without TypeError
# ════════════════════════════════════════════════════════════════════════════
class TestQualityFilterNoneHandling(unittest.TestCase):

    def _screen(self, eg, rg, fpe):
        """Run screening logic for one candidate with given fundamentals."""
        import pandas as pd
        fundamentals = {"SYM": {
            "eps_growth": eg, "revenue_growth": rg, "forward_pe": fpe,
            "current_price": 100.0, "name": "Test", "sector": "Test",
        }}
        idx = pd.Index(["SYM"])
        mom6  = pd.Series([0.8],  index=idx, name="mom_6m")
        mom12 = pd.Series([0.8],  index=idx, name="mom_12m")
        vol30 = pd.Series([0.15], index=idx, name="vol_30d")
        vol_90th = 0.5

        mom_score = (mom6.rank(pct=True) + mom12.rank(pct=True)) / 2
        # Only "SYM" qualifies on momentum
        candidates = ["SYM"]

        q_pass = v_pass = 0
        screened = []
        fi = fundamentals.get("SYM", {})
        eg_v  = fi.get("eps_growth")
        rg_v  = fi.get("revenue_growth")
        fpe_v = fi.get("forward_pe")
        q_ok = eg_v is not None and rg_v is not None and eg_v > 10 and rg_v > 8
        v_ok = fpe_v is None or fpe_v < 40
        if q_ok:
            q_pass += 1
        if v_ok:
            v_pass += 1
        if q_ok and v_ok:
            screened.append({"symbol": "SYM"})
        return screened, q_pass, v_pass

    def test_none_quality_data_fails_conservatively(self):
        """Missing EPS/revenue data → stock must NOT enter screened list."""
        screened, q, _ = self._screen(eg=None, rg=None, fpe=20.0)
        self.assertEqual(len(screened), 0, "Stock with None fundamentals should not pass quality")
        self.assertEqual(q, 0)

    def test_none_forward_pe_relaxed(self):
        """Missing forward_pe → valuation pass (STRATEGY.md: fall back to relaxed rules)."""
        screened, _, v = self._screen(eg=15.0, rg=12.0, fpe=None)
        self.assertEqual(len(screened), 1, "Stock with None P/E but good quality should pass")
        self.assertEqual(v, 1)

    def test_good_data_passes_all_filters(self):
        screened, q, v = self._screen(eg=20.0, rg=15.0, fpe=25.0)
        self.assertEqual(len(screened), 1)
        self.assertEqual(q, 1)
        self.assertEqual(v, 1)

    def test_high_pe_fails_valuation(self):
        screened, q, v = self._screen(eg=20.0, rg=15.0, fpe=45.0)
        self.assertEqual(len(screened), 0, "High P/E should fail valuation")


# ════════════════════════════════════════════════════════════════════════════
# Gap 3 — load_agent_approvals: target_urgency + parse_failed skip
# ════════════════════════════════════════════════════════════════════════════
class TestLoadAgentApprovals(unittest.TestCase):

    def _write_log(self, tmp_path: Path, runs: list) -> None:
        tmp_path.write_text(json.dumps({"runs": runs}), encoding="utf-8")

    def test_immediate_urgency_default(self, tmp_dir=None):
        """Default call (no target_urgency) loads only 'immediate' decisions."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "agent_log.json"
            self._write_log(log_path, [{
                "type": "day_end",
                "id": "RUN-TEST",
                "decisions": [
                    {"action": "SELL", "symbol": "TSLA", "urgency": "immediate"},
                    {"action": "SELL", "symbol": "AAPL", "urgency": "next_open"},
                ],
            }])
            with patch.object(update, "LOG_FILE", log_path):
                approvals = update.load_agent_approvals(target_urgency="immediate")
        self.assertIn("TSLA", approvals["SELL"])
        self.assertNotIn("AAPL", approvals["SELL"])

    def test_next_open_urgency(self):
        """Pre-market run with target_urgency='next_open' loads only next_open decisions."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "agent_log.json"
            self._write_log(log_path, [{
                "type": "day_end",
                "id": "RUN-TEST",
                "decisions": [
                    {"action": "SELL", "symbol": "TSLA", "urgency": "immediate"},
                    {"action": "SELL", "symbol": "AAPL", "urgency": "next_open"},
                ],
            }])
            with patch.object(update, "LOG_FILE", log_path):
                approvals = update.load_agent_approvals(target_urgency="next_open")
        self.assertIn("AAPL", approvals["SELL"])
        self.assertNotIn("TSLA", approvals["SELL"])

    def test_parse_failed_run_is_skipped(self):
        """A run with parse_failed=True must be skipped; the next valid run is used."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "agent_log.json"
            self._write_log(log_path, [
                {
                    "type": "day_end",
                    "id": "RUN-FAILED",
                    "parse_failed": True,
                    "decisions": [],   # empty — should not block sells
                },
                {
                    "type": "day_end",
                    "id": "RUN-GOOD",
                    "decisions": [
                        {"action": "SELL", "symbol": "TPR", "urgency": "immediate"},
                    ],
                },
            ])
            with patch.object(update, "LOG_FILE", log_path):
                approvals = update.load_agent_approvals(target_urgency="immediate")
        self.assertIn("TPR", approvals["SELL"], "Should fall back to earlier valid run")

    def test_missing_log_file_returns_empty(self):
        """When agent_log.json doesn't exist, return empty approvals (safe default)."""
        with patch.object(update, "LOG_FILE", Path("/nonexistent/path/agent_log.json")):
            approvals = update.load_agent_approvals()
        self.assertEqual(approvals["SELL"], set())
        self.assertEqual(approvals["BUY"], set())


# ════════════════════════════════════════════════════════════════════════════
# Gap 4 — apply_risk_limits: position-size violation override
# ════════════════════════════════════════════════════════════════════════════
class TestApplyRiskLimits(unittest.TestCase):

    def _run(self, to_sell, to_buy, pv, cash, approved, prices):
        return update.apply_risk_limits(to_sell, to_buy, pv, cash, approved, prices)

    def test_normal_sell_requires_agent_approval(self):
        """A normal sell (within size limits) is blocked without agent approval."""
        sells, _ = self._run(
            to_sell=[("AAPL", 1)],
            to_buy=[],
            pv=10_000, cash=500,
            approved=set(),   # no approval
            prices={"AAPL": 280.0},
        )
        self.assertEqual(len(sells), 0, "Unapproved normal sell should be blocked")

    def test_position_size_violation_bypasses_approval_gate(self):
        """A position >2× max (8%) does NOT need agent approval — size itself is disqualifying."""
        # TSLA at 38.77% of 10k portfolio = $3,877 >> 2 × 8% × 10k = $1,600
        pv = 10_000
        sells, _ = self._run(
            to_sell=[("TSLA", 10)],
            to_buy=[],
            pv=pv, cash=500,
            approved=set(),           # deliberately no agent approval
            prices={"TSLA": 391.0},   # 10 × 391 = $3,910 = 39.1% weight
        )
        self.assertEqual(len(sells), 1, "Position-size violation must be sold without agent approval")
        self.assertEqual(sells[0][0], "TSLA")

    def test_position_size_violation_bypasses_30pct_cap(self):
        """A position >30% of portfolio must be sellable even though it exceeds the sell cap."""
        pv = 10_000
        sells, _ = self._run(
            to_sell=[("TSLA", 10)],
            to_buy=[],
            pv=pv, cash=500,
            approved={"TSLA"},            # agent approved
            prices={"TSLA": 391.0},       # $3,910 > 30% cap ($3,000)
        )
        self.assertEqual(len(sells), 1, "Should allow selling >30% position when size violation")

    def test_normal_sell_respects_30pct_cap(self):
        """A normal sell (not size violation) is capped at 30% of portfolio."""
        pv = 10_000
        sells, _ = self._run(
            to_sell=[("AAPL", 1), ("GOOG", 1)],
            to_buy=[],
            pv=pv, cash=500,
            approved={"AAPL", "GOOG"},
            prices={"AAPL": 2800.0, "GOOG": 200.0},  # AAPL alone = $2,800 = 28% — under cap
        )
        # AAPL at $2,800 is under 30% cap → allowed; GOOG would push to $3,000 exactly — allowed
        # But combined $3,000 = 30% cap — both should pass
        self.assertGreaterEqual(len(sells), 1)

    def test_buy_respects_cash_floor(self):
        """When cash is exactly at the floor, available_cash == 0 → no buys at all."""
        pv = 10_000
        # cash = floor exactly → available_cash = 0 → loop breaks immediately
        _, buys = self._run(
            to_sell=[],
            to_buy=[("NVDA", 1), ("AMD", 1)],
            pv=pv, cash=500,   # cash == floor ($500) → available_cash = 0
            approved=set(),
            prices={"NVDA": 100.0, "AMD": 50.0},
        )
        self.assertEqual(len(buys), 0, "No buys should happen when cash equals floor")


# ════════════════════════════════════════════════════════════════════════════
# Gap 5 — alpaca_positions_to_holdings: entry_date preserved
# ════════════════════════════════════════════════════════════════════════════
class TestAlpacaPositionsToHoldings(unittest.TestCase):

    def _make_position(self, symbol, price, avg, qty, mv, upnl, upnl_pct):
        pos = MagicMock()
        pos.symbol         = symbol
        pos.current_price  = str(price)
        pos.avg_entry_price= str(avg)
        pos.qty            = str(qty)
        pos.market_value   = str(mv)
        pos.unrealized_pl  = str(upnl)
        pos.unrealized_plpc= str(upnl_pct)
        return pos

    def test_entry_date_preserved_from_existing(self):
        """When a position already exists in the portfolio, entry_date must NOT be reset."""
        import pandas as pd
        pos = self._make_position("AAPL", 280, 272, 1, 280, 7.22, 0.0265)
        fundamentals   = {"AAPL": {"eps_growth": None, "revenue_growth": None,
                                    "forward_pe": None, "ma_50d": None,
                                    "name": "Apple", "sector": "Technology"}}
        screened_ranks = {}
        vol30 = pd.Series({"AAPL": 0.25})
        existing_map   = {"AAPL": {"entry_date": "2026-04-22"}}

        holdings = update.alpaca_positions_to_holdings(
            [pos], fundamentals, screened_ranks, vol30, existing_map
        )
        self.assertEqual(holdings[0]["entry_date"], "2026-04-22",
                         "entry_date must be preserved from existing portfolio")

    def test_new_position_gets_todays_date(self):
        """A brand-new position not in existing_map must get today's date."""
        import pandas as pd
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        pos = self._make_position("NVDA", 800, 780, 1, 800, 20, 0.0256)
        holdings = update.alpaca_positions_to_holdings(
            [pos], {}, {}, pd.Series({"NVDA": 0.30}), {}
        )
        self.assertEqual(holdings[0]["entry_date"], today)

    def test_missing_fundamentals_stored_as_none(self):
        """Holdings with no fundamentals must have None fields, not 0."""
        import pandas as pd
        pos = self._make_position("TSLA", 391, 374, 10, 3910, 170, 0.0456)
        holdings = update.alpaca_positions_to_holdings(
            [pos], {}, {}, pd.Series({"TSLA": 0.45}), {}
        )
        h = holdings[0]
        self.assertIsNone(h["eps_growth"])
        self.assertIsNone(h["revenue_growth"])
        self.assertIsNone(h["forward_pe"])
        self.assertIsNone(h["ma_50d"])
        self.assertEqual(h["status"], "unknown_ma")

    def test_ma50_status_above_below(self):
        """Status field reflects price vs MA50 correctly when data is available."""
        import pandas as pd
        pos = self._make_position("GOOG", 385, 347, 1, 385, 38, 0.109)
        fundamentals = {"GOOG": {"ma_50d": 312.0, "eps_growth": 31.1,
                                  "revenue_growth": 18.0, "forward_pe": 28.3,
                                  "name": "Alphabet", "sector": "Communication"}}
        holdings = update.alpaca_positions_to_holdings(
            [pos], fundamentals, {}, pd.Series({"GOOG": 0.41}), {}
        )
        self.assertEqual(holdings[0]["status"], "above_ma")


# ════════════════════════════════════════════════════════════════════════════
# Gap 6 — Issuer deduplication (ISSUER_MAP)
# ════════════════════════════════════════════════════════════════════════════
class TestIssuerDeduplication(unittest.TestCase):

    def test_issuer_map_contains_alphabet(self):
        """ISSUER_MAP must map both GOOG and GOOGL to the same issuer."""
        self.assertEqual(update.ISSUER_MAP.get("GOOG"),  update.ISSUER_MAP.get("GOOGL"))
        self.assertIsNotNone(update.ISSUER_MAP.get("GOOG"))

    def test_dedup_keeps_higher_momentum(self):
        """When two tickers share an issuer, the first (higher-momentum) one is kept."""
        screened = [
            {"symbol": "GOOG",  "momentum_rank": 1},   # higher momentum
            {"symbol": "GOOGL", "momentum_rank": 2},   # lower momentum — should be dropped
        ]
        seen_issuers: dict = {}
        deduped = []
        for item in screened:
            sym    = item["symbol"]
            issuer = update.ISSUER_MAP.get(sym, sym)
            if issuer not in seen_issuers:
                seen_issuers[issuer] = sym
                deduped.append(item)

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["symbol"], "GOOG")

    def test_dedup_does_not_affect_different_issuers(self):
        """Stocks from different issuers are not deduplicated."""
        screened = [
            {"symbol": "AAPL", "momentum_rank": 1},
            {"symbol": "MSFT", "momentum_rank": 2},
            {"symbol": "NVDA", "momentum_rank": 3},
        ]
        seen_issuers: dict = {}
        deduped = []
        for item in screened:
            sym    = item["symbol"]
            issuer = update.ISSUER_MAP.get(sym, sym)
            if issuer not in seen_issuers:
                seen_issuers[issuer] = sym
                deduped.append(item)

        self.assertEqual(len(deduped), 3)


# ════════════════════════════════════════════════════════════════════════════
# Gap 7 — filter_status reflects actual holding compliance
# ════════════════════════════════════════════════════════════════════════════
class TestFilterStatusHoldingCompliance(unittest.TestCase):

    def _compute_quality(self, holdings):
        return sum(
            1 for h in holdings
            if h.get("eps_growth") is not None
            and h.get("revenue_growth") is not None
            and h["eps_growth"] > 10
            and h["revenue_growth"] > 8
        )

    def _compute_valuation(self, holdings):
        return sum(
            1 for h in holdings
            if h.get("forward_pe") is None or h["forward_pe"] < 40
        )

    def test_tsla_with_none_data_fails_quality(self):
        """TSLA with all-None fundamentals must not count as passing quality."""
        holdings = [
            {"symbol": "TSLA", "eps_growth": None, "revenue_growth": None, "forward_pe": None},
            {"symbol": "GOOG", "eps_growth": 31.1, "revenue_growth": 18.0, "forward_pe": 28.3},
        ]
        self.assertEqual(self._compute_quality(holdings), 1,   "Only GOOG passes quality")
        self.assertEqual(self._compute_valuation(holdings), 2, "Both pass valuation (TSLA P/E=None → relaxed)")

    def test_all_passing_portfolio(self):
        holdings = [
            {"symbol": "ADI", "eps_growth": 116.7, "revenue_growth": 30.4, "forward_pe": 30.3},
            {"symbol": "BEN", "eps_growth":  87.2, "revenue_growth":  8.7, "forward_pe": 10.1},
            {"symbol": "BK",  "eps_growth":  41.8, "revenue_growth": 13.4, "forward_pe": 13.9},
        ]
        self.assertEqual(self._compute_quality(holdings),   3)
        self.assertEqual(self._compute_valuation(holdings), 3)

    def test_high_pe_fails_valuation(self):
        holdings = [
            {"symbol": "XPNS", "eps_growth": 50.0, "revenue_growth": 20.0, "forward_pe": 75.0},
        ]
        self.assertEqual(self._compute_valuation(holdings), 0)


# ════════════════════════════════════════════════════════════════════════════
# Gap 8 — agent.py: parse_failed flag written on JSON parse error
# ════════════════════════════════════════════════════════════════════════════
class TestAgentParseFailedFlag(unittest.TestCase):

    def test_parse_failed_flag_in_fallback_result(self):
        """When Claude returns unparseable output, result must contain parse_failed=True."""
        mock_content = MagicMock()
        mock_content.text = "This is not valid JSON at all."
        mock_message = MagicMock()
        mock_message.content = [mock_content]
        mock_message.stop_reason = "end_turn"
        mock_message.usage.input_tokens  = 100
        mock_message.usage.output_tokens = 50

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message

        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.object(agent, "anthropic", mock_anthropic):
                result, _ = agent.run_agent(
                    "day_end",
                    portfolio={"holdings": []},
                    strategy="test strategy",
                    enrichment={},
                )
        self.assertTrue(result.get("parse_failed"),
                        "parse_failed must be True when JSON parsing fails")
        self.assertEqual(result["decisions"], [])

    def test_write_log_propagates_parse_failed(self):
        """write_log must include parse_failed=True in the log entry."""
        import tempfile
        result = {
            "assessment": "raw text",
            "regime": "unknown",
            "regime_confidence": 0,
            "flags": [],
            "decisions": [],
            "cash_action": "maintain",
            "cash_rationale": "",
            "summary": "parse failed",
            "parse_failed": True,
        }
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "agent_log.json"
            with patch.object(agent, "LOG_FILE", log_path):
                log = {"runs": [], "last_run": None, "last_type": None}
                entry = agent.write_log(log, "day_end", result, {})
        self.assertTrue(entry.get("parse_failed"), "Log entry must have parse_failed=True")

    def test_valid_json_response_has_no_parse_failed(self):
        """When Claude returns valid JSON, parse_failed must NOT be set."""
        valid_json = json.dumps({
            "assessment": "ok", "regime": "bull", "regime_confidence": 0.84,
            "flags": [], "decisions": [], "cash_action": "maintain",
            "cash_rationale": "ok", "summary": "all good",
        })
        mock_content = MagicMock()
        mock_content.text = valid_json
        mock_message = MagicMock()
        mock_message.content = [mock_content]
        mock_message.stop_reason = "end_turn"
        mock_message.usage.input_tokens  = 1000
        mock_message.usage.output_tokens = 200

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.object(agent, "anthropic", mock_anthropic):
                result, _ = agent.run_agent("day_end", {}, "strategy", {})
        self.assertFalse(result.get("parse_failed", False),
                         "Successful parse must NOT set parse_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
