"""Shared Watchy 2.0 test fixtures — synthetic data only, no private exports."""

from __future__ import annotations

from watchy.plan import (
    BLOCK_END,
    BLOCK_START,
    WeeklyPlan,
    validate_plan,
)

VALID_BLOCK = f"""{BLOCK_START}
Thesis: AI capex demand intact while price holds the 50-day average.
Buy-Zone-Low: 121.00
Buy-Zone-High: 123.00
Chase-Ceiling: 125.00
Invalidation-Level: 116.00
Invalidation-Condition: daily close below 116
Trim-Condition: N/A
Resistance-Low: 132.00
Resistance-High: 135.00
Take-Profit-Price: N/A
Guidance: consider a limit order within the planned range.
Dont-Do: do not chase above 125.
{BLOCK_END}"""

ADVICE_WITH_BLOCK = f"""Ticker: NVDA
Decision: BUY
Urgency: MEDIUM
Target: 122.00
Take-Profit: N/A

NVDA pulled back toward support; the analysts cite 121-123 as accumulation.

{VALID_BLOCK}
"""


def make_plan(**overrides) -> WeeklyPlan:
    """A valid, validated weekly base plan for the week of 2026-09-21."""
    fields = dict(
        ticker="NVDA",
        decision="BUY",
        urgency="MEDIUM",
        thesis="AI capex demand intact.",
        buy_zone_low=121.0,
        buy_zone_high=123.0,
        chase_ceiling=125.0,
        invalidation_level=116.0,
        invalidation_condition="daily close below 116",
        resistance_low=132.0,
        resistance_high=135.0,
        guidance="consider a limit order within the planned range.",
        dont_do="do not chase above 125.",
        upstream_verdict="BUY",
        valid_from_session="2026-09-21",
        expires_after_session="2026-09-25",
        input_price=124.0,
        input_price_ts="2026-09-21T10:30:00+00:00",
        created_ts="2026-09-21T10:35:00+00:00",
    )
    fields.update(overrides)
    return validate_plan(WeeklyPlan(**fields))
