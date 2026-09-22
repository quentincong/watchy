# Watchy 2.0 — Weekly Planning and Triggered Analysis

Status: implementation-ready  
Scope: implementation plan only; this document does not claim that the proposed policy improves returns  
Target release: `v2.0.0-rc.1` after implementation and tests, then `v2.0.0` after shadow validation

## 1. Objective

Change Watchy from a routine daily LLM adviser into a weekly planning and event-driven monitoring
system:

- Run one full analysis on the first US trading session of each week.
- Convert that analysis into a structured weekly trading plan.
- Let Tier 1 monitor the plan and technical conditions without an LLM.
- Invoke paid analysis only when a material trigger requires interpretation.
- Keep take-profit monitoring active.
- Send Telegram messages with context and guidance, not only a single decision field.
- Prevent a recommendation from becoming actionable when the price has already moved beyond its
  intended execution range.

The redesign is informed by historical evaluation, but its new routing rules and thresholds remain
hypotheses that require prospective validation. Watchy remains advisory-only and must not execute
trades.

## 2. Operating model

Watchy 2.0 has four modes:

1. **Weekly Full** — full TradingAgents analysis on the first trading session of the week. It creates
   the base plan for each ticker.
2. **Notify Only** — deterministic Tier 1 reminder derived from price, indicators, position state, and
   the current weekly plan. No LLM.
3. **Fast Recheck** — reuse the saved weekly digest and ask only the advisor to interpret a material
   but bounded change.
4. **Triggered Risk** — run the market, sentiment, and news analysts, bull/bear debate, simplified risk
   review, and advisor for a potentially thesis-changing event.

Take-profit remains a distinct path and keeps its existing behavior unless this plan explicitly
requires additional presentation or logging.

Routine daily Tier 2 analysis is removed from production scheduling. Preserve a configuration option
that can restore daily scheduling for comparison or rollback.

## 3. Non-goals

Do not add:

- automatic order placement or portfolio management;
- Schwab order-book integration;
- automatic parameter optimization from the historical study;
- a claim that the new workflow generates alpha;
- a new prediction model;
- automatic merging of historical private data into this repository;
- automatic trading based only on an LLM response.

## 4. Weekly plan contract

The weekly full run must produce and persist a machine-readable plan in addition to the human-readable
Telegram message. Required fields:

- `decision`: BUY, ADD, HOLD, TRIM, SELL, or WATCH
- `urgency`: HIGH, MEDIUM, or LOW using the existing order-deadline semantics
- `thesis`: concise reason the position or watch remains valid
- `buy_zone_low` and `buy_zone_high`: nullable numeric boundaries
- `chase_ceiling`: nullable numeric maximum price above which entry/add guidance is not actionable
- `invalidation_level`: nullable numeric risk boundary
- `invalidation_condition`: short textual condition when a single price is insufficient
- `trim_condition`: explicit condition for reducing exposure
- `resistance_low` and `resistance_high`: nullable numeric range
- `take_profit_price`: nullable numeric limit or trigger
- `guidance`: concrete user-facing instruction
- `dont_do`: concrete warning such as "do not chase above $X"
- `valid_from_session` and `expires_after_session`
- `source_message_id` or equivalent provenance
- `created_at`, market-price timestamp, and plan schema version

Numeric fields must be emitted through structured output or an equivalently strict parser. Validate
enum values, finite numbers, sensible range ordering, timestamps, and expiry before activating a plan.
Invalid or incomplete output may be stored for diagnosis but must not become actionable.

`HOLD` means no new action now. It must not silently mean buy, sell, or mechanically continue a
position. Position ownership and plan direction are separate facts.

## 5. Persistence and history

Add an `analysis_plan` table through a backward-compatible SQLite migration. Preserve history rather
than overwriting rows. Suggested logical fields:

- plan identity, ticker, plan kind, schema version, and status;
- all weekly plan contract fields;
- source digest/advice references;
- input price and input-price timestamp;
- validation status and validation errors;
- creation, activation, supersession, and expiry timestamps.

Plan kinds:

- `weekly_base`
- `event_override`

An event override is temporary context for the active weekly base plan. It must not destructively
rewrite the base plan. A successful new weekly run supersedes the previous weekly base plan. If the new
weekly run fails, the previous plan must be shown as expired or stale rather than silently extended.

Add `plan_reminder_state` or equivalent persisted state for transition-aware, restart-safe reminders:

