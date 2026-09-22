"""Technical indicator calculations using yfinance + pandas-ta.

No LLM calls. No side effects. Independently testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class IndicatorBundle:
    ticker: str
    timestamp: pd.Timestamp | None = None
    # price
    current_price: float | None = None
    # previous session close (Watchy 2.0: session move for the bearish-shock test)
    prev_close: float | None = None
    # when this bundle was fetched (UTC) — the market-price timestamp shown in
    # logs and messages and checked by the stale-data guard
    fetched_at: datetime | None = None
    # moving averages
    sma_50: float | None = None
    sma_150: float | None = None
    sma_200: float | None = None
    sma_200_1m_ago: float | None = None  # for slope direction
    # RSI
    rsi: float | None = None
    # MACD
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    # Bollinger Bands
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    # ATR
    atr: float | None = None
    avg_atr_20d: float | None = None
    # Volume
    volume: float | None = None
    avg_volume_20d: float | None = None
    # SEPA stage classification
    sepa_stage: int | None = None  # 1=Basing, 2=Advancing, 3=Topping, 4=Declining
    # raw data for debugging
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def compute_indicators(
    ticker: str,
    history: pd.DataFrame | None = None,
) -> IndicatorBundle | None:
    """Compute all technical indicators for a ticker.

    Args:
        ticker: The ticker symbol.
        history: Optional pre-fetched OHLCV DataFrame (columns: Open, High, Low,
                 Close, Volume). If None, data is fetched from yfinance.

    Returns:
        IndicatorBundle with all computed values, or None if data unavailable.
    """
    try:
        df = history if history is not None else _fetch_history(ticker)
    except Exception:
        logger.exception("Failed to fetch data for %s", ticker)
        return None

    if df is None or df.empty or "Close" not in df.columns:
        logger.warning("No price data for %s", ticker)
        return None

    close: pd.Series = df["Close"]
    if len(close) < 200:
        logger.warning("Insufficient history for %s: %d rows", ticker, len(close))
        return None

    bundle = IndicatorBundle(ticker=ticker)
    bundle.timestamp = df.index[-1] if hasattr(df.index[-1], "isoformat") else None
    bundle.current_price = float(close.iloc[-1])
    bundle.prev_close = float(close.iloc[-2])
    bundle.fetched_at = datetime.now(timezone.utc)

    # moving averages
    bundle.sma_50 = float(close.rolling(50).mean().iloc[-1])
    bundle.sma_150 = float(close.rolling(150).mean().iloc[-1])
    bundle.sma_200 = float(close.rolling(200).mean().iloc[-1])
    if len(close) >= 220:
        bundle.sma_200_1m_ago = float(close.rolling(200).mean().iloc[-21])

    # RSI
    bundle.rsi = _compute_rsi(close)

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    bundle.macd = float(macd_line.iloc[-1])
    bundle.macd_signal = float(signal_line.iloc[-1])
    bundle.macd_histogram = float(macd_line.iloc[-1] - signal_line.iloc[-1])

    # Bollinger Bands (20-period, 2 std)
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bundle.bb_middle = float(bb_mid.iloc[-1])
    bundle.bb_upper = float(bb_mid.iloc[-1] + 2 * bb_std.iloc[-1])
    bundle.bb_lower = float(bb_mid.iloc[-1] - 2 * bb_std.iloc[-1])

    # ATR (14-period)
    bundle.atr = _compute_atr(df)
    bundle.avg_atr_20d = float(
        pd.Series([_compute_atr(df, offset=i) for i in range(20)]).mean()
        if len(df) >= 34 else bundle.atr
    )

    # Volume
    if "Volume" in df.columns:
        bundle.volume = float(df["Volume"].iloc[-1])
        bundle.avg_volume_20d = float(df["Volume"].rolling(20).mean().iloc[-1])

    # SEPA stage classification
    bundle.sepa_stage = _classify_sepa_stage(bundle)

    return bundle


def detect_signals(
    bundle: IndicatorBundle,
    prev_state: dict,
) -> list[str]:
    """Detect which signals have fired given current indicators and previous state.

    Returns a list of signal type strings. Empty list means no signals.
    """
    signals: list[str] = []
    price = bundle.current_price
    if price is None:
        return signals

    # --- Golden Cross: 50 SMA crosses above 200 SMA + staircase forming ---
    sma50 = bundle.sma_50
    sma150 = bundle.sma_150
    sma200 = bundle.sma_200
    if sma50 and sma150 and sma200:
        prev_above = prev_state.get("prev_sma_50_above_200")
        now_above = sma50 > sma200
        # full staircase check: price > 50 > 150 > 200
        staircase = price > sma50 > sma150 > sma200
        # 200MA trending up
        sma200_rising = (
            bundle.sma_200_1m_ago is not None
            and sma200 > bundle.sma_200_1m_ago
        )
        # State is read back from SQLite as int (0/1), so compare by value,
        # not identity. `None` (no prior state) stays inert: a new ticker's
        # first scan never false-fires a cross.
        if (
            prev_above == 0
            and now_above
            and staircase
            and sma200_rising
        ):
            signals.append("golden_cross")

        # --- Death Cross: 50 SMA crosses below 200 SMA ---
        if prev_above == 1 and not now_above:
            signals.append("death_cross")

    # --- RSI extreme ---
    rsi = bundle.rsi
    if rsi is not None:
        prev_rsi = prev_state.get("prev_rsi")
        if rsi < 30 and (prev_rsi is None or prev_rsi >= 30):
            signals.append("rsi_oversold")
        elif rsi > 70 and (prev_rsi is None or prev_rsi <= 70):
            signals.append("rsi_overbought")

    # --- MACD crossover ---
    macd = bundle.macd
    macd_sig = bundle.macd_signal
    if macd is not None and macd_sig is not None:
        prev_above = prev_state.get("prev_macd_above_signal")
        now_above = macd > macd_sig
        if prev_above == 0 and now_above:
            signals.append("macd_bullish_cross")
        elif prev_above == 1 and not now_above:
            signals.append("macd_bearish_cross")

    # --- Level-based signals: fire only on ENTRY into the condition (#8) ---
    # These hold for as long as price/volume/ATR stays past the threshold. Firing
    # every scan (or every cooldown cycle) spams notifications, so we fire once on
    # the transition from "not in condition" to "in condition" and stay quiet until
    # it resets and re-crosses. `now[...]` is this scan's state; prev_state is the
    # persisted state from last scan (int 0/1 or None → both falsy; truthy compare,
    # never `is`, per #13).
    now = compute_level_states(bundle)

    if bundle.bb_upper and bundle.bb_lower:
        if now["prev_bollinger_above_upper"] and not prev_state.get("prev_bollinger_above_upper"):
            signals.append("bollinger_upper_breach")
        elif now["prev_bollinger_below_lower"] and not prev_state.get("prev_bollinger_below_lower"):
            signals.append("bollinger_lower_breach")

    # Volume anomaly — fire on entry into the strong (≥2x) zone. The weaker
    # moderate (≥1.5x) tier was removed as low-signal noise: a 1.5x blip is
    # common and cost an LLM call per fire for little actionable value.
    if now["prev_volume_anomaly"] and not prev_state.get("prev_volume_anomaly"):
        signals.append("volume_anomaly_strong")

    # ATR spike — fire on entry.
    if now["prev_atr_spike"] and not prev_state.get("prev_atr_spike"):
        signals.append("atr_spike")

    return signals


def compute_level_states(bundle: IndicatorBundle) -> dict[str, int]:
    """Current on/off state of each level-based signal condition (#8).

    Returned keys match the persisted `prev_*` state columns: in detect_signals
    these represent *this* scan's state (compared against the stored prev), and in
    tier1._update_state the same dict is written back as next scan's prev.
    """
    price = bundle.current_price
    above_upper = int(
        price is not None and bundle.bb_upper is not None and price >= bundle.bb_upper
    )
    below_lower = int(
        price is not None and bundle.bb_lower is not None and price <= bundle.bb_lower
    )
    vol_anomaly = 0
    if bundle.volume and bundle.avg_volume_20d and bundle.avg_volume_20d > 0:
        vol_anomaly = int(bundle.volume / bundle.avg_volume_20d >= 2.0)
    atr_spike = 0
    if bundle.atr and bundle.avg_atr_20d and bundle.avg_atr_20d > 0:
        atr_spike = int(bundle.atr >= 1.5 * bundle.avg_atr_20d)
    return {
        "prev_bollinger_above_upper": above_upper,
        "prev_bollinger_below_lower": below_lower,
        "prev_volume_anomaly": vol_anomaly,
        "prev_atr_spike": atr_spike,
    }


# How stale the cache's most-recent bar may be before yfinance-cache refetches
# it. yfc's default for a 1d interval is 12h — far too stale for an intraday
# "current price" scanner. A small value makes yfc refetch only the forming bar
# (cheap delta, not the full year) so Tier 1 sees a near-live price, while yfc's
# market-calendar awareness still avoids pointless refetches when the market is
# closed (a finalized bar won't change).
_CACHE_MAX_AGE = pd.Timedelta(minutes=10)


def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "rate" in msg or "too many" in msg


def _history_via_cache_or_direct(ticker: str, yf, yfc) -> pd.DataFrame | None:
    """Fetch 1y daily history, preferring the on-disk cache (#2).

    `yfinance_cache` only fetches new/outdated bars, cutting redundant Yahoo
    requests. It's verified numerically identical to yfinance
    (scripts/validate_yfc.py). Robustness: a rate-limit error bubbles up to the
    caller's backoff loop, but any *other* yfc failure (e.g. a yfinance/yfc
    metadata incompatibility) degrades to plain yfinance instead of crashing.
    `max_age` bounds how stale the latest bar may be (see _CACHE_MAX_AGE).
    """
    if yfc is not None:
        try:
            return yfc.Ticker(ticker).history(
                period="1y", interval="1d", max_age=_CACHE_MAX_AGE,
            )
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc):
                raise
            logger.warning(
                "yfinance-cache failed for %s (%s); falling back to yfinance",
                ticker, type(exc).__name__,
            )
    return yf.Ticker(ticker).history(period="1y", interval="1d")


def _fetch_history(ticker: str) -> pd.DataFrame | None:
    """Fetch OHLCV data with disk caching and rate-limit awareness."""
    import time

    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed")
        return None

    # Optional disk-cache layer; degrade to plain yfinance if not installed.
    try:
        import yfinance_cache as yfc
    except ImportError:
        yfc = None

    for attempt in range(3):
        try:
            df = _history_via_cache_or_direct(ticker, yf, yfc)
            if df is None or df.empty:
                # download() has no cache equivalent — use plain yfinance.
                df = yf.download(ticker, period="1y", interval="1d", progress=False)
            if df is not None and not df.empty:
                return df
        except Exception as exc:
            if _is_rate_limit(exc):
                wait = (attempt + 1) * 5
                logger.warning(
                    "yfinance rate-limited for %s (attempt %d/3), waiting %ds…",
                    ticker, attempt + 1, wait,
                )
                time.sleep(wait)
            else:
                raise

    logger.error("yfinance failed for %s after 3 retries", ticker)
    return None


def _compute_rsi(close: pd.Series, period: int = 14) -> float | None:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    result = rsi.iloc[-1]
    return float(result) if not pd.isna(result) else None


def _compute_atr(df: pd.DataFrame, period: int = 14, offset: int = 0) -> float:
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    if offset:
        tr = tr.iloc[: len(tr) - offset]
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr) if not pd.isna(atr) else 0.0


def _classify_sepa_stage(bundle: IndicatorBundle) -> int | None:
    """Classify stock into SEPA stage 1-4 based on MA positions.

    Returns:
        1 = Basing, 2 = Advancing, 3 = Topping, 4 = Declining, None = insufficient data
    """
    p = bundle.current_price
    s50 = bundle.sma_50
    s150 = bundle.sma_150
    s200 = bundle.sma_200
    sma200_rising = (
        bundle.sma_200_1m_ago is not None
        and s200 is not None
        and s200 > bundle.sma_200_1m_ago
    )

    if not all([p, s50, s150, s200]):
        return None

    # Stage 2: Advancing — full bullish alignment
    if p > s50 > s150 > s200 and sma200_rising:
        return 2

    # Stage 4: Declining — bearish alignment
    if p < s200 and s50 < s200 and not sma200_rising:
        return 4

    # Stage 3: Topping — price near highs but MAs flattening
    if p > s200 and s50 > s200 and not sma200_rising:
        return 3

    # Stage 1: Basing — price consolidating around 200MA
    return 1
