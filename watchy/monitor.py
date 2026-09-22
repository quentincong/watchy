"""Watchy 2.0 Tier 1 plan monitoring — the orchestration around the pure
plan_monitor / guards / messages functions.

Reads the current weekly plan and the persisted reminder state, classifies the
scan's price, persists the new state, and sends a deterministic Notify Only
reminder on a material transition. Never calls an LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from watchy.guards import StatusInputs, reminder_wording, select_status
from watchy.market_calendar import session_label
from watchy.messages import MessageContext, format_et, render_plan_card
from watchy.plan import (
    PlanFreshness,
    PositionState,
    ReminderState,
    Route,
    TelegramStatus,
    WeeklyPlan,
    plan_freshness,
)
from watchy.plan_monitor import Transition, detect_transition, plan_state

logger = logging.getLogger(__name__)


@dataclass
class PlanEval:
    ticker: str
    session: str
    plan: WeeklyPlan | None
    freshness: PlanFreshness
    state: ReminderState | None
    transition: Transition | None
    price: float | None
    price_ts: datetime | None
    atr: float | None
    data_stale: bool


def position_state_of(position_source: Any, ticker: str) -> PositionState:
    """HELD / WATCH / UNKNOWN. A lookup error is UNKNOWN, never "not held"."""
    if position_source is None:
        return PositionState.UNKNOWN
    try:
        pos = position_source.get_position(ticker)
    except Exception:  # noqa: BLE001
        logger.warning("position lookup failed for %s — position unknown", ticker, exc_info=True)
        return PositionState.UNKNOWN
    if pos is None or not getattr(pos, "quantity", 0):
        return PositionState.WATCH
    return PositionState.HELD


def data_is_stale(bundle: Any, now: datetime, max_age_min: float) -> bool:
    """Market data too old to support actionable wording.

    Stale when there is no price, the fetch is older than ``max_age_min``, or
    the latest daily bar is from an earlier session than today's (the feed did
    not update — e.g. a cached history served during a Yahoo outage).
    """
    if bundle is None or getattr(bundle, "current_price", None) is None:
        return True
    fetched = getattr(bundle, "fetched_at", None)
    if fetched is None or now - fetched > timedelta(minutes=max_age_min):
        return True
    bar = getattr(bundle, "timestamp", None)
    if bar is not None:
        try:
            bar_date = bar.date() if hasattr(bar, "date") else None
        except Exception:  # noqa: BLE001
            bar_date = None
        if bar_date is not None and bar_date < session_label(now):
            return True
    return False


def evaluate_plan(
    ticker: str,
    bundle: Any,
    config: Any,
    store: Any,
    now: datetime | None = None,
) -> PlanEval:
    """Classify the scan against the current plan (reads the store, no writes)."""
    from watchy.take_profit import bundle_avg_atr

    now = now or datetime.now(timezone.utc)
    session = session_label(now)
    plan = store.get_current_plan(ticker)
    fresh = plan_freshness(plan, session)
    price = getattr(bundle, "current_price", None)
    atr = bundle_avg_atr(bundle)
    wp = config.weekly_plan
    state = plan_state(plan, fresh, price, atr, approach_atr=wp.approach_atr)
    transition = None
    if state is not None and plan is not None:
        transition = detect_transition(
            store.get_reminder_state(ticker), plan.id, state, now,
            renotify_h=wp.renotify_h,
        )
    return PlanEval(
        ticker=ticker.upper(),
        session=session.isoformat(),
        plan=plan,
        freshness=fresh,
        state=state,
        transition=transition,
        price=price,
        price_ts=getattr(bundle, "fetched_at", None),
        atr=atr,
        data_stale=data_is_stale(bundle, now, wp.market_data_max_age_min),
    )


def persist_reminder(store: Any, ev: PlanEval) -> None:
    if ev.transition is None or ev.plan is None:
        return
    store.save_reminder_state(
        ev.ticker,
        plan_id=ev.plan.id,
        state=ev.state.value,
        state_since_ts=ev.transition.state_since_ts,
        notified=ev.transition.notified,
    )


def invalidate_if_broken(store: Any, ev: PlanEval, reason: str) -> bool:
    """Withdraw the plan once its thesis is broken (history is kept)."""
    if ev.plan is None or ev.plan.id is None or ev.freshness != PlanFreshness.ACTIVE:
        return False
    if store.deactivate_plan(ev.plan.id, status="invalidated"):
        logger.warning("PLAN_INVALIDATED %s id=%s reason=%s", ev.ticker, ev.plan.id, reason)
        return True
    return False


def source_freshness(plan: WeeklyPlan | None) -> str:
    if plan is None:
        return "no weekly plan"
    when = format_et(plan.created_ts, with_date=True)
    return f"weekly plan #{plan.id} from {when}" if when else f"weekly plan #{plan.id}"


def build_reminder(
    ev: PlanEval,
    position_state: PositionState,
    *,
    why_now: list[str],
    route: Route = Route.NOTIFY_ONLY,
    risk_trigger: bool = False,
    mode: str = "Tier 1 plan reminder; no new LLM analysis",
    notes: list[str] | None = None,
    stale_move_atr: float = 0.5,
) -> tuple[TelegramStatus, str]:
    """Deterministic status + rendered Notify Only message."""
    status = select_status(StatusInputs(
        position_state=position_state,
        freshness=ev.freshness,
        plan_state=ev.state,
        plan=ev.plan,
        data_stale=ev.data_stale,
        route=route,
        risk_trigger=risk_trigger,
        stale_move_atr=stale_move_atr,
    ))
    usable = ev.plan if ev.freshness in (
        PlanFreshness.ACTIVE, PlanFreshness.EXPIRED, PlanFreshness.INVALIDATED,
    ) else None
    guidance, dont = reminder_wording(
        usable, ev.state, ev.transition.kind if ev.transition else "",
        position_state, ev.price,
    )
    msg_notes = list(notes or [])
    if ev.data_stale:
        msg_notes.append("market data is stale — verify the live price before acting")
    ctx = MessageContext(
        ticker=ev.ticker,
        status=status,
        price=ev.price,
        price_ts=ev.price_ts,
        why_now=why_now,
        plan=ev.plan,
        plan_freshness=ev.freshness,
        plan_state=ev.state,
        guidance=guidance,
        dont_do=dont,
        verdict=ev.plan.upstream_verdict if ev.plan else "",
        advisor_decision=ev.plan.decision if ev.plan else "",
        mode=mode,
        source_freshness=source_freshness(ev.plan),
        notes=msg_notes,
    )
    return status, render_plan_card(ctx)