- outside relevant zone
- approaching buy zone
- inside buy zone
- above chase ceiling
- invalidated
- inside take-profit zone
- expired

Store enough state to deduplicate reminders across daemon restarts.

## 6. Deterministic validation and safety guards

Implement pure functions for:

- weekly plan validation;
- price-to-plan classification;
- expiry and staleness classification;
- verdict/advisor conflict classification;
- trigger routing;
- paid-analysis budget checks;
- post-analysis price revalidation;
- Telegram status selection.

Safety rules:

- A SELL or HOLD upstream verdict combined with advisor BUY/ADD must never be presented as an
  actionable buy. Show it as a conflict requiring human review.
- Missing, expired, stale, or invalid plans cannot generate actionable entry instructions.
- Price above the chase ceiling produces `DO NOT CHASE`, even if the analysis says BUY/ADD.
- Price beyond an invalidation boundary produces `PLAN INVALID` or `RISK REVIEW`, never an entry.
- LLM failure, malformed output, or exhausted budget must not suppress deterministic risk or
  take-profit notifications.
- All market-price timestamps and plan freshness must be visible in logs and available to the message
  renderer.

## 7. Tier 1 routing

Create one pure routing function that accepts ticker state, position state, active plan, trigger set,
latest price/indicators, cooldown state, and budget state. It returns exactly one highest-priority route:

- `TAKE_PROFIT`
- `TRIGGERED_RISK`
- `FAST_RECHECK`
- `NOTIFY_ONLY`
- `NO_ACTION`

When multiple triggers arrive together, run at most one analysis and retain every contributing reason
in logs and the Telegram explanation. Priority is:

`TAKE_PROFIT or invalidation > TRIGGERED_RISK > FAST_RECHECK > NOTIFY_ONLY > NO_ACTION`.

Initial routing policy:

| Condition | Held ticker | Watch-only ticker |
|---|---|---|
| Death cross | Triggered Risk | Invalidate a bullish plan and Notify Only |
| MACD bearish cross | Fast Recheck; escalate when combined with a material negative move | Notify Only |
| Bollinger lower breach | Fast Recheck; escalate when combined with a material negative move | Fast Recheck only when compatible with an active bullish buy plan; otherwise Notify Only |
| ATR/volume anomaly plus negative move | Triggered Risk | Notify Only |
| RSI overbought or Bollinger upper breach | Existing take-profit path when held and eligible | Notify Only or No Action |
| Golden cross or MACD bullish cross | Fast Recheck only when an active bullish plan exists and price remains executable | Same rule; otherwise Notify Only |
| Entry into planned buy zone | Notify Only by default | Notify Only by default |
| Above chase ceiling | Notify Only: Do Not Chase | Notify Only: Do Not Chase |
| Invalidation boundary crossed | Triggered Risk plus immediate deterministic warning | Notify Only and invalidate plan |
| Take-profit zone entered | Existing take-profit path | No Action |

Do not invoke paid analysis merely because price enters a previously approved buy zone. The weekly plan
already authorized monitoring that zone; the reminder should normally be mechanical.

## 8. Plan proximity monitoring

Tier 1 must also monitor weekly plan levels, independent of indicator crossovers:

- approaching the buy zone;
- entering the buy zone;
- leaving the buy zone;
- crossing the chase ceiling;
- crossing invalidation;
- entering take-profit/resistance territory;
- plan expiry.

Initial defaults:

```yaml
weekly_plan:
  approach_atr: 0.5
  stale_move_atr: 0.5
```

Only state transitions should notify. Do not repeat the same reminder every scan. A materially changed
plan, a new weekly plan, or re-entry into a state after leaving it may re-arm the reminder.

## 9. Post-analysis price revalidation

The price may move while an LLM analysis is running. Immediately before rendering an actionable
message:

1. fetch or use a sufficiently fresh current price;
2. compare it with the price and timestamp used by the analysis;
3. reclassify it against the active plan;
4. downgrade or block the action when the execution range is no longer valid.

Telegram status must be one of:

- `ACT NOW`
- `WAIT FOR LIMIT`
- `DO NOT CHASE`
- `RISK REVIEW`
- `PLAN INVALID`
- `STALE — RECHECK REQUIRED`
- `INFORMATION ONLY`

The current status is deterministic. The LLM may supply reasoning but may not override the price guard.

## 10. Paid-analysis limits

Add configurable per-ticker and global trading-day budgets. Initial configuration:

