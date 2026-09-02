"""Tier 2 — scheduled daily pipeline.

Runs the full TradingAgents 4-analyst pipeline (Market, Sentiment, News,
Fundamentals) with Bull/Bear debate for every ticker on the watchlist, catching
gradual drift and fundamental shifts that technical triggers miss.

Risk depth is day-of-week dependent (#14): simplified risk on ordinary trading
days, the full 3-way risk debate on the first trading day of each week. See
orchestrator.get_scheduled_spec.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from watchy.advisor import get_advice, parse_price
from watchy.config import TickerConfig, WatchyConfig
from watchy.digest_store import save_digest
from watchy.indicators import IndicatorBundle, compute_indicators
from watchy.locks import TickerLockRegistry
from watchy.market_calendar import is_weekly_full_risk_day
from watchy.notify import TelegramNotifier
from watchy.orchestrator import get_scheduled_spec, run_pipeline
from watchy.positions import get_position_source
from watchy.proximity import is_outside_proximity
from watchy.schwab_health import monitor_schwab
from watchy.state import StateStore
from watchy.take_profit import effective_floor_pct, is_in_zone, position_gain_pct

logger = logging.getLogger(__name__)


def run_daily_scan(
    config: WatchyConfig,
    store: StateStore,
    notifier: TelegramNotifier,
    *,
    pipeline_runner: Any = None,
    ticker_locks: TickerLockRegistry | None = None,
) -> dict[str, dict[str, Any]]:
    """Run Tier 2 for every ticker on the watchlist.

    Returns a dict mapping ticker → pipeline result.
    """
    results: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)

    logger.info("Tier 2 daily scan starting for %d tickers", len(config.watchlist))

    # Fetch positions once for the whole batch and reuse the snapshot across every
    # ticker (the account is the same for all of them). This both avoids N redundant
    # Schwab calls and gives the batch one consistent holdings view. The fetch here
    # also validates Schwab/OAuth: monitor_schwab reads the resolved snapshot and
    # alerts if it isn't live (expired token) or is nearing the 7-day limit.
    position_source = get_position_source(config)
    monitor_schwab(config, store, notifier, position_source)

    # Pre-fetch indicators for every ticker up front (throttled to avoid a
    # yfinance burst, #1) so we can both order the batch by priority and reuse
    # each bundle in the pipeline instead of re-fetching it.
    plan = _prefetch_plan(config, store, position_source, now)

    # Run in priority order (#21): held tickers first (capital at risk), then
    # watch-only nearest-to-target (most actionable), no-target/no-price last.
    # A long batch is interruptible (auto-update restart, crash, token expiry,
    # 429), so the most important names should be analysed before any of those.
    for entry in _ordered_run_plan(plan):
        ticker = entry.ticker
        if entry.cadence_skip:
            logger.info(
                "Tier 2 skip %s — not scheduled today (cadence %s, today=%s)",
                ticker,
                _format_days(_effective_tier2_days(entry.tc, config)),
                now.strftime("%a").lower(),
            )
            results[ticker] = {"ticker": ticker, "skipped": "not_scheduled_today"}
            continue
        if entry.skip:
            pct = _effective_proximity_pct(entry.tc, config, entry.avg_atr, entry.price)
            logger.info(
                "Tier 2 skip %s — not held, price %.2f outside %.2f%% of entry "
                "target %.2f (proximity gate)",
                ticker, entry.price, pct, entry.target,
            )
            results[ticker] = {
                "ticker": ticker, "skipped": "out_of_proximity", "target": entry.target,
            }
            continue
        try:
            results[ticker] = _run_ticker(
                entry, config, store, notifier, position_source,
                pipeline_runner, ticker_locks,
            )
        except Exception as exc:
            logger.exception("Tier 2 failed for %s", ticker)
            notifier.error(f"Tier 2: {ticker}", exc)
            results[ticker] = {"error": str(exc)}

    succeeded = sum(1 for r in results.values() if "error" not in r)
    # Advisor failures don't fail the run, so they were invisible in this line
    # until 2026-08-07 (3 silent ones in a batch that reported 18/18). Count them
    # separately rather than folding them into `succeeded`.
    advisor_failed = sum(1 for r in results.values() if r.get("advisor_failed"))
    skipped = sum(1 for r in results.values() if r.get("skipped"))
    logger.info(
        "Tier 2 daily scan complete: %d/%d succeeded (%d skipped, %d advisor-failed)",
        succeeded, len(config.watchlist), skipped, advisor_failed,
    )
    return results


@dataclass
class _PlanEntry:
    """One ticker's pre-fetched context for a Tier 2 batch (#21)."""
    ticker: str
    tc: TickerConfig | None
    bundle: IndicatorBundle | None
    state: dict[str, Any]
    held: bool
    price: float | None
    avg_atr: float | None
    target: float | None
    skip: bool
    # Not scheduled for Tier 2 today under the tiered cadence (tier2_days).
    # Distinct from `skip`, which is the price-proximity gate (#15) — the two
    # have different causes and get different log lines / result markers.
    cadence_skip: bool = False


