"""Tests for pipeline_runner: analyst/debate/risk mapping, result formatting, report saving."""

import inspect
import tempfile
from pathlib import Path

import pytest

from watchy.orchestrator import AnalystSet, DebateMode, PipelineSpec, RiskMode
from watchy.pipeline_runner import (
    _ANALYST_MAP,
    _ANALYST_LABELS,
    _extract_verdict,
    _format_result,
    _map_analysts,
    _map_debate,
    _map_risk,
    _save_report,
    _strip_preamble,
    DEFAULT_REPORTS_DIR,
    create_tradingagents_runner,
)


def test_v41_flash_is_default_for_both_reasoning_roles():
    params = inspect.signature(create_tradingagents_runner).parameters
    assert params["deep_think_llm"].default == "deepseek-flash"
    assert params["quick_think_llm"].default == "deepseek-flash"


class TestStripPreamble:
    def test_strips_conversational_lead_in_before_rule(self):
        text = (
            "Excellent, I now have comprehensive data. Let me compile the full report.\n"
            "\n---\n# GOOG Technical Analysis\nBody line."
        )
        out = _strip_preamble(text)
        assert out.startswith("# GOOG Technical Analysis")
        assert "Let me compile" not in out

    def test_strips_lead_in_before_heading_without_rule(self):
        text = "Now I have all the data. Let me compile.\n\n# Fundamentals\nBody."
        out = _strip_preamble(text)
        assert out.startswith("# Fundamentals")

    def test_keeps_content_that_starts_with_heading(self):
        text = "# Already Clean\nBody."
        assert _strip_preamble(text) == "# Already Clean\nBody."

    def test_no_marker_returns_stripped_original(self):
        text = "  just a plain paragraph with no markdown markers  "
        assert _strip_preamble(text) == "just a plain paragraph with no markdown markers"

    def test_recommendation_snippet_is_clean(self):
        final_state = {
            "market_report": (
                "Excellent, I now have comprehensive data. Let me compile the full "
                "report.\n\n---\n# GOOG Market Report\nRSI is 55."
            ),
        }
        result = _format_result(
            _spec_ticker(), _daily_spec(), ["market"], final_state, "BUY",
        )
        rec = result["recommendations"][0]
        assert rec.startswith("[Market] # GOOG Market Report")
        assert "Let me compile" not in rec


def _daily_spec():
    return PipelineSpec(
        analysts=AnalystSet.FULL, debate=DebateMode.BULL_BEAR, risk=RiskMode.SIMPLIFIED,
    )


def _spec_ticker():
    return "GOOG"


class TestExtractVerdict:
    def test_explicit_final_proposal_marker(self):
        assert _extract_verdict("... FINAL TRANSACTION PROPOSAL: **BUY**") == "BUY"

    def test_marker_without_asterisks(self):
        assert _extract_verdict("FINAL TRANSACTION PROPOSAL: SELL") == "SELL"

    def test_case_insensitive(self):
        assert _extract_verdict("final transaction proposal: hold") == "HOLD"

    def test_fallback_keyword(self):
        assert _extract_verdict("My recommendation is to HOLD for now.") == "HOLD"

    def test_empty_or_no_verdict(self):
        assert _extract_verdict("") == ""
        assert _extract_verdict("no clear call here") == ""

    def test_marker_wins_over_stray_keyword(self):
        text = "Some say buy, others sell. FINAL TRANSACTION PROPOSAL: **HOLD**"
        assert _extract_verdict(text) == "HOLD"

    def test_action_line(self):
        # Graduated subsets use a "**Action**: Sell" trader plan (the MOD bug).
        assert _extract_verdict("**Action**: Sell\n**Reasoning**: ...") == "SELL"

    def test_recommendation_line(self):
        assert _extract_verdict("Recommendation: BUY at these levels") == "BUY"

    def test_verdict_from_trader_plan_when_final_decision_empty(self):
        final_state = {
            "trader_investment_plan": "**Action**: Sell\n**Reasoning**: overvalued.",
            "final_trade_decision": "",
        }
        result = _format_result(
            "MOD", _daily_spec(), ["market", "social"], final_state, "",
        )
        assert result["verdict"] == "SELL"


class TestAnalystMapping:
    def test_none_to_empty(self):
        assert _map_analysts(AnalystSet.NONE) == []

    def test_market_only(self):
        assert _map_analysts(AnalystSet.MARKET_ONLY) == ["market"]

    def test_market_sentiment_uses_social_key(self):
        # TradingAgents uses "social" not "sentiment"
        assert _map_analysts(AnalystSet.MARKET_SENTIMENT) == ["market", "social"]

    def test_full_includes_all_four(self):
        assert _map_analysts(AnalystSet.FULL) == [
            "market", "social", "news", "fundamentals",
        ]

    def test_all_analyst_sets_are_mapped(self):
        """Every AnalystSet value must have a mapping entry."""
        for val in AnalystSet:
            assert val in _ANALYST_MAP, f"Missing mapping for {val}"


class TestLabelMapping:
    def test_social_maps_to_sentiment(self):
        assert _ANALYST_LABELS["social"] == "sentiment"

    def test_other_keys_unchanged(self):
        assert _ANALYST_LABELS["market"] == "market"
        assert _ANALYST_LABELS["news"] == "news"
        assert _ANALYST_LABELS["fundamentals"] == "fundamentals"


class TestDebateMapping:
    def test_bull_bear_is_one(self):
        assert _map_debate(DebateMode.BULL_BEAR) == 1

    def test_none_is_zero(self):
        assert _map_debate(DebateMode.NONE) == 0


