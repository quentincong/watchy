"""Watchy 2.0 Phase 4 — the pure Tier 1 router (every matrix row)."""

import pytest

from tests.fixtures_v2 import make_plan
from watchy.plan import PlanFreshness, PositionState as PS, ReminderState as RS, Route as R
from watchy.router import RouterInput, route

HELD, WATCH, UNKNOWN = PS.HELD, PS.WATCH, PS.UNKNOWN
PLAN = make_plan()  # bullish: zone 121-123, chase 125, inv 116


def _in(pos, signals=(), *, price=124.0, prev_close=124.5, atr=3.0, plan=PLAN,
        fresh=PlanFreshness.ACTIVE, state=RS.APPROACHING_BUY, enabled=True,
        digest_available=True, **kw):
    return RouterInput(
        ticker="NVDA", position_state=pos, plan=plan, freshness=fresh,
        plan_state=state, signals=list(signals), price=price, prev_close=prev_close,
        atr=atr, triggered_enabled=enabled, digest_available=digest_available, **kw,
    )


SHOCK = dict(price=121.5, prev_close=124.5, state=RS.IN_BUY_ZONE)   # -1.0 ATR move


class TestMatrix:
    # --- death cross ---
    @pytest.mark.parametrize("pos", [HELD, UNKNOWN])
    def test_death_cross_held(self, pos):
        d = route(_in(pos, ["death_cross"]))
        assert d.route == R.TRIGGERED_RISK and not d.invalidate_plan

    def test_death_cross_watch_invalidates_bullish_plan(self):
        d = route(_in(WATCH, ["death_cross"]))
        assert d.route == R.NOTIFY_ONLY and d.invalidate_plan

    def test_death_cross_watch_without_plan(self):
        d = route(_in(WATCH, ["death_cross"], plan=None, fresh=PlanFreshness.MISSING, state=None))
        assert d.route == R.NOTIFY_ONLY and not d.invalidate_plan

    # --- MACD bearish ---
    @pytest.mark.parametrize("pos", [HELD, UNKNOWN])
    def test_macd_bearish_held(self, pos):
        assert route(_in(pos, ["macd_bearish_cross"])).route == R.FAST_RECHECK
        assert route(_in(pos, ["macd_bearish_cross"], **SHOCK)).route == R.TRIGGERED_RISK

    def test_macd_bearish_watch(self):
        assert route(_in(WATCH, ["macd_bearish_cross"], **SHOCK)).route == R.NOTIFY_ONLY

    # --- Bollinger lower (and RSI oversold, same policy) ---
    @pytest.mark.parametrize("sig", ["bollinger_lower_breach", "rsi_oversold"])
    @pytest.mark.parametrize("pos", [HELD, UNKNOWN])
    def test_lower_breach_held(self, sig, pos):
        assert route(_in(pos, [sig])).route == R.FAST_RECHECK
        d = route(_in(pos, [sig], **SHOCK))
        assert d.route == R.TRIGGERED_RISK and d.material_negative

    @pytest.mark.parametrize("sig", ["bollinger_lower_breach", "rsi_oversold"])
    def test_lower_breach_watch(self, sig):
        assert route(_in(WATCH, [sig])).route == R.FAST_RECHECK       # executable bullish plan
        assert route(_in(WATCH, [sig], state=RS.ABOVE_CHASE, price=126.0)).route == R.NOTIFY_ONLY
        assert route(_in(WATCH, [sig], plan=None, fresh=PlanFreshness.MISSING,
                         state=None)).route == R.NOTIFY_ONLY
        hold = make_plan(decision="HOLD", urgency="LOW", buy_zone_low=None,
                         buy_zone_high=None, chase_ceiling=None)
        assert route(_in(WATCH, [sig], plan=hold, state=RS.OUTSIDE)).route == R.NOTIFY_ONLY

    # --- ATR / volume anomaly ---
    @pytest.mark.parametrize("sig", ["volume_anomaly_strong", "atr_spike"])
    def test_volatility(self, sig):
        assert route(_in(HELD, [sig], **SHOCK)).route == R.TRIGGERED_RISK
        assert route(_in(UNKNOWN, [sig], **SHOCK)).route == R.TRIGGERED_RISK
        assert route(_in(WATCH, [sig], **SHOCK)).route == R.NOTIFY_ONLY
        assert route(_in(HELD, [sig])).route == R.NOTIFY_ONLY        # no negative move

    # --- RSI overbought / Bollinger upper ---
    @pytest.mark.parametrize("sig", ["rsi_overbought", "bollinger_upper_breach"])
    def test_overbought(self, sig):
        assert route(_in(HELD, [sig])).route == R.NOTIFY_ONLY
        assert route(_in(HELD, [sig], take_profit_fire=True)).route == R.TAKE_PROFIT
        assert route(_in(WATCH, [sig])).route == R.NOTIFY_ONLY
        assert route(_in(WATCH, [sig], plan=None, fresh=PlanFreshness.MISSING,
                         state=None)).route == R.NO_ACTION

    # --- golden cross / MACD bullish ---
    @pytest.mark.parametrize("sig", ["golden_cross", "macd_bullish_cross"])
    @pytest.mark.parametrize("pos", [HELD, WATCH, UNKNOWN])
    def test_bullish_signals(self, sig, pos):
        assert route(_in(pos, [sig])).route == R.FAST_RECHECK
        assert route(_in(pos, [sig], state=RS.ABOVE_CHASE, price=126.0)).route == R.NOTIFY_ONLY
        assert route(_in(pos, [sig], fresh=PlanFreshness.EXPIRED, state=RS.EXPIRED)).route == R.NOTIFY_ONLY
        assert route(_in(pos, [sig], plan=None, fresh=PlanFreshness.MISSING,
                         state=None)).route == R.NOTIFY_ONLY

    # --- plan transitions ---
    @pytest.mark.parametrize("pos", [HELD, WATCH, UNKNOWN])
    def test_buy_zone_entry_is_mechanical(self, pos):
        d = route(_in(pos, state=RS.IN_BUY_ZONE, plan_transition="entered", price=122.0))
        assert d.route == R.NOTIFY_ONLY and d.reasons == ["price entered the weekly buy zone"]

    @pytest.mark.parametrize("pos", [HELD, WATCH, UNKNOWN])
    def test_above_chase(self, pos):
        d = route(_in(pos, state=RS.ABOVE_CHASE, plan_transition="entered", price=126.0))
        assert d.route == R.NOTIFY_ONLY

    @pytest.mark.parametrize("pos", [HELD, UNKNOWN])
    def test_invalidation_held(self, pos):
        d = route(_in(pos, state=RS.INVALIDATED, plan_transition="entered", price=115.0))
        assert d.route == R.TRIGGERED_RISK and d.immediate_warning and d.invalidate_plan

    def test_invalidation_watch(self):
        d = route(_in(WATCH, state=RS.INVALIDATED, plan_transition="entered", price=115.0))
        assert d.route == R.NOTIFY_ONLY and d.invalidate_plan and not d.immediate_warning

    def test_take_profit_zone(self):
        assert route(_in(HELD, take_profit_fire=True)).route == R.TAKE_PROFIT
        d = route(_in(WATCH, state=RS.IN_TAKE_PROFIT, plan_transition="entered", price=133.0))
        assert d.route == R.NO_ACTION
        d = route(_in(HELD, state=RS.IN_TAKE_PROFIT, plan_transition="entered", price=133.0))
        assert d.route == R.NOTIFY_ONLY

    def test_expired_and_left_zone(self):
        assert route(_in(WATCH, state=RS.EXPIRED, fresh=PlanFreshness.EXPIRED,
                         plan_transition="entered")).route == R.NOTIFY_ONLY
        assert route(_in(WATCH, state=RS.OUTSIDE,
                         plan_transition="left_buy_zone")).route == R.NOTIFY_ONLY

    def test_nothing_is_no_action(self):
        d = route(_in(HELD))
        assert d.route == d.effective_route == R.NO_ACTION