def _prefetch_plan(
    config: WatchyConfig,
    store: StateStore,
    position_source: Any,
    now: datetime,
) -> list[_PlanEntry]:
    """Compute indicators + gate decision for every watchlist ticker up front.

    Throttled between fetches (#1). The resulting bundle is reused by the
    pipeline so no ticker is fetched twice.
    """
    plan: list[_PlanEntry] = []
    for i, tc in enumerate(config.watchlist):
        if i > 0 and config.tier2_throttle_s > 0:
            time.sleep(config.tier2_throttle_s)
        ticker = tc.ticker
        bundle = compute_indicators(ticker)
        price = bundle.current_price if bundle is not None else None
        avg_atr = (bundle.avg_atr_20d or bundle.atr) if bundle is not None else None
        state = store.get_ticker_state(ticker)
        held = _is_held(position_source, ticker)
        target = _effective_target(tc, state)
        skip = _should_skip_tier2(price, tc, state, now, held, config, avg_atr)
        cadence_skip = _should_skip_cadence(
            tc, config, now, _in_take_profit_zone(position_source, ticker, config)
        )
        plan.append(
            _PlanEntry(
                ticker, tc, bundle, state, held, price, avg_atr, target, skip,
                cadence_skip,
            )
        )
    return plan


def _in_take_profit_zone(
    position_source: Any, ticker: str, config: WatchyConfig
) -> bool:
    """Whether this held position's gain has already crossed the floor (#28).

    The cadence must never be what hides a winner past its take-profit floor, so
    this is checked before deciding to skip. Purely mechanical — no LLM, no cost.
    """
    if not config.take_profit.enabled:
        return False
    try:
        pos = position_source.get_position(ticker)
    except Exception:  # noqa: BLE001
        logger.warning("cadence: position lookup failed for %s", ticker, exc_info=True)
        return False
    gain = position_gain_pct(pos)
    return is_in_zone(gain, effective_floor_pct(config.get_ticker_config(ticker), config))


def _ordered_run_plan(plan: list[_PlanEntry]) -> list[_PlanEntry]:
    """Order a batch by priority (#21): held first, then watch-only by ascending
    distance-to-target, then no-target/no-price last. Stable, so watchlist order
    breaks ties (and orders the no-target group)."""
    def key(e: _PlanEntry) -> tuple[int, float]:
        if e.held:
            return (0, 0.0)
        if e.target and e.price and e.target > 0:
            return (1, abs(e.price - e.target) / e.target)
        return (2, 0.0)

    return sorted(plan, key=key)


