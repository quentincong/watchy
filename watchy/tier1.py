"""Tier 1 — hourly signal scanner (data-only pre-filter, no LLM).

Fetches OHLCV + indicators, checks for signal breaches against thresholds and
cooldown windows, and fires the graduated analyst pipeline when a signal trips.
"""

from __future__ import annotations

import logging
from typing import Any

from watchy import take_profit as tpmod
from watchy.advisor import get_advice
from watchy.config import WatchyConfig
from watchy.digest_store import load_digest, save_digest
from watchy.indicators import (
    IndicatorBundle,
    compute_indicators,
    compute_level_states,
    detect_signals,
)
from watchy.locks import TickerLockRegistry
from watchy.notify import TelegramNotifier
from watchy.orchestrator import (
    PipelineSpec,
    get_cooldown_hours,
    get_pipeline,
    run_pipeline,
)
from watchy.positions import PositionSource, get_position_source
from watchy.schwab_health import monitor_schwab
from watchy.state import StateStore

logger = logging.getLogger(__name__)


def scan_ticker(
    ticker: str,
    config: WatchyConfig,
    store: StateStore,
    notifier: TelegramNotifier,
    *,
    pipeline_runner: Any = None,
    ticker_locks: TickerLockRegistry | None = None,
) -> list[str]:
    """Run Tier 1 scan for a single ticker.

    Returns the list of signal types that fired (empty if none).
    """
    logger.info("Tier 1 scan start: %s", ticker)

    bundle = compute_indicators(ticker)
    if bundle is None:
        logger.warning("Skipping %s — no indicator data", ticker)
        return []

    prev = store.get_ticker_state(ticker)
    fired_signals = detect_signals(bundle, prev)

    # filter out signals still in cooldown
    actionable: list[str] = []
    for sig in fired_signals:
        cooldown_h = get_cooldown_hours(sig, config.cooldown)
        if store.is_in_cooldown(ticker, sig, cooldown_h):
            logger.info("Signal %s for %s in cooldown, skipping", sig, ticker)
            continue
        actionable.append(sig)

    # One position source per scan, reused by both the signal pipeline and the
    # take-profit check, so a held ticker triggers at most one live fetch. Built
    # only when needed (a signal will run, or the take-profit gate is enabled).
    position_source: PositionSource | None = None
    if actionable or config.take_profit.enabled:
        position_source = get_position_source(config)

    pipeline_ran = False
    if actionable:
        # Serialize the pipeline for this ticker so a concurrent Tier 2 run for
        # the same symbol doesn't double-spend the analyst budget or interleave
        # state.
        lock = ticker_locks.get(ticker) if ticker_locks else _nullcontext()
        with lock:
            for sig in actionable:
                spec = get_pipeline(sig)
                _handle_signal(
                    ticker, sig, spec, bundle, config, store, notifier,
                    pipeline_runner, position_source,
                )
        pipeline_ran = True

    # Take-profit zone-entry trigger (#28): a held winner whose unrealized gain
    # crosses the floor intraday gets an advisor-only take-profit call so its
    # sell-limit is set the same day, not next morning. Returns the current zone
    # membership to persist for on-entry transition detection.
    tp_zone, tp_qty = _check_take_profit_zone(
        ticker, bundle, prev, config, store, notifier,
        position_source, pipeline_ran,
    )

    _update_state(store, bundle, ticker, take_profit_zone=tp_zone, quantity=tp_qty)
    logger.info("Tier 1 scan complete: %s — signals: %s", ticker, actionable)
    return actionable


def _nullcontext():
    from contextlib import nullcontext
    return nullcontext()