class TestRiskMapping:
    def test_full_is_one(self):
        assert _map_risk(RiskMode.FULL) == 1

    def test_simplified_is_zero(self):
        assert _map_risk(RiskMode.SIMPLIFIED) == 0

    def test_none_is_zero(self):
        assert _map_risk(RiskMode.NONE) == 0


class TestFormatResult:
    def test_minimal_result(self):
        spec = PipelineSpec(
            analysts=AnalystSet.MARKET_SENTIMENT,
            debate=DebateMode.BULL_BEAR,
            risk=RiskMode.SIMPLIFIED,
        )
        final_state = {}
        decision = "HOLD"

        result = _format_result("AAPL", spec, ["market", "social"], final_state, decision)

        assert result["ticker"] == "AAPL"
        assert result["analysts_run"] == ["market", "sentiment"]
        assert result["debate"] == "bull_bear"
        assert result["risk_mode"] == "simplified"
        assert result["recommendations"] == []
        assert result["risk_assessment"] is None
        assert "HOLD" in result["summary"]

    def test_recommendations_from_reports(self):
        spec = PipelineSpec(
            analysts=AnalystSet.MARKET_ONLY,
            debate=DebateMode.NONE,
            risk=RiskMode.NONE,
        )
        final_state = {
            "market_report": "Bullish signal — RSI oversold at 26, MACD crossover confirmed.",
        }
        decision = {"decision": "BUY"}

        result = _format_result("NVDA", spec, ["market"], final_state, decision)

        assert len(result["recommendations"]) == 1
        assert "Market" in result["recommendations"][0]
        assert "RSI oversold" in result["recommendations"][0]

    def test_risk_assessment_from_state(self):
        spec = PipelineSpec(
            analysts=AnalystSet.FULL,
            debate=DebateMode.BULL_BEAR,
            risk=RiskMode.FULL,
        )
        final_state = {
            "risk_debate_state": {
                "judge_decision": "Accept the trade with 2% risk allocation"
            }
        }
        decision = "BUY"

        result = _format_result("TSLA", spec, ["market"], final_state, decision)

        assert result["risk_assessment"] == "Accept the trade with 2% risk allocation"

    def test_full_reports_included(self):
        spec = PipelineSpec(
            analysts=AnalystSet.FULL,
            debate=DebateMode.BULL_BEAR,
            risk=RiskMode.FULL,
        )
        final_state = {
            "market_report": "Market report text",
            "sentiment_report": "Sentiment report text",
        }
        decision = "HOLD"

        result = _format_result("GOOG", spec, ["market", "social"], final_state, decision)

        assert "_reports" in result
        assert result["_reports"]["market_report"] == "Market report text"
        assert result["_reports"]["sentiment_report"] == "Sentiment report text"
        assert result["_reports"]["news_report"] is None

    def test_decision_string_passthrough(self):
        spec = PipelineSpec(
            analysts=AnalystSet.MARKET_ONLY,
            debate=DebateMode.NONE,
            risk=RiskMode.NONE,
        )
        result = _format_result("A", spec, ["market"], {}, "BUY")
        assert result["_decision_raw"] == "BUY"

    def test_surfaces_verdict_and_analyst_count(self):
        """#3: structured verdict + analyst count for the notification headline."""
        spec = PipelineSpec(AnalystSet.FULL, DebateMode.BULL_BEAR, RiskMode.SIMPLIFIED)
        final_state = {
            "final_trade_decision": "FINAL TRANSACTION PROPOSAL: **BUY**",
        }
        result = _format_result(
            "AAPL", spec, ["market", "social", "news", "fundamentals"], final_state, "BUY"
        )
        assert result["verdict"] == "BUY"
        assert result["analyst_count"] == 4

    def test_empty_verdict_when_no_decision(self):
        spec = PipelineSpec(AnalystSet.MARKET_ONLY, DebateMode.NONE, RiskMode.NONE)
        result = _format_result("A", spec, ["market"], {}, None)
        assert result["verdict"] == ""
        assert result["analyst_count"] == 1


class TestSaveReport:
    def test_saves_consolidated_md(self):
        final_state = {
            "market_report": "Market says bullish.",
            "sentiment_report": "Sentiment is positive.",
            "trader_investment_plan": "Buy at market open.",
            "risk_debate_state": {
                "judge_decision": "Approved with 2% risk."
            },
        }
        config = {"reports_dir": tempfile.mkdtemp()}

        path = _save_report(final_state, "NVDA", config)

        assert path is not None
        assert path.suffix == ".md"
        assert path.name.startswith("NVDA_")
        content = path.read_text()
        assert "# Trading Analysis Report: NVDA" in content
        assert "Market says bullish" in content
        assert "Sentiment is positive" in content
        assert "Buy at market open" in content
        assert "Approved with 2% risk" in content

        # Cleanup
        path.unlink()
        path.parent.rmdir()

    def test_filename_includes_ticker_and_timestamp(self):
        config = {"reports_dir": tempfile.mkdtemp()}
        path = _save_report({}, "TSLA", config)

        assert path is not None
        name = path.name
        assert name.startswith("TSLA_")
        assert name.endswith(".md")
        # TSLA_2026-06-02T...Z.md
        assert "T" in name
        assert "Z.md" in name

        path.unlink()
        path.parent.rmdir()

    def test_uses_default_dir_when_not_configured(self):
        path = _save_report({}, "TEST", {})
        if path is not None:
            # Should be under DEFAULT_REPORTS_DIR (path comparison, not string)
            expected = str(Path(DEFAULT_REPORTS_DIR))
            actual = str(path.parent)
            assert actual == expected, f"Expected {expected}, got {actual}"
            path.unlink()
