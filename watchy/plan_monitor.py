"""Watchy 2.0 plan monitoring — pure price-to-plan classification and
transition detection. No I/O, no LLM.

Tier 1 classifies each scan's price against the active weekly plan and only a
*transition* into a material state notifies. The persisted reminder state
(``plan_reminder_state``) makes that deduplication survive daemon restarts;
a new weekly plan re-arms every reminder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from watchy.plan import (
    PlanFreshness,
    ReminderState,
    WeeklyPlan,
    is_bullish_buy_plan,
)

# Entering one of these states is worth a reminder.
NOTIFY_STATES = frozenset({
    ReminderState.APPROACHING_BUY,
    ReminderState.IN_BUY_ZONE,
    ReminderState.ABOVE_CHASE,
    ReminderState.INVALIDATED,
    ReminderState.IN_TAKE_PROFIT,
    ReminderState.EXPIRED,
})
LEFT_BUY_ZONE = "left_buy_zone"


def classify_price(
    plan: WeeklyPlan,
    price: float | None,
    atr: float | None,
    *,
    approach_atr: float = 0.5,
) -> ReminderState:
    """Where ``price`` sits relative to a usable plan.

    Precedence, most protective first: invalidated, take-profit / resistance
    territory, above the chase ceiling, inside the buy zone, approaching it
    from above (within ``approach_atr`` ATRs), otherwise outside. A price
    below the buy zone but above invalidation reads OUTSIDE: it is a deeper
    discount the weekly plan did not authorise as an entry.
    """
    if price is None:
        return ReminderState.OUTSIDE
    if plan.invalidation_level is not None and price < plan.invalidation_level:
        return ReminderState.INVALIDATED
    tp_floor = _take_profit_floor(plan)
    if tp_floor is not None and price >= tp_floor:
        return ReminderState.IN_TAKE_PROFIT
    if plan.chase_ceiling is not None and price > plan.chase_ceiling:
        return ReminderState.ABOVE_CHASE
    if plan.has_buy_zone:
        if plan.buy_zone_low <= price <= plan.buy_zone_high:
            return ReminderState.IN_BUY_ZONE
        if (
            is_bullish_buy_plan(plan)
            and atr
            and atr > 0
            and plan.buy_zone_high < price <= plan.buy_zone_high + approach_atr * atr
        ):
            return ReminderState.APPROACHING_BUY
    return ReminderState.OUTSIDE


def _take_profit_floor(plan: WeeklyPlan) -> float | None:
    candidates = [
        v for v in (plan.resistance_low, plan.take_profit_price) if v is not None
    ]
    return min(candidates) if candidates else None


def plan_state(
    plan: WeeklyPlan | None,
    freshness: PlanFreshness,
    price: float | None,
    atr: float | None,
    *,
    approach_atr: float = 0.5,
) -> ReminderState | None:
    """Reminder state for this scan, or None when there is no usable plan to
    remind about (missing, never valid, not yet valid, or invalidated)."""
    if plan is None:
        return None
    if freshness == PlanFreshness.EXPIRED:
        return ReminderState.EXPIRED
    if freshness != PlanFreshness.ACTIVE:
        return None
    return classify_price(plan, price, atr, approach_atr=approach_atr)


@dataclass
class Transition:
    """Result of comparing this scan's plan state with the persisted one."""

    state: ReminderState
    prev_state: str | None
    notify: bool
    kind: str                      # entered / left_buy_zone / unchanged / quiet / suppressed
    state_since_ts: str
    notified: dict[str, str] = field(default_factory=dict)
    rearmed: bool = False

    @property
    def reason(self) -> str:
        if self.kind == LEFT_BUY_ZONE:
            return "price left the buy zone"
        if self.kind == "entered":
            return f"price is now {self.state.value.replace('_', ' ')}"
        return ""


def detect_transition(
    prev: dict,
    plan_id: int | None,
    state: ReminderState,
    now: datetime,
    *,
    renotify_h: float = 6.0,
) -> Transition:
    """Decide whether this scan's plan state deserves a reminder.

    * Same state as last scan (same plan) → silent.
    * Entering a NOTIFY_STATES member, or leaving the buy zone → notify,
      unless the same reminder already went out within ``renotify_h`` for this
      plan (a price flapping across a boundary each 30-minute scan).
    * A different plan id (new weekly plan) re-arms everything.
    """
    now_iso = now.isoformat()
    rearmed = bool(prev) and prev.get("plan_id") != plan_id
    if not prev or rearmed:
        prev_state = None
        notified: dict[str, str] = {}
        since = now_iso
    else:
        prev_state = prev.get("state")
        notified = dict(prev.get("notified") or {})
        since = prev.get("state_since_ts") or now_iso

    if prev_state == state.value:
        return Transition(state, prev_state, False, "unchanged", since, notified, rearmed)

    since = now_iso
    if state in NOTIFY_STATES:
        key, kind = state.value, "entered"
    elif prev_state == ReminderState.IN_BUY_ZONE.value:
        key, kind = LEFT_BUY_ZONE, LEFT_BUY_ZONE
    else:
        return Transition(state, prev_state, False, "quiet", since, notified, rearmed)

    last = notified.get(key)
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(hours=renotify_h):
                return Transition(state, prev_state, False, "suppressed", since, notified, rearmed)
        except ValueError:
            pass
    notified[key] = now_iso
    return Transition(state, prev_state, True, kind, since, notified, rearmed)
