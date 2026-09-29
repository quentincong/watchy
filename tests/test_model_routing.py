"""Tests for the TradingAgents LLM shim and the model-routing config (no LLM calls)."""

import types

import pytest

from watchy import llm_shim
from watchy.config import AdvisorConfig, WatchyConfig, _merge_secrets


class TestEffortTag:
    def test_round_trip(self):
        tagged = llm_shim.tag_effort("deepseek-flash", "max")
        assert tagged == "deepseek-flash@max"
        assert llm_shim.split_effort(tagged) == ("deepseek-flash", "max")

    def test_empty_effort_leaves_model_alone(self):
        assert llm_shim.tag_effort("deepseek-flash", "") == "deepseek-flash"
        assert llm_shim.tag_effort("deepseek-flash", None) == "deepseek-flash"
        assert llm_shim.split_effort("deepseek-flash") == ("deepseek-flash", None)

    def test_effort_is_normalised(self):
        assert llm_shim.tag_effort("m", " MAX ") == "m@max"


class TestWrapCreateLlmClient:
    def _recording(self):
        calls = []

        def original(provider, model, base_url=None, **kwargs):
            calls.append((provider, model, base_url, kwargs))
            return "client"

        return original, calls

    def test_tagged_model_gets_reasoning_effort(self):
        original, calls = self._recording()
        wrapped = llm_shim.wrap_create_llm_client(original)
        assert wrapped("deepseek", "deepseek-flash@max", None, callbacks=["cb"]) == "client"
        assert calls == [("deepseek", "deepseek-flash", None,
                          {"callbacks": ["cb"], "reasoning_effort": "max"})]

    def test_untagged_model_passes_through_unchanged(self):
        # The quick role: same model id, no tag, no reasoning_effort.
        original, calls = self._recording()
        wrapped = llm_shim.wrap_create_llm_client(original)
        wrapped(provider="deepseek", model="deepseek-flash", base_url=None)
        assert calls == [("deepseek", "deepseek-flash", None, {})]

    def test_wrapping_is_idempotent(self):
        original, _ = self._recording()
        once = llm_shim.wrap_create_llm_client(original)
        assert llm_shim.wrap_create_llm_client(once) is once


class TestDeepSeekCapabilities:
    def _caps(self, by_id):
        thinking = object()
        return types.SimpleNamespace(_BY_ID=dict(by_id), _DEEPSEEK_THINKING=thinking), thinking

    def test_registers_canonical_ids_as_thinking(self):
        # deepseek-flash fell through to _DEFAULT (forced tool_choice -> 400 ->
        # free-text fallback on every structured node) after the 9/10 rename.
        caps, thinking = self._caps({"deepseek-v4-flash": "v4"})
        added = llm_shim.register_deepseek_capabilities(caps)
        assert added == ["deepseek-flash", "deepseek-pro"]
        assert caps._BY_ID["deepseek-flash"] is thinking
        assert caps._BY_ID["deepseek-v4-flash"] == "v4"

    def test_keeps_an_entry_ta_already_defines(self):
        caps, _ = self._caps({"deepseek-flash": "ta-own"})
        assert llm_shim.register_deepseek_capabilities(caps) == ["deepseek-pro"]
        assert caps._BY_ID["deepseek-flash"] == "ta-own"


class TestRoutingConfig:
    def test_defaults_keep_historical_behaviour(self):
        cfg = WatchyConfig()
        assert cfg.advisor.primary == "gemini"
        assert cfg.pipeline.deep_reasoning_effort == ""
        assert cfg.openrouter.api_key == ""

    def test_parses_pipeline_and_advisor_sections(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text(
            "watchlist: [NVDA]\n"
            "pipeline: {deep_reasoning_effort: max}\n"
            "advisor: {primary: Qwen, qwen_reasoning_budget: 6000}\n",
            encoding="utf-8",
        )
        cfg = WatchyConfig.from_yaml(p)
        assert cfg.pipeline.deep_reasoning_effort == "max"
        assert cfg.advisor.primary == "qwen"
        assert cfg.advisor.qwen_reasoning_budget == 6000
        assert cfg.advisor.fallback_to_gemini is True

    def test_advisor_primary_typo_fails_loudly(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("advisor: {primary: qwn}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="advisor.primary"):
            WatchyConfig.from_yaml(p)

    def test_openrouter_key_comes_from_a_top_level_secrets_section(self, tmp_path):
        # Never under llm: — LLMConfig(**secrets["llm"]) rejects unknown keys.
        s = tmp_path / "secrets.yaml"
        s.write_text(
            "llm: {provider: gemini, model: gemini-3.5-flash, api_key: g}\n"
            "openrouter: {api_key: or-key}\n",
            encoding="utf-8",
        )
        cfg = _merge_secrets(WatchyConfig(), str(s))
        assert cfg.openrouter.api_key == "or-key"
        assert cfg.llm.api_key == "g"

    def test_repo_config_selects_qwen_and_default_effort(self):
        # max was tested 2026-09-29 and not enabled (PM unchanged, RM more bullish).
        from pathlib import Path

        cfg = WatchyConfig.from_yaml(Path(__file__).resolve().parent.parent / "config.yaml")
        assert cfg.advisor.primary == "qwen"
        assert cfg.advisor.fallback_to_gemini is True
        assert cfg.pipeline.deep_reasoning_effort == ""

    def test_advisor_config_default_model(self):
        assert AdvisorConfig().qwen_model == "qwen/qwen3.7-max"