def _run_ticker(
    entry: _PlanEntry,
    config: WatchyConfig,
    store: StateStore,
    notifier: TelegramNotifier,
    position_source: Any,
    pipeline_runner: Any = None,
    ticker_locks: TickerLockRegistry | None = None,
) -> dict[str, Any]:
    """Run the Tier 2 pipeline for one pre-planned ticker.

    The proximity gate (#15) was already decided in _prefetch_plan; the caller
    skips gated tickers, so this only runs names worth the tokens. The bundle
    pre-fetched there is reused for stage context (no second yfinance fetch).
    """
    ticker = entry.ticker
    logger.info("Tier 2: %s", ticker)
    now = datetime.now(timezone.utc)

    # stage context reuses the pre-fetched bundle (#21) — no re-fetch.
    bundle = entry.bundle
    stage_context = {}
    if bundle is not None:
        stage_context = {
            "sepa_stage": bundle.sepa_stage,
            "current_price": bundle.current_price,
            "sma_50": bundle.sma_50,
            "sma_200": bundle.sma_200,
            "rsi": bundle.rsi,
        }

    # Serialize against a concurrent Tier 1 pipeline for the same ticker.
    from contextlib import nullcontext
    lock = ticker_locks.get(ticker) if ticker_locks else nullcontext()

    spec = get_scheduled_spec(now)

    with lock:
        run_id = store.start_run(ticker, "tier2", "scheduled_daily")
        try:
            result = run_pipeline(ticker, spec, runner=pipeline_runner)
            if stage_context:
                result.setdefault("stage_context", stage_context)
            store.complete_run(run_id, success=True, summary=result.get("summary", ""))
            store.save_ticker_state(ticker, last_full_analysis_ts=_now_iso())
            # Stash the digest so the Tier 1 take-profit trigger (#28) can
            # re-advise a held winner intraday off the freshest daily analysis.
            save_digest(ticker, result)

            # synthesize advice (reuse the position source from the gate check).
            # Pass the pre-fetched bundle so the take-profit gate (#28) can read
            # the current price + ATR: a held winner past the floor gets an
            # explicit sell-limit directive injected into the advisor prompt.
            position_text = position_source.format_position_context(ticker)
            advice = get_advice(
                ticker, result, position_source, config,
                thinking_level=config.llm.gemini_thinking_tier2,
                indicator_bundle=bundle,
                store=store, source="tier2",
            )

            # An advisor failure is not fatal to the run (the analysis itself is
            # still worth sending), but it must not stay silent: no advice card
            # AND no derived-target refresh for the gate. Surface it and mark the
            # result so the batch summary can count it.
            if advice is None:
                logger.warning("Tier 2: no advice synthesized for %s", ticker)
                notifier.advisor_failed(ticker, "Tier 2 scheduled daily run")
                result["advisor_failed"] = True

            # Auto-derive the Tier 2 proximity target (#16) from the advisor's
            # Target field, so the gate self-maintains. Manual config.target_price
            # still wins at read time (see _effective_target); this only fills the
            # derived slot used when no manual target is set.
            if advice:
                derived = parse_price(advice.get("target"))
                if derived is not None:
                    store.save_ticker_state(
                        ticker,
                        derived_target_price=derived,
                        derived_target_ts=_now_iso(),
                    )

            notifier.pipeline_result(
                ticker, "scheduled_daily", result,
                position_text=position_text,
                advice=advice,
            )
            return result
        except Exception as exc:
            store.complete_run(run_id, success=False, summary=str(exc))
            raise


def _effective_target(
    tc: TickerConfig | None, state: dict[str, Any]
) -> float | None:
    """The target price the Tier 2 gate measures against.

    Manual ``config.target_price`` wins; otherwise the #16 auto-derived target
    stored in ``ticker_state``. None when neither exists (→ ticker not gated).
    """
    if tc is not None and tc.target_price is not None:
        return tc.target_price
    return state.get("derived_target_price")


def _is_held(position_source: Any, ticker: str) -> bool:
    """True if a non-zero position in *ticker* is known.

    Conservative on uncertainty: a lookup error counts as held (→ run), since
    skipping a ticker we might own is the dangerous outcome. A clean ``None``
    from the layered source means confidently not-held.
    """
    try:
        pos = position_source.get_position(ticker)
    except Exception:  # noqa: BLE001
        logger.warning("position lookup failed for %s — treating as held (will run)", ticker)
        return True
    return pos is not None and getattr(pos, "quantity", 0) != 0


