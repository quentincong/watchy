"""Watchy 2.0 contracts — weekly plan, reminder states, routes, Telegram status.

Pure and LLM-free. The weekly Full run asks the advisor to append a strict
``WEEKLY PLAN`` block; this module parses that block, validates it, and decides
whether the plan may become actionable. Everything downstream (plan monitoring,
routing, the price guard, the Telegram renderer) reads the typed ``WeeklyPlan``
defined here, never raw LLM text.

The guiding rule: invalid, incomplete, expired or missing plans are stored for
diagnosis but can never produce an actionable entry instruction.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

PLAN_SCHEMA_VERSION = 1


class Decision(str, Enum):
    BUY = "BUY"
    ADD = "ADD"
    HOLD = "HOLD"
    TRIM = "TRIM"
    SELL = "SELL"
    WATCH = "WATCH"


class Urgency(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class PlanKind(str, Enum):
    WEEKLY_BASE = "weekly_base"
    EVENT_OVERRIDE = "event_override"


class PlanStatus(str, Enum):
    ACTIVE = "active"            # validated and in force (until its expiry date)
    INVALID = "invalid"          # failed validation — diagnosis only, never actionable
    SUPERSEDED = "superseded"    # replaced by a newer weekly base plan
    DEACTIVATED = "deactivated"  # manually expired/deactivated; history kept
    INVALIDATED = "invalidated"  # thesis broken by price (or a watch-only death cross)


class ReminderState(str, Enum):
    """Where the price sits relative to the active plan (transition-notified)."""
    OUTSIDE = "outside"
    APPROACHING_BUY = "approaching_buy_zone"
    IN_BUY_ZONE = "inside_buy_zone"
    ABOVE_CHASE = "above_chase_ceiling"
    INVALIDATED = "invalidated"
    IN_TAKE_PROFIT = "inside_take_profit_zone"
    EXPIRED = "expired"


class PlanFreshness(str, Enum):
    ACTIVE = "active"
    MISSING = "missing"
    INVALID = "invalid"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    INVALIDATED = "invalidated"


class Route(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    TRIGGERED_RISK = "TRIGGERED_RISK"
    FAST_RECHECK = "FAST_RECHECK"
    NOTIFY_ONLY = "NOTIFY_ONLY"
    NO_ACTION = "NO_ACTION"


class TelegramStatus(str, Enum):
    ACT_NOW = "ACT NOW"
    WAIT_FOR_LIMIT = "WAIT FOR LIMIT"
    DO_NOT_CHASE = "DO NOT CHASE"
    RISK_REVIEW = "RISK REVIEW"
    PLAN_INVALID = "PLAN INVALID"
    STALE = "STALE — RECHECK REQUIRED"
    INFORMATION_ONLY = "INFORMATION ONLY"


class PositionState(str, Enum):
    HELD = "held"
    WATCH = "watch_only"
    UNKNOWN = "unknown"


BULLISH_DECISIONS = frozenset({Decision.BUY.value, Decision.ADD.value})
BEARISH_DECISIONS = frozenset({Decision.TRIM.value, Decision.SELL.value})

NUMERIC_FIELDS = (
    "buy_zone_low",
    "buy_zone_high",
    "chase_ceiling",
    "invalidation_level",
    "resistance_low",
    "resistance_high",
    "take_profit_price",
)
TEXT_FIELDS = (
    "thesis",
    "invalidation_condition",
    "trim_condition",
    "guidance",
    "dont_do",
)

# Block label -> field name. The labels are what the advisor is told to write.
_BLOCK_KEYS = {
    "thesis": "thesis",
    "buy-zone-low": "buy_zone_low",
    "buy-zone-high": "buy_zone_high",
    "chase-ceiling": "chase_ceiling",
    "invalidation-level": "invalidation_level",
    "invalidation-condition": "invalidation_condition",
    "trim-condition": "trim_condition",
    "resistance-low": "resistance_low",
    "resistance-high": "resistance_high",
    "take-profit-price": "take_profit_price",
    "guidance": "guidance",
    "dont-do": "dont_do",
}
BLOCK_START = "=== WEEKLY PLAN ==="
BLOCK_END = "=== END WEEKLY PLAN ==="

PLAN_BLOCK_INSTRUCTIONS = f"""
WEEKLY PLAN — after the detail paragraph, append this block EXACTLY, one field
per line, labels unchanged. It is parsed by a strict machine parser: numeric
fields take ONE plain number (like 121.50, no "$", no range, no words) or N/A.
Anything else makes the whole plan invalid and it will not be used.

