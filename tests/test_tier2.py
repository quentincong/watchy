"""Tests for Tier 2 inter-ticker throttle (#1) and batch ordering (#21).

The throttle now lives in the pre-fetch phase (one compute_indicators per
ticker, throttled), so these patch compute_indicators to avoid real yfinance
calls and stub the state lookup.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from watchy.config import TickerConfig, WatchyConfig
from watchy.tier2 import run_daily_scan


def _config(n_tickers: int, throttle: float = 2.0) -> WatchyConfig:
    return WatchyConfig(
        watchlist=[TickerConfig(ticker=f"T{i}") for i in range(n_tickers)],
        tier2_throttle_s=throttle,
    )


@contextmanager
def _patched(sleep_target=True):
    """Patch the network/data touchpoints so run_daily_scan stays offline."""
    with patch("watchy.tier2._run_ticker", return_value={"summary": "ok"}) as run, \
         patch("watchy.tier2.compute_indicators", return_value=None), \
         patch("watchy.tier2.time.sleep") as sleep:
        yield run, sleep


def _mocks():
    store, notifier = MagicMock(), MagicMock()
    store.get_ticker_state.return_value = {}
    return store, notifier


class TestTier2Throttle:
    def test_sleeps_between_tickers(self):
        config = _config(4)
        store, notifier = _mocks()

        with _patched() as (_run, mock_sleep):
            run_daily_scan(config, store, notifier)

        # one sleep between each pair of tickers (pre-fetch loop) → n-1 sleeps
        assert mock_sleep.call_count == 3
        for call in mock_sleep.call_args_list:
            assert call.args[0] == 2.0

    def test_no_sleep_for_single_ticker(self):
        config = _config(1)
        store, notifier = _mocks()

        with _patched() as (_run, mock_sleep):
            run_daily_scan(config, store, notifier)

        mock_sleep.assert_not_called()

    def test_zero_throttle_disables_sleep(self):
        config = _config(5, throttle=0.0)
        store, notifier = _mocks()

        with _patched() as (_run, mock_sleep):
            run_daily_scan(config, store, notifier)

        mock_sleep.assert_not_called()

    def test_all_tickers_still_processed(self):
        config = _config(3)
        store, notifier = _mocks()

        with _patched() as (mock_run, _sleep):
            results = run_daily_scan(config, store, notifier)

        assert mock_run.call_count == 3
        assert set(results.keys()) == {"T0", "T1", "T2"}


class TestTier2TakeProfitWiring:
    def test_run_ticker_passes_bundle_to_advisor(self):
        """The daily advisor call must receive the bundle so the #28 gate can
        read price + ATR for a held winner."""
        from watchy.indicators import IndicatorBundle
        from watchy.tier2 import _PlanEntry, _run_ticker

        bundle = IndicatorBundle(ticker="NVDA", current_price=189.0, avg_atr_20d=5.0)
        entry = _PlanEntry(
            ticker="NVDA", tc=TickerConfig(ticker="NVDA"), bundle=bundle,
            state={}, held=True, price=189.0, avg_atr=5.0, target=None, skip=False,
        )
        config = WatchyConfig(watchlist=[TickerConfig(ticker="NVDA")])
        store, notifier = MagicMock(), MagicMock()
        store.start_run.return_value = 1
        position_source = MagicMock()

        with patch("watchy.tier2.run_pipeline", return_value={"summary": "ok"}), \
             patch("watchy.tier2.get_advice", return_value=None) as adv:
            _run_ticker(entry, config, store, notifier, position_source)

        assert adv.call_args.kwargs["indicator_bundle"] is bundle


class TestTieredCadence:
    """Per-ticker tier2_days (cost/duration control), with two hard exemptions."""

    def _cfg(self, **kw):
        return WatchyConfig(watchlist=[TickerConfig(ticker="X", **kw)])

    def test_no_cadence_configured_runs_every_day(self):
        from watchy.tier2 import _should_skip_cadence
        cfg = self._cfg()
        tc = cfg.watchlist[0]
        with patch("watchy.tier2.is_weekly_full_risk_day", return_value=False):
            for wd in range(7):
                now = datetime(2026, 8, 3, tzinfo=timezone.utc) + timedelta(days=wd)
                assert _should_skip_cadence(tc, cfg, now, False) is False

    def test_skips_a_day_not_in_the_list(self):
        from watchy.tier2 import _should_skip_cadence
        cfg = self._cfg(tier2_days=["mon", "wed", "fri"])
        tc = cfg.watchlist[0]
        with patch("watchy.tier2.is_weekly_full_risk_day", return_value=False):
            tue = datetime(2026, 8, 4, tzinfo=timezone.utc)   # Tuesday
            wed = datetime(2026, 8, 5, tzinfo=timezone.utc)   # Wednesday
            assert _should_skip_cadence(tc, cfg, tue, False) is True
            assert _should_skip_cadence(tc, cfg, wed, False) is False

    def test_weekly_full_risk_day_is_never_skipped(self):
        from watchy.tier2 import _should_skip_cadence
        cfg = self._cfg(tier2_days=["fri"])
        tc = cfg.watchlist[0]
        tue = datetime(2026, 8, 4, tzinfo=timezone.utc)
        with patch("watchy.tier2.is_weekly_full_risk_day", return_value=True):
            assert _should_skip_cadence(tc, cfg, tue, False) is False

    def test_take_profit_zone_overrides_cadence(self):
        # A cost optimisation must never be what hides a winner past its floor.
        from watchy.tier2 import _should_skip_cadence
        cfg = self._cfg(tier2_days=["fri"])
        tc = cfg.watchlist[0]
        tue = datetime(2026, 8, 4, tzinfo=timezone.utc)
        with patch("watchy.tier2.is_weekly_full_risk_day", return_value=False):
            assert _should_skip_cadence(tc, cfg, tue, True) is False

    def test_per_ticker_overrides_global(self):
        from watchy.tier2 import _should_skip_cadence
        cfg = WatchyConfig(
            watchlist=[TickerConfig(ticker="X", tier2_days=["tue"])],
            tier2_days=["mon"],
        )
        tc = cfg.watchlist[0]
        tue = datetime(2026, 8, 4, tzinfo=timezone.utc)
        with patch("watchy.tier2.is_weekly_full_risk_day", return_value=False):
            assert _should_skip_cadence(tc, cfg, tue, False) is False

    def test_global_applies_when_ticker_has_none(self):
        from watchy.tier2 import _should_skip_cadence
        cfg = WatchyConfig(
            watchlist=[TickerConfig(ticker="X")], tier2_days=["mon"],
        )
        tc = cfg.watchlist[0]
        tue = datetime(2026, 8, 4, tzinfo=timezone.utc)
        with patch("watchy.tier2.is_weekly_full_risk_day", return_value=False):
            assert _should_skip_cadence(tc, cfg, tue, False) is True

    def test_unparseable_day_names_fall_back_to_every_day(self):
        # A typo must not silently mute a ticker.
        from watchy.tier2 import _effective_tier2_days
        cfg = self._cfg(tier2_days=["notaday", "???"])
        assert _effective_tier2_days(cfg.watchlist[0], cfg) is None

    def test_day_names_are_case_and_length_insensitive(self):
        from watchy.tier2 import _effective_tier2_days
        cfg = self._cfg(tier2_days=["Monday", " TUE ", "wed"])
        assert _effective_tier2_days(cfg.watchlist[0], cfg) == {0, 1, 2}


class TestAdviceLogWiring:
    """#31: Tier 2 hands the advisor a store and its source label."""

    def test_passes_store_and_source(self):
        from watchy.config import TickerConfig, WatchyConfig
        from watchy.indicators import IndicatorBundle
        from watchy.tier2 import _PlanEntry, _run_ticker

        bundle = IndicatorBundle(ticker="NVDA", current_price=189.0, avg_atr_20d=5.0)
        entry = _PlanEntry(
            ticker="NVDA", tc=TickerConfig(ticker="NVDA"), bundle=bundle,
            state={}, held=True, price=189.0, avg_atr=5.0, target=None, skip=False,
        )
        config = WatchyConfig(watchlist=[TickerConfig(ticker="NVDA")])
        store, notifier = MagicMock(), MagicMock()
        store.start_run.return_value = 1

        with patch("watchy.tier2.run_pipeline", return_value={"summary": "ok"}), \
             patch("watchy.tier2.get_advice", return_value=None) as adv:
            _run_ticker(entry, config, store, notifier, MagicMock())

        assert adv.call_args.kwargs["store"] is store
        assert adv.call_args.kwargs["source"] == "tier2"
