"""Watchy 2.0 Telegram message rendering — pure, no network.

Every material message carries the context a reader needs without opening the
logs: price with timestamp, the deterministic status, why it was sent now, the
weekly-plan context, guidance, a specific "do not", invalidation and expiry,
verdict vs advisor alignment, the analysis mode and source freshness, and the
reminder that Watchy is advisory. The status is chosen by watchy.guards; the
renderer never upgrades it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from watchy.plan import PlanFreshness, ReminderState, TelegramStatus, WeeklyPlan

try:
    from zoneinfo import ZoneInfo

    _NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _NY = None

ADVISORY_FOOTER = "Advisory only — Watchy does not place orders; you decide and execute."

_STATUS_ICON = {
    TelegramStatus.ACT_NOW: "🟢",
    TelegramStatus.WAIT_FOR_LIMIT: "🟡",
    TelegramStatus.DO_NOT_CHASE: "⛔",
    TelegramStatus.RISK_REVIEW: "🔴",
    TelegramStatus.PLAN_INVALID: "🔴",
    TelegramStatus.STALE: "⚪",
    TelegramStatus.INFORMATION_ONLY: "ℹ️",
}

_STATE_LABEL = {
    ReminderState.OUTSIDE: "outside the plan's zones",
    ReminderState.APPROACHING_BUY: "approaching the buy zone",
    ReminderState.IN_BUY_ZONE: "inside the buy zone",
    ReminderState.ABOVE_CHASE: "above the chase ceiling",
    ReminderState.INVALIDATED: "beyond the invalidation level",
    ReminderState.IN_TAKE_PROFIT: "in take-profit / resistance territory",
    ReminderState.EXPIRED: "plan expired",
}


def esc(text: object) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class MessageContext:
    ticker: str
    status: TelegramStatus
    price: float | None = None
    price_ts: datetime | str | None = None
    why_now: list[str] = field(default_factory=list)
    plan: WeeklyPlan | None = None
    plan_freshness: PlanFreshness | None = None
    plan_state: ReminderState | None = None
    override: WeeklyPlan | None = None
    guidance: str = ""
    dont_do: str = ""
    verdict: str = ""
    advisor_decision: str = ""
    advisor_urgency: str = ""
    alignment: str = ""
    mode: str = ""
    source_freshness: str = ""
    take_profit: str = ""
    notes: list[str] = field(default_factory=list)


def state_label(state: ReminderState | None) -> str:
    return _STATE_LABEL.get(state, "") if state is not None else ""


def format_et(ts: datetime | str | None, with_date: bool = False) -> str:
    """Render a timestamp in US/Eastern ("10:32 ET"), or "" when unknown."""
    if ts is None or ts == "":
        return ""
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return esc(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(_NY) if _NY is not None else ts
    suffix = " ET" if _NY is not None else " UTC"
    fmt = "%a %Y-%m-%d %H:%M" if with_date else "%H:%M"
    return local.strftime(fmt) + suffix


def _money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "n/a"


def _session(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%a %Y-%m-%d")
    except (TypeError, ValueError):
        return esc(iso or "?")


def plan_summary(plan: WeeklyPlan) -> str:
    """One line of plan levels, e.g. 'BUY; bullish while above $116; buy zone
    $121.00–$123.00; chase ceiling $125.00; resistance $132.00–$135.00'."""
    parts = [esc(plan.decision or "?")]
    if plan.invalidation_level is not None:
        parts.append(f"valid while above {_money(plan.invalidation_level)}")
    if plan.has_buy_zone:
        parts.append(f"buy zone {_money(plan.buy_zone_low)}–{_money(plan.buy_zone_high)}")
    if plan.chase_ceiling is not None:
        parts.append(f"chase ceiling {_money(plan.chase_ceiling)}")
    if plan.resistance_low is not None or plan.resistance_high is not None:
        lo = _money(plan.resistance_low) if plan.resistance_low is not None else "?"
        hi = _money(plan.resistance_high) if plan.resistance_high is not None else "?"
        parts.append(f"resistance {lo}–{hi}")
    if plan.take_profit_price is not None:
        parts.append(f"take-profit {_money(plan.take_profit_price)}")
    return "; ".join(parts)