def _handle_signal(
    ticker: str,
    sig: str,
    spec: PipelineSpec,
    bundle: IndicatorBundle,
    config: WatchyConfig,
    store: StateStore,
    notifier: TelegramNotifier,
    pipeline_runner: Any = None,
    position_source: PositionSource | None = None,
) -> None:
    """Log the signal, notify, launch the analyst pipeline, and synthesize position advice."""
    details = _bundle_summary(bundle)
    store.log_signal(ticker, sig, details)

    # Tier 1 daily rescan cap (#23): each signal trip launches a paid pipeline +
    # advisor, guarded only by per-signal cooldown, so a ticker tripping several
    # distinct signals in a day stacks several paid rescans. Cap the count per UTC
    # day; further trips are logged + notified but skip the LLM pipeline.
    cap = _rescan_cap(ticker, config)
    if cap is not None:
        runs_today = store.count_tier1_runs_today(ticker)
        if runs_today >= cap:
            logger.info(
                "Tier 1 rescan capped for %s (%s): %d/%d runs today, skipping pipeline",
                ticker, sig, runs_today, cap,
            )
            notifier.rescan_capped(ticker, sig, details, runs_today, cap)
            return

    # notify: signal fired
    notifier.signal_fired(
        ticker, sig, details,
        triggered_analysts=_analyst_list_from_spec(spec),
    )

    # launch analysts
    run_id = store.start_run(ticker, "tier1", sig)
    try:
        # Fetch the position first (then run the expensive pipeline). This also
        # validates Schwab/OAuth up front: monitor_schwab reads the resolved
        # snapshot and alerts if it isn't live (expired token) or is nearing the
        # 7-day limit. (Holdings feed the advisor below, not TradingAgents.)
        if position_source is None:
            position_source = get_position_source(config)
        position_text = position_source.format_position_context(ticker)
        monitor_schwab(config, store, notifier, position_source)

        result = run_pipeline(ticker, spec, runner=pipeline_runner)
        store.complete_run(run_id, success=True, summary=result.get("summary", ""))
        # Stash the digest so the take-profit zone trigger (#28) can re-advise
        # intraday without paying for a fresh pipeline.
        save_digest(ticker, result)

        advice = get_advice(
            ticker, result, position_source, config,
            thinking_level=config.llm.gemini_thinking_tier1,
            indicator_bundle=bundle,
            store=store, source="tier1",
        )

        notifier.pipeline_result(
            ticker, sig, result,
            position_text=position_text,
            advice=advice,
        )
    except Exception as exc:
        logger.exception("Pipeline failed for %s: %s", ticker, exc)
        store.complete_run(run_id, success=False, summary=str(exc))
        notifier.error(f"Tier 1 pipeline: {ticker}/{sig}", exc)


def _update_state(
    store: StateStore,
    bundle: IndicatorBundle,
    ticker: str,
    take_profit_zone: int | None = None,
    quantity: float | None = None,
) -> None:
    extra: dict[str, Any] = {}
    if take_profit_zone is not None:
        # #28 take-profit zone membership, for on-entry transition detection.
        extra["prev_take_profit_zone"] = take_profit_zone
    if quantity is not None:
        # #28 follow-up: last-seen share count — a drop is a fill, which re-arms
        # the zone-entry trigger (see take_profit.was_trimmed).
        extra["prev_quantity"] = quantity
    store.save_ticker_state(
        ticker,
        prev_sma_50_above_200=(
            1 if bundle.sma_50 and bundle.sma_200 and bundle.sma_50 > bundle.sma_200
            else 0
        ),
        prev_macd_above_signal=(
            1 if bundle.macd and bundle.macd_signal and bundle.macd > bundle.macd_signal
            else 0
        ),
        prev_rsi=bundle.rsi,
        prev_atr=bundle.atr,
        avg_volume_20d=bundle.avg_volume_20d,
        avg_atr_20d=bundle.avg_atr_20d,
        # transition flags for level-based signals (#8)
        **compute_level_states(bundle),
        **extra,
    )


