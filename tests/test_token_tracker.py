"""Tests for the TokenCostTracker callback: usage extraction, attribution, cost."""

from datetime import datetime, timezone
from types import SimpleNamespace

from watchy.token_tracker import (
    _PRICES_FLAT,
    _PRICES_OFFPEAK,
    _PRICES_PEAK,
    _PRICES_V41_OFFPEAK,
    _PRICES_V41_PEAK,
    _PRICES_V41_ROUTED_OFFPEAK,
    TokenCostTracker,
    _cost_usd,
    _extract_usage,
    _price_tier,
    _prices_at,
)


def _resp(model, input_tok, output_tok, cache_read=0, reasoning=0):
    """Build a minimal LLMResult-like object with usage_metadata on the message."""
    msg = SimpleNamespace(
        usage_metadata={
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "input_token_details": {"cache_read": cache_read},
            "output_token_details": {"reasoning": reasoning},
        }
    )
    gen = SimpleNamespace(message=msg)
    return SimpleNamespace(generations=[[gen]], llm_output={"model_name": model})


class TestPriceTier:
    def test_pro_detected_before_routing_cutover(self):
        before = datetime(2026, 9, 14, 3, 59, tzinfo=timezone.utc)
        assert _price_tier("deepseek-v4-pro", before) == "pro"

    def test_pro_alias_becomes_flash_at_routing_cutover(self):
        after = datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)
        assert _price_tier("deepseek-v4-pro", after) == "flash"

    def test_flash_default(self):
        assert _price_tier("deepseek-v4-flash") == "flash"
        assert _price_tier("") == "flash"
        assert _price_tier("something-unknown") == "flash"


class TestPricingWindow:
    """DeepSeek went peak/off-peak at 2026-08-16 16:00 UTC (2x on 01:00-04:00
    and 06:00-10:00 UTC). These pin the table selection to explicit instants so
    they don't silently change meaning as the clock moves."""

    def test_flat_before_cutover(self):
        before = datetime(2026, 8, 16, 15, 59, tzinfo=timezone.utc)
        assert _prices_at(before) is _PRICES_FLAT
        assert _prices_at(before)["flash"]["out"] == 0.28

    def test_offpeak_after_cutover(self):
        after = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        assert _prices_at(after) is _PRICES_OFFPEAK
        assert _prices_at(after)["flash"]["out"] == 0.66

    def test_peak_hours_priced_double(self):
        for hour in (1, 2, 3, 6, 7, 8, 9):
            peak = datetime(2026, 8, 17, hour, 30, tzinfo=timezone.utc)
            assert _prices_at(peak) is _PRICES_PEAK
        for tier in ("pro", "flash"):
            for key in ("in", "cache", "out"):
                assert _PRICES_PEAK[tier][key] == 2 * _PRICES_OFFPEAK[tier][key]

    def test_tier2_start_is_off_peak(self):
        # tier2_time_utc defaults to 10:02 precisely to clear the 06:00-10:00
        # window; if this ever fails the whole batch is being billed at 2x.
        assert _prices_at(
            datetime(2026, 8, 17, 10, 2, tzinfo=timezone.utc)
        ) is _PRICES_OFFPEAK

    def test_tier1_session_is_off_peak(self):
        for hour in (13, 16, 19):
            assert _prices_at(
                datetime(2026, 8, 17, hour, 0, tzinfo=timezone.utc)
            ) is _PRICES_OFFPEAK

    def test_v41_flash_prices_start_at_announced_cutover(self):
        before = datetime(2026, 9, 10, 3, 59, tzinfo=timezone.utc)
        after = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)
        assert _prices_at(before) is _PRICES_PEAK
        assert _prices_at(after) is _PRICES_V41_OFFPEAK
        assert _prices_at(after)["flash"] == {
            "in": 0.15, "cache": 0.003, "out": 0.60,
        }

    def test_v41_peak_is_double_offpeak(self):
        peak = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)
        assert _prices_at(peak) is _PRICES_V41_PEAK
        for key in ("in", "cache", "out"):
            assert _PRICES_V41_PEAK["flash"][key] == 2 * _PRICES_V41_OFFPEAK["flash"][key]

    def test_weekend_is_always_offpeak(self):
        saturday_peak_hour = datetime(2026, 9, 12, 6, 30, tzinfo=timezone.utc)
        assert _prices_at(saturday_peak_hour) is _PRICES_V41_OFFPEAK

    def test_pro_table_matches_flash_after_routing_cutover(self):
        after = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        assert _prices_at(after) is _PRICES_V41_ROUTED_OFFPEAK
        assert _prices_at(after)["pro"] == _prices_at(after)["flash"]