def render_plan_card(ctx: MessageContext) -> str:
    """The Watchy 2.0 message body (Telegram HTML, every dynamic value escaped)."""
    status = ctx.status
    lines = [f"<b>{esc(ctx.ticker)} — {_STATUS_ICON.get(status, '')} {esc(status.value)}</b>"]
    price_line = f"Price: {_money(ctx.price)}"
    when = format_et(ctx.price_ts)
    price_line += f" as of {when}" if when else " (timestamp unknown)"
    lines.append(price_line)

    if ctx.why_now:
        lines.append("")
        lines.append("<b>Why now:</b> " + esc("; ".join(ctx.why_now)) + ".")

    plan = ctx.plan
    fresh = ctx.plan_freshness
    if plan is None or fresh == PlanFreshness.MISSING:
        lines.append("<b>Weekly plan:</b> none active — entry guidance is information-only.")
    elif fresh == PlanFreshness.INVALID:
        lines.append("<b>Weekly plan:</b> failed validation — not actionable (see journal).")
    elif fresh == PlanFreshness.EXPIRED:
        lines.append(
            f"<b>Weekly plan:</b> expired after {_session(plan.expires_after_session)} "
            "— recheck required; its levels below are history, not guidance."
        )
        lines.append(f"<i>Expired plan:</i> {plan_summary(plan)}")
    else:
        lines.append(f"<b>Weekly plan:</b> {plan_summary(plan)}")
        if plan.thesis:
            lines.append(f"<b>Thesis:</b> {esc(plan.thesis)}")
    if ctx.plan_state is not None and fresh not in (None, PlanFreshness.MISSING, PlanFreshness.INVALID):
        lines.append(f"<b>Price vs plan:</b> {esc(state_label(ctx.plan_state))}")
    if ctx.override is not None and ctx.override.guidance:
        lines.append(
            f"<b>Event override:</b> {esc(ctx.override.decision)} — {esc(ctx.override.guidance)}"
        )

    guidance = ctx.guidance
    if guidance:
        lines.append(f"<b>Guidance:</b> {esc(guidance)}")
    if ctx.take_profit:
        lines.append(f"<b>💰 Take-Profit:</b> {esc(ctx.take_profit)}")
    if ctx.dont_do:
        lines.append(f"<b>Do not:</b> {esc(ctx.dont_do)}")

    if plan is not None and fresh not in (PlanFreshness.MISSING, PlanFreshness.INVALID):
        inv_bits = []
        if plan.invalidation_condition:
            inv_bits.append(esc(plan.invalidation_condition))
        if plan.invalidation_level is not None:
            inv_bits.append(f"level {_money(plan.invalidation_level)}")
        inv = "; ".join(inv_bits) or "none stated"
        lines.append(
            f"<b>Invalidation:</b> {inv}. Plan expires after "
            f"{_session(plan.expires_after_session)} session."
        )

    if ctx.verdict or ctx.advisor_decision or ctx.alignment:
        adv = esc(ctx.advisor_decision or "—")
        if ctx.advisor_urgency:
            adv += f" ({esc(ctx.advisor_urgency)})"
        align = esc(ctx.alignment or "unknown")
        if ctx.alignment == "conflict":
            align = "conflict — human review required"
        lines.append(
            f"<b>Verdict:</b> {esc(ctx.verdict or '—')} | <b>Advisor:</b> {adv} | "
            f"<b>Alignment:</b> {align}"
        )

    mode = esc(ctx.mode or "—")
    if ctx.source_freshness:
        mode += f"; {esc(ctx.source_freshness)}"
    lines.append(f"<b>Mode:</b> {mode}")
    for note in ctx.notes:
        lines.append(f"⚠ {esc(note)}")
    lines.append(f"<i>{ADVISORY_FOOTER}</i>")
    return "\n".join(lines)
