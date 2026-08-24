# Watchy

[![tests](https://github.com/quentincong/watchy/actions/workflows/ci.yml/badge.svg)](https://github.com/quentincong/watchy/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

> 🌐 Chinese version: [README.zh.md](README.zh.md)

A stock-monitoring daemon built on the [TradingAgents](https://github.com/TauricResearch/TradingAgents)
multi-agent LLM trading framework. Watchy watches your watchlist for you — an
hourly zero-cost technical indicator scan, a daily full-depth analysis, and
position-aware advice pushed to Telegram.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Watchy daemon                    │
│                                                   │
│  Tier 1 (hourly)            Tier 2 (daily)        │
│  ──────────────────────     ──────────────────    │
│  OHLCV + indicators         full 4-analyst        │
│  no LLM                     pipeline              │
│       │                     + debate              │
│       │                     + risk management     │
│       ▼                          │                │
│  signal breach?                  │                │
│       │                          │                │
│    ┌──┴──┐                       │                │
│    │ yes │───→ graduated ────────┘                │
│    │     │     subset                             │
│    │ no  │───→ update state,                      │
│    └─────┘     exit (zero cost)                   │
│                                                   │
│  after every analysis:                            │
│    position source → LLM advisor → Telegram       │
└─────────────────────────────────────────────────┘
```

**Tier 1** scans each ticker at a configurable interval (hourly by default) and
**runs only during US regular trading hours** — market closures, weekends, and
holidays are skipped automatically via `exchange_calendars` (DST-correct). It
fetches OHLCV through yfinance and computes technical indicators with no LLM
calls. It detects 11 signal types: golden/death cross (with the full moving-
average staircase confirmation), RSI extremes, MACD crossovers, Bollinger band
breaches, volume anomalies, and ATR spikes. When a signal fires, it launches a
graduated subset of TradingAgents analysts sized to the signal's significance.

**Tier 2** runs at a configured UTC time on **US trading days only**; weekends and
NYSE holidays (e.g. July 3) are skipped as redundant — the market is closed, the
run would only re-chew the prior close, and nothing is tradable that day. For every
watchlist ticker it launches the full
four-analyst pipeline (Market + Sentiment + News + Fundamentals) with a Bull/Bear
debate. Risk-management depth is day-of-week dependent: **simplified on ordinary
trading days, escalated to the full 3-way risk debate on the first trading day of
each week** (usually Monday; shifts to Tuesday when Monday is a holiday, so every
ticker still gets one guaranteed full-risk run per week without paying for a
separate weekend run on the stale Friday close).

**Tier 2 price-proximity gate (#15):** set a **global default** percent via the
top-level `min_price_proximity_pct` (applied to every watch-only ticker; a
per-ticker `min_price_proximity_pct` overrides it). On ordinary trading days the
expensive LLM pipeline is skipped when the current price is farther than that
percent from the **entry target** (saving DeepSeek cost). The gate is
**watch-only**: **a ticker you currently hold (the position source reports a
non-zero position) is always analysed**, regardless of price — capital at risk is
worth the daily tokens (a position-lookup error is treated as "held" too, erring
toward running). **The weekly full-risk run (first trading day of the week) always
runs** every ticker (a weekly full update incl. news). The entry target uses a
manual `target_price` first, otherwise an **auto-derived value (#16)**: each Tier
2 run extracts it from the advisor's structured `Target:` field — semantically an
**entry / accumulation level only** (not a stop-loss, not a take-profit) — and
stores it in `state.db` (a manual value always wins). Note **Tier 1 is never
gated** — it's the always-on 30-minute radar that covers far-from-target names
between gated Tier 2 runs.

**ATR-adaptive band (#15 follow-up, optional):** instead of a fixed percent, set
`atr_proximity_mult` (global, or per-ticker) to make the band `mult × ATR%`
(ATR% = `avg_atr_20d / price × 100`) — i.e. *skip when price is more than `mult`
typical trading days of movement from target*. Volatile names get a wider band,
calm names a narrower one. The band is clamped to `[proximity_pct_floor,
proximity_pct_ceiling]` (default 4–20%) and falls back to the fixed percent when
ATR data is unavailable. Calibrate the multiple against your watchlist with
`scripts/calibrate_atr_proximity.py` before enabling.

**Take-profit / anti-round-trip (#28):** protects unrealized gains on
held winners — the *don't-let-a-winner-round-trip* discipline. Controlled by the
top-level `take_profit:` block, **enabled by default** (live since 2026-07-23);
set `enabled: false` to turn it off. A held position whose unrealized
gain crosses `floor_gain_pct` (default 10%) enters the **take-profit zone**: an
explicit, fact-filled directive (ATR *runway* to the analysts' cited upside, a
reachable `price + k×ATR` sell-limit — anchored on the broker's live mark, the
same feed the gain is derived from) is injected into the advisor
prompt, so the LLM actively proposes **banking a whole-share tranche via a
sell-limit** — output as a new `Take-Profit:` line (e.g. *"sell 1 share at
192.50"*) — instead of staying silent while the gain fades. The mechanical
gain-gate is ground truth (it doesn't wait for the analysis to flag a top, which
it does inconsistently); the LLM only sizes the trim and sets the limit. It runs
on the **daily Tier 2** advice for every held name in the zone, plus a **Tier 1
intraday zone-entry trigger** that fires an advisor-only call (reusing the last
saved digest — no fresh pipeline) the moment gain crosses the floor between daily
runs, so the sell-limit is set the same day. **Advisory-only** — Watchy tells you
the price and share count; you place the order. `floor_gain_pct` is also
per-ticker overridable (`take_profit_floor_gain_pct`); see the `take_profit` keys
in Configuration.

That intraday trigger also **re-arms whenever the share count drops** — i.e. when
a sell-limit fills. It is otherwise edge-triggered on zone membership alone, and
this account sells highest-cost-first (HIFO): every trim strips the dearest lot
and *raises* the reported gain % on the shares that remain, at an unchanged
price. The zone flag would therefore latch on for the life of the position,
clearing only after a drawdown deep enough to drag the inflated gain back under
the floor — i.e. after the winner has already round-tripped. The share count is
the right signal because the protection here is the *standing* sell-limit, not
the alert: while an order is working an intraday spike fills it with no new
advice needed, so you are only exposed just after a fill, with the order gone.
Watchy never sees your orders, so it infers the fill from the position. A manual
sell re-arms it too, correctly. The per-signal `cooldown_h` still applies, and a
share count that *rises* (a stale cached snapshot serves the pre-trim, larger
figure) is never read as a fill.

For the same reason the gain % **arms the gate and goes no further**: its
magnitude is not passed to the advisor, and both the zone directive and the
standing prompt clause tell the model not to size or trigger off the
`Unrealized P&L` figure that remains visible in the position block (it is still
shown — you need the real broker number, and the same text goes to Telegram).
Whether the floor was crossed is a yes/no fact; *how extended* the move is comes
from price, ATR and the analysts' cited targets, none of which move when you
sell. The journal still logs the percentage (`take-profit zone active for X:
gain=…`) for reconciliation.

Position size decides which actions are actually placeable, so the directive
adapts to the share count:

| Shares | What it can propose |
|---|---|
| **≥ 2** | Normal case — trim a whole-share tranche at a sell-limit. |
| **exactly 1** | A partial trim is arithmetically impossible, so only a full exit or holding. A full exit is offered **only when the ATR runway says price is at the ceiling** (`< runway_near_atr`); with room left it holds, rather than cashing out a winner wholesale. |
| **< 1 (fractional)** | A sell-limit needs whole shares, so **no limit price is proposed** — only a market sell of part or all of the fractional position. This forfeits the pre-placed-limit safety net (you must act by hand), so it is asked for only when warranted. |

**Tier 2 batch order (#21):** the daily batch runs **held tickers first** (capital
at risk), then watch-only **nearest-to-target first**, then no-target names last —
so if a long batch is interrupted (auto-update restart, crash, token expiry), the
most important names were analysed first. Indicators are pre-fetched once per
ticker (throttled) and reused by the pipeline (no double fetch).

**After every analysis**, Watchy fetches the ticker's current position, calls a
lightweight LLM (Gemini by default) to synthesize a **condensed analysis digest**
(the decision chain + each analyst's summary tail, not the full prose) + position
into actionable advice, and pushes a natural-language summary to Telegram. The
advisor's own token usage is logged as a `GEMINICOST` line; its thinking level is
per-tier (`llm.gemini_thinking_tier1` and `llm.gemini_thinking_tier2`, both `low`)
in `secrets.yaml`.

The summary tail is anchored on the Markdown table every analyst prompt asks for
at the end of its report. If that table is missing, the digest silently falls back
to the report's *opening* lines instead of its conclusion — so that case logs a
greppable `ADVISOR_TAIL_FALLBACK` warning with the ticker and analyst. Watch it
after a model change: DeepSeek uses floating aliases and has shipped snapshots
unannounced, and trailing format instructions are the first thing a weaker
instruction-follower drops.

**The position source (#4) is layered, so it keeps working when Schwab can't
refresh:**

1. **Schwab API (live)** — the primary source. Each successful fetch is snapshotted
   to `~/watchy_config/positions_cache.json`.
2. **Cached snapshot** — when a live fetch fails (a token needing 7-day re-auth, an
   API error, a network outage), it falls back to the last good snapshot and labels
   the data's age in the push (e.g. `Schwab cache, ... (3d 4h old)`), never passing
   stale data off as live.
3. **Manual file** — the final fallback: `~/watchy_config/positions.yaml` (schema in
   `positions.example.yaml`). For bootstrapping before Schwab's first auth, or when
   no other data is available. Manual holdings are enriched with live yfinance prices
   for market value and unrealized P&L, **also age-labelled** — preferring the file's
   optional `as_of:` field (the date you state your holdings are current as of),
   otherwise the file's mtime.

> The Schwab live layer uses **`schwabdev`** (read-only: positions + balances). The
> first run needs a one-time browser OAuth on the host machine (schwabdev prints an
> authorization URL; paste the callback URL back into the terminal); tokens are stored
> at `tokens_path` (a schwabdev 3.x SQLite db, default `~/watchy_config/schwab_tokens.db`). The refresh token
> lasts 7 days; on expiry, re-auth — any live-fetch failure falls back to the cache
> then the manual file, so the daemon never stops. See the `schwab:` section of
> `secrets.example.yaml`.
>
> **Positions are fetched once per Tier 2 batch and shared across all tickers** (one
> consistent holdings view + one API call, instead of one call per ticker). Tier 1
> fetches on a fired signal, before running the pipeline.
>
> **Token-expiry alerts (no more silent staleness):** each Tier 2 batch (and each
> Tier 1 fired-signal scan) inspects the position snapshot it just resolved and pushes
> a Telegram alert when the refresh token has **already lapsed** (re-auth needed — the
> scan is on cached/manual data) or is **expiring soon** — three escalating stages at
> **≤3, ≤2, and ≤1 days left** (the ≤3-day stage buys enough lead time to survive a
> multi-day weekend gap when the VPS can't be touched). It also sends a **Friday
> reminder** to re-auth proactively, so the 7-day clock re-anchors before the weekend
> and never lapses mid-gap. No extra API call — it reads the fetch the scan already
> did. Alerts are deduped to at most one re-auth nag per day, one expiry warning per
> stage per auth cycle, and one Friday reminder per Friday. The 7-day clock is stamped
> by `scripts/schwab_oauth.py` on a successful auth, so re-auth via that script keeps
> the warnings accurate.

## Quick Start

```bash
# 1. Clone
cd ~
git clone https://github.com/quentincong/watchy.git

# 2. Install (editable, so git pull takes effect without reinstalling)
~/.pyenv/versions/3.11.9/envs/trading/bin/pip install -e ~/watchy

# 3. Create config files
mkdir -p ~/watchy_config
cp ~/watchy/config.yaml ~/watchy_config/config.yaml
cp ~/watchy/secrets.example.yaml ~/watchy_config/secrets.yaml

# 4. Fill in secrets (API keys, Telegram token)
nano ~/watchy_config/secrets.yaml

# 5. Edit the watchlist (can be done remotely on GitHub, synced via git pull)
nano ~/watchy_config/config.yaml

# 6. Run (for testing)
WATCHY_CONFIG=~/watchy_config/config.yaml python -m watchy.daemon
```

### Production (systemd)

```bash
sudo cp ~/watchy/watchy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watchy
journalctl -u watchy -f  # follow logs
```

## Configuration

Config is split across two files:

- **`config.yaml`** (safe to commit) — watchlist, thresholds, cooldowns
- **`secrets.yaml`** (git-ignored) — LLM API keys, Telegram token, Schwab credentials

See the full inline comments in `config.yaml` and `secrets.example.yaml`. Key settings:

| Setting | Purpose |
|---------|---------|
| `watchlist` | Tickers to monitor. Per-ticker overrides: Tier 1 interval, Tier 2 UTC time, `tier2_days` (tiered cadence, see below), optional `target_price`, and a per-ticker `min_price_proximity_pct` override (Tier 2 proximity gate, #15, defaults to the top-level global value; falls back to the #16 auto-derived target, never gated on the weekly full-risk day or when held). Tier 1 is never proximity-gated — it always scans during market hours. |
| `min_price_proximity_pct` | **Global default** percent for the Tier 2 proximity gate (#15), applied to every watch-only (non-held) ticker; on ordinary trading days skip the daily LLM when price is farther than this from the entry target. Held tickers and the weekly full-risk run (first trading day of the week) always run; Tier 1 is unaffected. Override per-ticker with the same key. Remove to disable globally. |
| `tier2_days` | **Tiered Tier 2 cadence.** Weekday abbreviations (`["mon","wed","fri"]`) a ticker runs its daily pipeline on; global default applies to any ticker without its own. Omit entirely for **every trading day** (the historical behaviour). One daily 4-analyst run costs roughly the same per ticker whatever the position is worth, so small positions can ride a lighter rotation and the batch still finishes before the 13:30 UTC open. **Never** skips the weekly full-risk day or a position already in the take-profit zone. |
| `max_tier1_pipelines_per_day` | **Tier 1 intraday rescan cap** (#23), global or per-ticker. Every Tier 1 signal trip launches a paid `[market+social]` pipeline + advisor, guarded only by per-signal cooldown, so a busy ticker tripping several distinct signals stacks several paid rescans in a day (observed: KLAC ×4, LRCX ×3). This caps Tier 1 LLM pipelines per ticker per UTC day; further trips are still logged and notified (`Signal Fired (rescan capped)`) but skip the pipeline. Override per-ticker with the same key; remove the line to disable the cap. Tier 2 scheduled runs are never affected. **Ships as `1`** — a rescan costs ~79% of a full Tier 2 run, not the half it did when the cap landed, because the `pro` Research Manager + Portfolio Manager pair runs on every pipeline and does not shrink when analysts are dropped. |
| `atr_proximity_mult` | Optional ATR-adaptive band (#15 follow-up), global or per-ticker. When set (and ATR data is available), the gate band is `mult × ATR%` (`ATR% = avg_atr_20d / price × 100`) instead of the fixed percent — wider for volatile names, narrower for calm ones. Clamped to `[proximity_pct_floor, proximity_pct_ceiling]` (default 4–20%); falls back to `min_price_proximity_pct` without ATR data. Calibrate with `scripts/calibrate_atr_proximity.py`. |
| `take_profit` | Take-profit / anti-round-trip (#28), **enabled** (`enabled: true` by default; set `false` to turn off). Keys: `floor_gain_pct` (unrealized-gain % that arms the zone, default 10; per-ticker override via `take_profit_floor_gain_pct`), `limit_atr_mult`/`stretch_atr_mult` (size the suggested sell-limit as `price + mult×ATR`, default 1.5/3.0), `runway_near_atr`/`runway_far_atr` (ATR-runway band edges, default 1.0/2.5), `cooldown_h` (intraday zone-entry trigger cooldown, default 24). A held winner past the floor gets a sell-limit directive on the daily Tier 2 advice and a same-day Tier 1 intraday trigger. Advisory-only, whole shares. |
| `signal_thresholds` | Detection thresholds for RSI, volume, ATR, etc. |
| `cooldown` | Per-signal cooldown window to suppress repeat pushes |
| `tier2_throttle_s` | Seconds to sleep between tickers in a Tier 2 daily scan (default 2.0), to smooth yfinance requests and avoid rate limits |
| `llm` | Advisor LLM config — supports Gemini, DeepSeek, OpenAI, Anthropic |
| `telegram` | Telegram bot token and chat ID |
| `schwab` | Schwab brokerage credentials (primary position source; auto-falls back to cache/manual file when unconfigured) |
| `positions.yaml` | Manual positions file (final fallback, in `~/watchy_config/`, not committed); schema in `positions.example.yaml`. **Set `total_account_value:`** (the full account figure from your broker — equities + cash + equivalents — used directly as the concentration denominator; or use `cash:` to have Watchy add the buffer to live stock value) so the advisor judges concentration against **Total Account Value**, not the stock-only total, avoiding false "over-concentration" TRIM advice |

> **Data fetching & caching:** market data is fetched via `yfinance` with a
> `yfinance-cache` disk layer on top (smart caching — only the missing/stale bars
> are pulled), cutting redundant Yahoo requests. The cache layer is an optional
> dependency — it falls back to plain `yfinance` when absent, and degrades
> gracefully on non-rate-limit cache errors without disrupting the scan.

## Signals Detected

| Signal | Logic | Default cooldown |
|--------|-------|------------------|
| Golden Cross | 50MA crosses above 200MA + full staircase (price > 50 > 150 > 200) + 200MA rising | 7 days |
| Death Cross | 50MA crosses below 200MA | 7 days |
| RSI Oversold | RSI drops below 30 | 12 hours |
| RSI Overbought | RSI rises above 70 | 12 hours |
| MACD Bullish Cross | MACD line crosses above the signal line | 24 hours |
| MACD Bearish Cross | MACD line crosses below the signal line | 24 hours |
| Bollinger Upper Breach | Price ≥ upper band (2σ) | 6 hours |
| Bollinger Lower Breach | Price ≤ lower band (2σ) | 6 hours |
| Volume Anomaly (≥2x) | Volume ≥ 2× the 20-day average | 4 hours |
| Moderate Volume (≥1.5x) | Volume ≥ 1.5× the 20-day average (notify only, no analysis) | 4 hours |
| ATR Spike | ATR ≥ 1.5× the 20-day average ATR | 6 hours |

> **Trigger semantics:** both crossover signals (golden/death, MACD, RSI) and
> level signals (Bollinger, volume, ATR) **fire on entry** — once, at the moment
> the condition goes from unmet to met. They stay silent while the condition
> persists, and only re-arm once it clears and crosses again. The cooldown is an
> additional dedup window layered on top of the trigger.

## Graduated Analyst Response

Not every signal warrants a full four-analyst debate. Watchy scales the call to
the signal's significance:

| Trigger | Analysts | Debate | Risk |
|---------|----------|--------|------|
| Tier 2 ordinary trading day | Market + Sentiment + News + Fundamentals | Bull/Bear | Simplified |
| Tier 2 first trading day of week | Market + Sentiment + News + Fundamentals | Bull/Bear | Full 3-way |
| Tier 2 weekend / NYSE holiday | — (skipped, market closed & redundant) | — | — |
| Golden / Death Cross | Market + Sentiment + News | Bull/Bear | Full 3-way |
| RSI, MACD, Bollinger, strong volume, ATR | Market + Sentiment | Bull/Bear | Simplified |
| Moderate volume (≥1.5x) | Market only | None | None |

## Telegram Message Examples

**On a signal firing:**
```
Signal Fired — $NVDA
Signal: Golden Cross (50MA ↑ 200MA)
Price: $142.37  RSI: 58.3  SEPA Stage: Advancing
Analysts launching: market, sentiment, news
```

**On analysis complete:**
```
Analysis Complete — $NVDA
Trigger: Golden Cross (50MA ↑ 200MA)
Verdict: 🟢 BUY (4 analysts)
```

> The analysis-complete message is a one-glance headline — just the verdict
> (BUY/SELL/HOLD + how many analysts ran). The Trader Plan, the Portfolio
> Manager's Risk / Final Call, and the raw per-analyst reports are **not** inlined;
> they all live in the complete `.md` report sent as an attachment. (Sparse
> pipelines with no verdict fall back to a short summary line.) The position +
> advisor advice ride in a **separate** message, kept in full:

```
Your Position:
Current position in NVDA:
  Shares: 50  Average cost: $98.40
  Market value: $7,118.50  Unrealized P&L: $2,198.50

Position Advice: 🟢 ADD (low urgency)
You hold 50 shares with 44% gain. The golden cross confirms the
uptrend is intact. Analysts are bullish with targets 15% above current.
Suggested size: 10-15 shares (~2% of portfolio)
Key risk: If price breaks below the 50MA, the signal is invalidated.
```

## File Structure

```
watchy/
├── config.yaml              # non-sensitive config (safe to commit, edit via GitHub)
├── secrets.example.yaml     # sensitive-config template (copy locally, fill in keys)
├── requirements.txt         # Python dependencies
├── watchy.service           # systemd unit file
├── project_doc.md           # full technical documentation
└── watchy/                  # package
    ├── __init__.py           # package marker
    ├── config.py             # YAML config → typed dataclasses
    ├── state.py              # SQLite state store (crossover memory, cooldown, history)
    ├── indicators.py         # technical-indicator computation (yfinance + pandas, no LLM)
    ├── proximity.py          # shared price-proximity gate (Tier 1 & Tier 2)
    ├── take_profit.py        # take-profit gain-gate + ATR-runway logic (#28, no LLM)
    ├── digest_store.py       # persist latest analysis digest per ticker (#28 reuse)
    ├── orchestrator.py       # graduated pipeline selection per signal type
    ├── advisor.py            # LLM synthesis: analysis digest + position → advice (GEMINICOST log)
    ├── positions.py          # layered position source: Schwab → cached snapshot → manual file
    ├── schwab.py             # Schwab brokerage API client (live layer, schwabdev)
    ├── notify.py             # Telegram bot notifications
    ├── tier1.py              # hourly signal scan
    ├── tier2.py              # daily full pipeline
    └── daemon.py             # APScheduler entry point
```

## Wiring TradingAgents

The `pipeline_runner` argument in `orchestrator.py` is the integration point. Pass
a callable `(ticker, PipelineSpec) -> dict` that invokes the appropriate
TradingAgents analyst subset. A stub is provided by default (logs only, no real
call); `watchy/pipeline_runner.py` is the real bridge.

## Documentation

See [`project_doc.md`](project_doc.md) for full technical documentation — module
internals, data flow, deployment, testing strategy, and a config reference.

## License

MIT
