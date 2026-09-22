# Watchy 2.0 — Plan Weekly, React When It Matters (`v2.0.0-rc.1`)

Watchy 2.0 turns daily AI opinions into a weekly trading plan with event-driven monitoring. The goal
is fewer but more useful interventions, explicit execution boundaries, preserved risk and take-profit
monitoring, and lower unnecessary LLM usage. It remains a decision-support system and does not claim
to predict returns or generate alpha.

This is a release candidate for **shadow validation**: the new routing rules and thresholds are
hypotheses that still require prospective validation. Paid triggered analysis ships disabled.

Package version: `2.0.0rc1` (PEP 440 form of `v2.0.0-rc.1`).

## Highlights

- **Weekly Full** — the full TradingAgents analysis (four analysts, bull/bear, full 3-way risk) now
  runs once, on the first trading session of each week, and the advisor emits a strict
  machine-readable **weekly plan**: decision, urgency, thesis, buy zone, chase ceiling, invalidation
  level/condition, trim condition, resistance, take-profit price, guidance, a concrete *do not*,
  validity sessions and provenance. Plans are validated (enums, finite numbers, range ordering,
  plausibility, timestamps, expiry) before activation; invalid output is stored for diagnosis and
  never becomes actionable. A failed refresh never extends last week's plan.
- **Plan monitoring (no LLM)** — Tier 1 classifies each scan against the plan and notifies only on
  transitions: approaching / entering / leaving the buy zone, crossing the chase ceiling, crossing
  invalidation (the plan is withdrawn), reaching resistance, plan expiry. Reminder state is
  persisted, so restarts don't repeat alerts; a new plan re-arms; boundary flapping is suppressed.
- **One pure router** — technical signals, plan transitions and the take-profit gate collapse into
  exactly one route per scan (`TAKE_PROFIT` / invalidation > `TRIGGERED_RISK` > `FAST_RECHECK` >
  `NOTIFY_ONLY` > `NO_ACTION`), with every reason kept for the log and the message.
- **Fast Recheck / Triggered Risk** — optional paid interpretation (advisor-only on the weekly digest,
  or a reduced market+sentiment+news pipeline) with per-ticker and global per-session budgets,
  atomic reservations, a ticker lock, and event overrides that never rewrite the weekly plan.
- **Deterministic safety guards** — `ACT NOW`, `WAIT FOR LIMIT`, `DO NOT CHASE`, `RISK REVIEW`,
  `PLAN INVALID`, `STALE — RECHECK REQUIRED`, `INFORMATION ONLY`. Price is re-checked after every
  analysis; above the chase ceiling is always `DO NOT CHASE`; verdict/advisor conflicts are shown for
  human review and a SELL/HOLD verdict never lets BUY/ADD advice read as actionable.
- **Richer Telegram** — price with timestamp, status, why now, plan context, guidance, *do not*,
  invalidation and expiry, verdict vs advisor alignment, analysis mode, source freshness, and the
  advisory reminder.
- **Observability and zero-cost replay** — a structured `ROUTE` record for every Tier 1 evaluation
  (journal + `route_log`), and `scripts/watchy_ctl.py replay`, a read-only routing replay that
  estimates volume, collapse rate, paid calls, budget suppressions, dedup and timing without any
  LLM call.
- **Operator controls** — `scripts/watchy_ctl.py status | plan show/history/expire | route |
  preview | weekly | replay`.

## Unchanged

- Take-profit (#28/#30) rules, cooldown and advisor call; its full test suite now also runs through
  the weekly router. It wins routing priority and is never limited by the triggered-analysis budget.
- DeepSeek V4.1 Flash for TradingAgents, Gemini 3.5 Flash (low thinking) for the advisor, the
  10:02 UTC off-peak start, Schwab position layering and token-expiry alerts.

## Migration

Additive SQLite migration (four new tables, `PRAGMA user_version = 2`) with a one-time online
backup `state.db.v0-backup-<UTC>` on first start; no existing data is altered. New config keys:
`tier2_schedule` (default `weekly`), `weekly_plan.*`, `triggered_analysis.*` (default disabled).
See [`WATCHY_2_OPERATIONS.md`](WATCHY_2_OPERATIONS.md) §2.

## Rollback

Configuration only: `tier2_schedule: daily` restores the exact 1.x behaviour (daily Tier 2 and paid
Tier 1 rescans); keep `triggered_analysis.enabled: false`. The database migration does not need to
be reverted. See [`WATCHY_2_OPERATIONS.md`](WATCHY_2_OPERATIONS.md) §7.

## Shadow-mode operating procedure

Deploy with `tier2_schedule: weekly` and `triggered_analysis.enabled: false`, observe routes and
mechanical reminders for one to two weeks, review alert volume, duplicates, missed risk cases, stale
plans, execution-window timing and estimated cost, then enable paid triggers for a small ticker
subset. Details: [`WATCHY_2_OPERATIONS.md`](WATCHY_2_OPERATIONS.md) §3–§5. `v2.0.0` follows only
after shadow validation and limited enablement succeed.

## Known limitations

- Replay cost figures are placeholders until replaced with measured costs; 1.x signal history lacks
  previous-close data for the bearish-shock test.
- Documented choices beyond the plan's routing table: `rsi_oversold` follows the Bollinger-lower
  row; an ATR/volume anomaly without a negative move is Notify Only; an unknown position uses the
  held rules; crossing invalidation withdraws the plan for held and watch-only tickers alike; a
  reminder whose wanted interpretation did not run is capped at `INFORMATION ONLY`.
