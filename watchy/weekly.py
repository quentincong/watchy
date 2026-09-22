"""Watchy 2.0 Weekly Full — turn the weekly analysis into a validated plan.

The Weekly Full run is the existing Tier 2 pipeline (all four analysts, the
bull/bear debate and the full 3-way risk debate) plus a strict ``WEEKLY PLAN``
block requested from the advisor. This module builds the typed plan from that
output, validates it, and persists it as history. Building is pure; only
``persist_plan`` touches the store.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from watchy.market_calendar import session_close_utc, session_label, week_session_bounds
from watchy.plan import (
    NUMERIC_FIELDS,
    TEXT_FIELDS,
    PlanKind,
    WeeklyPlan,
    normalize_decision,
    validate_plan,
)

logger = logging.getLogger(__name__)


def plan_validity(now: datetime) -> tuple[str, str]:
    """(valid_from_session, expires_after_session) for a plan created at ``now``.

    Valid from today's session through the last session of this week. A run
    after the week's final session has *closed* (Friday after 16:00 ET, the
    Thursday close of a holiday-shortened week, or a weekend) plans for the
    coming trading week instead, so it can never be born expired. A run before
    or during the final session keeps this week.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = session_label(now)
    first, last = week_session_bounds(now)
    if today > last or (today == last and now >= session_close_utc(last)):
        monday = today + timedelta(days=7 - today.weekday())
        probe = datetime(monday.year, monday.month, monday.day, 17, 0, tzinfo=timezone.utc)
        first, last = week_session_bounds(probe)
        return first.isoformat(), last.isoformat()
    return max(today, first).isoformat(), last.isoformat()


def build_weekly_plan(
    ticker: str,
    advice: dict[str, Any] | None,
    result: dict[str, Any] | None,
    *,
    held: bool | None,
    input_price: float | None,
    input_price_ts: datetime | str | None,
    now: datetime | None = None,
    source_ref: dict[str, Any] | None = None,
) -> WeeklyPlan:
    """Build and validate a weekly base plan from the advisor output (pure)."""
    now = now or datetime.now(timezone.utc)
    valid_from, expires_after = plan_validity(now)
    if isinstance(input_price_ts, datetime):
        input_price_ts = input_price_ts.isoformat()
    plan = WeeklyPlan(
        ticker=ticker.upper(),
        kind=PlanKind.WEEKLY_BASE.value,
        upstream_verdict=str((result or {}).get("verdict") or ""),
        valid_from_session=valid_from,
        expires_after_session=expires_after,
        input_price=input_price,
        input_price_ts=input_price_ts or "",
        created_ts=now.isoformat(),
        source_ref=dict(source_ref or {}),
    )
    if not advice:
        return validate_plan(plan, parse_errors=["advisor produced no advice"])

    plan.decision = normalize_decision(advice.get("decision"), held)
    plan.urgency = str(advice.get("urgency") or "").upper()
    if advice.get("_advice_log_id") is not None:
        plan.source_ref.setdefault("advice_log_id", advice["_advice_log_id"])

    block = advice.get("_plan_block")
    parse_errors: list[str] = []
    if block is None:
        parse_errors.append("weekly plan block missing")
    else:
        parse_errors.extend(block.errors)
        for key in NUMERIC_FIELDS + TEXT_FIELDS:
            if key in block.fields:
                setattr(plan, key, block.fields[key])
    return validate_plan(plan, parse_errors=parse_errors)


def failed_weekly_plan(
    ticker: str,
    reason: str,
    *,
    now: datetime | None = None,
    source_ref: dict[str, Any] | None = None,
) -> WeeklyPlan:
    """An invalid record of a weekly run that failed before producing advice.

    Stored so the failure is visible in plan history; never actionable, and it
    does not supersede (or extend) the previous plan.
    """
    now = now or datetime.now(timezone.utc)
    valid_from, expires_after = plan_validity(now)
    plan = WeeklyPlan(
        ticker=ticker.upper(),
        valid_from_session=valid_from,
        expires_after_session=expires_after,
        created_ts=now.isoformat(),
        source_ref=dict(source_ref or {}),
    )
    return validate_plan(plan, parse_errors=[f"weekly run failed: {reason}"])


