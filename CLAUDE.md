# Watchy — Project Instructions for Claude

Watchy is a stock-monitoring daemon built on top of TradingAgents.
Tier 1 = hourly technical signal scanner (no LLM). Tier 2 = scheduled daily LLM pipeline.

## How this file works

Keep CLAUDE.md lean — it loads into context every session. Durable ground truth
(conventions, workflow, architecture) lives here; dated status, ops detail, and
investigations live in `.claude/memory/*.md` (recalled on demand, committed to the repo
and synced across machines — see memory `watchy-memory-sync`). When a status block grows,
move the detail to a memory file and leave a one-line pointer here.

## Current status (2026-06-15)

Backlog #1–#18 essentially done; system deployed on the VPS and validated. Detail in memory:

- **Cost / per-component TOKENCOST** → memory `watchy-api-cost-baseline`. `pro` (deep_think) ≈30% of cost
  in just 2–3 calls; top nodes = Portfolio Manager + Market Analyst. Current run-rate (15 tickers, tiered
  pricing, measured 2026-08-21 off the DeepSeek usage CSV): **¥7.9/trading day ≈ ¥165/month ≈ ¥2.0k/year**.
  The 2026-07-31 V4-Flash retrain cost **+23% on the actual bill** (flash +35%, pro flat) — the "+40%"
  figure elsewhere is the token-side per-ticker number, not the billed one.
- **Tier 2 8% proximity gate (#15/#16)** → memory `watchy-pending-enable-tier2-gate`. Enabled globally;
  self-bootstraps off `derived_target_price`. Held tickers & the weekly full-risk day never gated; Tier 1
  never gated. Note the gate only bites watch-only names — with 13/18 tickers held it saves little.
- **Schwab live + token-expiry alerts (#4)** → memory `watchy-issue-plan`. Re-auth every ≤7 days on the
  VPS: `scripts/schwab_oauth.py --force` (must use `--force` to reset the 7-day clock).
- **VPS migration DONE (2026-06-24)** → memory `watchy-vps-migration`. Now single-machine on Bandwagon LA
  `qcvps` (2 GB, Ubuntu 24.04); old Hetzner box deleted. Steady-state resident ~460–510 MB (doesn't drop
  after a batch — CPython arena retention); watch swap when the planned Docker/CouchDB stack lands.
- **SSH airport-port workaround (sshd on 8022)** → memory `ssh-airport-port-block`. (New box uses plain
  port 22; Cloudflare Tunnel SSH still a TODO.)

⚠️ The auto-update timer restarts the daemon on every push — **don't push during the Tier-2 window
(~10:00–13:00 UTC; Mondays run long, to ~14:00)** or you interrupt the batch. Measure before trusting
this range — per-ticker time moves whenever the DeepSeek flash model is retrained (see below).

## Architecture / ops ground truth

- Daemon runs under systemd (`watchy.service`) as user `watchy`, env = `trading` pyenv
  (`/home/watchy/.pyenv/versions/3.11.9/envs/trading/bin/python`). Run repo scripts with THAT python
  (the bare `python` shim lacks `yfinance_cache`).
- Tier 1: every 30 min/ticker (jitter ±5 min, market-hours gated, event-driven). Tier 2: 10:02 UTC on
  **US trading days only** (weekends AND NYSE holidays skipped — there is no weekend run). The full 3-way
  risk debate rides the **first trading session of each week** (normally Monday, shifting to Tuesday on a
  holiday — `market_calendar.is_weekly_full_risk_day`); other days run 4 analysts with simplified risk.
- **Why 10:02 UTC**: DeepSeek's peak/off-peak billing goes live **2026-08-16 16:00 UTC** — peak = 01:00–04:00
  & 06:00–10:00 UTC (Beijing 09:00–12:00 & 14:00–18:00) at **2× the off-peak rate**. 10:02 is the earliest
  start that clears it, with two minutes of deliberate margin because DeepSeek does not document whether
  10:00 is the last peak minute or the first off-peak one. Starting earlier is not free — 08:00 would sit
  inside the window outright. Tier 1 (13:30–20:00 UTC) is off-peak too. The old V3/R1 off-peak *discount* is
  gone (retired with those models 2026-07-24). The batch should also finish before the 13:30 UTC market
  open; `tier2_days` (below) is what keeps it short enough. **Confirmed by the bill (2026-08-21)**: the
  post-cutover days came in at 1.8–2.1× the flat-price baseline, not the ~3.8× that peak billing would
  produce — every call is landing off-peak. Moving `tier2_time_utc` earlier would double the bill.
- **Tiered Tier 2 cadence** (`tier2_days`, per-ticker in `config.yaml`, global default = every day): one
  daily 4-analyst pipeline costs ~$9.6/yr per ticker regardless of position size, so small positions run on
  a lighter rotation. The weekly full-risk day and any position already in the take-profit zone always run,
  whatever the cadence says.
- Config: `config.yaml` (committed, non-secret). Secrets: `~/watchy_config/secrets.yaml` (off-repo, never
  committed). Under systemd the daemon reads `~/watchy/config.yaml` (the repo copy — the unit does NOT set
  `WATCHY_CONFIG`), so the live watchlist can differ from origin/main; check before relying on it.
- Logs: `journalctl -u watchy -f`. Job errors → Telegram; startup/config errors → journal only.
- **TradingAgents is installed separately** (`~/TradingAgents`, `pip install -e .`) — NOT in
  `requirements.txt`/`pyproject.toml`. See the capture checklist in memory `watchy-vps-migration`.

## Cross-machine workflow (local + VPS, synced via Git origin/main)

- `git pull` before any session; `git push` at every checkpoint and at session end.
- Commit at each checkpoint — don't let a finished, tested unit sit uncommitted. Reference the issue
  number(s) in the message (e.g. `Fix #9 concurrency …`).
- Keep GitHub issues (`quentincong/watchy`) in sync as you go — close fixed ones once tests pass.
- Commit the `.claude/` directory (shared config + synced memory under `.claude/memory/`). Do NOT commit
  `.claude/settings.local.json` (machine-local, git-ignored) or any secrets.
- Work directly on `main`.
- **If a pull/merge shows CONFLICTS: STOP and ask the human. Never auto-resolve.**

## Conventions

- Add dependencies to **both** `requirements.txt` and `pyproject.toml`.
- The live VPS `state.db` (`~/watchy/state.db`): schema changes need an `ALTER TABLE` migration, not just
  `CREATE TABLE IF NOT EXISTS`.
- Run `pytest` as the gate after each change/phase (pre-authorized — no need to ask). `tests/test_e2e.py`
  is a manual smoke needing real keys.

## Where things live

- Work plan: `docs/IMPLEMENTATION_PLAN.md`. Bugs / decisions / enhancements: GitHub issues
  (`quentincong/watchy`). Cross-machine knowledge: here, `docs/`, memory (`.claude/memory/`), or issues.

## Keeping docs in sync (do proactively, without being asked)

- **`README.md`**: update in the *same* change as any behavior/config/setup/ops change — never defer it.
- **`CLAUDE.md`**: update when conventions/workflow/architecture change. Keep it lean — push detail into memory.
- **`docs/IMPLEMENTATION_PLAN.md`**: update when a decision is revised, rather than silently diverging.
