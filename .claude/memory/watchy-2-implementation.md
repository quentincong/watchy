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

**🚀 DEPLOYED IN SHADOW + RC RELEASED (2026-09-22 ~04:35 UTC)** — pushed `18ce84c` (full
18ce84c1196a2e523235b43d5212c17ac623a973); VPS auto-updated, logs `Watchy 2.0.0rc1 starting` /
`tier2_schedule=weekly triggered_analysis.enabled=False`; migration → `user_version=2`, auto-backup
`~/watchy/state.db.v0-backup-20260922T043501Z`, manual backup `~/watchy_backups/state.db.pre-v2-manual-20260922`
(kept OUTSIDE the repo so the tree stays clean). **Real-model smoke (one paid NVDA Weekly Full via
`watchy_ctl.py weekly NVDA --yes`, ~7.5 min, off-peak):** Gemini emitted the WEEKLY PLAN block, 0 parse
errors, plan #1 active, 0 validation errors (ADD/MEDIUM, zone 220–221, chase 228.50, inv 214.40, valid
09-22..09-25, verdict BUY → agree, card status INFORMATION ONLY since price 227.38 was outside the zone).
Release: https://github.com/quentincong/watchy/releases/tag/v2.0.0-rc.1 (prerelease, tag on 18ce84c).
Known nits: `watchy_ctl.py weekly` configures no logging, so its PLAN_*/WEEKLY_CARD/TOKENCOST INFO lines are
lost (only DB rows + stdout JSON remain); a forced mid-week card still says "first trading session of the
week" in *Why now*. Next: 1–2 weeks shadow review per `docs/WATCHY_2_OPERATIONS.md` §4; first automatic
Weekly Full = Mon 2026-09-28 10:02 UTC; `v2.0.0` only after shadow + limited enablement.

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

**🔍 First automatic Weekly Full inspected (Mon 2026-09-28, Claude Code)** — 19/19 tickers ok, 10:02→11:58 UTC
(~6 min/ticker, done before the open); cost DeepSeek $1.22 + Gemini $0.63 = **$1.85 for the week** (V4.1 Flash
≈$0.064/ticker). `WEEKLY_PLANS valid=17 invalid=2`. Tier 1 dedup works (2nd scan round quiet). Findings:
1. **Plans born already invalidated (3/17: AMZN TRIM inv 256.18 @249.67, LUMN WATCH 6.39 @5.71, CEG WATCH 272
   @263.27)** — for bearish/non-entry theses Gemini writes an *upside* "thesis wrong" level into
   `Invalidation-Level`; `validate_plan` never checks inv < input price, so Tier 1 withdrew them on the first
   scan (AMZN routed TRIGGERED_RISK in shadow). Needs prompt wording + validator check.
2. **AVGO truncated**: GEMINICOST think 2654 + out 414 = 3068 ≈ `maxOutputTokens` 3072
   (`_ADVICE_MAX_TOKENS` 1024 + `_GEMINI_THINK_HEADROOM` 2048) → "weekly plan block not terminated". Others
   land 80–350 tokens under the cap (COHR 2992) — systemic; no finishReason check exists.
3. **yfinance_cache serves Friday's daily bar intraday** for some tickers (14:35 UTC: NVDA 225.07 vs live 231.39,
   VST, MRVL; at 13:30–14:00 nearly all) → `monitor.data_is_stale` flags every Tier 1 scan →
   "STALE — RECHECK REQUIRED" on every notify. Guard is right; the feed is wrong. Latent 1.x issue (1.x Tier 1
   silently used the stale close).
4. **SKHY (held, +21%) has <200 rows** → `compute_indicators` returns None → no input price → plan always
   invalid, and **no Tier 1 ROUTE at all** (unmonitored, take-profit zone-entry included).
5. Schwab refresh token lapsed ~13:05 UTC 9/28 (re-auth was 9/21 13:34) → positions served from 10:02 cache.
6. `watchy_ctl.py plan show` prints "(none)" for invalid-only tickers — ops doc §4 says it shows validation errors.
Plan-quality nit: several invalidation levels sit <0.5 ATR under the zone low (GOOG 334.0 vs 334.98, KLAC).
**Fixed same day (2026-09-28, Claude Code, user-approved):** (1) downside-only invalidation kept on purpose
— user accepted the argument: long-only account, monitor/guards/router all read invalidation as "long case
broken below", and an upside crossing on a TRIM plan would wrongly fire RISK REVIEW / Triggered Risk; the
upside "wrong above X" level goes into Resistance/Trim-Condition. Prompt now states the current price;
`validate_plan` rejects `invalidation_level >= input_price`. (2) Gemini ceiling 3072→7680
(`_ADVICE_MAX_TOKENS` 1536 + headroom 6144) + `ADVISOR_TRUNCATED` on MAX_TOKENS/length/max_tokens.
(3) `_history_via_cache_or_direct` refetches from plain yfinance when the yfc frame lacks today's bar after
the open (`YFC_STALE_BAR`; `market_calendar.session_open_utc`). Unit-tested only — by ~15:00 UTC yfc had
caught up live, so the refetch path wasn't exercised on the VPS yet; grep `YFC_STALE_BAR` next session open.
(4) `compute_indicators` returns a partial bundle from 34 rows (`MIN_HISTORY_ROWS`); `prev_sma_50_above_200`
stored as None without a 200-SMA. (6) `plan show` prints a rejected latest refresh. Item 5 (Schwab) the user
re-authed themselves. This week's withdrawn/missing plans (AMZN, LUMN, CEG, AVGO, SKHY) are NOT regenerated
by the fix — only a paid `watchy_ctl.py weekly TICKER --yes` or next Monday's batch does that.

