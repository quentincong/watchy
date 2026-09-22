---
name: watchy-2-implementation
description: Watchy 2.0 (weekly plan + event-driven Tier 1 routing) implementation log, design decisions and deviations from docs/WATCHY_2_IMPLEMENTATION_PLAN.md
metadata:
  type: project
---

Spec: `docs/WATCHY_2_IMPLEMENTATION_PLAN.md` (§22 = per-phase status). Started 2026-09-21 by Claude Code;
local commits only, nothing pushed/deployed until the user reviews.

**Phase 1 (contracts + persistence)** — `watchy/plan.py` (types, strict `=== WEEKLY PLAN ===` block
parser, `validate_plan`, `plan_freshness`), new tables `analysis_plan` / `plan_reminder_state` /
`triggered_budget` / `route_log`, `PRAGMA user_version=2`, one-time online backup
`state.db.v0-backup-<UTC>` before migrating a 1.x db (git-ignored). Budgets key on
`market_calendar.session_label()` = New York date (never UTC midnight, which is 20:00 ET).

**Phase 2 (weekly plan generation)** — daemon `_tier2_job`: weekly mode runs only when
`is_weekly_full_risk_day()`; `run_daily_scan(weekly=True, tickers=...)` = Weekly Full (FULL risk,
no cadence/gate), advisor `plan_request=True` → `watchy/weekly.py` build/validate/persist,
`save_digest(kind="weekly")`, card appended to the advice message, single
`weekly_plan_failures` alert per batch. Grep `PLAN_ACTIVE` / `PLAN_INVALID` / `WEEKLY_PLANS`.
`scripts/compare_gemini_*.py` format ADVISOR_PROMPT directly — keep their `.format()` kwargs in
sync with new prompt slots (`event_context`, `plan_instructions`).

Design decisions worth remembering:
- Plan decision comes from the advisor `Decision:` header; advisor HOLD on a **non-held** name is
  stored as WATCH (ownership and direction are separate facts). The block has no decision field on
  purpose — two decision fields could disagree.
- `invalidation_level` is always a *downside* boundary (the account is long-only).
- BUY/ADD without buy zone + chase ceiling = invalid plan; HOLD/WATCH with a zone but no chase =
  warning, and entry guidance stays informational.
- Levels outside 0.5×–2× the input price are rejected as implausible.
- Config: `tier2_schedule` (weekly default; `daily` = exact 1.x behaviour = rollback),
  `weekly_plan.*`, `triggered_analysis.*` (enabled:false = shadow).

**Why:** the user wants a lower-noise weekly-planning workflow; routing thresholds are hypotheses that
still need prospective shadow validation — never describe them as improving returns.
**How to apply:** read this before touching plan/route/guard code; keep §22 of the spec in sync.
