"""Watchy 2.0 Phase 4 — weekly-mode Tier 1 scan in shadow mode (no LLM)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tests.fixtures_v2 import make_plan
from watchy.config import TickerConfig, TriggeredAnalysisConfig, WatchyConfig
from watchy.indicators import IndicatorBundle
from watchy.market_calendar import session_label, week_session_bounds
from watchy.state import StateStore


def _bundle(price, prev_close=None):
    b = IndicatorBundle(ticker="NVDA", current_price=price, avg_atr_20d=3.0, atr=3.0,
                        prev_close=prev_close if prev_close is not None else price)
    b.fetched_at = datetime.now(timezone.utc)
    b.timestamp = pd.Timestamp(session_label())
    return b


def _plan(**kw):
    first, last = week_session_bounds()
    today = session_label()
    return make_plan(valid_from_session=min(first, today).isoformat(),
                     expires_after_session=max(last, today).isoformat(), **kw)


@pytest.fixture
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    yield s
    s.close()


def scan(store, price, signals, *, held=True, prev_close=None, enabled=False, config=None):
    from watchy.tier1 import scan_ticker
    config = config or WatchyConfig(
        watchlist=[TickerConfig(ticker="NVDA")],
        triggered_analysis=TriggeredAnalysisConfig(enabled=enabled),
    )
    notifier = MagicMock()
    notifier.send.return_value = True
    src = MagicMock()
    src.get_position.return_value = MagicMock(quantity=3, unrealized_pnl_pct=2.0) if held else None
    with patch("watchy.tier1.compute_indicators", return_value=_bundle(price, prev_close)), \
         patch("watchy.tier1.detect_signals", return_value=list(signals)), \
         patch("watchy.tier1.get_position_source", return_value=src), \
         patch("watchy.tier1.get_advice") as adv, \
         patch("watchy.tier1.run_pipeline") as run, \
         patch("watchy.advisor._call_gemini", side_effect=AssertionError("LLM called")):
        out = scan_ticker("NVDA", config, store, notifier)
    adv.assert_not_called()
    run.assert_not_called()
    return out, notifier


class TestShadowMode:
    def test_fast_recheck_downgraded_and_logged(self, store):
        store.insert_plan(_plan())
        out, notifier = scan(store, 122.0, ["macd_bearish_cross"])
        assert out == ["macd_bearish_cross"]
        text = notifier.send.call_args.args[0]
        assert "Shadow mode" in text and "Fast Recheck" in text
        # interpretation wanted but not run → entry wording capped
        assert "INFORMATION ONLY" in text and "WAIT FOR LIMIT" not in text
        row = store.get_route_log("NVDA")[-1]
        assert row["route"] == "FAST_RECHECK" and row["effective_route"] == "NOTIFY_ONLY"
        assert row["budget_result"] == "disabled" and row["shadow"] is True
        assert row["llm_invoked"] is False and row["notified"] is True
        assert row["position_state"] == "held" and row["plan_id"]
        assert store.count_triggered(row["session"]) == 0   # nothing reserved

    def test_triggered_risk_shadow_is_risk_review(self, store):
        store.insert_plan(_plan())
        _, notifier = scan(store, 124.0, ["death_cross"])
        assert "RISK REVIEW" in notifier.send.call_args.args[0]

    def test_multiple_triggers_one_message(self, store):
        store.insert_plan(_plan())
        _, notifier = scan(store, 121.5, ["macd_bearish_cross", "atr_spike", "rsi_oversold"],
                           prev_close=124.5)
        assert notifier.send.call_count == 1
        text = notifier.send.call_args.args[0]
        assert "MACD bearish cross" in text and "ATR spike" in text
        row = store.get_route_log("NVDA")[-1]
        assert row["route"] == "TRIGGERED_RISK" and len(row["reasons"]) >= 3

    def test_signal_cooldown_armed(self, store):
        store.insert_plan(_plan())
        scan(store, 124.0, ["macd_bearish_cross"])
        assert store.is_in_cooldown("NVDA", "macd_bearish_cross", 24)
        _, notifier = scan(store, 124.0, ["macd_bearish_cross"])
        notifier.send.assert_not_called()
        row = store.get_route_log("NVDA")[-1]
        assert row["cooled_down"] == ["macd_bearish_cross"] and row["route"] == "NO_ACTION"

    def test_no_plan_technical_warning_is_information(self, store):
        _, notifier = scan(store, 124.0, ["macd_bearish_cross"], held=False)
        text = notifier.send.call_args.args[0]
        assert "INFORMATION ONLY" in text and "none active" in text

    def test_watch_death_cross_withdraws_bullish_plan(self, store):
        pid = store.insert_plan(_plan())
        _, notifier = scan(store, 122.0, ["death_cross"], held=False)
        assert "PLAN INVALID" in notifier.send.call_args.args[0]
        assert store.get_plan(pid).status == "invalidated"

    def test_every_scan_logged_even_quiet(self, store):
        scan(store, 124.0, [])
        scan(store, 124.1, [])
        rows = store.get_route_log("NVDA")
        assert len(rows) == 2 and all(r["route"] == "NO_ACTION" for r in rows)
