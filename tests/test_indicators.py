"""Tests for indicators: computation and signal detection with synthetic data."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from watchy.indicators import (
    IndicatorBundle,
    compute_indicators,
    detect_signals,
    _compute_rsi,
    _classify_sepa_stage,
    _history_via_cache_or_direct,
)


def make_ohlcv(
    prices: list[float],
    *,
    volumes: list[float] | None = None,
    high_low_spread: float = 0.01,
) -> pd.DataFrame:
    """Build a synthetic OHLCV DataFrame from a list of closing prices."""
    n = len(prices)
    dates = pd.date_range(end=pd.Timestamp.now(), periods=n, freq="D")
    data = {
        "Open": [p * (1 - high_low_spread / 2) for p in prices],
        "High": [p * (1 + high_low_spread / 2) for p in prices],
        "Low": [p * (1 - high_low_spread / 2) for p in prices],
        "Close": prices,
        "Volume": volumes if volumes else [1_000_000] * n,
    }
    return pd.DataFrame(data, index=dates)


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

class TestRSI:
    def test_rsi_extreme_low(self):
        """A steadily declining price series → RSI should be very low."""
        n = 50
        prices = [100.0 - i * 1.0 for i in range(n)]  # drops from 100 to 51
        prices[25] = prices[24] + 0.5  # tiny bounce avoids NaN
        close = pd.Series(prices)
        rsi = _compute_rsi(close)
        assert rsi is not None
        assert rsi < 30, f"Expected RSI < 30, got {rsi:.1f}"

    def test_rsi_extreme_high(self):
        """A steadily rising price series → RSI should be very high."""
        n = 50
        # Add one small dip so avg_loss stays non-zero (avoids NaN).
        prices = [100.0 + i * 1.0 for i in range(n)]
        prices[25] = prices[24] - 0.5  # tiny dip
        close = pd.Series(prices)
        rsi = _compute_rsi(close)
        assert rsi is not None
        assert rsi > 70, f"Expected RSI > 70, got {rsi:.1f}"

    def test_rsi_mid_range(self):
        """Alternating up/down should keep RSI near 50."""
        n = 50
        prices = [100.0]
        for i in range(1, n):
            prices.append(prices[-1] + (1.0 if i % 2 == 0 else -1.0))
        close = pd.Series(prices)
        rsi = _compute_rsi(close)
        assert rsi is not None
        assert 40 < rsi < 60, f"Expected RSI ~50, got {rsi:.1f}"


# ---------------------------------------------------------------------------
# IndicatorBundle computation
# ---------------------------------------------------------------------------

class TestComputeIndicators:
    def test_returns_none_for_empty_df(self):
        assert compute_indicators("FAKE", pd.DataFrame()) is None

    def test_returns_none_for_short_history(self):
        df = make_ohlcv([100.0] * 10)  # only 10 days
        assert compute_indicators("FAKE", df) is None

    def test_recent_listing_gets_a_partial_bundle(self):
        """SKHY (55 rows on 2026-09-28) must still get a price and short-window
        indicators — a None bundle left a held position unplanned and unscanned."""
        prices = [100.0 + np.sin(i / 5) * 5 for i in range(55)]
        bundle = compute_indicators("SKHY", make_ohlcv(prices))
        assert bundle is not None
        assert bundle.current_price == pytest.approx(prices[-1])
        assert bundle.prev_close == pytest.approx(prices[-2])
        assert bundle.sma_50 is not None
        assert bundle.sma_150 is None and bundle.sma_200 is None
        assert bundle.sepa_stage is None
        assert bundle.atr and bundle.avg_atr_20d and bundle.rsi is not None
        assert bundle.bb_upper is not None and bundle.macd is not None
        # no cross can fire without a 200-day average
        signals = detect_signals(bundle, {"prev_sma_50_above_200": 0})
        assert "golden_cross" not in signals and "death_cross" not in signals

    def test_computes_all_fields(self):
        """With 250 days of flat-ish data, all fields should populate."""
        prices = [100.0 + np.sin(i / 20) * 5 for i in range(250)]
        df = make_ohlcv(prices)
        bundle = compute_indicators("TEST", df)

        assert bundle is not None
        assert bundle.ticker == "TEST"
        assert bundle.current_price is not None
        assert bundle.sma_50 is not None
        assert bundle.sma_200 is not None
        assert bundle.rsi is not None
        assert bundle.macd is not None
        assert bundle.macd_signal is not None
        assert bundle.bb_upper is not None
        assert bundle.bb_lower is not None
        assert bundle.atr is not None
        assert bundle.volume is not None
        assert bundle.avg_volume_20d is not None
        assert bundle.sepa_stage is not None

    def test_sepa_stage_present(self):
        """SEPA stage should be 1-4."""
        prices = [100.0 + i * 0.05 for i in range(250)]  # gentle uptrend
        df = make_ohlcv(prices)
        bundle = compute_indicators("TEST", df)
        assert bundle is not None
        assert bundle.sepa_stage in (1, 2, 3, 4)


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

class TestDetectSignals:
    def test_empty_for_no_signals(self):
        """Flat prices with no prior state should produce no signals."""
        prices = [100.0] * 250
        df = make_ohlcv(prices)
        bundle = compute_indicators("TEST", df)
        assert bundle is not None

        signals = detect_signals(bundle, {})
        # With exact flat prices, RSI might be NaN, so skip
        assert "golden_cross" not in signals
        assert "death_cross" not in signals

    def test_rsi_oversold_detected(self):
        """Fast decline → RSI < 30, prev_rsi was >= 30."""
        prices = [100.0 - i * 0.5 for i in range(250)]  # drops from 100 to -24
        df = make_ohlcv(prices)
        bundle = compute_indicators("TEST", df)
        assert bundle is not None

        signals = detect_signals(bundle, {"prev_rsi": 50.0})
        assert "rsi_oversold" in signals

    def test_rsi_overbought_detected(self):
        """Fast rise → RSI > 70, prev_rsi was <= 70."""
        n = 250
        prices = [100.0 + i * 0.5 for i in range(n)]
        prices[125] = prices[124] - 0.5  # tiny dip avoids NaN
        df = make_ohlcv(prices)
        bundle = compute_indicators("TEST", df)
        assert bundle is not None

        signals = detect_signals(bundle, {"prev_rsi": 50.0})
        assert "rsi_overbought" in signals

    def test_volume_anomaly_strong(self):
        """Last volume 3x the 20-day average."""
        prices = [100.0] * 250
        volumes = [1_000_000] * 249 + [3_000_000]  # spike on last day
        df = make_ohlcv(prices, volumes=volumes)
        bundle = compute_indicators("TEST", df)
        assert bundle is not None
        # Override volume ratio to guarantee trigger
        bundle.volume = 3_000_000
        bundle.avg_volume_20d = 1_000_000

        signals = detect_signals(bundle, {})
        assert "volume_anomaly_strong" in signals

    def test_volume_below_strong_does_not_fire(self):
        """1.7x volume is below the strong (2x) threshold — the moderate tier was
        removed, so no volume signal fires."""
        prices = [100.0] * 250
        df = make_ohlcv(prices)
        bundle = compute_indicators("TEST", df)
        assert bundle is not None
        bundle.volume = 1_700_000
        bundle.avg_volume_20d = 1_000_000

        signals = detect_signals(bundle, {})
        assert "volume_anomaly_moderate" not in signals
        assert "volume_anomaly_strong" not in signals

    def test_atr_spike(self):
        """ATR 2x the 20-day average ATR."""
        prices = [100.0] * 250
        df = make_ohlcv(prices)
        bundle = compute_indicators("TEST", df)
        assert bundle is not None
        bundle.atr = 4.0
        bundle.avg_atr_20d = 2.0

        signals = detect_signals(bundle, {})
        assert "atr_spike" in signals

    def test_macd_bullish_cross(self):
        """MACD crosses above signal line."""
        prices = [100.0] * 250
        df = make_ohlcv(prices)
        bundle = compute_indicators("TEST", df)
        assert bundle is not None
        bundle.macd = 0.5
        bundle.macd_signal = 0.3

        signals = detect_signals(bundle, {"prev_macd_above_signal": 0})
        assert "macd_bullish_cross" in signals

    def test_golden_cross_requires_staircase(self):
        """50 > 150 > 200 with rising 200MA + prev state False."""
        prices = [100.0 + i * 0.3 for i in range(250)]
        df = make_ohlcv(prices)
        bundle = compute_indicators("TEST", df)
        assert bundle is not None

        # Override MAs to create the staircase
        bundle.current_price = 110.0
        bundle.sma_50 = 105.0
        bundle.sma_150 = 102.0
        bundle.sma_200 = 100.0
        bundle.sma_200_1m_ago = 98.0  # 200MA rising

        signals = detect_signals(bundle, {"prev_sma_50_above_200": 0})
        assert "golden_cross" in signals

    def test_death_cross(self):
        """50 SMA crosses below 200 SMA."""
        prices = [100.0] * 250
        df = make_ohlcv(prices)
        bundle = compute_indicators("TEST", df)
        assert bundle is not None
        bundle.current_price = 90.0
        bundle.sma_50 = 95.0
        bundle.sma_200 = 100.0

        signals = detect_signals(bundle, {"prev_sma_50_above_200": 1})
        assert "death_cross" in signals


# ---------------------------------------------------------------------------
# Level-based signals fire only on entry (#8)
# ---------------------------------------------------------------------------

class TestLevelSignalTransitions:
    """Bollinger / Volume / ATR must fire on entry into the condition and stay
    quiet while it persists, re-firing only after it resets and re-crosses."""

    def _bundle(self, **over):
        b = IndicatorBundle(ticker="T")
        # neutral defaults that trip nothing
        b.current_price = 100.0
        b.bb_upper = 110.0
        b.bb_lower = 90.0
        b.volume = 1_000_000
        b.avg_volume_20d = 1_000_000
        b.atr = 1.0
        b.avg_atr_20d = 1.0
        for k, v in over.items():
            setattr(b, k, v)
        return b

    # --- Bollinger upper ---
    def test_bollinger_upper_entry_fires(self):
        b = self._bundle(current_price=115.0)  # >= bb_upper 110
        assert "bollinger_upper_breach" in detect_signals(b, {})

    def test_bollinger_upper_persist_silent(self):
        b = self._bundle(current_price=115.0)
        prev = {"prev_bollinger_above_upper": 1}
        assert "bollinger_upper_breach" not in detect_signals(b, prev)

    def test_bollinger_upper_reentry_fires(self):
        b = self._bundle(current_price=115.0)
        prev = {"prev_bollinger_above_upper": 0}  # reset since last breach
        assert "bollinger_upper_breach" in detect_signals(b, prev)

    # --- Bollinger lower ---
    def test_bollinger_lower_entry_then_persist(self):
        b = self._bundle(current_price=85.0)  # <= bb_lower 90
        assert "bollinger_lower_breach" in detect_signals(b, {})
        assert "bollinger_lower_breach" not in detect_signals(
            b, {"prev_bollinger_below_lower": 1}
        )

    # --- Volume ---
    def test_volume_entry_fires_strong_only(self):
        strong = self._bundle(volume=2_500_000)  # 2.5x → fires
        below = self._bundle(volume=1_700_000)  # 1.7x → below 2x, fires nothing
        assert "volume_anomaly_strong" in detect_signals(strong, {})
        assert detect_signals(below, {}) == []

    def test_volume_persist_silent(self):
        b = self._bundle(volume=2_500_000)
        assert detect_signals(b, {"prev_volume_anomaly": 1}) == []

    def test_volume_reentry_fires(self):
        b = self._bundle(volume=2_500_000)
        assert "volume_anomaly_strong" in detect_signals(b, {"prev_volume_anomaly": 0})

    # --- ATR ---
    def test_atr_entry_then_persist_then_reentry(self):
        b = self._bundle(atr=2.0, avg_atr_20d=1.0)  # 2x >= 1.5x
        assert "atr_spike" in detect_signals(b, {})  # entry
        assert "atr_spike" not in detect_signals(b, {"prev_atr_spike": 1})  # persist
        assert "atr_spike" in detect_signals(b, {"prev_atr_spike": 0})  # re-entry

    def test_compute_level_states_matches_thresholds(self):
        from watchy.indicators import compute_level_states
        b = self._bundle(current_price=115.0, volume=2_500_000, atr=2.0, avg_atr_20d=1.0)
        s = compute_level_states(b)
        assert s["prev_bollinger_above_upper"] == 1
        assert s["prev_bollinger_below_lower"] == 0
        assert s["prev_volume_anomaly"] == 1
        assert s["prev_atr_spike"] == 1


# ---------------------------------------------------------------------------
# Regression: crossover detection through a real SQLite round-trip (#13)
# ---------------------------------------------------------------------------

class TestCrossoverStateRoundTrip:
    """The bug: prev-state ints from SQLite were compared with `is False`/`is True`,
    which is always False in CPython — so crossovers never fired in production.
    Synthetic tests passed Python bools, which hid it. These tests persist real
    state and read it back so the round-trip int is what detect_signals sees.
    """

    def _store(self, tmp_path):
        from watchy.state import StateStore
        return StateStore(str(tmp_path / "state.db"))

    def test_golden_cross_fires_after_state_roundtrip(self, tmp_path):
        store = self._store(tmp_path)
        store.save_ticker_state("TEST", prev_sma_50_above_200=0)
        prev = store.get_ticker_state("TEST")
        assert prev["prev_sma_50_above_200"] == 0  # read back as int, not bool

        bundle = IndicatorBundle(ticker="TEST")
        bundle.current_price = 110.0
        bundle.sma_50 = 105.0
        bundle.sma_150 = 102.0
        bundle.sma_200 = 100.0
        bundle.sma_200_1m_ago = 98.0

        assert "golden_cross" in detect_signals(bundle, prev)
        store.close()

    def test_death_cross_fires_after_state_roundtrip(self, tmp_path):
        store = self._store(tmp_path)
        store.save_ticker_state("TEST", prev_sma_50_above_200=1)
        prev = store.get_ticker_state("TEST")
        assert prev["prev_sma_50_above_200"] == 1

        bundle = IndicatorBundle(ticker="TEST")
        bundle.current_price = 90.0
        bundle.sma_50 = 95.0
        bundle.sma_150 = 97.0
        bundle.sma_200 = 100.0

        assert "death_cross" in detect_signals(bundle, prev)
        store.close()

    def test_macd_bullish_cross_fires_after_state_roundtrip(self, tmp_path):
        store = self._store(tmp_path)
        store.save_ticker_state("TEST", prev_macd_above_signal=0)
        prev = store.get_ticker_state("TEST")
        assert prev["prev_macd_above_signal"] == 0

        bundle = IndicatorBundle(ticker="TEST")
        bundle.current_price = 100.0
        bundle.macd = 0.5
        bundle.macd_signal = 0.3

        assert "macd_bullish_cross" in detect_signals(bundle, prev)
        store.close()

    def test_no_false_fire_on_first_scan_none_state(self, tmp_path):
        """A brand-new ticker (no saved state → None) must not fire a cross."""
        store = self._store(tmp_path)
        prev = store.get_ticker_state("NEW")  # {} → .get returns None
        assert prev == {}

        bundle = IndicatorBundle(ticker="NEW")
        bundle.current_price = 110.0
        bundle.sma_50 = 105.0
        bundle.sma_150 = 102.0
        bundle.sma_200 = 100.0
        bundle.sma_200_1m_ago = 98.0

        signals = detect_signals(bundle, prev)
        assert "golden_cross" not in signals
        assert "death_cross" not in signals
        store.close()


# ---------------------------------------------------------------------------
# History fetch: yfinance-cache layer with robust fallback (#2)
# ---------------------------------------------------------------------------

class TestHistoryCacheFallback:
    def _df(self):
        return make_ohlcv([100.0] * 5)

    def test_uses_cache_when_available(self):
        df = self._df()
        yfc = MagicMock()
        yfc.Ticker.return_value.history.return_value = df
        yf = MagicMock()

        out = _history_via_cache_or_direct("AAPL", yf, yfc)
        assert out is df
        yfc.Ticker.assert_called_once_with("AAPL")
        yf.Ticker.assert_not_called()  # never touched plain yfinance

    def test_cache_call_bounds_staleness_with_max_age(self):
        """yfc must be called with an explicit small max_age, not its 12h default,
        so the intraday forming bar stays fresh (#2 staleness guard)."""
        from watchy.indicators import _CACHE_MAX_AGE

        yfc = MagicMock()
        yfc.Ticker.return_value.history.return_value = self._df()
        yf = MagicMock()

        _history_via_cache_or_direct("AAPL", yf, yfc)
        _, kwargs = yfc.Ticker.return_value.history.call_args
        assert kwargs.get("max_age") == _CACHE_MAX_AGE
        assert _CACHE_MAX_AGE < pd.Timedelta(hours=1)

    def test_falls_back_to_yfinance_when_cache_absent(self):
        df = self._df()
        yf = MagicMock()
        yf.Ticker.return_value.history.return_value = df

        out = _history_via_cache_or_direct("AAPL", yf, None)
        assert out is df
        yf.Ticker.assert_called_once_with("AAPL")

    def test_cache_structural_error_degrades_to_yfinance(self):
        """A non-rate-limit yfc failure (e.g. metadata KeyError) must not crash
        — it degrades to plain yfinance."""
        df = self._df()
        yfc = MagicMock()
        yfc.Ticker.return_value.history.side_effect = KeyError("exchangeTimezoneName")
        yf = MagicMock()
        yf.Ticker.return_value.history.return_value = df

        out = _history_via_cache_or_direct("AAPL", yf, yfc)
        assert out is df
        yf.Ticker.assert_called_once_with("AAPL")

    def test_cache_rate_limit_propagates(self):
        """A 429 from the cache must bubble up to the caller's backoff loop,
        not silently fall back."""
        yfc = MagicMock()
        yfc.Ticker.return_value.history.side_effect = Exception("429 Too Many Requests")
        yf = MagicMock()

        with pytest.raises(Exception, match="429"):
            _history_via_cache_or_direct("AAPL", yf, yfc)
        yf.Ticker.assert_not_called()


class TestStaleSessionBar:
    """yfinance-cache served Friday's bar an hour into Monday's session
    (2026-09-28: NVDA 225.07 vs live 231.39)."""

    SESSION_NOW = datetime(2026, 9, 28, 14, 35, tzinfo=timezone.utc)   # Mon 10:35 ET
    PREMARKET = datetime(2026, 9, 28, 10, 2, tzinfo=timezone.utc)      # Mon 06:02 ET

    def _df(self, last_day: str, close: float = 100.0):
        idx = pd.date_range(end=last_day, periods=5, freq="B", tz="America/New_York")
        return pd.DataFrame({"Open": close, "High": close, "Low": close,
                             "Close": close, "Volume": 1.0}, index=idx)

    def _sources(self, cached, fresh):
        yfc = MagicMock()
        yfc.Ticker.return_value.history.return_value = cached
        yf = MagicMock()
        if isinstance(fresh, Exception):
            yf.Ticker.return_value.history.side_effect = fresh
        else:
            yf.Ticker.return_value.history.return_value = fresh
        return yf, yfc

    def test_missing_bar_detection(self):
        from watchy.indicators import _missing_session_bar
        friday = self._df("2026-09-25")
        assert _missing_session_bar(friday, self.SESSION_NOW)
        assert not _missing_session_bar(friday, self.PREMARKET)          # not open yet
        assert not _missing_session_bar(self._df("2026-09-28"), self.SESSION_NOW)
        saturday = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
        assert not _missing_session_bar(friday, saturday)                # no session

    def test_refetches_when_cache_lacks_todays_bar(self):
        cached, fresh = self._df("2026-09-25", 225.07), self._df("2026-09-28", 231.39)
        yf, yfc = self._sources(cached, fresh)
        out = _history_via_cache_or_direct("NVDA", yf, yfc, now=self.SESSION_NOW)
        assert out is fresh
        yf.Ticker.assert_called_once_with("NVDA")

    def test_no_refetch_before_the_open(self):
        cached = self._df("2026-09-25")
        yf, yfc = self._sources(cached, self._df("2026-09-28"))
        assert _history_via_cache_or_direct("NVDA", yf, yfc, now=self.PREMARKET) is cached
        yf.Ticker.assert_not_called()

    def test_failed_refetch_keeps_cached_frame(self):
        cached = self._df("2026-09-25")
        yf, yfc = self._sources(cached, Exception("429 Too Many Requests"))
        assert _history_via_cache_or_direct("NVDA", yf, yfc, now=self.SESSION_NOW) is cached

    def test_older_refetch_is_ignored(self):
        cached = self._df("2026-09-25")
        yf, yfc = self._sources(cached, self._df("2026-09-24"))
        assert _history_via_cache_or_direct("NVDA", yf, yfc, now=self.SESSION_NOW) is cached


# ---------------------------------------------------------------------------
# SEPA classification
# ---------------------------------------------------------------------------

class TestSEPAStage:
    def test_stage_2_advancing(self):
        bundle = IndicatorBundle(ticker="X")
        bundle.current_price = 110.0
        bundle.sma_50 = 105.0
        bundle.sma_150 = 102.0
        bundle.sma_200 = 100.0
        bundle.sma_200_1m_ago = 98.0  # rising

        assert _classify_sepa_stage(bundle) == 2

    def test_stage_4_declining(self):
        bundle = IndicatorBundle(ticker="X")
        bundle.current_price = 90.0
        bundle.sma_50 = 95.0
        bundle.sma_150 = 98.0
        bundle.sma_200 = 100.0
        bundle.sma_200_1m_ago = 102.0  # falling

        assert _classify_sepa_stage(bundle) == 4

    def test_missing_data_returns_none(self):
        bundle = IndicatorBundle(ticker="X")
        assert _classify_sepa_stage(bundle) is None
