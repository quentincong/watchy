"""Tests for advisor: prompt formatting and advice parsing (no LLM calls)."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from watchy.advisor import (
    _analyst_summary_tail,
    _effective_key,
    _format_analysis,
    _gemini_cost_usd,
    _gemini_thinking_config,
    _parse_advice,
    _take_profit_guidance,
)


class TestGeminiThinkingConfig:
    def test_off_maps_to_minimal(self):
        # 3.6 rejects the legacy thinkingBudget (HTTP 400), so "off" -> minimal
        assert _gemini_thinking_config("off") == {"thinkingLevel": "minimal"}

    def test_levels_use_thinking_level(self):
        assert _gemini_thinking_config("low") == {"thinkingLevel": "low"}
        assert _gemini_thinking_config("medium") == {"thinkingLevel": "medium"}


class TestGeminiCost:
    def test_input_priced(self):
        assert abs(_gemini_cost_usd(1_000_000, 0, 0) - 1.50) < 1e-9

    def test_thinking_billed_as_output(self):
        # thinking tokens cost the same as visible output tokens
        assert _gemini_cost_usd(0, 0, 1_000_000) == _gemini_cost_usd(0, 1_000_000, 0)
        assert abs(_gemini_cost_usd(0, 0, 1_000_000) - 9.00) < 1e-9

    def test_zero(self):
        assert _gemini_cost_usd(0, 0, 0) == 0.0
from watchy.config import LLMConfig


class TestEffectiveKey:
    def test_deepseek_uses_deepseek_key(self):
        llm = LLMConfig(provider="deepseek", deepseek_api_key="ds-secret", api_key="")
        assert _effective_key(llm) == "ds-secret"

    def test_deepseek_falls_back_to_api_key(self):
        llm = LLMConfig(provider="deepseek", deepseek_api_key="", api_key="generic")
        assert _effective_key(llm) == "generic"

    def test_anthropic_uses_api_key(self):
        llm = LLMConfig(provider="anthropic", api_key="sk-ant", deepseek_api_key="ignored")
        assert _effective_key(llm) == "sk-ant"

    def test_both_empty_returns_empty(self):
        llm = LLMConfig(provider="deepseek", deepseek_api_key="", api_key="")
        assert _effective_key(llm) == ""


class TestParseAdvice:
    def test_parses_standard_format(self):
        raw = """Ticker: NVDA
Decision: BUY
Urgency: HIGH

NVDA is trading at $142 with RSI oversold at 26. I recommend a 2% allocation
with a stop-loss at $128 targeting $165-$170. Primary risk is upcoming earnings
on June 18. If it breaks below $135 before earnings, exit early."""
        parsed = _parse_advice(raw, "NVDA")
        assert parsed["ticker"] == "NVDA"
        assert parsed["decision"] == "BUY"
        assert parsed["urgency"] == "HIGH"
        assert "RSI oversold" in parsed["detail"]
        assert "stop-loss" in parsed["detail"]

    def test_parses_sell(self):
        raw = """Ticker: TSLA
Decision: SELL
Urgency: MEDIUM

Death cross confirmed with declining fundamentals. Exit full position at $245,
locking in gains before potential drop to $200."""
        parsed = _parse_advice(raw, "TSLA")
        assert parsed["ticker"] == "TSLA"
        assert parsed["decision"] == "SELL"
        assert parsed["urgency"] == "MEDIUM"

    def test_parses_hold(self):
        raw = """Ticker: AAPL
Decision: HOLD
Urgency: LOW

Price near fair value, no strong directional signals. Maintain current position."""
        parsed = _parse_advice(raw, "AAPL")
        assert parsed["decision"] == "HOLD"
        assert parsed["urgency"] == "LOW"

    def test_fallback_ticker_when_missing(self):
        raw = """Decision: BUY
Urgency: HIGH

Some detail here."""
        parsed = _parse_advice(raw, "FALLBACK")
        assert parsed["ticker"] == "FALLBACK"
        assert parsed["decision"] == "BUY"

    def test_case_insensitive_labels(self):
        raw = """ticker: nvda
decision: buy
urgency: high

