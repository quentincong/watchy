"""Watchy 2.0 Phase 3 — plan classification, transitions, Notify Only."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.fixtures_v2 import make_plan
from watchy.config import TickerConfig, WatchyConfig
from watchy.guards import StatusInputs, select_status
from watchy.indicators import IndicatorBundle
from watchy.monitor import data_is_stale, position_state_of
from watchy.plan import (
    PlanFreshness,
    PositionState,
    ReminderState as RS,
    TelegramStatus as TS,
)
from watchy.plan_monitor import classify_price, detect_transition, plan_state
from watchy.state import StateStore

T0 = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)  # Tue 11:00 ET


class TestClassify:
    plan = make_plan()  # zone 121-123, chase 125, inv 116, resistance 132-135

    @pytest.mark.parametrize("price,state", [
        (115.0, RS.INVALIDATED),
        (118.0, RS.OUTSIDE),          # below zone, above invalidation
        (121.0, RS.IN_BUY_ZONE),      # inclusive edges
        (123.0, RS.IN_BUY_ZONE),
        (124.0, RS.APPROACHING_BUY),  # within 0.5 * ATR(3) = 1.5 above zone
        (124.6, RS.OUTSIDE),
        (125.5, RS.ABOVE_CHASE),
        (132.0, RS.IN_TAKE_PROFIT),
        (None, RS.OUTSIDE),
    ])
    def test_matrix(self, price, state):
        assert classify_price(self.plan, price, 3.0, approach_atr=0.5) == state

    def test_no_atr_means_no_approach(self):
        assert classify_price(self.plan, 124.0, None) == RS.OUTSIDE

    def test_nullable_levels(self):
        hold = make_plan(decision="HOLD", urgency="LOW", buy_zone_low=None,
                         buy_zone_high=None, chase_ceiling=None, resistance_low=None,
                         resistance_high=None, invalidation_level=None)
        assert classify_price(hold, 50.0, 3.0) == RS.OUTSIDE

    def test_take_profit_price_alone(self):
        p = make_plan(resistance_low=None, resistance_high=None, take_profit_price=130.0)
        assert classify_price(p, 130.5, 3.0) == RS.IN_TAKE_PROFIT

    def test_plan_state_by_freshness(self):
        assert plan_state(self.plan, PlanFreshness.EXPIRED, 122.0, 3.0) == RS.EXPIRED
        assert plan_state(self.plan, PlanFreshness.INVALID, 122.0, 3.0) is None
        assert plan_state(None, PlanFreshness.MISSING, 122.0, 3.0) is None
        assert plan_state(self.plan, PlanFreshness.ACTIVE, 122.0, 3.0) == RS.IN_BUY_ZONE


class TestTransitions:
    def test_entry_notifies_then_repeat_is_silent(self):
        t1 = detect_transition({}, 1, RS.IN_BUY_ZONE, T0)
        assert t1.notify and t1.kind == "entered"
        prev = {"plan_id": 1, "state": t1.state.value, "state_since_ts": t1.state_since_ts,
                "notified": t1.notified}
        t2 = detect_transition(prev, 1, RS.IN_BUY_ZONE, T0 + timedelta(minutes=30))
        assert not t2.notify and t2.kind == "unchanged"

    def test_leaving_buy_zone_notifies_once(self):
        prev = {"plan_id": 1, "state": "inside_buy_zone", "notified": {}}
        t = detect_transition(prev, 1, RS.OUTSIDE, T0)
        assert t.notify and t.kind == "left_buy_zone"
        assert "left the buy zone" in t.reason

    def test_quiet_transition_to_outside(self):
        prev = {"plan_id": 1, "state": "approaching_buy_zone", "notified": {}}
        assert not detect_transition(prev, 1, RS.OUTSIDE, T0).notify

    def test_reentry_after_leaving_rearms_after_window(self):
        prev = {"plan_id": 1, "state": "outside",
                "notified": {"inside_buy_zone": (T0 - timedelta(hours=7)).isoformat()}}
        assert detect_transition(prev, 1, RS.IN_BUY_ZONE, T0, renotify_h=6).notify

    def test_flapping_within_window_is_suppressed(self):
        prev = {"plan_id": 1, "state": "outside",
                "notified": {"inside_buy_zone": (T0 - timedelta(hours=1)).isoformat()}}
        t = detect_transition(prev, 1, RS.IN_BUY_ZONE, T0, renotify_h=6)
        assert not t.notify and t.kind == "suppressed"
        assert t.state == RS.IN_BUY_ZONE  # state still advances

    def test_new_plan_rearms(self):
        prev = {"plan_id": 1, "state": "inside_buy_zone",
                "notified": {"inside_buy_zone": T0.isoformat()}}
        t = detect_transition(prev, 2, RS.IN_BUY_ZONE, T0 + timedelta(minutes=5))
        assert t.notify and t.rearmed and t.notified == {"inside_buy_zone": t.state_since_ts}

    def test_expiry_notifies_once_per_plan(self):
        t1 = detect_transition({"plan_id": 1, "state": "outside", "notified": {}}, 1, RS.EXPIRED, T0)
        assert t1.notify
        prev = {"plan_id": 1, "state": "expired", "notified": t1.notified}
        assert not detect_transition(prev, 1, RS.EXPIRED, T0 + timedelta(days=1)).notify


class TestNotifyStatus:
    def _s(self, **kw):
        base = dict(position_state=PositionState.WATCH, freshness=PlanFreshness.ACTIVE,
                    plan=make_plan())
        base.update(kw)
        return select_status(StatusInputs(**base))

    def test_buy_zone_is_wait_for_limit_never_act_now(self):
        assert self._s(plan_state=RS.IN_BUY_ZONE) == TS.WAIT_FOR_LIMIT
        assert self._s(plan_state=RS.APPROACHING_BUY) == TS.WAIT_FOR_LIMIT

    def test_above_chase(self):
        assert self._s(plan_state=RS.ABOVE_CHASE) == TS.DO_NOT_CHASE

    def test_invalidated_watch_vs_held(self):
        assert self._s(plan_state=RS.INVALIDATED) == TS.PLAN_INVALID
        assert self._s(plan_state=RS.INVALIDATED, position_state=PositionState.HELD) == TS.RISK_REVIEW
        assert self._s(plan_state=RS.INVALIDATED, position_state=PositionState.UNKNOWN) == TS.RISK_REVIEW

    def test_stale_data_wins(self):
        assert self._s(plan_state=RS.IN_BUY_ZONE, data_stale=True) == TS.STALE

    def test_expired_missing_invalid_never_actionable(self):
        assert self._s(plan_state=RS.EXPIRED, freshness=PlanFreshness.EXPIRED) == TS.STALE
        assert self._s(plan_state=None, freshness=PlanFreshness.MISSING, plan=None) == TS.INFORMATION_ONLY
        assert self._s(plan_state=None, freshness=PlanFreshness.INVALID) == TS.INFORMATION_ONLY

    def test_non_bullish_plan_zone_is_information(self):
        p = make_plan(decision="WATCH", urgency="LOW", chase_ceiling=None)
        assert self._s(plan=p, plan_state=RS.IN_BUY_ZONE) == TS.INFORMATION_ONLY


class TestHelpers:
    def test_position_state(self):
        src = MagicMock()
        src.get_position.return_value = None
        assert position_state_of(src, "X") == PositionState.WATCH
        src.get_position.return_value = MagicMock(quantity=3)
        assert position_state_of(src, "X") == PositionState.HELD
        src.get_position.side_effect = RuntimeError("schwab down")
        assert position_state_of(src, "X") == PositionState.UNKNOWN
        assert position_state_of(None, "X") == PositionState.UNKNOWN

    def test_stale_data(self):
        import pandas as pd
        b = IndicatorBundle(ticker="X", current_price=10.0)
        assert data_is_stale(b, T0, 30)                     # no fetch time
        b.fetched_at = T0 - timedelta(minutes=5)
        b.timestamp = pd.Timestamp("2026-09-22")
        assert not data_is_stale(b, T0, 30)
        b.timestamp = pd.Timestamp("2026-09-21")            # yesterday's bar
        assert data_is_stale(b, T0, 30)
        b.timestamp = pd.Timestamp("2026-09-22")
        b.fetched_at = T0 - timedelta(minutes=45)
        assert data_is_stale(b, T0, 30)
        assert data_is_stale(None, T0, 30)


# --- integration through Tier 1 with a real store --------------------------

def _bundle(price):
    import pandas as pd
    b = IndicatorBundle(ticker="NVDA", current_price=price, avg_atr_20d=3.0, atr=3.0)
    b.fetched_at = datetime.now(timezone.utc)
    from watchy.market_calendar import session_label
    b.timestamp = pd.Timestamp(session_label())
    return b


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "state.db")


def _this_week_plan(**kw):
    from watchy.market_calendar import session_label, week_session_bounds
    first, last = week_session_bounds()
    today = session_label()
    return make_plan(valid_from_session=min(first, today).isoformat(),
                     expires_after_session=max(last, today).isoformat(), **kw)


def _scan(store, price, held=False):
    from watchy.tier1 import scan_ticker
    config = WatchyConfig(watchlist=[TickerConfig(ticker="NVDA")])
    notifier = MagicMock()
    src = MagicMock()
    src.get_position.return_value = MagicMock(quantity=2, unrealized_pnl_pct=1.0) if held else None
    with patch("watchy.tier1.compute_indicators", return_value=_bundle(price)), \
         patch("watchy.tier1.detect_signals", return_value=[]), \
         patch("watchy.tier1.get_position_source", return_value=src), \
         patch("watchy.tier1.get_advice") as adv, \
         patch("watchy.tier1.run_pipeline") as run:
        scan_ticker("NVDA", config, store, notifier)
    adv.assert_not_called()
    run.assert_not_called()
    return notifier


class TestTier1PlanReminders:
    def test_zone_entry_notifies_once_and_survives_restart(self, db):
        store = StateStore(db)
        store.insert_plan(_this_week_plan())
        n1 = _scan(store, 122.0)
        assert n1.send.call_count == 1
        text = n1.send.call_args.args[0]
        assert "WAIT FOR LIMIT" in text and "Why now:" in text and "Do not:" in text
        assert "no new LLM analysis" in text
        n2 = _scan(store, 122.3)
        n2.send.assert_not_called()
        store.close()
        restarted = StateStore(db)            # daemon restart
        n3 = _scan(restarted, 122.1)
        n3.send.assert_not_called()
        restarted.close()

    def test_no_plan_sends_nothing(self, db):
        store = StateStore(db)
        _scan(store, 122.0).send.assert_not_called()
        store.close()

    def test_watch_invalidation_withdraws_plan(self, db):
        store = StateStore(db)
        pid = store.insert_plan(_this_week_plan())
        n = _scan(store, 110.0)
        assert "PLAN INVALID" in n.send.call_args.args[0]
        assert store.get_plan(pid).status == "invalidated"
        # withdrawn plan: no further entry reminders even back in the zone
        _scan(store, 122.0).send.assert_not_called()
        store.close()

    def test_held_invalidation_is_risk_review(self, db):
        store = StateStore(db)
        store.insert_plan(_this_week_plan())
        n = _scan(store, 110.0, held=True)
        assert "RISK REVIEW" in n.send.call_args.args[0]
        store.close()

    def test_new_weekly_plan_rearms(self, db):
        store = StateStore(db)
        store.insert_plan(_this_week_plan())
        _scan(store, 122.0)
        store.insert_plan(_this_week_plan())  # supersedes
        assert _scan(store, 122.0).send.call_count == 1
        store.close()