{BLOCK_START}
Thesis: <one sentence: why the position or watch remains valid>
Buy-Zone-Low: <lowest price of the planned entry/add zone, or N/A>
Buy-Zone-High: <highest price of the planned entry/add zone, or N/A>
Chase-Ceiling: <price above which a new entry/add must NOT be made, or N/A>
Invalidation-Level: <price BELOW which the thesis is broken, or N/A>
Invalidation-Condition: <short condition, e.g. "daily close below 116", or N/A>
Trim-Condition: <explicit condition for reducing exposure, or N/A>
Resistance-Low: <lower edge of the overhead resistance range, or N/A>
Resistance-High: <upper edge of the overhead resistance range, or N/A>
Take-Profit-Price: <sell-limit / take-profit trigger price, or N/A>
Guidance: <one concrete instruction for this week>
Dont-Do: <one concrete warning, e.g. "do not chase above 125">
{BLOCK_END}

Rules: Buy-Zone-Low <= Buy-Zone-High <= Chase-Ceiling. Invalidation-Level is
below the buy zone. For BUY or ADD the buy zone and Chase-Ceiling are required.
Give either an Invalidation-Level or an Invalidation-Condition. The plan is valid
through the last trading session of this week only.
"""

_NUM_RE = re.compile(r"^\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?$")
_NA_VALUES = frozenset({"N/A", "NA", "NONE", "-", ""})


@dataclass
class WeeklyPlan:
    """One plan row — a weekly base plan or a temporary event override."""

    ticker: str
    kind: str = PlanKind.WEEKLY_BASE.value
    decision: str = ""
    urgency: str = ""
    thesis: str = ""
    buy_zone_low: float | None = None
    buy_zone_high: float | None = None
    chase_ceiling: float | None = None
    invalidation_level: float | None = None
    invalidation_condition: str = ""
    trim_condition: str = ""
    resistance_low: float | None = None
    resistance_high: float | None = None
    take_profit_price: float | None = None
    guidance: str = ""
    dont_do: str = ""
    upstream_verdict: str = ""
    valid_from_session: str = ""         # ISO date of the first valid session
    expires_after_session: str = ""      # ISO date of the last valid session
    input_price: float | None = None
    input_price_ts: str = ""
    created_ts: str = ""
    schema_version: int = PLAN_SCHEMA_VERSION
    status: str = PlanStatus.INVALID.value
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    source_ref: dict[str, Any] = field(default_factory=dict)
    parent_plan_id: int | None = None
    id: int | None = None
    activated_ts: str | None = None
    superseded_ts: str | None = None
    deactivated_ts: str | None = None

    @property
    def is_valid(self) -> bool:
        return not self.validation_errors

    @property
    def has_buy_zone(self) -> bool:
        return self.buy_zone_low is not None and self.buy_zone_high is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedBlock:
    fields: dict[str, Any]
    errors: list[str]
    found: bool


def parse_plan_block(text: str | None) -> ParsedBlock:
    """Strictly parse the ``WEEKLY PLAN`` block out of an advisor response.

    Every label must appear exactly once; numeric fields accept one plain
    number or N/A. Errors are collected (not raised) so a malformed plan can
    still be stored for diagnosis.
    """
    fields: dict[str, Any] = {}
    errors: list[str] = []
    if not text or BLOCK_START not in text:
        return ParsedBlock({}, ["weekly plan block missing"], False)
    body = text.split(BLOCK_START, 1)[1]
    if BLOCK_END not in body:
        errors.append("weekly plan block not terminated")
    body = body.split(BLOCK_END, 1)[0]

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ":" not in stripped:
            errors.append(f"unparseable plan line: {stripped[:60]!r}")
            continue
        label, value = stripped.split(":", 1)
        key = _BLOCK_KEYS.get(label.strip().lower().lstrip("*-• ").rstrip("*"))
        if key is None:
            errors.append(f"unknown plan field: {label.strip()[:40]!r}")
            continue
        if key in fields:
            errors.append(f"duplicate plan field: {key}")
            continue
        value = value.strip().strip("*").strip()
        if key in NUMERIC_FIELDS:
            num, err = _parse_number(value)
            if err:
                errors.append(f"{key}: {err}")
            fields[key] = num
        else:
            fields[key] = "" if value.upper() in _NA_VALUES else value

    for key in _BLOCK_KEYS.values():
        if key not in fields:
            errors.append(f"missing plan field: {key}")
    return ParsedBlock(fields, errors, True)


def strip_plan_block(text: str) -> str:
    """Remove the plan block so it cannot pollute the advice detail paragraph."""
    if BLOCK_START not in text:
        return text
    head, rest = text.split(BLOCK_START, 1)
    tail = rest.split(BLOCK_END, 1)[1] if BLOCK_END in rest else ""
    return (head + tail).strip()


def _parse_number(value: str) -> tuple[float | None, str | None]:
    if value.upper() in _NA_VALUES:
        return None, None
    m = _NUM_RE.match(value)
    if not m:
        return None, f"not a single plain number: {value[:40]!r}"
    num = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
    if not math.isfinite(num) or num <= 0:
        return None, f"not a positive finite number: {value[:40]!r}"
    return num, None


def normalize_decision(advisor_decision: str | None, held: bool | None) -> str:
    """Plan decision from the advisor's Decision header.

    Position ownership and plan direction are separate facts: an advisor HOLD on
    a name the user doesn't own means "don't buy now", which the plan records as
    WATCH. HOLD on a held name stays HOLD — no new action now, never an implied
    buy or sell. Unrecognised values return "" (→ validation error).
    """
    d = (advisor_decision or "").strip().upper()
    d = re.split(r"[\s/(,.]", d, maxsplit=1)[0] if d else ""
    if d not in Decision.__members__:
        return ""
    if d == Decision.HOLD.value and held is False:
        return Decision.WATCH.value
    return d


def validate_plan(
    plan: WeeklyPlan,
    *,
    parse_errors: list[str] | None = None,
    now: datetime | None = None,
    max_level_ratio: float = 2.0,
) -> WeeklyPlan:
    """Validate a plan in place and set its status; returns the same plan.

    Checks enums, finite numbers, range ordering, required text, timestamps and
    expiry. Any error leaves ``status = invalid`` — stored, never actionable.
    """
    errors = list(parse_errors or [])
    warnings: list[str] = []

    if plan.decision not in Decision.__members__:
        errors.append(f"invalid decision: {plan.decision!r}")
    if plan.urgency not in Urgency.__members__:
        errors.append(f"invalid urgency: {plan.urgency!r}")
    if plan.decision == Decision.HOLD.value and plan.urgency not in ("", Urgency.LOW.value):
        errors.append("HOLD must carry LOW urgency")

    for key in NUMERIC_FIELDS:
        val = getattr(plan, key)
        if val is None:
            continue
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not math.isfinite(val) or val <= 0:
            errors.append(f"{key} is not a positive finite number")
            setattr(plan, key, None)

    ref = plan.input_price
    if ref is None or not isinstance(ref, (int, float)) or not math.isfinite(ref) or ref <= 0:
        errors.append("input price missing or invalid")
        ref = None
    if not plan.input_price_ts:
        errors.append("input price timestamp missing")

    lo, hi = plan.buy_zone_low, plan.buy_zone_high
    if (lo is None) != (hi is None):
        errors.append("buy zone needs both low and high")
    if lo is not None and hi is not None and lo > hi:
        errors.append("buy_zone_low above buy_zone_high")
    if hi is not None and plan.chase_ceiling is not None and hi > plan.chase_ceiling:
        errors.append("buy_zone_high above chase_ceiling")
    inv = plan.invalidation_level
    if inv is not None:
        if lo is not None and inv >= lo:
            errors.append("invalidation_level not below buy zone")
        elif lo is None and plan.chase_ceiling is not None and inv >= plan.chase_ceiling:
            errors.append("invalidation_level not below chase_ceiling")
        if plan.take_profit_price is not None and inv >= plan.take_profit_price:
            errors.append("invalidation_level not below take_profit_price")
    rlo, rhi = plan.resistance_low, plan.resistance_high
    if rlo is not None and rhi is not None and rlo > rhi:
        errors.append("resistance_low above resistance_high")
    if ref is not None:
        for key in NUMERIC_FIELDS:
            val = getattr(plan, key)
            if val is not None and not (ref / max_level_ratio <= val <= ref * max_level_ratio):
                errors.append(f"{key} {val:g} implausibly far from price {ref:g}")

    if plan.decision in BULLISH_DECISIONS and (not plan.has_buy_zone or plan.chase_ceiling is None):
        errors.append(f"{plan.decision} requires a buy zone and chase_ceiling")
    elif plan.has_buy_zone and plan.chase_ceiling is None:
        warnings.append("buy zone without chase_ceiling — entry guidance stays informational")
    if plan.decision == Decision.TRIM.value and not plan.trim_condition and plan.take_profit_price is None:
        errors.append("TRIM requires a trim_condition or take_profit_price")
    if plan.kind == PlanKind.WEEKLY_BASE.value:
        if inv is None and not plan.invalidation_condition:
            errors.append("invalidation_level or invalidation_condition required")
        for key in ("thesis", "guidance", "dont_do"):
            if not (getattr(plan, key) or "").strip():
                errors.append(f"{key} required")

    try:
        vf = date.fromisoformat(plan.valid_from_session)
        ex = date.fromisoformat(plan.expires_after_session)
        if ex < vf:
            errors.append("expires_after_session before valid_from_session")
    except (TypeError, ValueError):
        errors.append("validity sessions missing or malformed")
    if not plan.created_ts:
        errors.append("created_ts missing")

    plan.validation_errors = errors
    plan.validation_warnings = warnings
    plan.status = PlanStatus.INVALID.value if errors else PlanStatus.ACTIVE.value
    return plan


def plan_freshness(plan: WeeklyPlan | None, session: date) -> PlanFreshness:
    """Whether a plan may be used on trading session ``session``.

    A failed weekly refresh never silently extends the previous plan: once the
    session passes ``expires_after_session`` the plan reads EXPIRED even if its
    row is still ``active``.
    """
    if plan is None:
        return PlanFreshness.MISSING
    if plan.status == PlanStatus.INVALIDATED.value and not plan.validation_errors:
        return PlanFreshness.INVALIDATED
    if plan.status != PlanStatus.ACTIVE.value or plan.validation_errors:
        return PlanFreshness.INVALID
    try:
        vf = date.fromisoformat(plan.valid_from_session)
        ex = date.fromisoformat(plan.expires_after_session)
    except (TypeError, ValueError):
        return PlanFreshness.INVALID
    if session > ex:
        return PlanFreshness.EXPIRED
    if session < vf:
        return PlanFreshness.NOT_YET_VALID
    return PlanFreshness.ACTIVE


def is_bullish_buy_plan(plan: WeeklyPlan | None) -> bool:
    """A plan that authorises monitoring an entry: buy zone + chase ceiling,
    and a direction that isn't reducing exposure."""
    return (
        plan is not None
        and plan.decision not in BEARISH_DECISIONS
        and plan.has_buy_zone
        and plan.chase_ceiling is not None
    )


def direction(decision: str | None) -> str:
    d = (decision or "").upper()
    if d in BULLISH_DECISIONS:
        return "bullish"
    if d in BEARISH_DECISIONS:
        return "bearish"
    if d in ("HOLD", "WATCH"):
        return "neutral"
    return ""