```yaml
triggered_analysis:
  enabled: false
  max_per_ticker_per_trading_day: 1
  max_global_per_trading_day: 2
  bearish_shock_atr: 0.75

tier2_schedule: weekly
```

`enabled: false` is the initial shadow-mode setting. Weekly Full and the existing take-profit path are
not silently suppressed by the triggered-analysis budget. When a budget prevents analysis, send the
deterministic reminder if it is otherwise material and record `budget_exhausted`.

Use the exchange calendar's trading-session boundary, not local midnight, when resetting budgets.

## 11. Fast Recheck specification

Fast Recheck should:

- load the active weekly plan and its saved digest;
- include the latest price, relevant technical trigger, plan-relative status, position context, and
  freshness;
- invoke only the advisor path;
- produce an event override or information-only response;
- apply conflict and post-analysis price guards;
- never silently replace the weekly base plan.

If the digest or valid weekly plan is unavailable, fall back to Notify Only rather than expanding into
an unplanned full pipeline.

## 12. Triggered Risk specification

Triggered Risk should run only the components needed to reassess material downside or thesis change:

- market analyst;
- sentiment analyst;
- news analyst;
- bull/bear debate;
- simplified risk review;
- advisor.

Do not run the routine full weekly workflow unless explicitly routed as Weekly Full. Save the reasons
that caused escalation and all component/model/cost metadata already supported by Watchy.

## 13. Telegram contract

Telegram must remain readable without opening logs. Every material message should include, when
applicable:

- ticker and current price with timestamp;
- deterministic status;
- why the message was sent now;
- current weekly-plan context;
- practical guidance;
- a specific `do not` instruction;
- invalidation and expiry;
- upstream verdict;
- advisor action;
- whether they agree or conflict;
- analysis mode and source freshness;
- reminder that Watchy is advisory, not an execution system.

Example:

```text
NVDA — WAIT FOR LIMIT
Price: $123.40 as of 10:32 ET

Why now: price entered the weekly buy zone after a MACD bullish cross.
Weekly plan: bullish while above $116; buy zone $121–$123; chase ceiling $125.
Guidance: consider a limit order within the planned range if the thesis still fits your portfolio.
Do not: chase above $125.
Invalidation: daily close below $116. Plan expires after Friday's session.

Verdict: BUY | Advisor: HOLD | Alignment: conflict — human review required
Mode: Tier 1 plan reminder; no new LLM analysis
```

Long messages must continue to use the existing safe Telegram splitting behavior.

## 14. Failure behavior

- Weekly Full failure: alert once, retain the record, and do not extend an expired plan.
- No valid weekly plan: technical warnings may still notify, but entry guidance is information-only.
- Market data stale/unavailable: suppress actionable wording and label the message stale.
- Fast Recheck input missing: Notify Only.
- Triggered Risk failure: send the deterministic risk warning with an analysis-failed label.
- Malformed structured output: store diagnostics; do not activate the plan.
- Restart: reminder transitions and budgets remain deduplicated through persisted state.
- Database migration failure: fail safely and clearly; never recreate or discard the live database.

## 15. Manual controls and rollback

Provide documented controls to:

- force Weekly Full for one ticker;
- run a dry routing decision for one ticker;
- inspect the active plan and latest override;
- expire or deactivate a plan without deleting history;
- enable/disable triggered analysis globally;
- switch `tier2_schedule` between `weekly` and `daily`;
- render a Telegram preview without sending;
- run shadow mode without any paid triggered call.

The production rollback is configuration-based: restore daily Tier 2 and disable triggered analysis
without reverting the database migration.

## 16. Observability

For every Tier 1 evaluation, log enough structured data to reproduce routing:

- ticker, trading session, held/watch-only/unknown position state;
- all observed triggers;
- active plan ID and plan-relative state;
- selected route and all rejected/escalated alternatives;
- cooldown and budget result;
- whether an LLM was invoked;
- analysis components, model usage, latency, and cost;
- pre-analysis and post-analysis prices;
- final Telegram status and notification result.

Do not log secrets or private account details beyond the position context Watchy already allows.

## 17. Zero-cost routing replay

Before enabling paid triggers, implement an offline routing replay that uses existing Watchy logs/data
and, only when explicitly provided, read-only exported research data. It must not call an LLM or write
private data into the Watchy repository.

The replay should report:

