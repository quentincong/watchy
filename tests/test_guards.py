"""Watchy 2.0 Phase 5 — price revalidation and verdict/advisor conflict guards."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.fixtures_v2 import make_plan
from watchy.guards import (
    StatusInputs,
    classify_alignment,
    entry_blocked,
    revalidate,
    select_status,
)
from watchy.plan import (
    PlanFreshness as F,
    PositionState as PS,
    ReminderState as RS,
    Route,
    TelegramStatus as TS,
)

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
PLAN = make_plan()


def status(**kw):
    base = dict(position_state=PS.WATCH, freshness=F.ACTIVE, plan=PLAN,
                plan_state=RS.IN_BUY_ZONE, verdict="BUY")
    base.update(kw)
    return select_status(StatusInputs(**base))


class TestAlignment:
    @pytest.mark.parametrize("v,a,out", [
        ("BUY", "ADD", "agree"), ("SELL", "TRIM", "agree"), ("HOLD", "WATCH", "agree"),
        ("BUY", "HOLD", "conflict"), ("HOLD", "BUY", "conflict"), ("SELL", "BUY", "conflict"),
        ("", "BUY", "unknown"), ("BUY", "", "unknown"),
    ])
    def test_matrix(self, v, a, out):
        assert classify_alignment(v, a) == out

    @pytest.mark.parametrize("v,a,blocked", [
        ("SELL", "BUY", True), ("HOLD", "BUY", True), ("HOLD", "ADD", True),
        ("BUY", "BUY", False), ("BUY", "HOLD", False), ("", "BUY", False),
    ])
    def test_entry_blocked(self, v, a, blocked):
        assert entry_blocked(v, a, None) is blocked

    def test_sell_verdict_blocks_bullish_plan(self):
        watch = make_plan(decision="WATCH", urgency="LOW")
        assert entry_blocked("SELL", "WATCH", watch)


class TestAnalysedStatus:
    def test_act_now_needs_everything(self):
        assert status(advisor_decision="BUY", advisor_urgency="HIGH") == TS.ACT_NOW

    def test_medium_is_wait(self):
        assert status(advisor_decision="BUY", advisor_urgency="MEDIUM") == TS.WAIT_FOR_LIMIT

    @pytest.mark.parametrize("verdict", ["SELL", "HOLD"])
    def test_conflict_never_actionable_buy(self, verdict):
        assert status(verdict=verdict, advisor_decision="BUY",
                      advisor_urgency="HIGH") == TS.INFORMATION_ONLY
        assert status(verdict=verdict, advisor_decision="ADD", advisor_urgency="HIGH",
                      position_state=PS.HELD) == TS.INFORMATION_ONLY

    def test_unknown_verdict_no_act_now(self):
        assert status(verdict="", advisor_decision="BUY", advisor_urgency="HIGH") == TS.WAIT_FOR_LIMIT

    def test_chase_beats_buy(self):
        assert status(plan_state=RS.ABOVE_CHASE, advisor_decision="BUY",
                      advisor_urgency="HIGH") == TS.DO_NOT_CHASE

    def test_invalidation_beats_buy(self):
        assert status(plan_state=RS.INVALIDATED, advisor_decision="BUY") == TS.PLAN_INVALID
        assert status(plan_state=RS.INVALIDATED, advisor_decision="BUY",
                      position_state=PS.HELD) == TS.RISK_REVIEW

    def test_price_moved_during_analysis_is_stale(self):
        assert status(advisor_decision="BUY", advisor_urgency="HIGH",
                      price_moved_atr=0.8, stale_move_atr=0.5) == TS.STALE
        assert status(advisor_decision="BUY", advisor_urgency="HIGH",
                      price_moved_atr=0.3, stale_move_atr=0.5) == TS.ACT_NOW

    def test_stale_data_and_bad_plans(self):
        assert status(advisor_decision="BUY", data_stale=True) == TS.STALE
        assert status(advisor_decision="BUY", freshness=F.EXPIRED) == TS.STALE
        assert status(advisor_decision="BUY", freshness=F.MISSING, plan=None,
                      plan_state=None) == TS.INFORMATION_ONLY
        assert status(advisor_decision="BUY", freshness=F.INVALID, plan=None,
                      plan_state=None) == TS.INFORMATION_ONLY

    def test_bearish(self):
        assert status(verdict="SELL", advisor_decision="SELL", advisor_urgency="HIGH",
                      position_state=PS.HELD, plan_state=RS.OUTSIDE) == TS.ACT_NOW
        assert status(verdict="HOLD", advisor_decision="TRIM", advisor_urgency="HIGH",
                      position_state=PS.HELD, plan_state=RS.OUTSIDE) == TS.WAIT_FOR_LIMIT
        assert status(verdict="SELL", advisor_decision="SELL", advisor_urgency="HIGH",
                      plan_state=RS.OUTSIDE) == TS.INFORMATION_ONLY

    def test_triggered_risk_route_is_risk_review(self):
        assert status(advisor_decision="BUY", advisor_urgency="HIGH",
                      route=Route.TRIGGERED_RISK) == TS.RISK_REVIEW

    def test_interpretation_pending_caps_entry(self):
        assert status(interpretation_pending=True) == TS.INFORMATION_ONLY
        assert status(interpretation_pending=True, plan_state=RS.ABOVE_CHASE) == TS.DO_NOT_CHASE


class TestRevalidate:
    def _r(self, post, pre=124.0, pre_age=5, post_age=0):
        return revalidate(
            PLAN, F.ACTIVE, pre_price=pre, pre_ts=NOW - timedelta(minutes=pre_age),
            post_price=post, post_ts=None if post is None else NOW - timedelta(minutes=post_age),
            atr=3.0, now=NOW,
        )

    def test_moves_into_buy_zone(self):
        r = self._r(122.0)
        assert r.state == RS.IN_BUY_ZONE and abs(r.moved_atr - 2 / 3) < 1e-9

    def test_moves_through_chase_ceiling(self):
        r = self._r(126.0)
        assert r.state == RS.ABOVE_CHASE
        assert status(plan_state=r.state, advisor_decision="BUY", advisor_urgency="HIGH",
                      price_moved_atr=r.moved_atr) == TS.DO_NOT_CHASE

    def test_moves_through_invalidation(self):
        r = self._r(115.0)
        assert r.state == RS.INVALIDATED
        assert status(plan_state=r.state, advisor_decision="BUY") == TS.PLAN_INVALID

    def test_small_move_keeps_act_now(self):
        r = self._r(122.5, pre=122.0)
        assert status(plan_state=r.state, advisor_decision="BUY", advisor_urgency="HIGH",
                      price_moved_atr=r.moved_atr, data_stale=r.stale) == TS.ACT_NOW

    def test_refresh_failed_fresh_pre_price(self):
        r = self._r(None, pre_age=5)
        assert not r.stale and r.price == 124.0 and r.reason

    def test_refresh_failed_old_pre_price_is_stale(self):
        r = self._r(None, pre_age=90)
        assert r.stale
        assert status(data_stale=r.stale, advisor_decision="BUY", advisor_urgency="HIGH") == TS.STALE


class TestWeeklyCardGuards:
    def _card(self, advice, post, verdict="BUY", held=False):
        from watchy.indicators import IndicatorBundle
        from watchy.weekly import render_weekly_card
        plan = make_plan(upstream_verdict=verdict)
        b = IndicatorBundle(ticker="NVDA", current_price=124.0, avg_atr_20d=3.0)
        b.fetched_at = NOW - timedelta(minutes=10)
        return render_weekly_card(plan, advice, {}, b, NOW, held=held,
                                  post_price=post, post_ts=NOW)

    def test_act_now_when_everything_lines_up(self):
        # input 124.0 → 122.9 after the analysis: 0.37 ATR, inside the zone
        card = self._card({"decision": "BUY", "urgency": "HIGH"}, 122.9)
        assert "ACT NOW" in card

    def test_large_move_into_zone_is_stale(self):
        # 124.0 → 121.5 = 0.83 ATR while the LLM ran: recheck, don't act
        card = self._card({"decision": "BUY", "urgency": "HIGH"}, 121.5)
        assert "STALE" in card and "moved 0.83 ATR" in card

    def test_price_ran_above_chase_during_batch(self):
        card = self._card({"decision": "BUY", "urgency": "HIGH"}, 125.4)
        assert "DO NOT CHASE" in card

    def test_conflict_shown_and_blocked(self):
        card = self._card({"decision": "BUY", "urgency": "HIGH"}, 122.9, verdict="SELL")
        assert "conflict — human review required" in card
        assert "ACT NOW" not in card and "WAIT FOR LIMIT" not in card


class TestReminderConflict:
    def test_sell_verdict_plan_reminder_is_information(self):
        assert status(verdict="SELL", plan=make_plan(upstream_verdict="SELL")) == TS.INFORMATION_ONLY
