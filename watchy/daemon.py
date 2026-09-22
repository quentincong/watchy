"""Watchy daemon — main entry point with APScheduler setup.

Two scheduled jobs:
  Tier 1 — runs per-ticker on configurable hourly intervals
  Tier 2 — runs once daily per ticker at configured UTC time
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from watchy import __version__
from watchy.config import WatchyConfig, load_config
from watchy.market_calendar import get_calendar as get_market_calendar
from watchy.market_calendar import is_trading_day, is_weekly_full_risk_day
from watchy.locks import TickerLockRegistry
from watchy.notify import TelegramNotifier
from watchy.state import StateStore
from watchy.pipeline_runner import create_tradingagents_runner
from watchy.tier1 import scan_ticker
from watchy.tier2 import run_daily_scan


def setup_logging(config: WatchyConfig) -> None:
    log_file = os.path.expanduser(config.log_file)
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # log_level applies to the ROOT logger, so DEBUG also switched on the HTTP
    # clients — and openai._base_client logs each request's full json_data, i.e.
    # the entire LLM prompt (tens of KB per call) into journald. That buried the
    # greppable TOKENCOST/gate lines and ate the retention we keep for cost
    # reconciliation. Muzzle the body dumps but keep watchy's own DEBUG intact.
    # httpx stays at INFO on purpose: one short "HTTP Request: POST ... 200 OK"
    # line per call is genuinely useful and costs nothing.
    for noisy in ("openai._base_client", "httpcore"):
        logging.getLogger(noisy).setLevel(max(root.level, logging.INFO))


def build_scheduler(
    config: WatchyConfig,
    store: StateStore,
    notifier: TelegramNotifier,
    *,
    pipeline_runner: Any = None,
    ticker_locks: TickerLockRegistry | None = None,
) -> BackgroundScheduler:
    if ticker_locks is None:
        ticker_locks = TickerLockRegistry()

    # Size the pool so every ticker can run concurrently without queuing.
    max_workers = max(10, len(config.watchlist) + 4)
    scheduler = BackgroundScheduler(
        timezone="UTC",
        executors={"default": ThreadPoolExecutor(max_workers=max_workers)},
    )

    from datetime import datetime, timedelta, timezone as _tz
    base = datetime.now(_tz.utc) + timedelta(seconds=10)

    # Tier 1: per-ticker hourly scans. Stagger first-fire by ticker index and add
    # jitter so 16 tickers don't stampede yfinance in the same second.
    for idx, tc in enumerate(config.watchlist):
        scheduler.add_job(
            _tier1_job,
            trigger=IntervalTrigger(
                hours=tc.tier1_interval_h,
                jitter=300,
                start_date=base + timedelta(seconds=idx * 7),
            ),
            args=[tc.ticker, config, store, notifier, pipeline_runner, ticker_locks],
            id=f"tier1_{tc.ticker}",
            name=f"Tier 1 — {tc.ticker}",
            replace_existing=True,
            misfire_grace_time=120,
        )

    # Tier 2: one job per unique UTC time (processes all tickers in that slot)
    seen_times: set[str] = set()
    for tc in config.watchlist:
        if tc.tier2_time_utc in seen_times:
            continue
        seen_times.add(tc.tier2_time_utc)
        hour, minute = tc.tier2_time_utc.split(":")
        scheduler.add_job(
            _tier2_job,
            trigger=CronTrigger(hour=int(hour), minute=int(minute)),
            args=[config, store, notifier, pipeline_runner, ticker_locks],
            id=f"tier2_{tc.tier2_time_utc.replace(':', '')}",
            name=f"Tier 2 — {tc.tier2_time_utc} UTC",
            replace_existing=True,
            misfire_grace_time=120,
        )

    return scheduler


# US regular-session hours in UTC during EDT (09:30–16:00 ET). Used only by the
# weekday fallback; the exchange-calendar path handles DST + holidays correctly.
_MARKET_OPEN_UTC = dtime(13, 30)
_MARKET_CLOSE_UTC = dtime(20, 0)


def _regular_session_window(now: datetime) -> bool:
    """Weekday + 13:30–20:00 UTC check (EDT hours; ~1h off under EST, no holidays).

    Fallback for when exchange_calendars can't be loaded.
    """
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return _MARKET_OPEN_UTC <= now.timetz().replace(tzinfo=None) <= _MARKET_CLOSE_UTC


def _is_market_open(now: datetime | None = None) -> bool:
    """True if the US equity market (XNYS) is in its regular session.

    Prefers exchange_calendars (already a dependency via yfinance-cache) for
    holiday- and DST-correct hours; degrades to a weekday+UTC-window check if the
    calendar can't be loaded.
    """
    now = now or datetime.now(timezone.utc)
    cal = get_market_calendar()
    if cal is not None:
        try:
            import pandas as pd
            return bool(cal.is_open_on_minute(pd.Timestamp(now).tz_convert("UTC")))
        except Exception:
            logging.getLogger("watchy.daemon").warning(
                "exchange_calendars minute check failed; using weekday/UTC-window",
                exc_info=True,
            )
    return _regular_session_window(now)


def _tier1_job(
    ticker: str,
    config: WatchyConfig,
    store: StateStore,
    notifier: TelegramNotifier,
    pipeline_runner: Any = None,
    ticker_locks: TickerLockRegistry | None = None,
) -> None:
    logger = logging.getLogger("watchy.daemon")
    # Tier 1 reacts to live price action — outside the regular session the bars
    # don't change, so skip the scan entirely (#7). Tier 2 is NOT gated this way:
    # it runs daily regardless (weekend news/sentiment still matter).
    if not _is_market_open():
        logger.debug("Tier 1 %s skipped — US market closed", ticker)
        return
    try:
        fired = scan_ticker(
            ticker, config, store, notifier,
            pipeline_runner=pipeline_runner, ticker_locks=ticker_locks,
        )
        if fired:
            logger.info("Tier 1 %s: fired %s", ticker, fired)
    except Exception:
        logger.exception("Tier 1 job failed for %s", ticker)
        notifier.error(f"Tier 1 job: {ticker}", sys.exc_info()[1])


def _is_tier2_day(now: datetime | None = None) -> bool:
    """Tier 2 runs only on US trading days; weekends and NYSE holidays are skipped.

    The weekly full 3-way risk debate rides on the **first trading day of each
    week** (see market_calendar.is_weekly_full_risk_day), not a separate weekend
    run — a weekend batch analysed the stale Friday close and was re-chewed by
    Monday's simplified run on the same close (redundant cost). Falls back to a
    Mon–Fri check (holiday-blind) if the exchange calendar can't be loaded.
    """
    return is_trading_day(now)


def _tier2_job(
    config: WatchyConfig,
    store: StateStore,
    notifier: TelegramNotifier,
    pipeline_runner: Any = None,
    ticker_locks: TickerLockRegistry | None = None,
) -> None:
    logger = logging.getLogger("watchy.daemon")
    if not _is_tier2_day():
        logger.debug("Tier 2 skipped — market closed (weekend/holiday)")
        return
    weekly = config.weekly_mode
    if weekly and not is_weekly_full_risk_day():
        # Watchy 2.0: routine daily Tier 2 is off; Tier 1 monitors the weekly
        # plan instead. `tier2_schedule: daily` restores the 1.x daily run.
        logger.info("Tier 2 skipped — tier2_schedule=weekly, not the week's first session")
        return
    try:
        run_daily_scan(
            config, store, notifier,
            pipeline_runner=pipeline_runner, ticker_locks=ticker_locks,
            weekly=weekly,
        )
    except Exception:
        logger.exception("Tier 2 job failed")
        notifier.error("Tier 2 job", sys.exc_info()[1])


def main(config_path: str | None = None) -> None:
    config = load_config(config_path)
    setup_logging(config)
    logger = logging.getLogger("watchy.daemon")

    logger.info("Watchy %s starting", __version__)
    logger.info(
        "Mode: tier2_schedule=%s triggered_analysis.enabled=%s",
        config.tier2_schedule, config.triggered_analysis.enabled,
    )
    logger.info("Watchlist: %s", [tc.ticker for tc in config.watchlist])

    store = StateStore()
    notifier = TelegramNotifier(config.telegram.bot_token, config.telegram.chat_id)
    ticker_locks = TickerLockRegistry()

    # Wire the real TradingAgents pipeline runner (DeepSeek by default).
    # API key from secrets.yaml, injected as env var before TA imports.
    pipeline_runner = create_tradingagents_runner(
        deepseek_api_key=config.llm.deepseek_api_key,
    )

    scheduler = build_scheduler(
        config, store, notifier,
        pipeline_runner=pipeline_runner, ticker_locks=ticker_locks,
    )

    def _shutdown(signum, frame):
        logger.info("Shutting down (signal=%d)", signum)
        scheduler.shutdown(wait=False)
        store.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    try:
        # keep alive — scheduler runs in background thread
        signal.pause()
    except AttributeError:
        # Windows doesn't have signal.pause()
        import time
        while True:
            time.sleep(60)


if __name__ == "__main__":
    main()