- trigger and route counts by ticker and position state;
- simultaneous-trigger collapse rate;
- estimated paid-call count and estimated cost;
- budget suppressions;
- time between trigger and price leaving the proposed execution range;
- reminder repetition before and after state deduplication;
- missing-plan and stale-plan frequencies.

The replay evaluates routing volume and timing, not profitability. Do not tune thresholds to maximize
historical returns.

## 18. Implementation phases

Each phase must end with focused tests, the full non-E2E test suite, updated documentation, updated
shared memory, and a checkpoint commit. Work directly on `main` per repository instructions. Do not
push during the live Tier 2 window. If pull/merge conflicts occur, stop and ask the user.

### Phase 1 — Contracts and persistence

- Define plan, reminder-state, route, and Telegram-status types.
- Add backward-compatible database migrations and history queries.
- Add strict validators and fixtures.

### Phase 2 — Weekly plan generation

- Extend weekly prompts/structured output.
- Persist a validated weekly base plan.
- Handle supersession, expiry, and failed refresh.
- Render the expanded weekly Telegram message.

### Phase 3 — Plan monitoring

- Add deterministic price-to-plan classification.
- Add persisted transition detection and reminder deduplication.
- Add Notify Only Telegram messages.

### Phase 4 — Pure trigger router

- Implement the routing matrix, priority arbitration, cooldown, and budget decisions as pure logic.
- Add shadow-mode logging with triggered calls disabled.

### Phase 5 — Price and conflict guards

- Add post-analysis price refresh/revalidation.
- Add chase, invalidation, stale-data, and verdict/advisor conflict guards.

### Phase 6 — Fast Recheck

- Reuse the weekly digest and advisor-only path.
- Persist event overrides without changing the weekly base plan.
- Add fallback behavior and cost accounting.

### Phase 7 — Triggered Risk

- Add the reduced risk pipeline.
- Add concurrency protection, budgets, failure notifications, and component/cost logs.

### Phase 8 — Replay and operational controls

- Add zero-cost routing replay and summary output.
- Add plan inspection, forced run, expiry, dry-run, preview, and rollback controls.

### Phase 9 — Documentation and release preparation

- Update README files, configuration examples, `CLAUDE.md`, operational docs, and shared memory.
- Unify the package version in `pyproject.toml` with release tags; do not leave it at `0.1.0`.
- Document schema migration, rollback, and VPS deployment steps.
- Prepare release notes using the positioning below.

## 19. Test requirements

At minimum, add tests for:

- migration from a representative existing database;
- plan validation and malformed/partial output;
- range ordering and nullable levels;
- weekly supersession and failed-weekly expiry;
- reminder transitions, re-entry, restart deduplication, and expiry;
- every routing-matrix row for held, watch-only, and unknown positions;
- simultaneous-trigger priority and single-analysis enforcement;
- trading-session budget resets and concurrency;
- missing plan/digest and stale market data;
- post-analysis price movement through buy zone, chase ceiling, and invalidation;
- verdict/advisor conflict blocking;
- Telegram content, escaping, and message splitting;
- take-profit regression behavior;
- daily-schedule rollback compatibility;
- replay determinism and proof that replay makes no LLM calls.

Keep `tests/test_e2e.py` manual unless the repository's existing policy changes. Do not require real keys
for the automated test suite.

## 20. Deployment and release gates

1. **Local complete:** all phases implemented, documentation current, automated tests pass, working tree
   contains no private data.
2. **Shadow:** deploy with `tier2_schedule: weekly` only when operationally approved; leave
   `triggered_analysis.enabled: false`. Observe routes and mechanical reminders for one to two weeks.
3. **Review:** inspect alert volume, duplicates, missed risk cases, stale plans, execution-window timing,
   and estimated cost. Change thresholds only for operational reasons supported by the shadow data.
4. **Limited enablement:** enable paid triggers for a small subset of tickers with the configured global
   and per-ticker caps.
5. **Production:** expand only if messages are timely, understandable, and operationally reliable.

Create `v2.0.0-rc.1` after implementation and tests, before or at the start of shadow validation. Create
`v2.0.0` only after shadow validation and limited enablement succeed.

Recommended release title:

> **Watchy 2.0 — Plan Weekly, React When It Matters**

Recommended positioning:

> Watchy 2.0 turns daily AI opinions into a weekly trading plan with event-driven monitoring. The goal
> is fewer but more useful interventions, explicit execution boundaries, preserved risk and take-profit
> monitoring, and lower unnecessary LLM usage. It remains a decision-support system and does not claim
> to predict returns or generate alpha.

