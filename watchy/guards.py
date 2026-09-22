"""Watchy 2.0 safety guards — deterministic status selection and wording.

Pure functions. The Telegram status is decided here from price, plan, data
freshness, route and (when an analysis ran) the advisor and upstream verdict.
The LLM may supply reasoning but can never override these guards:

* stale market data, an expired/missing/invalid plan, or a price that moved
  past ``stale_move_atr`` since the analysis can never read as actionable;
* a price above the chase ceiling is ``DO NOT CHASE`` whatever the advice;
* a price beyond invalidation is ``PLAN INVALID`` / ``RISK REVIEW``;
* a verdict/advisor conflict is shown for human review, never ``ACT NOW``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from watchy.plan import (
    PlanFreshness,
    PositionState,
    ReminderState,
    Route,
    TelegramStatus,
    WeeklyPlan,
    direction,
    is_bullish_buy_plan,
)


@dataclass
class StatusInputs:
    position_state: PositionState
    freshness: PlanFreshness
    plan_state: ReminderState | None = None
    plan: WeeklyPlan | None = None
    data_stale: bool = False
    route: Route = Route.NOTIFY_ONLY
    risk_trigger: bool = False
    advisor_decision: str = ""
    advisor_urgency: str = ""
    verdict: str = ""                 # upstream TradingAgents verdict (BUY/SELL/HOLD)
    price_moved_atr: float | None = None
    stale_move_atr: float = 0.5
    # The router wanted an interpretation (Fast Recheck / Triggered Risk) that
    # did not run — shadow mode, budget, missing inputs or a failure. Without
    # it an entry status would rest on a mechanical reading of a changed
    # situation, so entry wording is capped at INFORMATION ONLY.
    interpretation_pending: bool = False


def select_status(inp: StatusInputs) -> TelegramStatus:
    """The one deterministic Telegram status for a message (see module doc)."""
    status = _select_status(inp)
    if inp.interpretation_pending and status in (
        TelegramStatus.ACT_NOW, TelegramStatus.WAIT_FOR_LIMIT,
    ):
        return TelegramStatus.INFORMATION_ONLY
    return status


def classify_alignment(verdict: str | None, advisor: str | None) -> str:
    """"agree" / "conflict" / "unknown" between the upstream verdict and the
    advisor (or, for a mechanical reminder, the weekly plan's decision).

    Any difference in direction is a conflict to show for human review; only
    the entry-blocking subset (see ``entry_blocked``) changes the status.
    """
    v, a = direction(verdict), direction(advisor)
    if not v or not a:
        return "unknown"
    return "agree" if v == a else "conflict"


def entry_blocked(verdict: str | None, advisor: str | None, plan: WeeklyPlan | None) -> bool:
    """A buy must never be presented as actionable when the upstream verdict
    is SELL or HOLD and the advisor says BUY/ADD, or when the verdict is SELL
    and the plan still carries a bullish buy zone."""
    v = (verdict or "").upper()
    if direction(advisor) == "bullish" and v in ("SELL", "HOLD"):
        return True
    return v == "SELL" and is_bullish_buy_plan(plan)


def _select_status(inp: StatusInputs) -> TelegramStatus:
    held_or_unknown = inp.position_state != PositionState.WATCH
    if inp.data_stale:
        return TelegramStatus.STALE
    if inp.plan_state == ReminderState.INVALIDATED or inp.freshness == PlanFreshness.INVALIDATED:
        return TelegramStatus.RISK_REVIEW if held_or_unknown else TelegramStatus.PLAN_INVALID
    if inp.route == Route.TRIGGERED_RISK or (inp.risk_trigger and held_or_unknown):
        return TelegramStatus.RISK_REVIEW
    analysed = bool(inp.advisor_decision)
    if (
        inp.freshness == PlanFreshness.ACTIVE
        and inp.plan_state == ReminderState.ABOVE_CHASE
        and (is_bullish_buy_plan(inp.plan) or direction(inp.advisor_decision) == "bullish")
    ):
        # Above the chase ceiling is DO NOT CHASE whatever the analysis says,
        # including when the price ran there while the analysis was running.
        return TelegramStatus.DO_NOT_CHASE
    if (
        analysed
        and inp.price_moved_atr is not None
        and inp.price_moved_atr > inp.stale_move_atr
    ):
        return TelegramStatus.STALE
    if inp.freshness == PlanFreshness.EXPIRED or inp.plan_state == ReminderState.EXPIRED:
        return TelegramStatus.STALE
    if inp.freshness != PlanFreshness.ACTIVE:
        return TelegramStatus.INFORMATION_ONLY

    advisor_dir = direction(inp.advisor_decision)
    bullish_plan = is_bullish_buy_plan(inp.plan)
    proxy = inp.advisor_decision or (inp.plan.decision if inp.plan else "")
    alignment = classify_alignment(inp.verdict, proxy)
    blocked = entry_blocked(inp.verdict, proxy, inp.plan)

    if analysed:
        if advisor_dir == "bullish":
            if blocked or not bullish_plan:
                return TelegramStatus.INFORMATION_ONLY
            if inp.plan_state == ReminderState.IN_BUY_ZONE:
                if inp.advisor_urgency == "HIGH" and alignment == "agree":
                    return TelegramStatus.ACT_NOW
                return TelegramStatus.WAIT_FOR_LIMIT
            if inp.plan_state == ReminderState.APPROACHING_BUY:
                return TelegramStatus.WAIT_FOR_LIMIT
            return TelegramStatus.INFORMATION_ONLY
        if advisor_dir == "bearish":
            if inp.position_state != PositionState.HELD:
                return TelegramStatus.INFORMATION_ONLY
            if inp.advisor_urgency == "HIGH" and alignment == "agree":
                return TelegramStatus.ACT_NOW
            return TelegramStatus.WAIT_FOR_LIMIT
        return TelegramStatus.INFORMATION_ONLY

    # Tier 1 mechanical reminder — never ACT NOW without a fresh analysis.
    if blocked:
        return TelegramStatus.INFORMATION_ONLY
    if bullish_plan and inp.plan_state in (ReminderState.IN_BUY_ZONE, ReminderState.APPROACHING_BUY):
        return TelegramStatus.WAIT_FOR_LIMIT
    if (
        inp.plan_state == ReminderState.IN_TAKE_PROFIT
        and inp.position_state == PositionState.HELD
        and inp.plan is not None
        and inp.plan.take_profit_price is not None
    ):
        return TelegramStatus.WAIT_FOR_LIMIT
    return TelegramStatus.INFORMATION_ONLY


def _m(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "n/a"


def reminder_wording(
    plan: WeeklyPlan | None,
    state: ReminderState | None,
    transition_kind: str,
    position_state: PositionState,
    price: float | None,
) -> tuple[str, str]:
    """Deterministic (guidance, do-not) text for a Notify Only reminder."""
    if plan is None or state is None:
        return (
            "no valid weekly plan — treat this as information only",
            "do not open or add a position from this alert alone",
        )
    dont = plan.dont_do or (
        f"do not chase above {_m(plan.chase_ceiling)}" if plan.chase_ceiling else ""
    )
    held = position_state == PositionState.HELD
    if state == ReminderState.EXPIRED:
        return (
            f"the weekly plan expired after {plan.expires_after_session}; its levels are "
            "history until the next Weekly Full succeeds",
            "do not act on last week's levels",
        )
    if state == ReminderState.INVALIDATED:
        return (
            f"price {_m(price)} is below the invalidation level {_m(plan.invalidation_level)} — "
            + ("review the position and your stop" if held or position_state == PositionState.UNKNOWN
               else "the watch thesis is broken; the plan is withdrawn"),
            "do not enter or add while the plan is invalid",
        )
    if state == ReminderState.ABOVE_CHASE:
        return (
            f"price {_m(price)} is above the chase ceiling {_m(plan.chase_ceiling)} — "
            "wait for a pullback into the plan or let it go",
            dont or f"do not chase above {_m(plan.chase_ceiling)}",
        )
    if state == ReminderState.IN_BUY_ZONE:
        return (
            (plan.guidance or "the planned entry zone is reached")
            + f" (buy zone {_m(plan.buy_zone_low)}–{_m(plan.buy_zone_high)})",
            dont,
        )
    if state == ReminderState.APPROACHING_BUY:
        return (
            f"price is approaching the buy zone {_m(plan.buy_zone_low)}–{_m(plan.buy_zone_high)}; "
            "prepare a limit order inside the zone if the thesis still fits your portfolio",
            dont,
        )
    if state == ReminderState.IN_TAKE_PROFIT:
        tp = plan.take_profit_price
        if held:
            text = "price reached the plan's resistance / take-profit territory"
            text += f" — consider keeping a sell-limit near {_m(tp)}" if tp else " — review the trim condition"
            if plan.trim_condition:
                text += f"; trim condition: {plan.trim_condition}"
            return text, dont
        return "price reached resistance — no entry here", dont or "do not buy into resistance"
    if transition_kind == "left_buy_zone":
        return (
            "price left the buy zone; a resting limit inside the zone may not fill — "
            "no chase needed",
            dont,
        )
    return plan.guidance, dont


@dataclass
class Revalidation:
    """The price an actionable message is rendered against, re-checked after
    the analysis finished (the price may have moved while the LLM ran)."""

    price: float | None
    price_ts: datetime | None
    moved_atr: float | None
    state: ReminderState | None
    stale: bool
    reason: str = ""


def revalidate(
    plan: WeeklyPlan | None,
    freshness: PlanFreshness,
    *,
    pre_price: float | None,
    pre_ts: datetime | None,
    post_price: float | None,
    post_ts: datetime | None,
    atr: float | None,
    now: datetime,
    approach_atr: float = 0.5,
    max_age_min: float = 30.0,
) -> Revalidation:
    """Reclassify against the plan at render time (pure).

    Uses the refreshed post-analysis price when available, else the
    pre-analysis price if it is still fresh enough; otherwise the result is
    stale and nothing downstream may read as actionable.
    """
    from watchy.plan_monitor import plan_state

    price, ts = (post_price, post_ts) if post_price is not None else (pre_price, pre_ts)
    reason = "" if post_price is not None else "price not refreshed after the analysis"
    stale = price is None or ts is None or now - ts > timedelta(minutes=max_age_min)
    if stale and not reason:
        reason = "price is older than the freshness limit"
    moved = None
    if pre_price is not None and post_price is not None and atr and atr > 0:
        moved = abs(post_price - pre_price) / atr
    state = plan_state(plan, freshness, price, atr, approach_atr=approach_atr)
    return Revalidation(price, ts, moved, state, stale, reason)