detail"""
        parsed = _parse_advice(raw, "NVDA")
        assert parsed["ticker"] == "nvda"  # preserves case of value
        assert parsed["decision"] == "BUY"  # uppercased
        assert parsed["urgency"] == "HIGH"

    def test_decision_after_preamble_line(self):
        """A preamble before the header must not drop decision/urgency (AVGO bug)."""
        raw = (
            'Here is my assessment.\n\n'
            'Ticker: AVGO\nDecision: HOLD\nUrgency: LOW\n\n'
            'Wait for confirmation before adding.'
        )
        parsed = _parse_advice(raw, "AVGO")
        assert parsed["decision"] == "HOLD"
        assert parsed["urgency"] == "LOW"
        assert "Wait for confirmation" in parsed["detail"]
        assert "Here is my assessment" in parsed["detail"]

    def test_blank_first_line_still_parses(self):
        raw = "\nDecision: BUY\nUrgency: HIGH\n\nDetail here."
        parsed = _parse_advice(raw, "NVDA")
        assert parsed["decision"] == "BUY"
        assert parsed["urgency"] == "HIGH"
        assert parsed["detail"] == "Detail here."

    def test_no_header_lines(self):
        """If there are no Ticker:/Decision:/Urgency: lines, everything is detail."""
        raw = "Just a plain recommendation to BUY NVDA at these levels."
        parsed = _parse_advice(raw, "NVDA")
        assert parsed["ticker"] == "NVDA"
        assert parsed["decision"] == ""
        assert parsed["urgency"] == ""
        assert parsed["detail"] == raw

    def test_parses_take_profit_line(self):
        raw = (
            "Ticker: NVDA\nDecision: TRIM\nUrgency: MEDIUM\n"
            "Target: N/A\nTake-Profit: sell 1 share at 192.50\n\n"
            "Bank one share into strength; hold the rest."
        )
        parsed = _parse_advice(raw, "NVDA")
        assert parsed["decision"] == "TRIM"
        assert parsed["take_profit"] == "sell 1 share at 192.50"
        # the Take-Profit header must not leak into the detail paragraph
        assert "192.50" not in parsed["detail"]

    def test_take_profit_defaults_empty(self):
        raw = "Ticker: NVDA\nDecision: HOLD\nUrgency: LOW\n\nHold."
        parsed = _parse_advice(raw, "NVDA")
        assert parsed["take_profit"] == ""


class TestAnalystSummaryTail:
    def test_returns_table_plus_trailing_conclusion(self):
        report = (
            "Long prose body about the stock.\n\nMore prose.\n\n"
            "| Signal | Direction |\n|---|---|\n| RSI | Bullish |\n\n"
            "FINAL TRANSACTION PROPOSAL: **BUY**\nReasoning: momentum holds.\n"
        )
        tail = _analyst_summary_tail(report)
        assert tail is not None
        assert "| Signal | Direction |" in tail          # the table
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in tail  # the conclusion after it
        assert "Reasoning: momentum holds." in tail
        assert "Long prose body" not in tail             # the prose before it is dropped

    def test_none_without_table(self):
        assert _analyst_summary_tail("Just prose, no table here.") is None

    def test_anchors_on_last_table(self):
        report = "| a | b |\n|---|---|\n\nmid\n\n| c | d |\n|---|---|\n| e | f |\n\nDONE\n"
        tail = _analyst_summary_tail(report)
        assert "| c | d |" in tail and "DONE" in tail
        assert "| a | b |" not in tail and "mid" not in tail


class TestFormatAnalysis:
    def test_feeds_summary_tail_not_full_prose(self):
        result = {
            "_reports": {
                "market_report": (
                    "LONG PROSE that must not be fed to the advisor. " * 20
                    + "\n\n| Metric | Value |\n|---|---|\n| Trend | Up |\n\n"
                    + "FINAL ASSESSMENT: accumulate.\n"
                ),
            },
        }
        text = _format_analysis(result)
        assert "| Trend | Up |" in text              # the summary table is fed
        assert "FINAL ASSESSMENT: accumulate." in text  # its trailing conclusion too
        assert "LONG PROSE" not in text              # the prose before the table is not

    def test_report_without_table_falls_back_to_head(self):
        result = {"_reports": {"news_report": "Headline-only note, no table."}}
        text = _format_analysis(result)
        assert "Headline-only note" in text

    def test_missing_table_warns(self, caplog):
        # The fallback swaps the report's conclusion for its opening. That is
        # invisible in the advice itself, so it must be greppable in the log.
        result = {"ticker": "NVDA", "_reports": {"news_report": "No table here."}}
        with caplog.at_level(logging.WARNING, logger="watchy.advisor"):
            _format_analysis(result)
        assert "ADVISOR_TAIL_FALLBACK" in caplog.text
        assert "NVDA" in caplog.text
        assert "News Analyst" in caplog.text

    def test_table_present_does_not_warn(self, caplog):
        result = {
            "ticker": "NVDA",
            "_reports": {"news_report": "| A | B |\n|---|---|\n| 1 | 2 |\nDone."},
        }
        with caplog.at_level(logging.WARNING, logger="watchy.advisor"):
            _format_analysis(result)
        assert "ADVISOR_TAIL_FALLBACK" not in caplog.text

    def test_includes_trader_plan(self):
        result = {"trader_plan": "**Action**: Buy\n**Entry Price**: 180"}
        text = _format_analysis(result)
        assert "Entry Price" in text and "180" in text

    def test_falls_back_to_recommendations(self):
        result = {
            "_reports": {},
            "recommendations": ["[Market] truncated...", "[Sentiment] truncated..."],
        }
        text = _format_analysis(result)
        assert "truncated" in text

    def test_includes_risk_assessment(self):
        result = {
            "_reports": {},
            "risk_assessment": "Risk is moderate due to sector rotation.",
        }
        text = _format_analysis(result)
        assert "Risk is moderate" in text

    def test_includes_decision(self):
        result = {
            "_reports": {},
            "_decision_raw": "FINAL: BUY with 2% position size.",
        }
        text = _format_analysis(result)
        assert "FINAL: BUY" in text

    def test_includes_sepa_stage(self):
        result = {
            "_reports": {},
            "stage_context": {"sepa_stage": 2},
        }
        text = _format_analysis(result)
        assert "Advancing" in text

    def test_empty_result_returns_json(self):
        result = {}
        text = _format_analysis(result)
        assert text == "{}"


class _FakeSource:
    """Minimal PositionSource stand-in for take-profit guidance tests."""

    def __init__(self, pos):
        self._pos = pos

    def get_position(self, ticker):
        return self._pos


def _held(gain_pct):
    from watchy.positions import Position

    p = Position(ticker="NVDA", quantity=3, average_cost=163.33, current_price=189.0)
    p.unrealized_pnl_pct = gain_pct
    return p


class TestTakeProfitGuidance:
    def _config(self, enabled, floor=10.0):
        from watchy.config import TakeProfitConfig, TickerConfig, WatchyConfig

        return WatchyConfig(
            watchlist=[TickerConfig(ticker="NVDA")],
            take_profit=TakeProfitConfig(enabled=enabled, floor_gain_pct=floor),
        )

    def _bundle(self):
        from watchy.indicators import IndicatorBundle

        return IndicatorBundle(ticker="NVDA", current_price=189.0, avg_atr_20d=5.0)

    def test_disabled_returns_empty(self):
        src = _FakeSource(_held(15.7))
        out = _take_profit_guidance(
            "NVDA", "target $200", src, self._config(enabled=False), self._bundle()
        )
        assert out == ""

    def test_below_floor_returns_empty(self):
        src = _FakeSource(_held(5.0))
        out = _take_profit_guidance(
            "NVDA", "target $200", src, self._config(enabled=True), self._bundle()
        )
        assert out == ""

    def test_not_held_returns_empty(self):
        src = _FakeSource(None)
        out = _take_profit_guidance(
            "NVDA", "target $200", src, self._config(enabled=True), self._bundle()
        )
        assert out == ""

    def test_in_zone_builds_guidance(self):
        src = _FakeSource(_held(15.7))
        out = _take_profit_guidance(
            "NVDA", "resistance at $200", src, self._config(enabled=True), self._bundle()
        )
        assert "TAKE-PROFIT ZONE ACTIVE" in out
        assert "+10% take-profit floor" in out   # the arming fact...
        assert "15.7" not in out                 # ...but never the magnitude (#30)

    def test_anchors_on_the_broker_mark_not_the_bundle(self):
        """Regression: the limit followed the (staler, lower) yfinance quote.

        The gain is derived from the broker's mark, so the limit has to be too —
        anchoring on a lower feed emits a limit that fills cheaper than intended.
        """
        from watchy.indicators import IndicatorBundle

        src = _FakeSource(_held(15.7))  # broker mark 189.0
        stale = IndicatorBundle(ticker="NVDA", current_price=185.0, avg_atr_20d=5.0)
        out = _take_profit_guidance(
            "NVDA", "resistance at $200", src, self._config(enabled=True), stale
        )
        assert "Current price $189.00" in out
        assert "$185.00" not in out
        # limit = 189 + 1.5x5 = 196.50 (not 192.50), stretch = 189 + 3x5 = 204.00
        assert "$196.50" in out and "$204.00" in out

    def test_per_ticker_floor_override(self):
        from watchy.config import TakeProfitConfig, TickerConfig, WatchyConfig

        cfg = WatchyConfig(
            watchlist=[TickerConfig(ticker="NVDA", take_profit_floor_gain_pct=20.0)],
            take_profit=TakeProfitConfig(enabled=True, floor_gain_pct=10.0),
        )
        src = _FakeSource(_held(15.7))  # above global 10 but below per-ticker 20
        assert _take_profit_guidance("NVDA", "target $200", src, cfg, self._bundle()) == ""


class TestStandingPromptGainAnchor:
    """The always-on take-profit clause must not anchor on the gain % (#30).

    This clause ships on EVERY advisor call, gate armed or not, and it used to
    point the model at the position block's "Unrealized P&L" with a "roughly
    15%+" anchor. Under highest-cost-first selling that figure ratchets up with
    each trim at an unchanged price, so it cannot be a threshold.
    """

    def test_no_numeric_gain_anchor(self):
        from watchy.advisor import ADVISOR_PROMPT

        assert "15%+" not in ADVISOR_PROMPT
        assert "MEANINGFUL\nunrealized gain" not in ADVISOR_PROMPT

    def test_carries_the_hifo_caveat(self):
        from watchy.advisor import ADVISOR_PROMPT

        assert "COST-BASIS CAVEAT" in ADVISOR_PROMPT
        assert "highest-cost-first" in ADVISOR_PROMPT

    def test_still_asks_for_trims_on_extended_winners(self):
        # Removing the magnitude must not remove the willingness to bank a gain:
        # the user's pain is selling too late, not too early.
        from watchy.advisor import ADVISOR_PROMPT

        assert "IN PROFIT" in ADVISOR_PROMPT
        assert "lean toward TRIM" in ADVISOR_PROMPT


class TestPostJsonRetry:
    """Transient HTTP failures must not cost a ticker its whole advice (#28).

    Before this, all three provider calls were a bare urlopen(timeout=30) with
    no retry: one dropped connection lost the advice card AND the #16
    derived-target write. Observed 2026-08-07 on NVT/TSM/COHR.
    """

    def _resp(self, payload=b'{"ok": 1}'):
        resp = MagicMock()
        resp.read.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        return resp

    def test_returns_payload_on_first_try(self):
        from watchy.advisor import _post_json
        with patch("urllib.request.urlopen", return_value=self._resp()) as u:
            assert _post_json("http://x", b"{}", {}) == {"ok": 1}
        assert u.call_count == 1

    def test_retries_a_dropped_connection_then_succeeds(self):
        # http.client.RemoteDisconnected is exactly the _read_status failure
        # seen in the 2026-08-07 tracebacks; urllib does NOT wrap it in URLError.
        import http.client
        from watchy.advisor import _post_json
        side = [http.client.RemoteDisconnected("peer closed"), self._resp()]
        with patch("urllib.request.urlopen", side_effect=side) as u, \
             patch("watchy.advisor.time.sleep") as sleep:
            assert _post_json("http://x", b"{}", {}) == {"ok": 1}
        assert u.call_count == 2
        assert sleep.call_count == 1

    def test_retries_a_socket_timeout(self):
        from watchy.advisor import _post_json
        with patch("urllib.request.urlopen",
                   side_effect=[TimeoutError("timed out"), self._resp()]) as u, \
             patch("watchy.advisor.time.sleep"):
            assert _post_json("http://x", b"{}", {}) == {"ok": 1}
        assert u.call_count == 2

    def test_gives_up_after_the_attempt_budget(self):
        import http.client
        from watchy.advisor import _post_json
        boom = http.client.RemoteDisconnected("peer closed")
        with patch("urllib.request.urlopen", side_effect=boom) as u, \
             patch("watchy.advisor.time.sleep"):
            with pytest.raises(http.client.RemoteDisconnected):
                _post_json("http://x", b"{}", {}, attempts=3)
        assert u.call_count == 3

    def test_retries_429_and_5xx(self):
        import urllib.error
        from watchy.advisor import _post_json
        for code in (429, 500, 503):
            err = urllib.error.HTTPError("http://x", code, "boom", {}, None)
            with patch("urllib.request.urlopen",
                       side_effect=[err, self._resp()]) as u, \
                 patch("watchy.advisor.time.sleep"):
                assert _post_json("http://x", b"{}", {}) == {"ok": 1}
            assert u.call_count == 2, code

    def test_does_not_retry_a_real_4xx(self):
        # A bad key or malformed request will never succeed — fail fast instead
        # of burning the backoff budget.
        import urllib.error
        from watchy.advisor import _post_json
        err = urllib.error.HTTPError("http://x", 401, "unauthorized", {}, None)
        with patch("urllib.request.urlopen", side_effect=err) as u, \
             patch("watchy.advisor.time.sleep"):
            with pytest.raises(urllib.error.HTTPError):
                _post_json("http://x", b"{}", {})
        assert u.call_count == 1

    def test_backoff_grows_between_attempts(self):
        import http.client
        from watchy.advisor import _post_json
        boom = http.client.RemoteDisconnected("peer closed")
        with patch("urllib.request.urlopen", side_effect=boom), \
             patch("watchy.advisor.time.sleep") as sleep:
            with pytest.raises(http.client.RemoteDisconnected):
                _post_json("http://x", b"{}", {}, attempts=3)
        assert [c.args[0] for c in sleep.call_args_list] == [2.0, 4.0]


class _AdviceSource:
    """PositionSource stand-in for end-to-end get_advice() tests."""

    def __init__(self, pos=None):
        self._pos = pos

    def get_position(self, ticker):
        return self._pos

    def format_position_context(self, ticker):
        return "1 share @ $220.00"

    def format_portfolio_context(self):
        return "Total value: $5,835.77"


_ADVICE_WITH_TP = (
    "Ticker: COHR\n"
    "Decision: HOLD\n"
    "Urgency: LOW\n"
    "Target: 260.00\n"
    "Take-Profit: sell 1 share at 295.00\n"
    "\n"
    "A detail paragraph explaining the call."
)


def _advice_config(tp_enabled, ticker="COHR"):
    from watchy.config import (
        LLMConfig, TakeProfitConfig, TickerConfig, WatchyConfig,
    )

    return WatchyConfig(
        watchlist=[TickerConfig(ticker=ticker)],
        llm=LLMConfig(provider="gemini", model="gemini-3.5-flash", api_key="k"),
        take_profit=TakeProfitConfig(enabled=tp_enabled, floor_gain_pct=10.0),
    )


class TestVolunteeredTakeProfitDropped:
    """A Take-Profit line only counts when the #28 gain-gate armed the zone.

    With no zone armed the model fills the field unprompted anyway, and on a
    1-share holding "sell 1 share at X" is a FULL EXIT — which notify.py renders
    under the Take-Profit heading next to a HOLD decision. Observed on COHR in 8
    of 10 volunteered emissions, 2026-08-31.
    """

    def test_dropped_when_zone_inactive(self):
        import watchy.advisor as adv

        with patch.object(adv, "_call_gemini", return_value=_ADVICE_WITH_TP):
            out = adv.get_advice(
                "COHR", {}, _AdviceSource(), _advice_config(tp_enabled=False)
            )
        assert out["decision"] == "HOLD"
        assert out["take_profit"] == ""

    def test_kept_when_zone_active(self):
        import watchy.advisor as adv
        from watchy.indicators import IndicatorBundle

        pos = _held(15.7)            # NVDA, 3 shares, gain over the 10% floor
        cfg = _advice_config(tp_enabled=True, ticker="NVDA")
        bundle = IndicatorBundle(ticker="NVDA", current_price=189.0, avg_atr_20d=5.0)
        advice = _ADVICE_WITH_TP.replace("Ticker: COHR", "Ticker: NVDA")

        with patch.object(adv, "_call_gemini", return_value=advice):
            out = adv.get_advice(
                "NVDA", {}, _AdviceSource(pos), cfg, indicator_bundle=bundle
            )
        assert out["take_profit"] == "sell 1 share at 295.00"

    def test_untouched_when_model_wrote_na(self):
        import watchy.advisor as adv

        na = _ADVICE_WITH_TP.replace("sell 1 share at 295.00", "N/A")
        with patch.object(adv, "_call_gemini", return_value=na):
            out = adv.get_advice(
                "COHR", {}, _AdviceSource(), _advice_config(tp_enabled=False)
            )
        assert out["take_profit"] == "N/A"


class TestGeminiPerModelPricing:
    """GEMINICOST must follow llm.model — one hardcoded tier under-reported ~17%."""

    def test_35_flash_tier(self):
        assert _gemini_cost_usd(1_000_000, 0, 0, "gemini-3.5-flash") == pytest.approx(1.50)
        assert _gemini_cost_usd(0, 1_000_000, 0, "gemini-3.5-flash") == pytest.approx(9.00)

    def test_37_flash_is_the_cheaper_tier(self):
        assert _gemini_cost_usd(1_000_000, 0, 0, "gemini-3.7-flash") == pytest.approx(0.75)
        assert _gemini_cost_usd(0, 1_000_000, 0, "gemini-3.7-flash") == pytest.approx(3.75)

    def test_38_flash_shares_the_37_tier(self):
        assert _gemini_cost_usd(1_000_000, 0, 0, "gemini-3.8-flash") == pytest.approx(0.75)
        assert _gemini_cost_usd(0, 1_000_000, 0, "gemini-3.8-flash") == pytest.approx(3.75)

    def test_thinking_bills_at_the_output_rate(self):
        assert _gemini_cost_usd(0, 0, 1_000_000, "gemini-3.7-flash") == pytest.approx(3.75)

    def test_unknown_model_falls_back_to_35(self):
        # Older callers pass no model at all and must keep their previous numbers.
        assert _gemini_cost_usd(1_000_000, 0, 0) == pytest.approx(1.50)
        assert _gemini_cost_usd(1_000_000, 0, 0, "gemini-9-future") == pytest.approx(1.50)


class TestGeminiThinkingLevelsPerModel:
    """3.7-flash dropped "minimal" — sending it is an HTTP 400 (verified 2026-08-31)."""

    def test_off_still_minimal_on_35(self):
        assert _gemini_thinking_config("off", "gemini-3.5-flash") == {"thinkingLevel": "minimal"}

    def test_off_falls_back_to_low_on_37(self):
        assert _gemini_thinking_config("off", "gemini-3.7-flash") == {"thinkingLevel": "low"}

    def test_minimal_clamped_on_37(self):
        # Asking for minimal explicitly must not produce a 400 either.
        assert _gemini_thinking_config("minimal", "gemini-3.7-flash") == {"thinkingLevel": "low"}

    def test_supported_levels_pass_through_on_37(self):
        for lvl in ("low", "medium", "high"):
            assert _gemini_thinking_config(lvl, "gemini-3.7-flash") == {"thinkingLevel": lvl}

    def test_38_behaves_like_37(self):
        # 3.8-flash rejects minimal the same way (HTTP 400, verified 2026-09-02).
        assert _gemini_thinking_config("off", "gemini-3.8-flash") == {"thinkingLevel": "low"}
        assert _gemini_thinking_config("minimal", "gemini-3.8-flash") == {"thinkingLevel": "low"}
        for lvl in ("low", "medium", "high"):
            assert _gemini_thinking_config(lvl, "gemini-3.8-flash") == {"thinkingLevel": lvl}


class TestPromptGuardrails:
    """G1 dropped (single-sector book made it a constant); G3 states its reason."""

    def test_sector_lean_guard_is_gone(self):
        from watchy.advisor import ADVISOR_PROMPT

        assert "already heavy in this name or sector" not in ADVISOR_PROMPT
        assert "Avoid over-concentration in any single sector" not in ADVISOR_PROMPT

    def test_portfolio_composition_sentence_kept(self):
        from watchy.advisor import ADVISOR_PROMPT

        assert "Consider the user's overall portfolio composition" in ADVISOR_PROMPT

    def test_concentration_math_guard_kept(self):
        # G2 is what still restrains a trim-for-concentration call.
        from watchy.advisor import ADVISOR_PROMPT

        assert "CRITICAL — concentration math" in ADVISOR_PROMPT

    def test_odd_lot_guard_gives_the_limit_order_reason(self):
        from watchy.advisor import ADVISOR_PROMPT

        assert "ODD-LOT / TINY-POSITION GUARD" in ADVISOR_PROMPT
        assert "WHOLE-SHARE SELL-LIMIT" in ADVISOR_PROMPT
        assert "MARKET order" in ADVISOR_PROMPT

    def test_high_priced_share_may_be_market_trimmed(self):
        # User's call 2026-08-31: a >= $1,000 share may be trimmed fractionally,
        # accepting that it executes as a market order with no limit price.
        from watchy.advisor import ADVISOR_PROMPT

        assert "$1,000 per share" in ADVISOR_PROMPT
        assert "MARKET sell" in ADVISOR_PROMPT


class _RecordingStore:
    """StateStore stand-in capturing log_advice kwargs."""

    def __init__(self, boom=False):
        self.rows = []
        self._boom = boom

    def log_advice(self, ticker, **kwargs):
        if self._boom:
            raise RuntimeError("disk full")
        self.rows.append({"ticker": ticker, **kwargs})


class TestAdviceLogging:
    """#31: record what was advised, at what price, on what holding.

    Four model evaluations have been decided on proxies because nothing
    persisted the decisions. This is the row a forward-return score reads.
    """

    def test_logs_the_decision_and_the_book_state(self):
        import watchy.advisor as adv
        from watchy.indicators import IndicatorBundle

        store = _RecordingStore()
        pos = _held(15.7)                     # NVDA, 3 shares @ 163.33, mark 189
        cfg = _advice_config(tp_enabled=False, ticker="NVDA")
        bundle = IndicatorBundle(ticker="NVDA", current_price=185.0, avg_atr_20d=5.0)
        advice = _ADVICE_WITH_TP.replace("Ticker: COHR", "Ticker: NVDA")

        with patch.object(adv, "_call_gemini", return_value=advice):
            adv.get_advice(
                "NVDA", {}, _AdviceSource(pos), cfg,
                thinking_level="low", indicator_bundle=bundle,
                store=store, source="tier2",
            )

        assert len(store.rows) == 1
        row = store.rows[0]
        assert row["ticker"] == "NVDA"
        assert row["source"] == "tier2"
        assert row["decision"] == "HOLD"
        assert row["urgency"] == "LOW"
        assert row["target"] == "260.00"
        assert row["quantity"] == 3
        assert row["average_cost"] == 163.33
        assert row["gain_pct"] == 15.7

    def test_price_anchors_on_the_broker_mark_not_the_bundle(self):
        # Same rule the take-profit limit follows: a logged price and a logged
        # gain must come from one feed, or the pair can't be scored later.
        import watchy.advisor as adv
        from watchy.indicators import IndicatorBundle

        store = _RecordingStore()
        pos = _held(15.7)                                  # current_price 189.0
        bundle = IndicatorBundle(ticker="NVDA", current_price=185.0)
        advice = _ADVICE_WITH_TP.replace("Ticker: COHR", "Ticker: NVDA")

        with patch.object(adv, "_call_gemini", return_value=advice):
            adv.get_advice(
                "NVDA", {}, _AdviceSource(pos),
                _advice_config(tp_enabled=False, ticker="NVDA"),
                indicator_bundle=bundle, store=store, source="tier1",
            )
        assert store.rows[0]["price"] == 189.0

    def test_records_model_and_effort_for_attribution(self):
        # A later model switch has to split the history, not contaminate it.
        import watchy.advisor as adv

        store = _RecordingStore()
        with patch.object(adv, "_call_gemini", return_value=_ADVICE_WITH_TP):
            adv.get_advice(
                "COHR", {}, _AdviceSource(), _advice_config(tp_enabled=False),
                thinking_level="low", store=store, source="tier2",
            )
        assert store.rows[0]["model"] == "gemini-3.5-flash"
        assert store.rows[0]["thinking_level"] == "low"

    def test_logs_the_gated_take_profit_not_the_volunteered_one(self):
        # The dropped-TP gate runs first, so the log matches what was sent.
        import watchy.advisor as adv

        store = _RecordingStore()
        with patch.object(adv, "_call_gemini", return_value=_ADVICE_WITH_TP):
            adv.get_advice(
                "COHR", {}, _AdviceSource(), _advice_config(tp_enabled=False),
                store=store, source="tier2",
            )
        assert store.rows[0]["take_profit"] == ""
        assert store.rows[0]["zone_armed"] is False

    def test_zone_armed_when_guidance_injected(self):
        import watchy.advisor as adv
        from watchy.indicators import IndicatorBundle

        store = _RecordingStore()
        pos = _held(15.7)
        cfg = _advice_config(tp_enabled=True, ticker="NVDA")
        bundle = IndicatorBundle(ticker="NVDA", current_price=189.0, avg_atr_20d=5.0)
        advice = _ADVICE_WITH_TP.replace("Ticker: COHR", "Ticker: NVDA")

        with patch.object(adv, "_call_gemini", return_value=advice):
            adv.get_advice(
                "NVDA", {}, _AdviceSource(pos), cfg,
                indicator_bundle=bundle, store=store, source="take_profit_zone",
            )
        assert store.rows[0]["zone_armed"] is True
        assert store.rows[0]["take_profit"] == "sell 1 share at 295.00"

    def test_no_store_is_not_an_error(self):
        # Scripts and the A/B harness call get_advice with no state store.
        import watchy.advisor as adv

        with patch.object(adv, "_call_gemini", return_value=_ADVICE_WITH_TP):
            out = adv.get_advice(
                "COHR", {}, _AdviceSource(), _advice_config(tp_enabled=False)
            )
        assert out["decision"] == "HOLD"

    def test_log_failure_never_loses_the_advice(self, caplog):
        # The advice card is already paid for and on its way to Telegram.
        # Instrumentation must not be able to take it down.
        import watchy.advisor as adv

        store = _RecordingStore(boom=True)
        with patch.object(adv, "_call_gemini", return_value=_ADVICE_WITH_TP):
            out = adv.get_advice(
                "COHR", {}, _AdviceSource(), _advice_config(tp_enabled=False),
                store=store, source="tier2",
            )
        assert out["decision"] == "HOLD"
        assert "Advice log write failed" in caplog.text

    def test_position_lookup_failure_still_logs_the_decision(self):
        # A broker hiccup must degrade the row, not skip it.
        import watchy.advisor as adv

        class _Broken(_AdviceSource):
            def get_position(self, ticker):
                raise RuntimeError("schwab 500")

        store = _RecordingStore()
        with patch.object(adv, "_call_gemini", return_value=_ADVICE_WITH_TP):
            adv.get_advice(
                "COHR", {}, _Broken(), _advice_config(tp_enabled=False),
                store=store, source="tier2",
            )
        assert store.rows[0]["decision"] == "HOLD"
        assert store.rows[0]["quantity"] is None