## 21. Completion criteria

Implementation is complete only when:

- routine daily Tier 2 is disabled by production configuration but remains a documented rollback;
- first-session Weekly Full reliably produces a validated, persisted plan;
- Tier 1 monitors plan levels and routes triggers deterministically;
- repeated scans do not spam Telegram;
- paid analysis obeys concurrency, cooldown, and daily budgets;
- price movement during analysis cannot produce stale actionable advice;
- conflicting model judgments are visible and non-actionable;
- take-profit behavior remains operational;
- Telegram includes context, guidance, boundaries, freshness, and provenance;
- all required automated tests pass;
- README, config examples, operational documentation, shared memory, and version metadata agree;
- no exported Schwab, Telegram, or research data is committed;
- shadow and rollback procedures are documented and runnable.

## 22. Implementation status

Tracked per phase as the work lands (local checkpoints on `main`).

- **Phase 1 — Contracts and persistence: done.** `watchy/plan.py` defines the plan, reminder-state,
  route, position-state and Telegram-status types, the strict `WEEKLY PLAN` block parser and
  `validate_plan`. `state.py` adds `analysis_plan`, `plan_reminder_state`, `triggered_budget` and
  `route_log` (all additive `CREATE TABLE IF NOT EXISTS`), sets `PRAGMA user_version = 2`, and takes a
  one-time SQLite online backup (`state.db.v0-backup-<UTC>`) before upgrading a 1.x database. A
  migration error raises and leaves the database in place. `market_calendar` gains
  `session_label` (New York session date, used for budgets) and `week_session_bounds` (plan validity).
- **Phase 2 — Weekly plan generation: done.** `tier2_schedule: weekly` makes the 10:02 UTC job run
  only on the week's first session, as a Weekly Full batch (every ticker, full 3-way risk, cadence
  and proximity gate bypassed). The advisor gets the strict plan-block instructions
  (`plan_request=True`); `watchy/weekly.py` builds, validates and persists the plan (valid plans
  supersede the previous weekly base; invalid ones and pipeline failures are stored as `invalid`
  rows that never extend the old plan). A separate `<TICKER>_weekly_digest.json` preserves the
  analysis the plan came from. The weekly card (`watchy/messages.py`) is appended to the advice
  message; one batch alert lists tickers without a valid plan. Until the Phase 5 guards land, every
  weekly card is labelled `INFORMATION ONLY`.
