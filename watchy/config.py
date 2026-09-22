"""YAML config loader for Watchy."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class TickerConfig:
    ticker: str
    tier1_interval_h: float = 0.5
    # 10:02 UTC = 06:02 ET, i.e. Beijing 18:02. DeepSeek's peak/off-peak billing
    # goes live 2026-08-16 16:00 UTC: peak = 01:00-04:00 and 06:00-10:00 UTC
    # (Beijing 09:00-12:00 and 14:00-18:00) at 2x the off-peak rate. The two
    # minutes are deliberate margin — DeepSeek does not document whether 10:00 is
    # the last peak minute or the first off-peak one, and being wrong doubles the
    # whole batch. Starting earlier is NOT free: 08:00 would sit inside the window
    # outright. The batch should also finish before the 13:30 UTC market open; see
    # tier2_days for the cadence that keeps it short.
    tier2_time_utc: str = "10:02"
    # Optional tiered cadence: weekday abbreviations this ticker runs Tier 2 on
    # ("mon".."fri", case-insensitive). None inherits WatchyConfig.tier2_days, and
    # a global None means "every trading day" (the historical behaviour). Lets the
    # daily spend go where it is worth it — a $63 position costs the same ~$9.6/yr
    # to analyse daily as an $850 one. NEVER skips the weekly full-risk day or a
    # ticker already in the take-profit zone (see tier2._should_skip_cadence).
    tier2_days: list[str] | None = None
    # Optional manual entry/accumulation target. Used by the Tier 2 proximity
    # gate (#15) as the effective target when set (else the #16 auto-derived one).
    # Tier 1 is never proximity-gated.
    target_price: float | None = None
    # Optional per-ticker override of the Tier 2 price-proximity gate (#15). When
    # set (or inherited from the global WatchyConfig.min_price_proximity_pct), the
    # daily LLM pipeline is skipped if the current price is farther than this
    # percent from the effective target (manual target_price, else the #16
    # auto-derived one). The weekly full-risk run (first trading day of the week)
    # and held tickers always run. A value here overrides the global default.
    min_price_proximity_pct: float | None = None
    # Optional per-ticker ATR-adaptive proximity band (#15 follow-up). When set
    # (or inherited from WatchyConfig.atr_proximity_mult), the gate band becomes
    # mult x ATR% (ATR% = avg_atr_20d / price x 100) instead of the fixed
    # min_price_proximity_pct — i.e. "skip when price is more than `mult` typical
    # trading days of movement from target". Clamped to the global floor/ceiling.
    # Falls back to the fixed pct when ATR data is unavailable. Overrides the
    # global mult for this ticker.
    atr_proximity_mult: float | None = None
    # Optional per-ticker override of the Tier 1 intraday rescan cap. When set
    # (or inherited from WatchyConfig.max_tier1_pipelines_per_day), at most this
    # many Tier 1 LLM pipelines run for this ticker per UTC day; further signal
    # trips are logged + notified but skip the paid pipeline/advisor. None = no
    # cap. Overrides the global default for this ticker.
    max_tier1_pipelines_per_day: int | None = None
    # Optional per-ticker override of the take-profit floor (#28). When set (or
    # inherited from WatchyConfig.take_profit.floor_gain_pct), a held position in
    # this name enters the take-profit zone once its unrealized gain crosses this
    # percent. Overrides the global floor for this ticker.
    take_profit_floor_gain_pct: float | None = None


@dataclass
class SignalThresholds:
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    volume_ratio_strong: float = 2.0
    atr_ratio: float = 1.5


@dataclass
class CooldownConfig:
    rsi_extreme_h: int = 12
    macd_cross_h: int = 24
    bollinger_breach_h: int = 6
    volume_anomaly_h: int = 4
    atr_spike_h: int = 6
    golden_cross_d: int = 7


@dataclass
class LLMConfig:
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    api_base: str | None = None
    deepseek_api_key: str = ""
    # Gemini advisor thinking level per tier (only used when provider == "gemini").
    # gemini-3.5/3.6-flash control thinking with thinkingConfig.thinkingLevel and
    # rejects the legacy thinkingBudget (HTTP 400), so "off" maps to minimal (the
    # cheapest tier; observed ~0 thinking tokens). Valid: off / minimal / low /
    # medium / high. BOTH tiers run "low". Tier 1 was "off" until 2026-08-13, but
    # it carries the intraday take-profit zone-entry advice whose `Take-Profit:`
    # line is regex-extracted, and minimal is measurably the worst tier for that:
    # AA scores gemini-3.5-flash (minimal) at 47.3% IFBench / 58.3% AA-LCR / 74%
    # hallucination, vs 74.6% / 79.7% / 62% one tier up. ~$8/yr for all of Tier 1.
    # Tier 2 stays low — medium was measured as no better for the decision.
    gemini_thinking_tier1: str = "low"
    gemini_thinking_tier2: str = "low"


@dataclass
class TakeProfitConfig:
    """Take-profit / anti-round-trip settings (#28).

    A held position whose unrealized gain crosses ``floor_gain_pct`` enters the
    "take-profit zone": the advisor prompt is augmented with an explicit,
    fact-filled directive (unrealized gain, ATR runway, a reachable sell-limit)
    so Gemini actively proposes banking part of the gain via a whole-share
    sell-limit order, instead of staying silent while a winner round-trips. The
    mechanical gain-gate is ground truth (it doesn't wait for the analysis to
    flag a top, which it does inconsistently); the LLM only sizes the trim and
    sets the limit. Advisory-only — the user places the limit order.

    ``limit_atr_mult`` / ``stretch_atr_mult`` size the suggested sell-limit as
    ``price + mult x ATR`` — a "good-day-reachable" level so a limit order fills
    into a spike rather than sitting too high. ``runway_near_atr`` /
    ``runway_far_atr`` are the ATR-runway band edges: below near = at the ceiling
    (bank most/all), above far = room to run (hold / only a stretch share).
    """
    enabled: bool = False
    floor_gain_pct: float = 10.0
    limit_atr_mult: float = 1.5
    stretch_atr_mult: float = 3.0
    runway_near_atr: float = 1.0
    runway_far_atr: float = 2.5
    cooldown_h: int = 24


@dataclass
class WeeklyPlanConfig:
    """Watchy 2.0 plan monitoring (Tier 1, no LLM).

    ``approach_atr``: a price within this many ATRs above the buy zone reads
    "approaching". ``stale_move_atr``: if price moves more than this many ATRs
    between an analysis's input price and the moment its message is rendered,
    the message is downgraded to STALE — RECHECK REQUIRED. ``renotify_h``: the
    same plan state is not re-notified within this window even if price flaps
    across a boundary (transitions still update the persisted state).
    ``market_data_max_age_min``: a price older than this is treated as stale.
    """
    approach_atr: float = 0.5
    stale_move_atr: float = 0.5
    renotify_h: float = 6.0
    market_data_max_age_min: float = 30.0


@dataclass
class TriggeredAnalysisConfig:
    """Paid Tier 1 analysis (Fast Recheck / Triggered Risk) — Watchy 2.0.

    ``enabled: false`` is shadow mode: routes are computed, logged and the
    deterministic reminder is sent, but no paid call is made. Budgets reset on
    the exchange trading-session boundary. Weekly Full and the take-profit path
    are never limited by this budget. ``bearish_shock_atr``: a session move of
    at least this many ATRs down counts as a material negative move.
    ``tickers``: optional allow-list for limited enablement (empty = all).
    """
    enabled: bool = False
    max_per_ticker_per_trading_day: int = 1
    max_global_per_trading_day: int = 2
    bearish_shock_atr: float = 0.75
    tickers: list[str] = field(default_factory=list)


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class SchwabConfig:
    api_key: str = ""          # Schwab app key
    api_secret: str = ""       # Schwab app secret
    account_id: str = ""       # account number to use; blank = first linked account
    enabled: bool = False
    callback_url: str = "https://127.0.0.1"
    tokens_path: str = "~/watchy_config/schwab_tokens.db"  # schwabdev 3.x SQLite token store


@dataclass
class WatchyConfig:
    watchlist: list[TickerConfig] = field(default_factory=list)
    signal_thresholds: SignalThresholds = field(default_factory=SignalThresholds)
    cooldown: CooldownConfig = field(default_factory=CooldownConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    schwab: SchwabConfig = field(default_factory=SchwabConfig)
    take_profit: TakeProfitConfig = field(default_factory=TakeProfitConfig)
    log_level: str = "INFO"
    log_file: str = "~/watchy/watchy.log"
    # Seconds to sleep between tickers in a Tier 2 daily scan, to avoid a
    # burst of yfinance requests tripping rate limits (#1).
    tier2_throttle_s: float = 2.0
    # Global default for the Tier 2 price-proximity gate (#15), applied to every
    # watch-only ticker that doesn't set its own min_price_proximity_pct. None
    # disables the gate globally. Held tickers and the weekly full-risk run
    # (first trading day of the week) are never gated.
    min_price_proximity_pct: float | None = None
    # Global ATR-adaptive proximity band (#15 follow-up), applied to every
    # watch-only ticker that doesn't set its own atr_proximity_mult. When set
    # (and ATR data is available) the gate band is mult x ATR% instead of the
    # fixed min_price_proximity_pct; otherwise it falls back to the fixed pct.
    # None keeps the fixed-pct behaviour. See TickerConfig.atr_proximity_mult.
    atr_proximity_mult: float | None = None
    # Clamp bounds for an ATR-derived band, so a freak low/high-volatility
    # reading can't make the band absurd. Only used when an ATR mult is active.
    proximity_pct_floor: float = 4.0
    proximity_pct_ceiling: float = 20.0
    # Global cap on Tier 1 intraday LLM rescans per ticker per UTC day, applied to
    # every ticker that doesn't set its own max_tier1_pipelines_per_day. Each Tier 1
    # signal trip launches a paid [market+social] pipeline + advisor (guarded only by
    # per-signal cooldown), so a busy ticker tripping several distinct signals stacks
    # several paid rescans in a day. This caps that. None disables the cap globally.
    # Tier 2 scheduled runs are never affected.
    max_tier1_pipelines_per_day: int | None = None
    # Global default tiered cadence for Tier 2 (see TickerConfig.tier2_days).
    # None = every trading day, i.e. the historical behaviour, so leaving this
    # unset changes nothing. A per-ticker tier2_days overrides it.
    tier2_days: list[str] | None = None
    # Watchy 2.0 scheduling. "weekly" (2.0 default): Tier 2 runs only on the
    # first trading session of each week and produces a structured weekly plan;
    # Tier 1 monitors the plan and routes triggers. "daily": the 1.x behaviour
    # (daily Tier 2 + paid Tier 1 signal rescans) — the configuration rollback.
    tier2_schedule: str = "weekly"
    weekly_plan: WeeklyPlanConfig = field(default_factory=WeeklyPlanConfig)
    triggered_analysis: TriggeredAnalysisConfig = field(
        default_factory=TriggeredAnalysisConfig
    )

    @property
    def weekly_mode(self) -> bool:
        return str(self.tier2_schedule).strip().lower() != "daily"

    def get_ticker_config(self, ticker: str) -> TickerConfig | None:
        """Return the TickerConfig for a symbol (case-insensitive), or None."""
        t = ticker.upper()
        for tc in self.watchlist:
            if tc.ticker.upper() == t:
                return tc
        return None

    @classmethod
    def from_yaml(cls, path: str | Path) -> WatchyConfig:
        path = Path(os.path.expanduser(path))
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        # UTF-8 explicitly: config.yaml is committed/synced across machines and may
        # carry non-ASCII comments; the platform default (e.g. gbk on Windows)
        # would choke on them.
        with open(path, encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        return cls(
            watchlist=[
                TickerConfig(**t) if isinstance(t, dict) else TickerConfig(ticker=t)
                for t in raw.get("watchlist", [])
            ],
            signal_thresholds=SignalThresholds(
                **raw.get("signal_thresholds", {})
            ),
            cooldown=CooldownConfig(**raw.get("cooldown", {})),
            llm=LLMConfig(**raw.get("llm", {})),
            telegram=TelegramConfig(**raw.get("telegram", {})),
            schwab=SchwabConfig(**raw.get("schwab", {})),
            take_profit=TakeProfitConfig(**raw.get("take_profit", {})),
            log_level=raw.get("log_level", "INFO"),
            log_file=raw.get("log_file", "~/watchy/watchy.log"),
            tier2_throttle_s=raw.get("tier2_throttle_s", 2.0),
            min_price_proximity_pct=raw.get("min_price_proximity_pct"),
            atr_proximity_mult=raw.get("atr_proximity_mult"),
            proximity_pct_floor=raw.get("proximity_pct_floor", 4.0),
            proximity_pct_ceiling=raw.get("proximity_pct_ceiling", 20.0),
            max_tier1_pipelines_per_day=raw.get("max_tier1_pipelines_per_day"),
            tier2_days=raw.get("tier2_days"),
            tier2_schedule=_schedule(raw.get("tier2_schedule", "weekly")),
            weekly_plan=WeeklyPlanConfig(**(raw.get("weekly_plan") or {})),
            triggered_analysis=TriggeredAnalysisConfig(
                **(raw.get("triggered_analysis") or {})
            ),
        )


def _schedule(value: Any) -> str:
    """Validate tier2_schedule; a typo must fail at startup, not silently pick
    a mode that changes what gets paid for."""
    v = str(value).strip().lower()
    if v not in ("weekly", "daily"):
        raise ValueError(f"tier2_schedule must be 'weekly' or 'daily', got {value!r}")
    return v


def _merge_secrets(config: WatchyConfig, secrets_path: str) -> WatchyConfig:
    """Merge secrets.yaml into config — secrets override corresponding sections."""
    if not os.path.exists(secrets_path):
        return config

    with open(secrets_path, encoding="utf-8") as f:
        secrets: dict[str, Any] = yaml.safe_load(f) or {}

    if "llm" in secrets:
        config.llm = LLMConfig(**secrets["llm"])
    if "telegram" in secrets:
        config.telegram = TelegramConfig(**secrets["telegram"])
    if "schwab" in secrets:
        config.schwab = SchwabConfig(**secrets["schwab"])

    return config


def load_config(path: str | None = None) -> WatchyConfig:
    if path is None:
        path = os.environ.get(
            "WATCHY_CONFIG", os.path.expanduser("~/watchy/config.yaml")
        )
    path = os.path.expanduser(path)
    config = WatchyConfig.from_yaml(path)

    # Secrets always live in ~/watchy_config/, never in the git repo
    secrets_path = os.path.expanduser("~/watchy_config/secrets.yaml")
    return _merge_secrets(config, secrets_path)