def _check_take_profit_zone(
    ticker: str,
    bundle: IndicatorBundle,
    prev: dict[str, Any],
    config: WatchyConfig,
    store: StateStore,
    notifier: TelegramNotifier,
    position_source: PositionSource | None,
    pipeline_ran: bool,
) -> tuple[int | None, float | None]:
    """Detect a held winner crossing the take-profit floor intraday (#28).

    Returns ``(zone_membership, share_count)`` to persist — 1/0 for the zone,
    and the quantity that the next scan diffs against to spot a fill. Both are
    None when the gate is off / state can't be read (leave the stored values
    untouched). Fires an advisor-only take-profit call on the transition INTO
    the zone, and again after a fill; other steady-state advice is Tier 2's job.
    """
    if not config.take_profit.enabled or position_source is None:
        return None, None

    try:
        pos = position_source.get_position(ticker)
    except Exception:  # noqa: BLE001
        logger.warning("take-profit: position lookup failed for %s", ticker, exc_info=True)
        return None, None

    # Record the count even below the floor, so a sell made while out of the
    # zone can't be mistaken for a fresh fill on the next entry into it.
    qty = pos.quantity if pos is not None else 0.0

    gain = tpmod.position_gain_pct(pos)
    floor = tpmod.effective_floor_pct(config.get_ticker_config(ticker), config)
    in_zone = tpmod.is_in_zone(gain, floor)
    if not in_zone:
        return 0, qty

    # A drop in share count means the standing sell-limit filled, leaving the
    # position unprotected — re-arm even though the zone flag is still set. Under
    # highest-cost-first lot accounting the flag would otherwise latch at 1 for
    # the life of the position; see tpmod.was_trimmed for why quantity, not gain.
    trimmed = tpmod.was_trimmed(prev.get("prev_quantity"), qty)

    prev_zone = prev.get("prev_take_profit_zone")
    if prev_zone and not trimmed:
        # Already in the zone last scan → steady state; the daily Tier 2 run
        # re-advises with the same directive, no intraday call needed.
        return 1, qty
    if pipeline_ran:
        # A technical signal already ran the advisor this scan with the
        # take-profit directive injected — don't fire a second call.
        return 1, qty
    if store.is_in_cooldown(ticker, "take_profit_zone", config.take_profit.cooldown_h):
        return 1, qty

    if trimmed:
        logger.info(
            "Take-profit re-arm for %s: shares %s → %s (fill), zone still active",
            ticker, prev.get("prev_quantity"), qty,
        )
    _fire_take_profit(ticker, bundle, config, store, notifier, position_source, gain)
    return 1, qty


def _fire_take_profit(
    ticker: str,
    bundle: IndicatorBundle,
    config: WatchyConfig,
    store: StateStore,
    notifier: TelegramNotifier,
    position_source: PositionSource,
    gain_pct: float,
) -> None:
    """Advisor-only take-profit call reusing the last digest (no fresh pipeline)."""
    logger.info("Take-profit zone entered for %s (gain +%.1f%%)", ticker, gain_pct)
    # Record the fire first so the cooldown holds even if the advisor call fails.
    store.log_signal(ticker, "take_profit_zone", _bundle_summary(bundle))

    loaded = load_digest(ticker)
    analysis_result = loaded[0] if loaded else {}
    if loaded is None:
        logger.info("Take-profit %s: no cached digest — mechanical-facts-only advice", ticker)

    try:
        advice = get_advice(
            ticker, analysis_result, position_source, config,
            thinking_level=config.llm.gemini_thinking_tier1,
            indicator_bundle=bundle,
            store=store, source="take_profit_zone",
        )
        position_text = position_source.format_position_context(ticker)
        notifier.take_profit_alert(ticker, gain_pct, advice, position_text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Take-profit advisor failed for %s", ticker)
        notifier.error(f"Take-profit: {ticker}", exc)


def _bundle_summary(bundle: IndicatorBundle) -> dict[str, Any]:
    return {
        "current_price": bundle.current_price,
        "sma_50": bundle.sma_50,
        "sma_200": bundle.sma_200,
        "rsi": bundle.rsi,
        "macd": bundle.macd,
        "macd_signal": bundle.macd_signal,
        "bb_upper": bundle.bb_upper,
        "bb_lower": bundle.bb_lower,
        "atr": bundle.atr,
        "volume": bundle.volume,
        "avg_volume_20d": bundle.avg_volume_20d,
        "sepa_stage": bundle.sepa_stage,
    }


def _analyst_list_from_spec(spec: PipelineSpec) -> list[str]:
    from watchy.orchestrator import _analyst_names
    return _analyst_names(spec.analysts)


def _rescan_cap(ticker: str, config: WatchyConfig) -> int | None:
    """Effective Tier 1 daily rescan cap: per-ticker override else global (#23)."""
    tc = config.get_ticker_config(ticker)
    if tc is not None and tc.max_tier1_pipelines_per_day is not None:
        return tc.max_tier1_pipelines_per_day
    return config.max_tier1_pipelines_per_day
