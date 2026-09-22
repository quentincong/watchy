"""Watchy 2.0 Phases 6–7 — Fast Recheck and Triggered Risk (LLM boundary mocked)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tests.fixtures_v2 import make_plan
from watchy.config import TickerConfig, TriggeredAnalysisConfig, WatchyConfig
from watchy.indicators import IndicatorBundle
from watchy.locks import TickerLockRegistry
from watchy.market_calendar import session_label, week_session_bounds
from watchy.orchestrator import AnalystSet, DebateMode, RiskMode
from watchy.state import StateStore


def _bundle(price, prev_close=None, ticker="NVDA"):
    b = IndicatorBundle(ticker=ticker, current_price=price, avg_atr_20d=3.0, atr=3.0,
                        prev_close=prev_close if prev_close is not None else price)
    b.fetched_at = datetime.now(timezone.utc)
    b.timestamp = pd.Timestamp(session_label())
    return b


def _plan(ticker="NVDA", **kw):
    first, last = week_session_bounds()
    today = session_label()
    return make_plan(ticker=ticker, valid_from_session=min(first, today).isoformat(),
                     expires_after_session=max(last, today).isoformat(), **kw)


@pytest.fixture
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    yield s
    s.close()


ADVICE = {"decision": "BUY", "urgency": "HIGH", "detail": "Pullback into support; thesis intact.",
          "target": "122", "take_profit": "", "_advice_log_id": 5}


def run_scan(store, *, ticker="NVDA", price=122.0, prev_close=None, signals=("macd_bearish_cross",),
             held=True, advice=ADVICE, post_price=None, digest=True, pipeline=None,
             per_ticker=1, global_max=2, locks=None, runner_error=None):
    from watchy.tier1 import scan_ticker

    config = WatchyConfig(
        watchlist=[TickerConfig(ticker=ticker)],
        triggered_analysis=TriggeredAnalysisConfig(
            enabled=True, max_per_ticker_per_trading_day=per_ticker,
            max_global_per_trading_day=global_max),
    )
    notifier = MagicMock()
    notifier.send.return_value = True
    notifier.pipeline_result.return_value = True
    src = MagicMock()
    src.get_position.return_value = MagicMock(quantity=3, unrealized_pnl_pct=2.0) if held else None
    src.format_position_context.return_value = "pos"
    saved_at = datetime.now(timezone.utc)
    result = pipeline or {"verdict": "SELL", "summary": "risk up", "report_path": None}
    pp = post_price if post_price is not None else price
    with patch("watchy.tier1.compute_indicators", return_value=_bundle(price, prev_close, ticker)), \
         patch("watchy.tier1.detect_signals", return_value=list(signals)), \
         patch("watchy.tier1.get_position_source", return_value=src), \
         patch("watchy.monitor.weekly_digest_available", return_value=digest), \
         patch("watchy.triggered.load_digest",
               return_value=({"verdict": "BUY"}, saved_at) if digest else None) as load, \
         patch("watchy.triggered.save_digest") as save, \
         patch("watchy.triggered.get_advice", return_value=advice) as adv, \
         patch("watchy.triggered.run_pipeline", return_value=result,
               side_effect=runner_error) as run, \
         patch("watchy.schwab_health.monitor_schwab"), \
         patch("watchy.monitor.fetch_live_price",
               side_effect=lambda t: (pp, datetime.now(timezone.utc))), \
         patch("watchy.advisor._call_gemini", side_effect=AssertionError("real LLM")):
        scan_ticker(ticker, config, store, notifier, ticker_locks=locks)
    return notifier, adv, run, save, load


class TestFastRecheck:
    def test_advisor_only_with_event_context_and_override(self, store):
        base_id = store.insert_plan(_plan())
        notifier, adv, run, save, load = run_scan(store)
        run.assert_not_called()
        kw = adv.call_args.kwargs
        assert kw["source"] == "fast_recheck"
        assert "EVENT CONTEXT" in kw["event_context"] and "MACD bearish cross" in kw["event_context"]
        assert load.call_args.kwargs == {"kind": "weekly"}
        text = notifier.send.call_args.args[0]
        assert "Fast Recheck" in text and "Why now:" in text
        # base untouched; override references it with copied levels
        assert store.get_active_plan("NVDA").id == base_id
        ov = store.get_latest_override("NVDA", base_id)
        assert ov is not None and ov.chase_ceiling == 125.0 and ov.decision == "BUY"
        assert ov.valid_from_session == ov.expires_after_session == session_label().isoformat()
        row = store.get_route_log("NVDA")[-1]
        assert row["llm_invoked"] is True and row["analysis_components"] == ["advisor"]
        assert row["effective_route"] == "FAST_RECHECK" and row["override_id"] == ov.id
        assert store.count_triggered(row["session"], "NVDA") == 1

    def test_missing_digest_falls_back_to_notify(self, store):
        store.insert_plan(_plan())
        notifier, adv, run, *_ = run_scan(store, digest=False)
        adv.assert_not_called()
        text = notifier.send.call_args.args[0]
        assert "no valid weekly plan/digest" in text
        assert store.count_triggered(session_label().isoformat()) == 0

    def test_no_plan_falls_back_to_notify(self, store):
        notifier, adv, *_ = run_scan(store, held=True)
        adv.assert_not_called()
        assert notifier.send.call_count == 1

    def test_advisor_failure_keeps_deterministic_notice(self, store):
        store.insert_plan(_plan())
        notifier, adv, *_ = run_scan(store, advice=None)
        adv.assert_called_once()
        text = notifier.send.call_args.args[0]
        assert "analysis failed" in text
        row = store.get_route_log("NVDA")[-1]
        assert row["analysis_error"] and row["effective_route"] == "NOTIFY_ONLY"

    def test_price_moved_above_chase_during_recheck(self, store):
        store.insert_plan(_plan())
        # watch-only lower breach with an executable bullish plan → Fast Recheck
        notifier, *_ = run_scan(store, signals=["bollinger_lower_breach"], held=False,
                                price=122.0, post_price=125.6)
        text = notifier.send.call_args.args[0]
        assert "DO NOT CHASE" in text and "ACT NOW" not in text

    def test_act_now_when_price_still_in_zone(self, store):
        store.insert_plan(_plan())
        notifier, *_ = run_scan(store, signals=["bollinger_lower_breach"], held=False,
                                price=122.0, post_price=122.2)
        assert "ACT NOW" in notifier.send.call_args.args[0]


class TestBudgetAndConcurrency:
    def test_per_ticker_budget(self, store):
        store.insert_plan(_plan())
        run_scan(store)
        notifier, adv, *_ = run_scan(store, signals=["bollinger_lower_breach"])
        adv.assert_not_called()
        assert "budget exhausted" in notifier.send.call_args.args[0]
        assert store.get_route_log("NVDA")[-1]["budget_result"] == "budget_exhausted"

    def test_global_budget_across_tickers(self, store):
        for t in ("NVDA", "AMZN", "TSM"):
            store.insert_plan(_plan(ticker=t))
        run_scan(store, ticker="NVDA")
        run_scan(store, ticker="AMZN")
        _, adv, *_ = run_scan(store, ticker="TSM")
        adv.assert_not_called()
        assert store.count_triggered(session_label().isoformat()) == 2

    def test_lock_held_means_busy(self, store):
        store.insert_plan(_plan())
        locks = TickerLockRegistry()
        locks.get("NVDA").acquire()
        try:
            notifier, adv, *_ = run_scan(store, locks=locks)
        finally:
            locks.get("NVDA").release()
        adv.assert_not_called()
        assert "in progress" in notifier.send.call_args.args[0]
        assert store.count_triggered(session_label().isoformat()) == 0


class TestTriggeredRisk:
    def test_reduced_pipeline_and_risk_message(self, store):
        store.insert_plan(_plan())
        notifier, adv, run, save, _ = run_scan(store, signals=["death_cross"],
                                               advice={**ADVICE, "decision": "TRIM",
                                                       "urgency": "MEDIUM"})
        spec = run.call_args.args[1]
        assert (spec.analysts, spec.debate, spec.risk) == (
            AnalystSet.MARKET_SENTIMENT_NEWS, DebateMode.BULL_BEAR, RiskMode.SIMPLIFIED)
        assert save.call_args.kwargs.get("kind", "") == ""      # weekly digest untouched
        assert adv.call_args.kwargs["source"] == "triggered_risk"
        card = notifier.pipeline_result.call_args.kwargs["plan_card"]
        assert "RISK REVIEW" in card and "Triggered Risk" in card
        row = store.get_route_log("NVDA")[-1]
        assert row["effective_route"] == "TRIGGERED_RISK" and "news" in row["analysis_components"]
        assert row["upstream_verdict"] == "SELL"

    def test_invalidation_sends_immediate_warning_first(self, store):
        store.insert_plan(_plan())
        notifier, *_ = run_scan(store, signals=[], price=114.0)
        first = notifier.send.call_args_list[0].args[0]
        assert "RISK REVIEW" in first and "follow-up message" in first
        notifier.pipeline_result.assert_called_once()

    def test_pipeline_failure_sends_labelled_warning(self, store):
        store.insert_plan(_plan())
        notifier, adv, run, *_ = run_scan(store, signals=["death_cross"],
                                          runner_error=RuntimeError("deepseek 503"))
        adv.assert_not_called()
        text = notifier.send.call_args.args[0]
        assert "RISK REVIEW" in text and "analysis failed: RuntimeError: deepseek 503" in text
        assert store._conn.execute(
            "SELECT outcome FROM triggered_budget").fetchone()[0] == "failed"
