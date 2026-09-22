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

**Phase 3 (plan monitoring)** — pure `plan_monitor.classify_price/plan_state/detect_transition`,
`guards.select_status` + `reminder_wording`, orchestration in `monitor.py`; Tier 1 weekly mode calls
it after the take-profit check. Grep `PLAN_REMINDER` / `PLAN_INVALIDATED`. Legacy Tier 1 tests are
pinned to `tier2_schedule="daily"` (they cover the rollback path).

**Phase 4 (router)** — `router.route(RouterInput) -> RouteDecision` (pure). Weekly-mode Tier 1 =
`monitor.scan_planned` (dispatched from `tier1.scan_ticker`); the 1.x path below it is the daily
rollback. `tier1._take_profit_decision` (pure-ish) + `_fire_take_profit`; take-profit tests run in
both modes (`TestTakeProfitZoneWeekly`). Every scan → `ROUTE {json}` + `route_log` row. Paid routes
reserve budget in `triggered.py` (Phases 6–7).

**Phase 5 (guards)** — `guards.revalidate`, `classify_alignment`, `entry_blocked`,
`monitor.fetch_live_price`; weekly card status = real guard (grep `WEEKLY_CARD`). Order in
`_select_status`: stale feed > invalidation > TR route > DO NOT CHASE > moved>stale_move_atr >
plan freshness > entry/exit. `ACT NOW` only with advisor HIGH + verdict agree + in buy zone
(entries) or held (exits).

**Phases 6–7 (paid analysis)** — `triggered.execute()`: non-blocking ticker lock (`busy`),
atomic `try_reserve_triggered` (keyed by session), FR = advisor on `load_digest(kind="weekly")` +
`event_context`, TR = `TRIGGERED_RISK_SPEC` (market+sentiment+news, bull/bear, simplified) + advisor,
`build_override` copies base levels (advisor can't move boundaries), guards after
`fetch_live_price`. Grep `TRIGGERED`. Committed together (shared module).

**Phase 8 (replay + controls)** — `watchy/replay.py` (`ReadOnlyStore` = sqlite `mode=ro`; 1.x-db
tolerant; `reroute_logged` determinism check), `watchy/ctl.py` + `scripts/watchy_ctl.py`
(status / plan show|history|expire / route / preview / weekly --yes / replay). Replay costs are
placeholders ($0.01 FR, $0.06 TR) — re-measure before quoting. `tier1._bundle_summary` now logs
`prev_close` + `avg_atr_20d` into signal_log so future replays can recompute the shock test.

**Phase 9 (docs/release)** — README EN/ZH 2.0 sections, `docs/WATCHY_2_OPERATIONS.md` (exact VPS
shadow deploy steps, checklist, rollback), `docs/RELEASE_NOTES_v2.0.0-rc.1.md`, version `2.0.0rc1`
(test-enforced equal in pyproject + `__init__`). Weekly batch sets kv `weekly_full_running` to hold
"plan expired" reminders during a long Monday batch. **Nothing pushed, deployed or tagged** — the
owner must review, then push outside the Tier-2 window (ideally Fri after 20:00 UTC / weekend so
Monday's Weekly Full creates the first plans) and tag `v2.0.0-rc.1` (`gh release create` needs the
FULL sha). The older note in [[watchy-git-workflow]] about `0.1.0` is superseded by this bump.

**RC corrections (2026-09-22)** — forward-version guard in `StateStore._refuse_newer_schema` (runs
before the WAL pragma; a newer `user_version` → RuntimeError, db untouched); `plan_validity` uses
`market_calendar.session_close_utc()` so a forced Weekly Full after the final session's close plans
next week (calendar-aware early closes / holiday weeks; 16:00 ET fallback).

Design decisions worth remembering:
- Plan decision comes from the advisor `Decision:` header; advisor HOLD on a **non-held** name is
  stored as WATCH (ownership and direction are separate facts). The block has no decision field on
  purpose — two decision fields could disagree.
- `invalidation_level` is always a *downside* boundary (the account is long-only).
- BUY/ADD without buy zone + chase ceiling = invalid plan; HOLD/WATCH with a zone but no chase =
  warning, and entry guidance stays informational.
- Invalidation crossing withdraws the plan (status `invalidated`) for held AND watch-only — stricter
  than the spec's table (which only says watch-only), chosen so a broken thesis can never produce a
  later entry reminder. `store.get_current_plan()` returns active/invalidated/deactivated rows so the
  UI can say "invalidated" instead of "no plan".
- A mechanical (no-LLM) reminder can never be `ACT NOW`; best case is `WAIT FOR LIMIT`. If the
  router wanted an interpretation that didn't run (shadow/budget/input missing/failure), entry
  wording is capped at `INFORMATION ONLY` (`StatusInputs.interpretation_pending`).
- Conflict display vs blocking: any direction difference shows "conflict — human review" (spec §13
  example BUY/HOLD still WAIT FOR LIMIT); only verdict SELL/HOLD + advisor BUY/ADD (and verdict SELL
  + bullish buy plan) *blocks* entry wording.
- Matrix gaps filled: `rsi_oversold` = Bollinger-lower row; volume/ATR anomaly w/o negative move =
  Notify Only; UNKNOWN position = held rules (conservative, like the 1.x Tier 2 gate).
- Levels outside 0.5×–2× the input price are rejected as implausible.
- Config: `tier2_schedule` (weekly default; `daily` = exact 1.x behaviour = rollback),
  `weekly_plan.*`, `triggered_analysis.*` (enabled:false = shadow).

**Why:** the user wants a lower-noise weekly-planning workflow; routing thresholds are hypotheses that
still need prospective shadow validation — never describe them as improving returns.
**How to apply:** read this before touching plan/route/guard code; keep §22 of the spec in sync.
