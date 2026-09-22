"""Watchy 2.0 Phase 2 — Weekly Full plan generation, persistence, message."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.fixtures_v2 import ADVICE_WITH_BLOCK
from watchy.config import LLMConfig, TickerConfig, WatchyConfig
from watchy.indicators import IndicatorBundle
from watchy.orchestrator import RiskMode
from watchy.plan import BLOCK_START, PlanStatus, parse_plan_block
from watchy.state import StateStore
from watchy.tier2 import run_daily_scan
from watchy.weekly import build_weekly_plan, failed_weekly_plan, plan_validity

MON = datetime(2026, 9, 21, 10, 30, tzinfo=timezone.utc)


def _advice(text=ADVICE_WITH_BLOCK, decision="BUY", urgency="MEDIUM"):
    return {
        "decision": decision,
        "urgency": urgency,
        "detail": "x",
        "_plan_block": parse_plan_block(text),
        "_raw": text,
        "_advice_log_id": 11,
    }


class TestPlanValidity:
    def test_monday_run_valid_all_week(self):
        assert plan_validity(MON) == ("2026-09-21", "2026-09-25")

    def test_midweek_force_valid_from_today(self):
        wed = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)
        assert plan_validity(wed) == ("2026-09-23", "2026-09-25")

    def test_friday_during_session_keeps_this_week(self):
        fri = datetime(2026, 9, 25, 19, 59, tzinfo=timezone.utc)   # 15:59 ET
        assert plan_validity(fri) == ("2026-09-25", "2026-09-25")

    def test_friday_after_close_plans_next_week(self):
        fri = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)    # 16:00 ET close
        assert plan_validity(fri) == ("2026-09-28", "2026-10-02")
        late = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)    # 21:00 ET Friday
        assert plan_validity(late) == ("2026-09-28", "2026-10-02")

    def test_holiday_short_week_after_thursday_close(self):
        pytest.importorskip("exchange_calendars")
        # Good Friday 2026-04-03: Thursday 04-02 is the week's last session.
        during = datetime(2026, 4, 2, 18, 0, tzinfo=timezone.utc)
        assert plan_validity(during) == ("2026-04-02", "2026-04-02")
        after = datetime(2026, 4, 2, 20, 30, tzinfo=timezone.utc)  # 16:30 EDT
        assert plan_validity(after) == ("2026-04-06", "2026-04-10")
        friday = datetime(2026, 4, 3, 15, 0, tzinfo=timezone.utc)  # holiday
        assert plan_validity(friday) == ("2026-04-06", "2026-04-10")

    def test_early_close_uses_calendar(self):
        pytest.importorskip("exchange_calendars")
        # 2026-11-27 (day after Thanksgiving) closes 13:00 ET = 18:00 UTC.
        assert plan_validity(datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc)) == (
            "2026-11-30", "2026-12-04")
        assert plan_validity(datetime(2026, 11, 27, 17, 30, tzinfo=timezone.utc)) == (
            "2026-11-27", "2026-11-27")

    def test_fallback_without_calendar(self, monkeypatch):
        import watchy.market_calendar as mc
        monkeypatch.setattr(mc, "get_calendar", lambda: None)
        assert plan_validity(datetime(2026, 9, 25, 19, 0, tzinfo=timezone.utc)) == (
            "2026-09-25", "2026-09-25")
        assert plan_validity(datetime(2026, 9, 25, 20, 5, tzinfo=timezone.utc)) == (
            "2026-09-28", "2026-10-02")

    def test_weekend_force_plans_next_week(self):
        sat = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
        assert plan_validity(sat) == ("2026-09-28", "2026-10-02")


class TestBuildWeeklyPlan:
    def _build(self, advice, held=True, price=124.0):
        return build_weekly_plan(
            "nvda", advice, {"verdict": "BUY"}, held=held,
            input_price=price, input_price_ts=MON, now=MON,
            source_ref={"run_id": 3},
        )

    def test_valid_plan(self):
        plan = self._build(_advice())
        assert plan.status == PlanStatus.ACTIVE.value, plan.validation_errors
        assert plan.ticker == "NVDA" and plan.upstream_verdict == "BUY"
        assert plan.chase_ceiling == 125.0
        assert plan.source_ref == {"run_id": 3, "advice_log_id": 11}
        assert plan.expires_after_session == "2026-09-25"

    def test_hold_on_unheld_becomes_watch(self):
        plan = self._build(_advice(decision="HOLD", urgency="LOW"), held=False)
        assert plan.decision == "WATCH" and plan.is_valid

    def test_malformed_block_is_invalid(self):
        bad = ADVICE_WITH_BLOCK.replace("Chase-Ceiling: 125.00", "Chase-Ceiling: around 125")
        plan = self._build(_advice(bad))
        assert plan.status == PlanStatus.INVALID.value
        assert any("chase_ceiling" in e for e in plan.validation_errors)

    def test_missing_block_is_invalid(self):
        adv = _advice()
        adv["_plan_block"] = None
        assert "weekly plan block missing" in self._build(adv).validation_errors

    def test_no_advice_is_invalid(self):
        assert not self._build(None).is_valid

    def test_missing_price_is_invalid(self):
        assert not self._build(_advice(), price=None).is_valid

    def test_failed_record(self):
        plan = failed_weekly_plan("NVDA", "TimeoutError: x", now=MON)
        assert not plan.is_valid and plan.validation_errors[0].startswith("weekly run failed")


class TestAdvisorPlanRequest:
    def _cfg(self):
        return WatchyConfig(
            watchlist=[TickerConfig(ticker="NVDA")],
            llm=LLMConfig(provider="gemini", model="gemini-3.5-flash", api_key="k"),
        )

    def _source(self):
        src = MagicMock()
        src.format_position_context.return_value = None
        src.format_portfolio_context.return_value = None
        src.get_position.return_value = None
        return src

    def test_plan_block_requested_parsed_and_stripped(self):
        import watchy.advisor as adv

        with patch.object(adv, "_call_gemini", return_value=ADVICE_WITH_BLOCK) as call:
            out = adv.get_advice("NVDA", {}, self._source(), self._cfg(), plan_request=True)
        prompt = call.call_args.args[0]
        assert BLOCK_START in prompt
        assert out["decision"] == "BUY"
        assert out["_plan_block"].errors == []
        assert "Chase-Ceiling" not in out["detail"]
        assert out["_raw"].endswith("=== END WEEKLY PLAN ===")

    def test_daily_prompt_unchanged(self):
        import watchy.advisor as adv

        with patch.object(adv, "_call_gemini", return_value="Decision: HOLD\nUrgency: LOW") as call:
            out = adv.get_advice("NVDA", {}, self._source(), self._cfg())
        assert BLOCK_START not in call.call_args.args[0]
        assert "_plan_block" not in out

    def test_event_context_injected(self):
        import watchy.advisor as adv

        with patch.object(adv, "_call_gemini", return_value="Decision: HOLD\nUrgency: LOW") as call:
            adv.get_advice("NVDA", {}, self._source(), self._cfg(),
                           event_context="EVENT CONTEXT: macd bearish cross")
        assert "EVENT CONTEXT: macd bearish cross" in call.call_args.args[0]


def _bundle(ticker="NVDA", price=124.0):
    b = IndicatorBundle(ticker=ticker, current_price=price, avg_atr_20d=3.0)
    b.fetched_at = MON
    return b


@pytest.fixture
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    yield s
    s.close()


def _weekly_run(store, config, advice, runner_side_effect=None, tickers=None, post_price=124.0):
    notifier = MagicMock()
    source = MagicMock()
    source.get_position.return_value = None
    source.format_position_context.return_value = None
    pipeline = MagicMock(return_value={"verdict": "BUY", "summary": "ok"},
                         side_effect=runner_side_effect)
    with patch("watchy.tier2.compute_indicators", side_effect=lambda t: _bundle(t)), \
         patch("watchy.tier2.get_position_source", return_value=source), \
         patch("watchy.tier2.monitor_schwab"), \
         patch("watchy.tier2.run_pipeline", pipeline), \
         patch("watchy.tier2.save_digest", return_value="/tmp/d.json") as save, \
         patch("watchy.tier2.get_advice", return_value=advice) as get_adv, \
         patch("watchy.tier2.time.sleep"), \
         patch("watchy.tier2.fetch_live_price",
               side_effect=lambda t: (post_price, datetime.now(timezone.utc))):
        results = run_daily_scan(config, store, notifier, weekly=True, tickers=tickers)
    return results, notifier, pipeline, save, get_adv


class TestWeeklyTier2:
    def test_weekly_run_persists_plan_and_renders_card(self, store):
        config = WatchyConfig(watchlist=[TickerConfig(ticker="NVDA", tier2_days=["wed"])])
        results, notifier, pipeline, save, get_adv = _weekly_run(store, config, _advice())
        # cadence (wed only) never applies to Weekly Full
        assert results["NVDA"]["plan_valid"] is True
        plan = store.get_active_plan("NVDA")
        assert plan is not None and plan.source_ref["mode"] == "weekly_full"
        assert plan.source_ref["digest_path"] == "/tmp/d.json"
        spec = pipeline.call_args.args[1]
        assert spec.risk == RiskMode.FULL
        assert get_adv.call_args.kwargs["plan_request"] is True
        assert any(c.kwargs.get("kind") == "weekly" for c in save.call_args_list)
        card = notifier.pipeline_result.call_args.kwargs["plan_card"]
        assert "NVDA —" in card and "chase ceiling $125.00" in card
        assert "Advisory only" in card
        notifier.weekly_plan_failures.assert_not_called()

    def test_invalid_plan_stored_not_active_and_alerted_once(self, store):
        config = WatchyConfig(watchlist=[TickerConfig(ticker="NVDA"), TickerConfig(ticker="AMZN")])
        bad = _advice(ADVICE_WITH_BLOCK.replace("Buy-Zone-Low: 121.00\n", ""))
        results, notifier, *_ = _weekly_run(store, config, bad)
        assert store.get_active_plan("NVDA") is None
        assert store.get_latest_plan("NVDA").validation_errors
        notifier.weekly_plan_failures.assert_called_once()
        assert notifier.weekly_plan_failures.call_args.args[0] == ["AMZN", "NVDA"]
        card = notifier.pipeline_result.call_args.kwargs["plan_card"]
        assert "NOT active" in card and "INFORMATION ONLY" in card

    def test_pipeline_failure_records_failed_plan(self, store):
        config = WatchyConfig(watchlist=[TickerConfig(ticker="NVDA")])
        results, notifier, *_ = _weekly_run(store, config, _advice(),
                                            runner_side_effect=RuntimeError("boom"))
        latest = store.get_latest_plan("NVDA")
        assert latest is not None and not latest.is_valid
        assert "RuntimeError: boom" in latest.validation_errors[0]
        notifier.weekly_plan_failures.assert_called_once()

    def test_failed_refresh_does_not_extend_previous_plan(self, store):
        from tests.fixtures_v2 import make_plan
        from watchy.plan import PlanFreshness, plan_freshness
        from datetime import date

        store.insert_plan(make_plan(valid_from_session="2026-09-14",
                                    expires_after_session="2026-09-18"))
        config = WatchyConfig(watchlist=[TickerConfig(ticker="NVDA")])
        _weekly_run(store, config, None)  # advisor failed → invalid plan
        active = store.get_active_plan("NVDA")
        assert plan_freshness(active, date(2026, 9, 21)) == PlanFreshness.EXPIRED

    def test_force_single_ticker(self, store):
        config = WatchyConfig(watchlist=[TickerConfig(ticker="NVDA"), TickerConfig(ticker="AMZN")])
        results, *_ = _weekly_run(store, config, _advice(), tickers=["nvda"])
        assert list(results) == ["NVDA"]


class TestDaemonWeeklySchedule:
    def test_weekly_mode_skips_ordinary_days(self):
        from watchy.daemon import _tier2_job

        config = MagicMock(weekly_mode=True)
        with patch("watchy.daemon._is_tier2_day", return_value=True), \
             patch("watchy.daemon.is_weekly_full_risk_day", return_value=False), \
             patch("watchy.daemon.run_daily_scan") as scan:
            _tier2_job(config, MagicMock(), MagicMock())
        scan.assert_not_called()

    def test_weekly_mode_runs_first_session(self):
        from watchy.daemon import _tier2_job

        config = MagicMock(weekly_mode=True)
        with patch("watchy.daemon._is_tier2_day", return_value=True), \
             patch("watchy.daemon.is_weekly_full_risk_day", return_value=True), \
             patch("watchy.daemon.run_daily_scan") as scan:
            _tier2_job(config, MagicMock(), MagicMock())
        assert scan.call_args.kwargs["weekly"] is True

    def test_daily_rollback_runs_every_trading_day(self):
        from watchy.daemon import _tier2_job

        config = MagicMock(weekly_mode=False)
        with patch("watchy.daemon._is_tier2_day", return_value=True), \
             patch("watchy.daemon.is_weekly_full_risk_day", return_value=False), \
             patch("watchy.daemon.run_daily_scan") as scan:
            _tier2_job(config, MagicMock(), MagicMock())
        assert scan.call_args.kwargs["weekly"] is False


class TestConfigSchedule:
    def test_defaults_and_yaml(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("watchlist: [NVDA]\ntier2_schedule: daily\n"
                     "triggered_analysis: {enabled: false, max_global_per_trading_day: 3}\n",
                     encoding="utf-8")
        cfg = WatchyConfig.from_yaml(p)
        assert cfg.tier2_schedule == "daily" and not cfg.weekly_mode
        assert cfg.triggered_analysis.max_global_per_trading_day == 3
        assert cfg.weekly_plan.approach_atr == 0.5
        assert WatchyConfig().weekly_mode is True
        assert WatchyConfig().triggered_analysis.enabled is False

    def test_typo_fails_loudly(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("tier2_schedule: weeky\n", encoding="utf-8")
        with pytest.raises(ValueError, match="tier2_schedule"):
            WatchyConfig.from_yaml(p)


class TestCardEscaping:
    def test_llm_text_is_escaped(self):
        from tests.fixtures_v2 import make_plan
        from watchy.weekly import render_weekly_card

        plan = make_plan(guidance="<script>buy</script> & hold")
        card = render_weekly_card(plan, {"decision": "BUY"}, {}, None, MON)
        assert "<script>" not in card and "&lt;script&gt;" in card and "&amp; hold" in card