- **Phase 3 — Plan monitoring: done.** `watchy/plan_monitor.py` classifies price against the plan
  (precedence: invalidated > take-profit/resistance > above chase > in buy zone > approaching from
  above within `approach_atr`·ATR > outside; below the zone but above invalidation reads *outside*)
  and detects transitions against the persisted `plan_reminder_state`. Only entering a material
  state, or leaving the buy zone, notifies; a new plan re-arms; the same reminder is not repeated
  within `weekly_plan.renotify_h` (boundary flapping). `watchy/guards.select_status` picks the
  deterministic status (a mechanical reminder is never `ACT NOW`); `watchy/monitor.py` sends the
  Notify Only card. **Deviation (conservative):** crossing the invalidation level withdraws the plan
  (`status = invalidated`, history kept) for held as well as watch-only tickers, so no later scan
  can emit entry guidance from a broken thesis; held tickers additionally route to Triggered Risk
  (Phase 4). Stale market data (no fetch time, older than `market_data_max_age_min`, or a previous
  session's bar) forces `STALE — RECHECK REQUIRED`.
- **Phase 4 — Pure trigger router: done.** `watchy/router.py::route()` implements §7's matrix and
  priority (invalidation-driven Triggered Risk ranks with TAKE_PROFIT; take-profit wins the tie so
  the existing path is preserved), keeps every reason, and records rejected/superseded alternatives
  and cooled-down triggers. Paid routes are downgraded to Notify Only when disabled (shadow), not on
  the `triggered_analysis.tickers` allow-list, Fast Recheck inputs are missing, or a budget is spent.
  In weekly mode Tier 1 runs `monitor.scan_planned` (the 1.x paid-rescan path remains for
  `tier2_schedule: daily`); the #28 gate was split into `_take_profit_decision` +
  `_fire_take_profit` with unchanged rules, and the whole take-profit test suite also runs through
  the weekly router. Every evaluation writes a `ROUTE {json}` journal line and a `route_log` row.
  **Additions to the matrix (documented choices):** `rsi_oversold` follows the Bollinger-lower row;
  a volume/ATR anomaly *without* a negative move is Notify Only; unknown position state uses the
  held (risk-side) rules, as the 1.x Tier 2 gate did; when a wanted interpretation did not run
  (shadow, budget, missing input) the reminder's entry wording is capped at `INFORMATION ONLY`.
- **Phase 5 — Price and conflict guards: done.** `guards.revalidate()` reclassifies the plan against
  a price refreshed after the analysis (`monitor.fetch_live_price`, via the cached scanner fetch);
  if the refresh fails the pre-analysis price is used only while younger than
  `market_data_max_age_min`. `select_status` precedence: stale feed → invalidation → Triggered Risk
  → above chase (`DO NOT CHASE`, even when the price ran there during the analysis) → moved more than
  `stale_move_atr` → expired/missing/invalid plan → entry/exit rules. `classify_alignment` shows any
  verdict/advisor direction difference as a conflict for human review; `entry_blocked` (verdict
  SELL/HOLD with advisor BUY/ADD, or verdict SELL with a bullish buy plan) caps entry wording at
  `INFORMATION ONLY`; `ACT NOW` requires `agree`, HIGH urgency, a fresh price and an executable plan
  state. Weekly cards now carry a real status; the pre-market prior-close bar does not mark them
  stale by itself.
- **Phases 6–7 — Fast Recheck and Triggered Risk: done (one combined checkpoint; they share
  `watchy/triggered.py`).** Before spending, the ticker lock is taken non-blocking (a Weekly Full or
  another analysis in progress → Notify Only, `busy`) and a slot is reserved atomically in
  `triggered_budget`; the reservation is marked `ok`/`failed`. Fast Recheck loads the *weekly*
  digest (missing digest or plan → Notify Only, no reservation) and calls only the advisor with an
  event-context block. Triggered Risk runs market + sentiment + news, bull/bear and simplified risk,
  saves only the *latest* digest (the weekly digest stays the plan's source), then the advisor; an
  invalidation-driven run first sends the deterministic warning. Both persist an `event_override`
  (one-session validity, the base plan's levels copied so the advisor cannot move boundaries),
  re-check the price, and render through the guards. Failures fall back to the deterministic
  reminder labelled "analysis failed". Model/thinking level, components, latency, pre/post price and
  override id go into the ROUTE record; token cost stays in the existing `TOKENCOST`/`GEMINICOST`
  lines.
- **Phase 8 — Replay and operational controls: done.** `watchy/replay.py` opens the database with
  SQLite `mode=ro` (it cannot write), tolerates 1.x databases, groups signal rows fired within
  120 s into one scan, infers held/watch from the advice log, and replays the router with simulated
  per-session budgets; it re-routes every logged 2.0 `ROUTE` record as a determinism check and
  derives dedup and time-to-leave-range statistics from the `route_log` sequence. Per-call costs are
  explicit placeholders (`--cost-*`) to be replaced with measured `TOKENCOST`/`GEMINICOST` figures.
  An exported research CSV is read only when passed explicitly; `--out` refuses paths inside the
  repository. `scripts/watchy_ctl.py` (`watchy/ctl.py`) provides `status`, `plan show|history|expire`,
  dry `route` / `preview` (no LLM, no Telegram, no writes), `weekly TICKER --yes` (paid force) and
  `replay`. Schedule and enablement switches stay configuration edits + restart, as §15 requires.
- **Plan-expiry hold (follow-up):** the Weekly Full batch marks itself in `kv`
  (`weekly_full_running = <session>`) so Tier 1 does not announce N "plan expired" reminders while a
  long Monday batch overlaps the open; failures are still alerted once by the batch.
- **Phase 9 — Documentation and release preparation: done.** README (EN/ZH), `project_doc.md`,
  `CLAUDE.md`, `config.yaml` comments, [`WATCHY_2_OPERATIONS.md`](WATCHY_2_OPERATIONS.md) (deployment,
  shadow checklist, limited enablement, controls, migration, rollback) and
  [`RELEASE_NOTES_v2.0.0-rc.1.md`](RELEASE_NOTES_v2.0.0-rc.1.md). Package version `2.0.0rc1` in both
  `pyproject.toml` and `watchy/__init__.py` (a test enforces equality). No tag, release, push or
  deployment was made — those need owner review (§20 gate 2 onward).