class TestCost:
    def test_flash_cost_math(self):
        now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        p = _prices_at(now)["flash"]
        expected = p["in"] + p["out"]  # 1M miss input + 1M output, no cache
        assert abs(_cost_usd("flash", 1_000_000, 0, 1_000_000, now) - expected) < 1e-9

    def test_cache_hit_is_cheaper(self):
        now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        full = _cost_usd("flash", 1_000_000, 0, 0, now)
        cached = _cost_usd("flash", 1_000_000, 1_000_000, 0, now)
        assert cached < full
        assert abs(cached - _prices_at(now)["flash"]["cache"]) < 1e-9

    def test_pro_dearer_than_flash_before_routing_cutover(self):
        before = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        assert _cost_usd("pro", 1_000_000, 0, 1_000_000, before) > _cost_usd(
            "flash", 1_000_000, 0, 1_000_000, before
        )

    def test_pro_alias_uses_flash_price_after_routing_cutover(self):
        after = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        assert _cost_usd("deepseek-v4-pro", 1_000_000, 0, 1_000_000, after) == _cost_usd(
            "deepseek-flash", 1_000_000, 0, 1_000_000, after
        )


class TestExtractUsage:
    def test_reads_usage_metadata(self):
        inp, cached, out, reason, model = _extract_usage(
            _resp("deepseek-v4-pro", 100, 40, 25, reasoning=30)
        )
        assert (inp, cached, out, reason, model) == (100, 25, 40, 30, "deepseek-v4-pro")

    def test_reasoning_defaults_to_zero(self):
        _, _, _, reason, _ = _extract_usage(_resp("deepseek-v4-flash", 100, 40))
        assert reason == 0

    def test_openai_style_fallback(self):
        resp = SimpleNamespace(
            generations=[[SimpleNamespace(message=SimpleNamespace(usage_metadata=None))]],
            llm_output={
                "model_name": "deepseek-v4-flash",
                "token_usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 30},
                    "completion_tokens_details": {"reasoning_tokens": 35},
                },
            },
        )
        inp, cached, out, reason, model = _extract_usage(resp)
        assert (inp, cached, out, reason) == (200, 30, 50, 35)

    def test_empty_response_is_zero(self):
        resp = SimpleNamespace(generations=[], llm_output={})
        assert _extract_usage(resp) == (0, 0, 0, 0, "")


class TestTrackerAttribution:
    def _run(self, tracker, run_id, model, node, input_tok, output_tok, cache=0, reasoning=0):
        tracker.on_chat_model_start(
            {}, [], run_id=run_id, metadata={"langgraph_node": node, "ls_model_name": model}
        )
        tracker.on_llm_end(_resp(model, input_tok, output_tok, cache, reasoning), run_id=run_id)

    def test_attributes_by_model_and_node(self):
        t = TokenCostTracker()
        self._run(t, "r1", "deepseek-flash", "Market Analyst", 1000, 200)
        self._run(t, "r2", "deepseek-flash", "Research Manager", 500, 300)

        assert t.by_node["Market Analyst"].calls == 1
        assert t.by_node["Research Manager"].input == 500
        assert t.by_model["flash"].output == 500
        assert t.by_model["flash"].usd > 0
        assert t.total_usd() == t.by_model["flash"].usd

    def test_reasoning_attributed_and_reported(self):
        t = TokenCostTracker()
        self._run(t, "r1", "deepseek-flash", "Portfolio Manager", 500, 300, reasoning=210)
        assert t.by_node["Portfolio Manager"].reasoning == 210
        assert t.by_model["flash"].reasoning == 210
        # reasoning surfaces in the greppable dict (a subset of "out")
        d = t.by_node["Portfolio Manager"].as_dict()
        assert d["reason"] == 210 and d["out"] == 300

    def test_node_falls_back_to_unknown(self):
        t = TokenCostTracker()
        t.on_chat_model_start({}, [], run_id="x", metadata=None)
        t.on_llm_end(_resp("deepseek-v4-flash", 10, 5), run_id="x")
        assert t.by_node["unknown"].calls == 1

    def test_zero_usage_not_recorded(self):
        t = TokenCostTracker()
        t.on_chat_model_start({}, [], run_id="x", metadata={"langgraph_node": "n"})
        t.on_llm_end(SimpleNamespace(generations=[], llm_output={}), run_id="x")
        assert "n" not in t.by_node

    def test_log_summary_no_calls_is_safe(self):
        TokenCostTracker().log_summary("AAPL", "lbl")  # must not raise