class TestArbitration:
    def test_single_analysis_all_reasons_kept(self):
        d = route(_in(HELD, ["macd_bearish_cross", "bollinger_lower_breach", "atr_spike"], **SHOCK))
        assert d.route == R.TRIGGERED_RISK
        assert len(d.reasons) == 3
        assert sum(1 for c in d.candidates if c.route == R.TRIGGERED_RISK) == 3

    def test_triggered_risk_beats_fast_recheck(self):
        d = route(_in(HELD, ["macd_bearish_cross", "death_cross"]))
        assert d.route == R.TRIGGERED_RISK
        assert any("FAST_RECHECK superseded" in r for r in d.rejected)

    def test_take_profit_beats_triggered_risk(self):
        d = route(_in(HELD, ["death_cross"], take_profit_fire=True))
        assert d.route == R.TAKE_PROFIT
        assert any("TRIGGERED_RISK superseded" in r for r in d.rejected)

    def test_invalidation_ranks_with_take_profit(self):
        d = route(_in(HELD, ["golden_cross"], state=RS.INVALIDATED,
                      plan_transition="entered", price=115.0))
        assert d.route == R.TRIGGERED_RISK and d.immediate_warning

    def test_cooldown_recorded(self):
        d = route(_in(HELD, cooled_down=["macd_bearish_cross"]))
        assert d.route == R.NO_ACTION and d.rejected == ["macd_bearish_cross: in cooldown"]


class TestDowngrades:
    def test_shadow_mode(self):
        d = route(_in(HELD, ["death_cross"], enabled=False))
        assert d.route == R.TRIGGERED_RISK and d.effective_route == R.NOTIFY_ONLY
        assert d.shadow and d.budget_result == "disabled"

    def test_not_allowed(self):
        d = route(_in(HELD, ["death_cross"], ticker_allowed=False))
        assert d.effective_route == R.NOTIFY_ONLY and d.budget_result == "not_allowed"

    def test_budget_exhausted_per_ticker_and_global(self):
        d = route(_in(HELD, ["death_cross"], budget_ticker_used=1, max_per_ticker=1))
        assert d.effective_route == R.NOTIFY_ONLY and d.budget_result == "budget_exhausted"
        d = route(_in(HELD, ["death_cross"], budget_global_used=2, max_global=2))
        assert d.budget_result == "budget_exhausted"

    def test_fast_recheck_needs_plan_and_digest(self):
        d = route(_in(HELD, ["macd_bearish_cross"], digest_available=False))
        assert d.effective_route == R.NOTIFY_ONLY and d.budget_result == "input_missing"
        d = route(_in(HELD, ["macd_bearish_cross"], plan=None, fresh=PlanFreshness.MISSING, state=None))
        assert d.budget_result == "input_missing"

    def test_take_profit_never_budget_limited(self):
        d = route(_in(HELD, take_profit_fire=True, enabled=False, budget_global_used=99))
        assert d.effective_route == R.TAKE_PROFIT

    def test_ok(self):
        d = route(_in(HELD, ["macd_bearish_cross"]))
        assert d.effective_route == R.FAST_RECHECK and d.budget_result == "ok" and d.paid

    def test_deterministic(self):
        inp = _in(HELD, ["macd_bearish_cross", "atr_spike"], **SHOCK)
        assert route(inp).to_dict() == route(inp).to_dict()
