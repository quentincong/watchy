# Watchy Implementation Plan

> **Active work:** The implementation-ready plan for the next major redesign is
> [`WATCHY_2_IMPLEMENTATION_PLAN.md`](WATCHY_2_IMPLEMENTATION_PLAN.md). The material below is retained
> as the completed 1.x implementation history and must not be mistaken for the current backlog.

The original file-by-file backlog (issues #1–#14) is **complete**. Finished work lives in
git history and the closed GitHub issues. The only remaining items are **deferred by choice**:
the #4 bearish-skip (dropped until Schwab is authoritative) and open-orders (optional) — plus
the pre-deploy smoke steps.

## Status (as of 2026-06-08)

Done, pushed, unit-tested (192 tests green), and closed on GitHub:

- **Phase 1:** #13 crossover signals, #11 Telegram 4096 split, #10 DeepSeek advisor key
- **Phase 2:** #9 concurrency (RLock + per-ticker locks + scheduler jitter), #1 Tier 2 throttle,
  #2 yfinance-cache (with robust fallback + `max_age` staleness bound)
- **Phase 3:** #14 Tier 2 cadence (+ Saturday skip), #8 level-signal transitions (+ DB `_migrate`),
  #7 Tier 1 market-hours guard
- **Phase 4 (partial):** #5 price-proximity skip, #3 Telegram content/verdict

**No blocking work left.** The #4 position-context backend has **landed** (see below); the
bearish-skip is **dropped for now** and open-orders is **optional** — both deferred by choice,
not blockers. Deployable now — the position source degrades gracefully
(Schwab live → cache → manual file → no context).

## Pre-deploy smoke steps — DONE & PASSED (VPS, 2026-06-08)

1. **`tests/test_e2e.py GOOG`** ✅ — full pipeline → manual-file position → advisor → Telegram.
   Surfaced & fixed a live Telegram 400 (sendMessage payload was missing `chat_id`).
2. **`scripts/validate_yfc.py`** ✅ (Monday market hours) — yfc tracks the still-forming bar within
   0.0112% (« 0.2% threshold), `Final?=False`, `max_age=10min`, verdict `OK — yfc compatible`. No
   `Final?`/`FetchDate` degrade-guard needed.

Daemon is live under systemd (`watchy.service`, env `trading`). Run repo scripts with that env's
python — the bare `python` shim lacks the optional `yfinance_cache` (harmless fallback).

## #4 — Position data source  *(was: "Schwab API integration")*

**Goal — the only reason this feature exists:** know the user's current **position + open orders**
per ticker, to (a) give the advisor position context and (b) drive the Tier 1 bearish-skip.

### (a) Position-context backend — **DONE** (`watchy/positions.py`)

Settled design: a layered, robust `PositionSource` so the daemon keeps working when Schwab can't
refresh (the 7-day reauth was the blocker). Fallback chain:

```
Schwab API (live)  →  on-disk cached last-good snapshot (flagged stale)  →  manual positions.yaml
```

- **`SchwabClient`** (`schwab.py`) is the live layer — **implemented via `schwabdev`** (read-only:
  positions + balances). `get_account_summary` returns `None` on unavailable/error (schwabdev missing,
  OAuth not done, expired refresh token, API failure) and an `AccountSummary` only on genuine success.
  Lazy, cached client; account selected by `account_id` (or first linked). Mapping/selection unit-tested
  with a faked client (`tests/test_schwab.py`, 9). Needs a one-time browser OAuth on the daemon host;
  refresh token lasts 7 days. **Open orders** are not fetched yet — optional follow-up (`account_orders`).
- **`PositionCache`** writes a timestamped JSON snapshot on every successful live fetch and serves it
  (labelled with its age) when the live fetch fails — stale-but-real data survives token lapses.
- **`FilePositionSource`** reads `~/watchy_config/positions.yaml` (schema in `positions.example.yaml`)
  as the final backstop; enriches with live yfinance prices to derive market value / unrealized P&L.
  Also age-labelled: `as_of()` prefers an explicit `as_of:` field, else the file's mtime.
- **`RobustPositionSource`** memoizes one snapshot per scan and appends provenance (`source: …`) so
  stale/fallback data is never presented as live. Wired into advisor, tier1, tier2, and the e2e test.
- Tests: `tests/test_positions.py` (19) — parsing, enrichment, cache round-trip, full fallback chain.

### (b) Tier 1 bearish-skip — **DROPPED for now** (former #6); revisit only with authoritative Schwab

**Decision (2026-06-08, user):** do **not** implement while the manual file is the position source.
The skip's only payoff is saving LLM cost on a bearish cross for a name you don't hold — not worth the
failure mode it introduces. Determining "confirmed-empty" from the manual file is unsafe/high-upkeep:
the user won't list non-held tickers as `quantity: 0`, and an opt-in "file is authoritative" flag would
silently go stale (a forgotten holding → its death cross gets wrongly skipped = a missed sell alert).

**Revisit condition:** only once **Schwab is live and authoritative** — then held/not-held is known for
free with zero upkeep. If built then, gate it strictly:

- Skip **only** when an *authoritative live Schwab* fetch reports the ticker as not-held
  (CONFIRMED-EMPTY). Manual file, cached snapshot, fetch-error, or Schwab-disabled → **UNKNOWN → always
  run** the pipeline. Never infer "empty" from the manual file's silence.
- Applies to `death_cross` / `macd_bearish_cross` only; `rsi_overbought` / `bollinger_upper_breach` are
  SEPA entry signals and are never skipped.

## #28 — Take-profit / anti-round-trip  *(LLM + limit-order design; landed)*
The user's #1 pain: winners run up, the gain isn't banked, they round-trip. Pure prompt tuning
(the f284518 clause) can't fix it — the advisor is downstream of the analysis, which still calls a
top "strong", and the LLM judges "extended" inconsistently. **Resolved design** (decided with the
user, not the issue's original mechanical-trailing-stop plan):
- **Keep the LLM in charge**; the mechanical part is a tiny **gain-gate** that only decides *when*
  to wake the advisor and hands it ground-truth facts. Advisory-only — the user places the order.
- **floor = unrealized gain %** (arms the take-profit zone; default 10, per-ticker override); **runway
  = ATR** (how many ATRs of room to the analysts' cited upside → bank now vs. let it run). Upside
  level extracted best-effort from the digest; degrades to a pure `price + k×ATR` limit when absent.
- **Execution = pre-placed sell-limit**, whole shares only (user doesn't trade fractional). The limit
  catches the intraday high on its own, so daily cadence suffices — hence the trigger split:
  - **Primary: daily Tier 2** injects the directive for every held name in the zone;
  - **Intraday: Tier 1 zone-entry trigger** fires an advisor-only call (reuses the last saved
    digest — no fresh pipeline) the moment gain crosses the floor between daily runs, cooldown-
    guarded, transition-detected via `state.prev_take_profit_zone` (ALTER TABLE migration).
- New: `watchy/take_profit.py` (pure logic), `watchy/digest_store.py` (reuse), `take_profit` config
  block (shipped opt-in; **enabled by default since 2026-07-27** after running live on the VPS from
  2026-07-23), advisor `Take-Profit:` output line, `notify.take_profit_alert`.
- **Deferred:** hybrid full-pipeline handoff on fire; extracting a first-class resistance level (the
  regex extractor is best-effort). Superset of #17 candidate A (sell-side); distinct from #26 (buy-side).

## 2026-09-10 — Advisor urgency correction + DeepSeek V4.1 migration

- Advisor `Urgency` is now strictly an order deadline: HIGH=today, MEDIUM=within five sessions,
  LOW=no order change this week. HOLD is always LOW; actionable timing requires an actionable Decision.
- Entry-only `Target` may time BUY/ADD but never SELL/TRIM. Exit timing uses an exit level in the detail
  or the armed `Take-Profit` level. The parser normalizes leading enum text and safely defaults unknown
  urgency values to LOW with a warning.
- Odd-lot behavior is arithmetic rather than subjective: an integer position of at least two shares may
  trim 1..quantity-1 whole shares; one ordinary-priced whole share cannot TRIM; existing fractional and
  single ≥$1,000 positions may use an explicitly labelled fractional market trim.
- TradingAgents defaults both reasoning roles to canonical `deepseek-flash`. DeepSeek retired V4 Flash
  immediately and announced that V4 Pro will route to V4.1 Flash on 2026-09-14, so the old two-model
  split cannot persist. The API request shape and default high thinking mode remain compatible; only the
  model IDs, cost tables, and dated Pro-alias routing needed code changes.

## Resolved design decisions (context)
- **#13** crossover → `== 0` / `== 1` (not `not prev`, which false-fires on a ticker's first scan).
- **#14** Tier 2 → ordinary trading days simplified risk, **first trading day of the week** full
  3-way risk. Runs **only on XNYS trading days** (weekends + holidays skipped via `is_session`).
  The weekly full-risk run rides the first session of the week (usually Mon; shifts to Tue when Mon
  is a holiday) instead of a separate weekend run — the old Sunday+Monday pair analysed the same
  stale Friday close, so dropping the weekend run removes that duplication while keeping the weekly
  full-risk guarantee. Shared calendar helpers live in `watchy/market_calendar.py`
  (`is_trading_day`, `is_weekly_full_risk_day`); the gate (#15) never gates the weekly full day.
  Calendar-less fallback: trading day = Mon–Fri, weekly full day = Monday.
- **#8** level signals → fire on entry (transition-aware); `state._migrate()` ALTER TABLEs the new
  columns into the live VPS `state.db`.
- **#7** → Tier 1 only runs in the US regular session (`exchange_calendars`, DST/holiday correct);
  Tier 2 is **not** gated (weekend news/sentiment still matter).
- **Closed:** #6 (folded into #4), #12 (duplicate of #11).
