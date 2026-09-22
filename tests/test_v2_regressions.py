"""Watchy 2.0 cross-cutting coverage: daily rollback, message splitting,
session-keyed budgets, expiry hold during the weekly batch."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tests.fixtures_v2 import make_plan
from watchy.config import TickerConfig, WatchyConfig
from watchy.indicators import IndicatorBundle
from watchy.market_calendar import session_label
from watchy.messages import MessageContext, render_plan_card
from watchy.notify import _split_message
from watchy.plan import PlanFreshness, PositionState, Route, TelegramStatus
from watchy.router import RouterInput, route
from watchy.state import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    yield s
    s.close()


class TestDailyRollback:
    def test_daily_tier1_keeps_the_1x_paid_rescan_path(self):
        from watchy.tier1 import scan_ticker

        config = WatchyConfig(watchlist=[TickerConfig(ticker="AAPL")], tier2_schedule="daily")
        store, notifier = MagicMock(), MagicMock()
        store.get_ticker_state.return_value = {}
        store.is_in_cooldown.return_value = False
        store.count_tier1_runs_today.return_value = 0
        b = IndicatorBundle(ticker="AAPL", current_price=100.0, avg_atr_20d=2.0)
        with patch("watchy.tier1.compute_indicators", return_value=b), \
             patch("watchy.tier1.detect_signals", return_value=["macd_bearish_cross"]), \
             patch("watchy.tier1.run_pipeline", return_value={"summary": "ok"}) as run, \
             patch("watchy.tier1.get_advice", return_value={}), \
             patch("watchy.tier1.get_position_source"), \
             patch("watchy.tier1.monitor_schwab"), \
             patch("watchy.monitor.scan_planned") as planned:
            scan_ticker("AAPL", config, store, notifier)
        run.assert_called_once()
        planned.assert_not_called()

    def test_daily_tier2_requests_no_plan(self, store):
        from watchy.tier2 import run_daily_scan

        config = WatchyConfig(watchlist=[TickerConfig(ticker="NVDA")], tier2_schedule="daily")
        src = MagicMock()
        src.get_position.return_value = None
        with patch("watchy.tier2.compute_indicators", return_value=None), \
             patch("watchy.tier2.get_position_source", return_value=src), \
             patch("watchy.tier2.monitor_schwab"), \
             patch("watchy.tier2.run_pipeline", return_value={"verdict": "HOLD"}), \
             patch("watchy.tier2.save_digest") as save, \
             patch("watchy.tier2.get_advice", return_value={"decision": "HOLD"}) as adv, \
             patch("watchy.tier2.time.sleep"):
            run_daily_scan(config, store, MagicMock())
        assert adv.call_args.kwargs["plan_request"] is False
        assert adv.call_args.kwargs["source"] == "tier2"
        assert all(c.kwargs.get("kind", "") == "" for c in save.call_args_list)
        assert store.get_latest_plan("NVDA") is None

    def test_rollback_keeps_v2_tables_and_data(self, tmp_path):
        path = str(tmp_path / "state.db")
        s = StateStore(path)
        s.insert_plan(make_plan())
        s.close()
        s = StateStore(path)                  # reopen as a daily-mode daemon would
        assert s.get_plan_history("NVDA")
        s.close()


class TestMessageSplitting:
    def test_long_card_splits_with_balanced_tags(self):
        plan = make_plan(thesis="t " * 1500)
        text = render_plan_card(MessageContext(
            ticker="NVDA", status=TelegramStatus.WAIT_FOR_LIMIT, price=122.0,
            why_now=["price entered the weekly buy zone"] * 3, plan=plan,
            plan_freshness=PlanFreshness.ACTIVE, guidance="g " * 2500, dont_do="do not chase",
            mode="Tier 1 plan reminder; no new LLM analysis",
        ))
        chunks = _split_message(text)
        assert len(chunks) > 1 and all(len(c) <= 4000 for c in chunks)
        for c in chunks:
            assert c.count("<b>") == c.count("</b>")
            assert c.count("<i>") == c.count("</i>")


class TestUnknownPositionRows:
    @pytest.mark.parametrize("sig", ["rsi_overbought", "bollinger_upper_breach"])
    def test_overbought_unknown(self, sig):
        d = route(RouterInput(ticker="X", position_state=PositionState.UNKNOWN,
                              plan=make_plan(), freshness=PlanFreshness.ACTIVE, signals=[sig],
                              price=124.0))
        assert d.route == Route.NOTIFY_ONLY


class TestSessionBudget:
    def test_after_hours_utc_midnight_still_counts_to_same_session(self, store):
        # 20:30 ET Monday = 00:30 UTC Tuesday — same exchange session label.
        late = datetime(2026, 9, 22, 0, 30, tzinfo=timezone.utc)
        mon = session_label(late).isoformat()
        assert mon == "2026-09-21"
        assert store.try_reserve_triggered(mon, "NVDA", "FAST_RECHECK", 1, 2)
        assert store.try_reserve_triggered(session_label(
            datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)).isoformat(),
            "NVDA", "FAST_RECHECK", 1, 2) is None


class TestExpiryHeldDuringWeeklyBatch:
    def _scan(self, store):
        from watchy.tier1 import scan_ticker

        b = IndicatorBundle(ticker="NVDA", current_price=122.0, avg_atr_20d=3.0, prev_close=122.0)
        b.fetched_at = datetime.now(timezone.utc)
        b.timestamp = pd.Timestamp(session_label())
        notifier = MagicMock()
        src = MagicMock()
        src.get_position.return_value = None
        with patch("watchy.tier1.compute_indicators", return_value=b), \
             patch("watchy.tier1.detect_signals", return_value=[]), \
             patch("watchy.tier1.get_position_source", return_value=src):
            scan_ticker("NVDA", WatchyConfig(watchlist=[TickerConfig(ticker="NVDA")]),
                        store, notifier)
        return notifier

    def test_expired_notice_waits_for_the_batch(self, store):
        store.insert_plan(make_plan(valid_from_session="2020-01-06",
                                    expires_after_session="2020-01-10"))
        store.set_kv("weekly_full_running", session_label().isoformat())
        self._scan(store).send.assert_not_called()
        store.set_kv("weekly_full_running", "")
        n = self._scan(store)
        assert n.send.call_count == 1 and "STALE" in n.send.call_args.args[0]
        self._scan(store).send.assert_not_called()      # once per plan

    def test_weekly_batch_sets_and_clears_flag(self, store):
        from watchy.tier2 import run_daily_scan

        seen = []
        src = MagicMock()
        src.get_position.return_value = None

        def pipeline(*a, **k):
            seen.append(store.get_kv("weekly_full_running"))
            return {"verdict": "HOLD"}

        with patch("watchy.tier2.compute_indicators", return_value=None), \
             patch("watchy.tier2.get_position_source", return_value=src), \
             patch("watchy.tier2.monitor_schwab"), \
             patch("watchy.tier2.run_pipeline", side_effect=pipeline), \
             patch("watchy.tier2.save_digest"), \
             patch("watchy.tier2.get_advice", return_value=None), \
             patch("watchy.tier2.fetch_live_price", return_value=(None, None)), \
             patch("watchy.tier2.time.sleep"):
            run_daily_scan(WatchyConfig(watchlist=[TickerConfig(ticker="NVDA")]), store,
                           MagicMock(), weekly=True)
        assert seen == [session_label().isoformat()]
        assert store.get_kv("weekly_full_running") == ""


class TestVersion:
    def test_package_version_matches_pyproject(self):
        import re
        from pathlib import Path

        import watchy
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert re.search(r'^version = "([^"]+)"', text, re.M).group(1) == watchy.__version__
        assert watchy.__version__ == "2.0.0rc1"


class TestPlanWording:
    def _card(self, plan, fresh):
        return render_plan_card(MessageContext(
            ticker="NVDA", status=TelegramStatus.INFORMATION_ONLY, plan=plan, plan_freshness=fresh))

    def test_deactivated_and_invalidated_wording(self):
        p = make_plan()
        p.status = "deactivated"
        assert "deactivated by the operator" in self._card(p, PlanFreshness.INVALID)
        p.status = "invalidated"
        card = self._card(p, PlanFreshness.INVALIDATED)
        assert "invalidated (thesis broken)" in card and "Withdrawn plan" in card
        bad = make_plan(decision="MAYBE")
        assert "failed validation" in self._card(bad, PlanFreshness.INVALID)