def persist_plan(store: Any, plan: WeeklyPlan, raw_output: str = "") -> int | None:
    """Insert the plan row (valid or invalid); log a greppable PLAN line.

    Never raises: losing the plan row must not lose the analysis message the
    user already paid for. Returns the row id, or None on a store failure.
    """
    try:
        pid = store.insert_plan(plan, raw_output=raw_output)
    except Exception:  # noqa: BLE001
        logger.exception("Plan persist failed for %s", plan.ticker)
        return None
    if plan.validation_errors:
        logger.warning(
            "PLAN_INVALID %s id=%s kind=%s errors=%s",
            plan.ticker, pid, plan.kind, plan.validation_errors,
        )
    else:
        logger.info(
            "PLAN_ACTIVE %s id=%s kind=%s decision=%s zone=%s-%s chase=%s inv=%s "
            "valid=%s..%s price=%s@%s",
            plan.ticker, pid, plan.kind, plan.decision, plan.buy_zone_low,
            plan.buy_zone_high, plan.chase_ceiling, plan.invalidation_level,
            plan.valid_from_session, plan.expires_after_session,
            plan.input_price, plan.input_price_ts,
        )
    return pid


def render_weekly_card(
    plan: WeeklyPlan,
    advice: dict[str, Any] | None,
    result: dict[str, Any],
    bundle: Any,
    now: datetime,
    *,
    held: bool | None = None,
    post_price: float | None = None,
    post_ts: datetime | None = None,
    stale_move_atr: float = 0.5,
    approach_atr: float = 0.5,
    max_age_min: float = 30.0,
) -> str:
    """The expanded weekly Telegram card appended to the advice message.

    The status is deterministic: the plan is reclassified against a price
    refreshed after the analysis finished (§9), so a move during a long batch
    cannot leave an actionable entry on a price that has already left the
    range. Pre-market the latest bar is the prior close; that is expected for
    the 10:02 UTC run and does not by itself mark the card stale.
    """
    from watchy.guards import StatusInputs, classify_alignment, revalidate, select_status
    from watchy.messages import MessageContext, format_et, render_plan_card
    from watchy.plan import PlanFreshness, PositionState
    from watchy.take_profit import bundle_avg_atr

    advice = advice or {}
    notes: list[str] = []
    if plan.validation_errors:
        freshness = PlanFreshness.INVALID
        shown = "; ".join(plan.validation_errors[:3])
        more = len(plan.validation_errors) - 3
        notes.append(
            "weekly plan failed validation and is NOT active: "
            + shown + (f" (+{more} more)" if more > 0 else "")
        )
    else:
        freshness = PlanFreshness.ACTIVE

    pre_ts = getattr(bundle, "fetched_at", None) if bundle is not None else None
    if pre_ts is None and plan.input_price_ts:
        try:
            pre_ts = datetime.fromisoformat(plan.input_price_ts)
        except ValueError:
            pre_ts = None
    atr = bundle_avg_atr(bundle)
    reval = revalidate(
        plan, freshness,
        pre_price=plan.input_price, pre_ts=pre_ts,
        post_price=post_price, post_ts=post_ts,
        atr=atr, now=now, approach_atr=approach_atr, max_age_min=max_age_min,
    )
    if reval.reason:
        notes.append(reval.reason)
    if reval.moved_atr is not None and reval.moved_atr > stale_move_atr:
        notes.append(
            f"price moved {reval.moved_atr:.2f} ATR while the analysis ran "
            f"(from {plan.input_price:,.2f}) — recheck before acting"
        )
    pstate = (
        PositionState.HELD if held else
        PositionState.WATCH if held is False else PositionState.UNKNOWN
    )
    decision = str(advice.get("decision") or "")
    status = select_status(StatusInputs(
        position_state=pstate,
        freshness=freshness,
        plan_state=reval.state,
        plan=plan if plan.is_valid else None,
        data_stale=reval.stale,
        advisor_decision=decision,
        advisor_urgency=str(advice.get("urgency") or ""),
        verdict=plan.upstream_verdict,
        price_moved_atr=reval.moved_atr,
        stale_move_atr=stale_move_atr,
    ))
    ctx = MessageContext(
        ticker=plan.ticker,
        status=status,
        price=reval.price,
        price_ts=reval.price_ts,
        why_now=["weekly full analysis for the first trading session of the week"],
        plan=plan,
        plan_freshness=freshness,
        plan_state=reval.state,
        guidance=plan.guidance if plan.is_valid else "",
        dont_do=plan.dont_do if plan.is_valid else "",
        verdict=plan.upstream_verdict,
        advisor_decision=decision,
        advisor_urgency=str(advice.get("urgency") or ""),
        alignment=classify_alignment(plan.upstream_verdict, decision),
        mode="Weekly Full — full TradingAgents analysis + advisor",
        source_freshness=f"analysis completed {format_et(now)}",
        notes=notes,
    )
    logger.info(
        "WEEKLY_CARD %s status=%s state=%s pre=%s post=%s moved_atr=%s",
        plan.ticker, status.value, reval.state, plan.input_price, post_price, reval.moved_atr,
    )
    return render_plan_card(ctx)
