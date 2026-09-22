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

from watchy.guards import (
    StatusInputs,
    classify_alignment,
    reminder_wording,
    select_status,
)
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


def fetch_live_price(ticker: str) -> tuple[float | None, datetime | None]:
    """A refreshed price for post-analysis revalidation: (price, fetched_at).

    Goes through the scanner's cached fetch (yfinance-cache refetches the
    forming bar once it is older than ~10 minutes). Returns (None, None) on
    any failure — the guard then falls back to the pre-analysis price if it
    is still fresh, else labels the message stale.
    """
    from watchy.positions import _latest_price

    try:
        price = _latest_price(ticker)
    except Exception:  # noqa: BLE001
        logger.warning("live price refresh failed for %s", ticker, exc_info=True)
        return None, None
    if price is None:
        return None, None
    return price, datetime.now(timezone.utc)


def data_is_stale(
    bundle: Any, now: datetime, max_age_min: float, require_session_bar: bool = True
) -> bool:
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
    if require_session_bar and bar is not None:
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
    interpretation_pending: bool = False,
) -> tuple[TelegramStatus, str]:
    """Deterministic status + rendered Notify Only message."""
    status = select_status(StatusInputs(
        interpretation_pending=interpretation_pending,
        position_state=position_state,
        freshness=ev.freshness,
        plan_state=ev.state,
        plan=ev.plan,
        data_stale=ev.data_stale,
        route=route,
        risk_trigger=risk_trigger,
        stale_move_atr=stale_move_atr,
        verdict=ev.plan.upstream_verdict if ev.plan else "",
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
        alignment=classify_alignment(
            ev.plan.upstream_verdict, ev.plan.decision) if ev.plan else "",
        mode=mode,
        source_freshness=source_freshness(ev.plan),
        notes=msg_notes,
    )
    return status, render_plan_card(ctx)


# --- Watchy 2.0 Tier 1 scan (weekly mode) ----------------------------------

_DOWNGRADE_NOTES = {
    "disabled": "Shadow mode: paid analysis is disabled — would have run {route}",
    "not_allowed": "{route} not run: ticker is not enabled for paid analysis",
    "input_missing": "{route} not run: no valid weekly plan/digest — Notify Only fallback",
    "budget_exhausted": "{route} not run: paid-analysis budget exhausted for this session",
    "unavailable": "{route} not run: paid analysis unavailable",
    "busy": "{route} not run: another analysis for this ticker is in progress",
}


def scan_planned(
    ticker: str,
    bundle: Any,
    prev: dict[str, Any],
    config: Any,
    store: Any,
    notifier: Any,
    *,
    pipeline_runner: Any = None,
    ticker_locks: Any = None,
    now: datetime | None = None,
) -> list[str]:
    """One Watchy 2.0 Tier 1 evaluation: triggers → one route → one action.

    Technical signals, plan transitions and the take-profit gate are collected,
    routed by the pure router, and at most one analysis runs. Every evaluation
    writes a structured ROUTE record (journal + route_log table).
    """
    import json
    import time

    from watchy import tier1 as t1
    from watchy.orchestrator import get_cooldown_hours
    from watchy.router import RouterInput, route

    now = now or datetime.now(timezone.utc)
    started = time.monotonic()
    fired = t1.detect_signals(bundle, prev)
    actionable: list[str] = []
    cooled: list[str] = []
    for sig in fired:
        if store.is_in_cooldown(ticker, sig, get_cooldown_hours(sig, config.cooldown)):
            cooled.append(sig)
        else:
            actionable.append(sig)
    details = t1._bundle_summary(bundle)
    for sig in actionable:
        store.log_signal(ticker, sig, details)   # arms the per-signal cooldown

    position_source = t1.get_position_source(config)
    pstate = position_state_of(position_source, ticker)
    tp_zone, tp_qty, tp_fire, tp_gain = t1._take_profit_decision(
        ticker, prev, config, store, position_source, False,
    )

    ev = evaluate_plan(ticker, bundle, config, store, now)
    transition = ev.transition.kind if ev.transition is not None and ev.transition.notify else ""
    ta = config.triggered_analysis
    allowed = not ta.tickers or ticker.upper() in {t.upper() for t in ta.tickers}
    decision = route(RouterInput(
        ticker=ticker,
        position_state=pstate,
        plan=ev.plan,
        freshness=ev.freshness,
        plan_state=ev.state,
        plan_transition=transition,
        signals=actionable,
        cooled_down=cooled,
        price=ev.price,
        prev_close=getattr(bundle, "prev_close", None),
        atr=ev.atr,
        take_profit_fire=tp_fire,
        triggered_enabled=ta.enabled,
        ticker_allowed=allowed,
        digest_available=weekly_digest_available(ticker),
        budget_ticker_used=store.count_triggered(ev.session, ticker),
        budget_global_used=store.count_triggered(ev.session),
        max_per_ticker=ta.max_per_ticker_per_trading_day,
        max_global=ta.max_global_per_trading_day,
        bearish_shock_atr=ta.bearish_shock_atr,
    ))

    record: dict[str, Any] = {
        "evaluated_ts": now.isoformat(),
        "session": ev.session,
        "ticker": ticker.upper(),
        "position_state": pstate.value,
        "triggers": fired,
        "cooled_down": cooled,
        "plan_id": ev.plan.id if ev.plan else None,
        "plan_freshness": ev.freshness.value,
        "plan_state": ev.state.value if ev.state else None,
        "plan_transition": ev.transition.kind if ev.transition else None,
        "price": ev.price,
        "prev_close": getattr(bundle, "prev_close", None),
        "atr": ev.atr,
        "price_ts": ev.price_ts.isoformat() if ev.price_ts else None,
        "data_stale": ev.data_stale,
        "take_profit_fire": tp_fire,
        **{k: v for k, v in decision.to_dict().items() if k != "candidates"},
        "candidates": decision.to_dict()["candidates"],
        "llm_invoked": False,
        "status": None,
        "notified": False,
    }

    effective = decision.effective_route
    if effective == Route.TAKE_PROFIT:
        t1._fire_take_profit(ticker, bundle, config, store, notifier, position_source, tp_gain)
        record["llm_invoked"] = True
        record["status"] = "take_profit_alert"
        record["notified"] = True

    if effective in (Route.FAST_RECHECK, Route.TRIGGERED_RISK):
        from watchy import triggered

        outcome = triggered.execute(
            decision, ev, pstate, bundle, config, store, notifier, position_source,
            pipeline_runner=pipeline_runner, ticker_locks=ticker_locks, now=now,
        )
        record.update(outcome)
        effective = Route(outcome.get("effective_route", effective.value))

    if effective == Route.NOTIFY_ONLY and record["status"] is None:
        shown = ev
        if decision.invalidate_plan:
            from dataclasses import replace

            shown = replace(ev, freshness=PlanFreshness.INVALIDATED)
        notes = []
        budget = record.get("budget_result", decision.budget_result)
        if decision.route != Route.NOTIFY_ONLY and budget in _DOWNGRADE_NOTES:
            notes.append(_DOWNGRADE_NOTES[budget].format(
                route=decision.route.value.replace("_", " ").title()))
        if record.get("analysis_error"):
            notes.append(f"analysis failed: {record['analysis_error']}")
        status, text = build_reminder(
            shown, pstate,
            why_now=decision.reasons,
            route=decision.route,
            risk_trigger=decision.risk,
            notes=notes,
            stale_move_atr=config.weekly_plan.stale_move_atr,
            interpretation_pending=decision.route in (Route.FAST_RECHECK, Route.TRIGGERED_RISK),
        )
        record["status"] = status.value
        record["notified"] = bool(notifier.send(text))
    if decision.invalidate_plan:
        invalidate_if_broken(store, ev, "; ".join(decision.reasons))

    persist_reminder(store, ev)
    t1._update_state(store, bundle, ticker, take_profit_zone=tp_zone, quantity=tp_qty)
    record["latency_s"] = round(time.monotonic() - started, 3)
    try:
        store.log_route(record)
    except Exception:  # noqa: BLE001
        logger.exception("route_log write failed for %s", ticker)
    logger.info("ROUTE %s", json.dumps(record, default=str, separators=(",", ":")))
    return actionable


def weekly_digest_available(ticker: str) -> bool:
    from watchy.digest_store import _path

    try:
        return _path(ticker, kind="weekly").exists()
    except Exception:  # noqa: BLE001
        return False
