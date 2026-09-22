"""Shared US-equity (XNYS) trading-calendar helpers.

Centralises the exchange_calendars access used by the daemon's market-hours
guard, the Tier 2 trading-day guard, and the weekly full-risk-day predicate so
they all load one calendar and degrade to the same weekday fallbacks together.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger("watchy.market_calendar")

_calendar = None
_calendar_failed = False


def get_calendar():
    """Lazily load the XNYS exchange calendar, or None if it can't be loaded.

    exchange_calendars ships with yfinance-cache, so it's normally present; if
    the import ever fails we cache the failure and callers fall back to plain
    weekday checks (holiday-blind, but never crash).
    """
    global _calendar, _calendar_failed
    if _calendar_failed:
        return None
    if _calendar is None:
        try:
            import exchange_calendars as xcals
            _calendar = xcals.get_calendar("XNYS")
        except Exception:
            logger.warning(
                "exchange_calendars unavailable; using weekday fallbacks",
                exc_info=True,
            )
            _calendar_failed = True
            return None
    return _calendar


def is_trading_day(now: datetime | None = None) -> bool:
    """True if ``now``'s date is a regular US equity trading session.

    Falls back to Mon–Fri (holiday-blind) if the calendar can't be loaded.
    """
    now = now or datetime.now(timezone.utc)
    cal = get_calendar()
    if cal is not None:
        try:
            import pandas as pd
            return bool(cal.is_session(pd.Timestamp(now.date())))
        except Exception:
            logger.warning("is_session check failed; weekday fallback", exc_info=True)
    return now.weekday() < 5  # Mon–Fri


def is_weekly_full_risk_day(now: datetime | None = None) -> bool:
    """True on the **first trading session of the (Mon–Sun) week**.

    This is the day Tier 2 escalates to the full 3-way risk debate and bypasses
    the proximity gate, so every ticker gets one guaranteed full-risk run per
    week. Keying off "first session of the week" (rather than literally Monday)
    keeps that weekly guarantee even when Monday is a market holiday — the run
    shifts to Tuesday. Falls back to Monday (weekday 0) if the calendar can't be
    loaded.
    """
    now = now or datetime.now(timezone.utc)
    cal = get_calendar()
    if cal is None:
        return now.weekday() == 0  # fallback: Monday
    try:
        import pandas as pd
        d = pd.Timestamp(now.date())
        if not cal.is_session(d):
            return False
        prev = cal.previous_session(d)
        # First session of the week iff the previous session is in a prior
        # ISO week (ISO weeks start Monday, matching our Mon–Sun grouping).
        return (prev.isocalendar()[0], prev.isocalendar()[1]) != (
            d.isocalendar()[0], d.isocalendar()[1]
        )
    except Exception:
        logger.warning("weekly-full-risk-day check failed; Monday fallback", exc_info=True)
        return now.weekday() == 0


# --- Watchy 2.0: trading-session labels and weekly plan validity -----------

try:  # zoneinfo is stdlib on 3.9+; the tz database ships with tzdata on Windows.
    from zoneinfo import ZoneInfo

    _NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - only on a box with no tz database
    _NY = None


def _ny_date(now: datetime) -> date:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if _NY is not None:
        return now.astimezone(_NY).date()
    # No tz database: EDT offset is the closer approximation for most of the year.
    return (now.astimezone(timezone.utc) - timedelta(hours=4)).date()


def session_label(now: datetime | None = None) -> date:
    """The exchange trading-session date that ``now`` belongs to.

    Budgets and reminder dedup reset on this boundary rather than UTC or server
    midnight: UTC midnight falls at 20:00 ET, i.e. inside the after-hours of the
    session it would wrongly split. The label is the New York calendar date,
    which is exactly the XNYS session date for every minute Tier 1 runs (it only
    runs during the regular session). Pure date arithmetic — no calendar needed.
    """
    return _ny_date(now or datetime.now(timezone.utc))


def week_session_bounds(now: datetime | None = None) -> tuple[date, date]:
    """First and last trading sessions of the ISO week containing ``now``.

    A weekly plan is valid from the week's first session through its last one.
    Holiday-aware when the exchange calendar loads (a Good Friday week ends on
    Thursday); otherwise Monday–Friday of that week.
    """
    d = session_label(now)
    monday = d - timedelta(days=d.weekday())
    friday = monday + timedelta(days=4)
    cal = get_calendar()
    if cal is not None:
        try:
            import pandas as pd

            sessions = cal.sessions_in_range(pd.Timestamp(monday), pd.Timestamp(friday))
            if len(sessions):
                return sessions[0].date(), sessions[-1].date()
        except Exception:
            logger.warning("week-session bounds failed; Mon–Fri fallback", exc_info=True)
    return monday, friday


def session_close_utc(day: date) -> datetime:
    """Regular-session close of ``day`` in UTC.

    Uses the exchange calendar (so early closes such as the day after
    Thanksgiving are exact); falls back to 16:00 New York time.
    """
    cal = get_calendar()
    if cal is not None:
        try:
            import pandas as pd

            close = cal.session_close(pd.Timestamp(day))
            return close.to_pydatetime().astimezone(timezone.utc)
        except Exception:
            logger.warning("session_close lookup failed; 16:00 ET fallback", exc_info=True)
    if _NY is not None:
        return datetime(day.year, day.month, day.day, 16, 0, tzinfo=_NY).astimezone(timezone.utc)
    return datetime(day.year, day.month, day.day, 20, 0, tzinfo=timezone.utc)
