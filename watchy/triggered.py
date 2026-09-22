"""Watchy 2.0 paid triggered analysis (Fast Recheck / Triggered Risk).

Placeholder until Phases 6–7: every paid route is downgraded to the
deterministic Notify Only reminder.
"""

from __future__ import annotations

from typing import Any


def execute(decision: Any, ev: Any, pstate: Any, bundle: Any, config: Any, store: Any,
            notifier: Any, position_source: Any, **_: Any) -> dict[str, Any]:
    return {"effective_route": "NOTIFY_ONLY", "budget_result": "unavailable"}
