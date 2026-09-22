# Watchy 2.0 — Operations, Shadow Mode, Migration and Rollback

Applies to `v2.0.0-rc.1` (package version `2.0.0rc1`). Watchy remains advisory-only: it never
places orders. The weekly-plan workflow and its routing thresholds are an initial policy that still
needs prospective validation; nothing here claims better returns or prediction accuracy.

## 1. What changes on deploy

| Area | 1.x (`tier2_schedule: daily`) | 2.0 (`tier2_schedule: weekly`, shipped default) |
|---|---|---|
| Tier 2 | Every trading day at 10:02 UTC (cadence + proximity gate) | Only the first trading session of the week, as **Weekly Full** for every ticker |
| Plans | none | A validated, persisted weekly plan per ticker (`analysis_plan`) |
| Tier 1 signal | Paid `[market+sentiment]` rescan + advisor (capped per day) | Pure router → one route per scan; paid routes only if `triggered_analysis.enabled` |
| Tier 1 plan levels | none | Deterministic reminders on transitions (buy zone, chase ceiling, invalidation, resistance, expiry) |
| Take-profit (#28) | Gain-floor trigger, advisor-only | **Unchanged rules**; it wins routing priority, never budget-limited |
| Telegram | Verdict + advice | Plus a status card: price/time, deterministic status, why now, plan context, guidance, *do not*, invalidation/expiry, verdict vs advisor, mode, freshness |

Paid triggered analysis ships **disabled** (`triggered_analysis.enabled: false`) — that is shadow
mode: routes are computed, logged and the deterministic reminder is sent, but no Fast Recheck or
Triggered Risk call is made. The Weekly Full and take-profit calls still run.

## 2. Database migration

- All 2.0 tables are additive (`CREATE TABLE IF NOT EXISTS`): `analysis_plan`,
  `plan_reminder_state`, `triggered_budget`, `route_log`. No existing table or column changes.
- On the first start against a 1.x database (`PRAGMA user_version` 0) the daemon writes a one-time
  SQLite online backup next to it: `~/watchy/state.db.v0-backup-<UTC stamp>` (git-ignored), then
  sets `user_version = 2`.
- A migration error stops the daemon with a `state.db migration failed ... left in place` message in
  the journal. The database is never recreated or deleted; fix the cause (disk, permissions) and
  restart, or restore the backup.
- 1.x code runs unchanged against a migrated database (it ignores the new tables and
  `user_version`), so a code rollback does not require a database rollback.

## 3. Shadow-mode deployment on the VPS (exact steps)

Deploying means pushing to `origin/main`; `watchy-update.timer` pulls and restarts the daemon
within ~5 minutes. **This requires the owner's approval — it was not done by the implementation
run.** Pick a window outside the market session and outside Monday 10:00–14:00 UTC; Friday after
20:00 UTC or the weekend is best, so Monday's Weekly Full creates the first plans.

1. Local: `git log --oneline origin/main..main` — review the 2.0 commits; run
   `pytest tests/ --ignore=tests/test_e2e.py -q`.
2. VPS: confirm a clean tree so the auto-update fast-forward will not refuse:
   `git -C ~/watchy status --short` (a local `config.yaml` edit blocks the pull — stash or
   `git checkout -- config.yaml` first, and carry any intended change into the repo instead).
3. VPS: optional manual backup (the daemon also takes one):
   `~/.pyenv/versions/3.11.9/envs/trading/bin/python -c "import sqlite3,os; s=sqlite3.connect(os.path.expanduser('~/watchy/state.db')); d=sqlite3.connect(os.path.expanduser('~/watchy/state.db.pre-v2-manual')); s.backup(d); d.close(); s.close()"`
4. Confirm the repo config carries `tier2_schedule: weekly` and `triggered_analysis.enabled: false`.
5. Push `main` (owner). Wait for the timer, or `sudo systemctl restart watchy` on the VPS.
6. Verify in `journalctl -u watchy -n 100`:
   - `Watchy 2.0.0rc1 starting`
   - `Mode: tier2_schedule=weekly triggered_analysis.enabled=False`
   - no `migration failed`; `ls ~/watchy/state.db.v0-backup-*` shows the backup.
7. `PY=~/.pyenv/versions/3.11.9/envs/trading/bin/python; $PY ~/watchy/scripts/watchy_ctl.py status`
   → `user_version=2`, `SHADOW MODE`.
8. If deployed mid-week, tickers have no plan until Monday: Tier 1 still sends technical warnings
   (information-only) and take-profit alerts. To bootstrap a ticker now (paid, one full pipeline
   each): `$PY ~/watchy/scripts/watchy_ctl.py weekly NVDA --yes`.

## 4. Daily / weekly shadow checklist (1–2 weeks)

Journal markers (all greppable):

| Marker | Meaning |
|---|---|
| `WEEKLY_PLANS valid=N invalid=M` | Monday batch summary; `PLAN_ACTIVE` / `PLAN_INVALID` per ticker |
| `WEEKLY_CARD` | status chosen for each weekly card after the post-analysis price re-check |
| `ROUTE {json}` | every Tier 1 evaluation: triggers, plan state, route, effective route, budget, status |
| `PLAN_INVALIDATED` | a plan withdrawn after its invalidation level broke (or watch-only death cross) |
| `TRIGGERED` | a paid Fast Recheck / Triggered Risk (only once enabled) |
| `TOKENCOST` / `GEMINICOST` | unchanged per-call cost lines |

Weekly review:

1. `$PY scripts/watchy_ctl.py replay --days 7 --shadow` and without `--shadow` (estimated paid
   volume if enabled). Output goes to stdout; `--out` must be outside the repository.
2. Alert volume and duplicates: reminders with vs without dedup; any ticker messaging every scan.
3. Missed risk: compare `RISK REVIEW` / `PLAN INVALID` messages with what the market did.
4. Stale plans: `missing_plan_rate`, `stale_plan_rate`; any `PLAN_INVALID` on Monday and why
   (`plan show TICKER` prints validation errors).
5. Execution-window timing: `minutes_to_leave_execution_range` — whether a reminder would have
   arrived in time.
6. Estimated cost vs budget caps (replace the replay's placeholder per-call costs with measured
   `TOKENCOST`/`GEMINICOST` figures before quoting).

Change thresholds only for operational reasons the shadow data supports — never to maximise
historical returns.

## 5. Limited enablement (after review)

```yaml
triggered_analysis:
  enabled: true
  max_per_ticker_per_trading_day: 1
  max_global_per_trading_day: 2
  tickers: ["NVDA", "AVGO"]     # small subset first; [] = all
```

Commit to `main` (preferred — keeps the VPS tree clean) and let the timer restart the daemon.
Budgets reset on the New York trading-session date and survive restarts (`triggered_budget`).

## 6. Manual controls (`scripts/watchy_ctl.py`, run with the trading-pyenv python)

| Command | Effect |
|---|---|
| `status` | effective mode, budgets, schema version (read-only) |
| `plan show TICKER` / `plan history TICKER` | current plan + freshness, latest override, reminder state, session budget (read-only) |
| `plan expire TICKER --yes` | deactivate the active plan; history kept |
| `route TICKER [--price P] [--signal S] [--position held\|watch_only\|unknown]` | dry routing decision — no LLM, no Telegram, no writes |
| `preview TICKER [...]` | render the Telegram card for that route without sending |
| `weekly TICKER --yes` | force a Weekly Full for one ticker (paid; sends Telegram) |
| `replay [--days N] [--since ISO] [--events-csv PATH] [--shadow] [--json] [--out PATH]` | zero-cost routing replay (read-only db) |

Enable/disable triggered analysis and switch `tier2_schedule` by editing `config.yaml` and
restarting (`status` shows what the daemon will read).

## 7. Rollback

**Configuration rollback (preferred, no database change):**

```yaml
tier2_schedule: daily          # 1.x daily Tier 2 + paid Tier 1 rescans (max_tier1_pipelines_per_day)
triggered_analysis:
  enabled: false
```

Commit and push (the timer restarts the daemon), or for an emergency edit `~/watchy/config.yaml`
on the VPS and `sudo systemctl restart watchy` — then restore a clean tree
(`git checkout -- config.yaml` after the fix is in the repo) or future auto-updates will refuse to
pull. The 2.0 tables stay and are simply unused; switching back to `weekly` later resumes with the
stored history (plans from before the switch will read as expired).

**Code rollback (only if 2.0 code itself misbehaves):** restore `main`'s tree to the last 1.x commit
(`8c82edb`; the `v1.1.0` tag, `b3aae12`, predates the 1-share take-profit change) with a revert
commit and push. No database restore is needed (§2). Restoring `state.db.v0-backup-*` is only for actual
database damage and discards everything written since the upgrade.

## 8. Known limits

- Replay per-call costs are placeholders; the 1.x `signal_log` lacks previous-close data, so
  historical bearish-shock detection is only exact for signals logged by 2.0.
- The Weekly Full's pre-market price is the prior close; the card re-checks price right before it
  is rendered, but entries still need the regular session.
- A daemon crash during the Monday batch leaves the "batch running" marker set for that session,
  which only delays expired-plan reminders until the next session.