def _effective_proximity_pct(
    tc: TickerConfig | None,
    config: WatchyConfig | None,
    avg_atr: float | None = None,
    price: float | None = None,
) -> float | None:
    """The proximity percent that gates this ticker (#15).

    ATR-adaptive mode (#15 follow-up): if an ``atr_proximity_mult`` is set
    (per-ticker, else the global default) *and* ATR data is available, the band
    is ``mult * ATR%`` (ATR% = avg_atr / price * 100), clamped to the global
    floor/ceiling. Otherwise it falls back to the fixed percent — a per-ticker
    ``min_price_proximity_pct`` overriding the global default. None disables.
    """
    g_mult = config.atr_proximity_mult if config is not None else None
    mult = tc.atr_proximity_mult if (tc is not None and tc.atr_proximity_mult is not None) else g_mult
    if mult is not None and avg_atr and price and price > 0:
        pct = mult * (avg_atr / price * 100.0)
        if config is not None:
            pct = min(config.proximity_pct_ceiling, max(config.proximity_pct_floor, pct))
        return pct

    if tc is not None and tc.min_price_proximity_pct is not None:
        return tc.min_price_proximity_pct
    return config.min_price_proximity_pct if config is not None else None


_WEEKDAY_NUM = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WEEKDAY_NAME = {n: d for d, n in _WEEKDAY_NUM.items()}


def _format_days(days: set[int] | None) -> str:
    """Weekday numbers back to a stable 'mon,wed,fri' for logging."""
    if not days:
        return "every-day"
    return ",".join(_WEEKDAY_NAME[n] for n in sorted(days))


def _effective_tier2_days(
    tc: TickerConfig | None, config: WatchyConfig | None
) -> set[int] | None:
    """Weekday numbers this ticker runs Tier 2 on; None = every trading day.

    Per-ticker tier2_days wins over the global default. Unparseable names are
    ignored rather than fatal — a typo should not silently mute a ticker, and if
    nothing survives parsing we fall back to "every day".
    """
    raw = None
    if tc is not None and tc.tier2_days is not None:
        raw = tc.tier2_days
    elif config is not None:
        raw = config.tier2_days
    if not raw:
        return None
    days = {
        n for d in raw
        if (n := _WEEKDAY_NUM.get(str(d).strip().lower()[:3])) is not None
    }
    return days or None


def _should_skip_cadence(
    tc: TickerConfig | None,
    config: WatchyConfig | None,
    now: datetime,
    in_take_profit_zone: bool,
) -> bool:
    """Whether the tiered cadence takes this ticker off today's batch.

    Two hard exemptions, so cadence can only ever delay a routine read:

    * the weekly full-risk day (first trading session of the week) — every
      ticker keeps its guaranteed weekly full pass;
    * a position already in the take-profit zone — the one thing the user most
      needs surfaced must not be what a cost optimisation hides.
    """
    if is_weekly_full_risk_day(now):
        return False
    if in_take_profit_zone:
        return False
    days = _effective_tier2_days(tc, config)
    if days is None:
        return False
    return now.weekday() not in days


def _should_skip_tier2(
    price: float | None,
    tc: TickerConfig | None,
    state: dict[str, Any],
    now: datetime,
    held: bool,
    config: WatchyConfig | None = None,
    avg_atr: float | None = None,
) -> bool:
    """Whether Tier 2 should skip this ticker on this day (#15).

    Held tickers are never gated (capital at risk → always analyse). The first
    trading day of the week (the weekly full-risk run) is never gated, so every
    ticker gets one guaranteed full pass per week. Otherwise skip only when a
    proximity percent applies (ATR-adaptive or fixed, per-ticker or global) and
    price is outside that band of the effective *entry* target.
    """
    if held:                              # never gate a position you hold
        return False
    if is_weekly_full_risk_day(now):      # weekly full-risk run — always run
        return False
    pct = _effective_proximity_pct(tc, config, avg_atr, price)
    if pct is None:
        return False
    return is_outside_proximity(price, _effective_target(tc, state), pct)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
