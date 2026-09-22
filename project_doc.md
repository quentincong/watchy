# Watchy — Technical Documentation

## Overview

Watchy is a long-running Python daemon that monitors a configurable list of stock tickers using a two-tier scheduling architecture. It sits on top of the [TradingAgents](https://github.com/TauricResearch/TradingAgents) multi-agent LLM trading framework, acting as a cost-efficient pre-filter: Tier 1 runs cheap technical indicator scans hourly and only invokes LLM-based analysis when a signal breaches; Tier 2 runs a full-depth analysis once daily regardless of signals.

**Tech stack:** Python 3.11+, APScheduler, yfinance, pandas, SQLite, Telegram Bot API.

> **Watchy 2.0 (`v2.0.0-rc.1`).** With the default `tier2_schedule: weekly`, Tier 2 runs only on the
> first trading session of each week (Weekly Full) and produces a validated, persisted weekly plan;
> Tier 1 monitors the plan and routes technical triggers through a pure router, with paid Fast
> Recheck / Triggered Risk disabled by default (shadow mode). This document describes the shared
> machinery and the 1.x daily design that `tier2_schedule: daily` restores. For 2.0 see
> [`docs/WATCHY_2_IMPLEMENTATION_PLAN.md`](docs/WATCHY_2_IMPLEMENTATION_PLAN.md) (spec, §22 status),
> [`docs/WATCHY_2_OPERATIONS.md`](docs/WATCHY_2_OPERATIONS.md) and the README.

---

## File Structure

```
~/watchy/
├── config.yaml                  # user-editable configuration
├── requirements.txt             # Python dependencies
├── watchy.service               # systemd unit file
├── project_doc.md               # this document
└── watchy/                      # package (deployed inside ~/TradingAgents)
    ├── __init__.py              # package marker, version
    ├── config.py                # YAML config loader → typed dataclasses
    ├── state.py                 # SQLite state store (crossover memory, cooldown, history)
    ├── indicators.py            # technical indicator calculations (no LLM, no side effects)
    ├── orchestrator.py          # graduated analyst pipeline selection per signal type
    ├── notify.py                # Telegram Bot notifications (natural language summaries)
    ├── tier1.py                 # hourly signal scanner (Tier 1)
    ├── tier2.py                 # daily full pipeline (Tier 2)
    └── daemon.py                # main entry point, APScheduler setup, signal handlers
```

---

## Module Reference

### 1. `watchy/__init__.py`

Package marker. Exposes `__version__` (`"2.0.0rc1"`, the PEP 440 form of the `v2.0.0-rc.1` tag; kept equal to `pyproject.toml`).

---

### 2. `watchy/config.py` — Configuration Loader

**Responsibility:** Load and validate `config.yaml` into strongly-typed dataclasses. Provides a single entry point `load_config()` that respects the `WATCHY_CONFIG` environment variable.

**Dataclass hierarchy:**

| Class | Fields | Purpose |
|-------|--------|---------|
| `TickerConfig` | `ticker`, `tier1_interval_h`, `tier2_time_utc` | Per-ticker watchlist entry |
| `SignalThresholds` | `rsi_oversold` (30), `rsi_overbought` (70), `volume_ratio_strong` (2.0), `atr_ratio` (1.5) | Thresholds for signal detection |
| `CooldownConfig` | `rsi_extreme_h` (12), `macd_cross_h` (24), `bollinger_breach_h` (6), `volume_anomaly_h` (4), `atr_spike_h` (6), `golden_cross_d` (7) | Per-signal cooldown windows |
| `LLMConfig` | `provider`, `model`, `api_key`, `api_base` | LLM backend (Anthropic or OpenAI) |
| `TelegramConfig` | `bot_token`, `chat_id` | Telegram Bot credentials |
| `SchwabConfig` | `api_key`, `api_secret`, `account_id`, `enabled` | Schwab API stub (future scope) |
| `WatchyConfig` | All of the above + `log_level`, `log_file` | Root config object |

**Core implementation logic — `WatchyConfig.from_yaml()`:**

1. Resolve `~` in the config path via `os.path.expanduser()`.
2. Parse YAML with `yaml.safe_load()`.
3. Walk each section: watchlist entries are normalized (bare string → `TickerConfig` with defaults; dict → `TickerConfig(**dict)`). Nested sections are unpacked via `**kwargs` into their respective dataclasses.
4. If a section is missing from YAML, the dataclass default constructor kicks in — all fields have sensible defaults.

**`load_config()` entry point:**
1. Check `WATCHY_CONFIG` env var; if unset, default to `~/watchy/config.yaml`.
2. Delegate to `WatchyConfig.from_yaml()`.

---

### 3. `watchy/state.py` — SQLite State Store

**Responsibility:** Persist per-ticker indicator history (for crossover detection), signal cooldown state, and run history. All tickers are stored uppercase for case-insensitive lookup.

**Database path:** `~/watchy/state.db` (configurable via constructor).

**Schema — three tables:**

| Table | Key columns | Purpose |
|-------|-------------|---------|
| `ticker_state` | `ticker` (PK), `prev_sma_50_above_200`, `prev_macd_above_signal`, `prev_rsi`, `prev_atr`, `avg_volume_20d`, `avg_atr_20d`, `last_full_analysis_ts`, `updated_ts` | Previous indicator values for crossover/threshold-crossing detection |
| `signal_log` | `id` (PK), `ticker`, `signal_type`, `fired_ts`, `details` (JSON), `notified` | Immutable log of every signal that fired, used for cooldown enforcement |
| `run_history` | `id` (PK), `ticker`, `tier`, `trigger_type`, `started_ts`, `completed_ts`, `success`, `summary` | Audit trail of every pipeline execution |

**Indexes:** `(ticker, signal_type)` on `signal_log` for fast cooldown queries; `(fired_ts)` for time-range queries; `(ticker, started_ts)` on `run_history` for per-ticker history lookups.

**Core implementation logic:**

**`get_ticker_state(ticker)`:** Returns a dict of all columns for the ticker, or empty dict if the ticker has never been scanned. Uses `SELECT * LIMIT 0` to introspect column names dynamically rather than hardcoding them.

**`save_ticker_state(ticker, **kwargs)`:** Uses SQLite `INSERT ... ON CONFLICT DO UPDATE` (upsert). Automatically stamps `updated_ts`. The caller passes only the fields that changed — the method constructs the SQL dynamically from `kwargs` keys.

**`is_in_cooldown(ticker, signal_type, cooldown_hours)`:** Queries `signal_log` for any row matching `(ticker, signal_type)` where `fired_ts > (now - cooldown_hours)`. Returns `True` if any row exists. This is the gate that prevents re-firing the same signal within its cooldown window.

**`log_signal(ticker, signal_type, details)`:** Inserts a row into `signal_log` with the current UTC timestamp and optional JSON details (indicator values at time of firing).

**`start_run(ticker, tier, trigger_type)` / `complete_run(run_id, success, summary)`:** Bookend calls for pipeline execution tracking. `start_run` returns the run ID; `complete_run` stamps the completion time and result.

**Journal mode:** WAL (Write-Ahead Logging) is enabled at connect time for concurrent read/write performance.

---

### 4. `watchy/indicators.py` — Technical Indicator Calculations

**Responsibility:** Fetch OHLCV data from Yahoo Finance and compute all technical indicators. This module is a pure function of its inputs — no LLM calls, no database access, no side effects. Independently testable.

**Key data structure — `IndicatorBundle`:**

A dataclass holding all computed values for one ticker at one point in time: `current_price`, `sma_50`, `sma_150`, `sma_200`, `sma_200_1m_ago`, `rsi`, `macd`/`macd_signal`/`macd_histogram`, `bb_upper`/`bb_middle`/`bb_lower`, `atr`, `avg_atr_20d`, `volume`, `avg_volume_20d`, `sepa_stage` (1-4).

**Core implementation logic:**

**`compute_indicators(ticker, history=None)`:**

1. **Data acquisition:** If `history` DataFrame is provided (for testing), use it directly. Otherwise call `_fetch_history(ticker)` which uses `yfinance.Ticker.history(period="1y", interval="1d")` with a fallback to `yf.download()` if the single-ticker API returns empty.
2. **Guard checks:** Return `None` if the DataFrame is empty, missing `Close` column, or has fewer than 200 rows (insufficient for 200MA).
3. **Moving averages:** Simple rolling means at 50, 150, and 200 periods. `sma_200_1m_ago` is the 200MA value from 21 trading days prior (≈1 calendar month), used to determine whether the 200MA is sloping upward.
4. **RSI (Relative Strength Index, 14-period):** Uses Wilder's smoothing (EMA with `alpha = 1/14`). Computed as `100 - 100/(1 + RS)` where `RS = avg_gain / avg_loss`. Handles division by zero by replacing zero loss with NaN.
5. **MACD:** 12-period EMA minus 26-period EMA for the MACD line; 9-period EMA of the MACD line for the signal line. Histogram = MACD − signal.
6. **Bollinger Bands (20-period, 2σ):** Middle band = 20-period SMA; upper/lower = middle ± 2 × 20-period standard deviation.
7. **ATR (Average True Range, 14-period):** True Range = `max(high − low, |high − prev_close|, |low − prev_close|)`. ATR = 14-period rolling mean of TR. `avg_atr_20d` = mean of the last 20 daily ATR values (computed by recalculating ATR at 20 offsets from the end of the series).
8. **Volume:** Latest volume and 20-day rolling average from the `Volume` column (if present).
9. **SEPA stage classification:** Delegates to `_classify_sepa_stage()`.

**`detect_signals(bundle, prev_state)`:**

Compares current indicator values against previous state to detect state transitions (not just threshold states). Returns a list of signal type strings.

Signal detection rules:

| Signal | Detection Logic |
|--------|-----------------|
| `golden_cross` | `prev_sma_50_above_200 == False` AND `sma_50 > sma_200` now AND full staircase (`price > 50MA > 150MA > 200MA`) AND 200MA trending up |
| `death_cross` | `prev_sma_50_above_200 == True` AND `sma_50 < sma_200` now |
| `rsi_oversold` | RSI crossed below 30 (was ≥30 or unknown, now <30) |
| `rsi_overbought` | RSI crossed above 70 (was ≤70 or unknown, now >70) |
| `macd_bullish_cross` | `prev_macd_above_signal == False` AND `macd > signal` now |
| `macd_bearish_cross` | `prev_macd_above_signal == True` AND `macd < signal` now |
| `bollinger_upper_breach` | `price >= bb_upper` |
| `bollinger_lower_breach` | `price <= bb_lower` |
| `volume_anomaly_strong` | `volume / avg_volume_20d >= 2.0` |
| `atr_spike` | `atr >= 1.5 × avg_atr_20d` |

Key design detail: golden cross requires the **full MA staircase** (price above 50MA above 150MA above 200MA) **and** the 200MA trending up for at least one month. This filters out false crosses where MAs touch briefly in a choppy market without a genuine trend change. Death cross only requires the simple 50/200 cross — the staircase condition isn't needed because downside moves tend to be faster and more decisive.

**`_classify_sepa_stage(bundle)`:**

Classifies the stock into one of four stages based on MA positions:

| Stage | Name | Criteria |
|-------|------|----------|
| 1 | Basing | Default — price consolidating around MAs, no clear alignment |
| 2 | Advancing | `price > 50MA > 150MA > 200MA` and 200MA trending up — **the only stage suitable for buying** |
| 3 | Topping | `price > 200MA` and `50MA > 200MA` but 200MA NOT trending up — distribution phase |
| 4 | Declining | `price < 200MA` and `50MA < 200MA` and 200MA NOT trending up — bearish alignment |

**`_fetch_history(ticker)`:**

Lazy-imports yfinance. Tries `yf.Ticker(ticker).history()` first (single-ticker API), falls back to `yf.download()` if empty. Re-raises exceptions for the caller to handle.

**`_compute_rsi(close, period=14)`:**

Wilder's smoothed RSI using EMA rather than simple moving average. `alpha = 1/period` gives the standard Wilder smoothing. Returns `None` if the result is NaN (insufficient data).

**`_compute_atr(df, period=14, offset=0)`:**

Computes ATR with an optional `offset` parameter — when offset > 0, the last `offset` rows are excluded from the rolling window. This enables computing historical ATR values for `avg_atr_20d` without recalculating the entire series from scratch each time.

---

### 5. `watchy/orchestrator.py` — Graduated Pipeline Selection

**Responsibility:** Map each signal type to the appropriate depth of TradingAgents analysis. Different signals warrant different levels of LLM-based analysis depending on their rarity, significance, and cost profile.

**Enums:**

| Enum | Values | Meaning |
|------|--------|---------|
| `AnalystSet` | `NONE`, `MARKET_ONLY`, `MARKET_SENTIMENT`, `MARKET_SENTIMENT_NEWS`, `FULL` | Which analysts to invoke |
| `DebateMode` | `NONE`, `BULL_BEAR` | Whether to run opposing-view debate |
| `RiskMode` | `NONE`, `SIMPLIFIED`, `FULL` | Risk management depth |

**`PipelineSpec` dataclass:** Bundles `(analysts, debate, risk)` into a single spec object.

**Signal → Pipeline mapping (`SIGNAL_PIPELINE` dict):**

| Trigger Type | Analysts | Debate | Risk | Rationale |
|-------------|----------|--------|------|-----------|
| `scheduled_daily` (Tier 2) | FULL (4 analysts) | Bull/Bear | Full 3-way | Baseline deep analysis |
| `golden_cross` | Market + Sentiment + News | Bull/Bear | Full 3-way | Rare structural event (1-2×/ticker/year), macro context matters |
| `death_cross` | Market + Sentiment + News | Bull/Bear | Full 3-way | Same rarity as golden cross |
| `rsi_oversold` / `rsi_overbought` | Market + Sentiment | Bull/Bear | Simplified | Momentum signals, sentiment provides second opinion |
| `macd_bullish_cross` / `macd_bearish_cross` | Market + Sentiment | Bull/Bear | Simplified | Momentum shift confirmation |
| `bollinger_upper_breach` / `bollinger_lower_breach` | Market + Sentiment | Bull/Bear | Simplified | Volatility breakout context |
| `volume_anomaly_strong` (≥2x) | Market + Sentiment | Bull/Bear | Simplified | Unusual activity, needs context |
| `atr_spike` | Market + Sentiment | Bull/Bear | Simplified | Volatility regime change |

**Risk mode detail:**
- **Full:** 3-way aggressive/conservative/neutral portfolio manager debate
- **Simplified:** Portfolio manager directly evaluates the trader proposal without the 3-way debate (saves tokens when analysis is already narrow)
- **None:** Analyst output is final, no risk assessment (for info-only signals)

**`get_pipeline(signal_type)`:** Dictionary lookup with a fallback to `MARKET_SENTIMENT + simplified risk` for unknown signal types (logged as a warning).

**`get_cooldown_hours(signal_type, cooldown_config)`:** Maps each signal type to its configured cooldown in hours. Golden/death cross cooldown is stored in days in config (`golden_cross_d: 7`) and converted to hours here (×24).

**`run_pipeline(ticker, spec, *, runner=None)`:**

This is the TradingAgents integration point. It accepts an optional `runner` callable with signature `(ticker: str, spec: PipelineSpec) -> dict`. If no runner is provided, it returns a stub result indicating what *would* have run. On the VPS, the runner is wired to actual TradingAgents analyst invocations.

The stub result structure:
```python
{
    "ticker": str,
    "analysts_run": list[str],       # e.g. ["market", "sentiment"]
    "debate": str,                   # "bull_bear" or "none"
    "risk_mode": str,                # "full", "simplified", or "none"
    "recommendations": list[str],    # empty in stub
    "risk_assessment": None,         # str when real
    "summary": str,                  # stub message describing what would run
}
```

**`_analyst_names(analyst_set)`:** Converts enum to list of analyst name strings for display/logging.

---

### 6. `watchy/notify.py` — Telegram Notifications

**Responsibility:** Push natural-language summaries to a Telegram chat on three event types: signal fired, pipeline result, and errors. Uses only stdlib `urllib` (zero additional dependencies).

**`TelegramNotifier` class:**

- **Constructor:** Takes `bot_token` and `chat_id`. If either is empty, notifications are disabled and logged locally instead.
- **`send(message)`:** Posts a message with HTML parse mode. Returns `True` on success.
- **`signal_fired(ticker, signal_type, indicators, triggered_analysts)`:** Formats a "Signal Fired" alert with ticker, human-readable signal label, current price, RSI, SEPA stage, and the list of analysts being launched.
- **`pipeline_result(ticker, signal_type, result)`:** Formats an "Analysis Complete" message with the trigger, recommendation text, risk assessment, and summary.
- **`error(context, error)`:** Formats a critical error alert with context and exception details.

**`_post(method, payload)`:**

Raw Telegram Bot API call using `urllib.request`:
1. Construct URL: `https://api.telegram.org/bot{token}/{method}`
2. JSON-encode the payload, POST with `Content-Type: application/json`.
3. 10-second timeout.
4. Check `ok` field in response; log on failure.

**`_signal_label(signal_type)`:** Maps internal signal type strings to human-readable labels with emoji-free descriptions (e.g., `"golden_cross"` → `"Golden Cross (50MA ↑ 200MA)"`).

**`_stage_name(stage)`:** Maps SEPA stage integer to name (`1` → `"Basing"`, etc.).

---

### 7. `watchy/tier1.py` — Tier 1 Hourly Signal Scanner

**Responsibility:** Run the data-only pre-filter for a single ticker. This is the cost-saving layer — it fetches OHLCV and computes indicators without any LLM calls, checks for signal breaches, filters out signals still in cooldown, and only then launches the graduated analyst pipeline.

**`scan_ticker(ticker, config, store, notifier, *, pipeline_runner=None)`:**

Complete Tier 1 scan lifecycle for one ticker:

1. **Fetch indicators:** `compute_indicators(ticker)` — returns `None` if data unavailable (logged, ticker skipped).
2. **Load previous state:** `store.get_ticker_state(ticker)` — returns empty dict on first run.
3. **Detect signals:** `detect_signals(bundle, prev)` — returns list of signal strings that breached.
4. **Cooldown filter:** For each fired signal, call `get_cooldown_hours(sig, config.cooldown)` and `store.is_in_cooldown(...)`. Signals still in cooldown are dropped (logged).
5. **If no actionable signals:** Update state and return `[]` — no LLM calls made.
6. **For each actionable signal:**
   - Look up pipeline spec via `get_pipeline(sig)`
   - Delegate to `_handle_signal()`

Returns the list of signal types that were acted upon.

**`_handle_signal(ticker, sig, spec, bundle, ...)`:**

1. Create indicator summary dict from bundle.
2. Log signal to `signal_log` table (this enables future cooldown enforcement).
3. Send "Signal Fired" Telegram notification with indicator context and triggered analyst names.
4. Record run start in `run_history` (tier="tier1", trigger_type=sig).
5. Execute pipeline via `run_pipeline(ticker, spec, runner=pipeline_runner)`.
6. On success: mark run complete in history, send "Analysis Complete" notification.
7. On failure: log exception, mark run as failed, send error notification.

**`_update_state(store, bundle, ticker)`:**

Persists the current indicator snapshot to `ticker_state` for next scan's crossover detection. Stores `prev_sma_50_above_200` and `prev_macd_above_signal` as booleans (0/1), `prev_rsi` and `prev_atr` as floats, and the 20-day rolling averages.

---

### 8. `watchy/tier2.py` — Tier 2 Daily Full Pipeline

**Responsibility:** Run the full-depth TradingAgents analysis on every ticker in the watchlist. This is the scheduled baseline that catches gradual drift and fundamental shifts that technical triggers might miss.

**`FULL_PIPELINE` constant:** `PipelineSpec(analysts=FULL, debate=BULL_BEAR, risk=FULL)` — immutable spec for Tier 2 runs.

**`run_daily_scan(config, store, notifier, *, pipeline_runner=None)`:**

1. Extract ticker list from config watchlist.
2. Iterate all tickers sequentially (not parallel — respects API rate limits).
3. For each ticker, call `_run_ticker()`.
4. If a ticker fails, the error is caught, logged, notified, and recorded in the results dict — subsequent tickers continue unaffected.
5. Returns `dict[ticker → result]`.

**`_run_ticker(ticker, config, store, notifier, pipeline_runner)`:**

1. **Enrich with stage context:** Compute indicators for the ticker. If successful, extract `sepa_stage`, `current_price`, `sma_50`, `sma_200`, and `rsi` as context for the analyst prompts.
2. Record run start in `run_history` (tier="tier2", trigger_type="scheduled_daily").
3. Execute `run_pipeline(ticker, FULL_PIPELINE, runner=pipeline_runner)`.
4. Attach stage context to the result dict.
5. Mark run complete, update `last_full_analysis_ts` on ticker state.
6. Send pipeline result notification.
7. On failure: mark run failed and re-raise (caught by `run_daily_scan`).

---

### 9. `watchy/daemon.py` — Main Entry Point

**Responsibility:** Bootstrap the entire daemon: load config, set up logging, initialize state store and notifier, build the APScheduler job schedule, register OS signal handlers for graceful shutdown, and keep the process alive.

**`setup_logging(config)`:**

1. Resolve log file path, create parent directories.
2. Set root logger level from config.
3. Add a `RotatingFileHandler` (10MB max, 5 backups).
4. Add a `StreamHandler` to stdout.
5. Both handlers use the same format: `2026-06-01T12:00:00 [INFO] module: message`.

**`build_scheduler(config, store, notifier)`:**

Creates a `BackgroundScheduler` in UTC timezone and registers two job types:

- **Tier 1 jobs:** One `IntervalTrigger` job per ticker at the configured interval (default: 1 hour). Each job calls `_tier1_job(ticker, config, store, notifier)`.
- **Tier 2 jobs:** One `CronTrigger` job per unique `tier2_time_utc` value (deduplicated — if 3 tickers share "22:00", only one job fires and processes all 3). Each job calls `_tier2_job(config, store, notifier)`.

**`_tier1_job(ticker, ...)`:** Thin wrapper around `scan_ticker()` with exception handling. Catches all exceptions, logs them, and sends an error notification — the scheduler thread continues unaffected.

**`_tier2_job(config, ...)`:** Thin wrapper around `run_daily_scan()` with the same resilient error handling pattern.

**`main(config_path=None)`:**

1. Load config (from argument or `WATCHY_CONFIG` env var or default path).
2. Set up logging.
3. Instantiate `StateStore` and `TelegramNotifier`.
4. Build and start the scheduler.
5. Register `SIGINT` and `SIGTERM` handlers that call `scheduler.shutdown()`, `store.close()`, and `sys.exit(0)`.
6. Block the main thread with `signal.pause()` (Unix) or a `while True: time.sleep(60)` loop (Windows fallback).

---

## Data Flow

### Tier 1 — Signal-Triggered Flow

```
┌──────────┐    ┌─────────────┐    ┌──────────┐    ┌───────────────┐
│ daemon   │───→│ tier1.py    │───→│ indicators│───→│ yfinance API  │
│ scheduler│    │ scan_ticker │    │ .py       │    │ (OHLCV data)  │
└──────────┘    └─────────────┘    └──────────┘    └───────────────┘
                      │                  │
                      │ ← IndicatorBundle│
                      │                  │
                      ▼                  │
               ┌──────────┐              │
               │ state.py │ prev_state   │
               │ get_ticker_state        │
               └──────────┘              │
                      │                  │
                      ▼                  │
               ┌──────────────┐          │
               │ indicators.py│←─────────┘
               │ detect_signals
               └──────────────┘
                      │
                      ▼ (signal list)
               ┌──────────────┐
               │ orchestrator │ cooldown check → pipeline spec
               │ .py          │
               └──────────────┘
                      │
                      ▼
               ┌──────────┐    ┌───────────────┐
               │ notify.py│───→│ Telegram API  │
               │ (signal   │    └───────────────┘
               │  fired)   │
               └──────────┘
                      │
                      ▼
               ┌──────────────┐
               │ orchestrator │───→ TradingAgents (LLM)
               │ run_pipeline │
               └──────────────┘
                      │
                      ▼
               ┌──────────┐    ┌───────────────┐
               │ notify.py│───→│ Telegram API  │
               │ (result)  │    └───────────────┘
               └──────────┘
```

### Tier 2 — Daily Flow

```
┌──────────┐    ┌─────────────┐    ┌───────────────┐
│ daemon   │───→│ tier2.py    │───→│ TradingAgents │ (LLM — all 4 analysts
│ scheduler│    │ run_daily   │    │ FULL pipeline │  + debate + risk mgmt)
└──────────┘    │ _scan       │    └───────────────┘
                └─────────────┘           │
                      │                   │
                      │ ← result ─────────┘
                      │
                      ▼
               ┌──────────┐    ┌───────────────┐
               │ notify.py│───→│ Telegram API  │
               │ (result)  │    └───────────────┘
               └──────────┘
                      │
                      ▼
               ┌──────────┐
               │ state.py │ (update last_full_analysis_ts)
               └──────────┘
```

---

## Configuration Reference (`config.yaml`)

```yaml
watchlist:
  - ticker: "NVDA"               # ticker symbol (required)
    tier1_interval_h: 1          # hours between Tier 1 scans (default: 1)
    tier2_time_utc: "22:00"      # daily Tier 2 time in UTC (default: "22:00")

signal_thresholds:               # all optional, defaults shown
  rsi_oversold: 30
  rsi_overbought: 70
  volume_ratio_strong: 2.0       # fires the volume_anomaly_strong Tier 1 pipeline
  atr_ratio: 1.5

cooldown:                        # prevents re-firing same signal within window
  rsi_extreme_h: 12
  macd_cross_h: 24
  bollinger_breach_h: 6
  volume_anomaly_h: 4
  atr_spike_h: 6
  golden_cross_d: 7              # golden/death cross cooldown in DAYS

llm:
  provider: "anthropic"          # "anthropic" or "openai"
  model: "claude-sonnet-4-6"
  api_key: ""                    # set via env var or inline
  # api_base: ""                 # optional custom endpoint

telegram:
  bot_token: ""                  # from @BotFather
  chat_id: ""                    # your Telegram chat ID

schwab:                          # future scope — leave disabled
  enabled: false
  api_key: ""
  api_secret: ""
  account_id: ""

log_level: "INFO"                # DEBUG, INFO, WARNING, ERROR
log_file: "~/watchy/watchy.log"  # rotated at 10MB, 5 backups
```

---

## Deployment

### Prerequisites

- Ubuntu 24.04 VPS
- Python 3.11.9 (pyenv + venv)
- TradingAgents installed via `pip install -e .` at `~/TradingAgents`
- Dependencies from `requirements.txt`: `yfinance`, `pandas`, `numpy`, `pyyaml`, `apscheduler`

### Install

```bash
# 1. Place watchy package inside TradingAgents
cp -r watchy/ ~/TradingAgents/watchy/

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Create config directory and edit
mkdir -p ~/watchy
cp config.yaml ~/watchy/config.yaml
nano ~/watchy/config.yaml          # fill in API keys, Telegram creds, watchlist

# 4. Install systemd service
sudo cp watchy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watchy

# 5. Check status
sudo systemctl status watchy
journalctl -u watchy -f            # follow logs
```

### Wiring TradingAgents

The `pipeline_runner` callable in `orchestrator.py` is the integration point. Replace the stub with a function like:

```python
def tradingagents_runner(ticker: str, spec: PipelineSpec) -> dict:
    """Invoke TradingAgents with the appropriate analyst subset."""
    # spec.analysts  → which analysts to run
    # spec.debate    → Bull/Bear debate or none
    # spec.risk      → Full, Simplified, or None risk management
    ...
    return {
        "ticker": ticker,
        "analysts_run": [...],
        "recommendations": [...],
        "risk_assessment": "...",
        "summary": "...",
    }
```

Then pass it when building the scheduler or calling scan functions directly.

---

## Testing Strategy

Each module is independently testable:

| Module | What to test | Dependencies to mock |
|--------|-------------|---------------------|
| `config.py` | YAML parsing, defaults, env var resolution | `builtins.open` (or tempfile) |
| `state.py` | SQLite CRUD, cooldown logic | `sqlite3` in `:memory:` mode |
| `indicators.py` | Indicator math correctness | `_fetch_history` (pass `history` DataFrame directly) |
| `orchestrator.py` | Signal→pipeline mapping, cooldown lookup | None (pure lookup) |
| `notify.py` | Message formatting, disabled mode | `urllib.request.urlopen` |
| `tier1.py` | Scan lifecycle, signal→pipeline dispatch | `compute_indicators`, `store`, `notifier`, `run_pipeline` |
| `tier2.py` | Daily scan iteration, error isolation | `compute_indicators`, `store`, `notifier`, `run_pipeline` |
| `daemon.py` | Scheduler job registration, signal handling | All sub-modules |
