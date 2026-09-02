"""Tests for the Tier 1 scan.

Tier 1 is an unconditional safety net (the #5 price-proximity skip was removed):
during market hours it always reads state and runs signal detection, regardless of
how far price sits from any configured target. Cost is controlled by which signals
fire the LLM pipeline and by cooldowns, not by gating the cheap scan.
"""

from unittest.mock import MagicMock, patch

from watchy.config import TakeProfitConfig, TickerConfig, WatchyConfig
from watchy.indicators import IndicatorBundle
from watchy.positions import Position
from watchy.tier1 import scan_ticker


def _bundle(price: float) -> IndicatorBundle:
    b = IndicatorBundle(ticker="AAPL")
    b.current_price = price
    b.avg_atr_20d = 5.0
    return b


def _config(**ticker_kwargs) -> WatchyConfig:
    return WatchyConfig(watchlist=[TickerConfig(ticker="AAPL", **ticker_kwargs)])


class TestScanAlwaysRuns:
    def test_scan_runs_even_when_price_far_from_target(self):
        # A configured target no longer gates Tier 1 — the scan always proceeds.
        config = _config(target_price=180.0)
        store, notifier = MagicMock(), MagicMock()
        store.get_ticker_state.return_value = {}
        with patch("watchy.tier1.compute_indicators", return_value=_bundle(210.0)), \
             patch("watchy.tier1.detect_signals", return_value=[]):
            out = scan_ticker("AAPL", config, store, notifier)
        assert out == []
        store.get_ticker_state.assert_called_once()  # never skipped on proximity

    def test_scan_normal_without_target(self):
        config = _config()  # no target at all → still scans
        store, notifier = MagicMock(), MagicMock()
        store.get_ticker_state.return_value = {}
        with patch("watchy.tier1.compute_indicators", return_value=_bundle(210.0)), \
             patch("watchy.tier1.detect_signals", return_value=[]):
            scan_ticker("AAPL", config, store, notifier)
        store.get_ticker_state.assert_called_once()

    def test_scan_skips_only_when_no_indicator_data(self):
        config = _config(target_price=180.0)
        store, notifier = MagicMock(), MagicMock()
        with patch("watchy.tier1.compute_indicators", return_value=None):
            out = scan_ticker("AAPL", config, store, notifier)
        assert out == []
        store.get_ticker_state.assert_not_called()  # no data → nothing to scan


class TestRescanCap:
    """Tier 1 daily rescan cap (#23) — limits paid pipelines per ticker per UTC day."""

    def _fire(self, config, runs_today):
        """Drive a single signal trip; return (store, notifier)."""
        store, notifier = MagicMock(), MagicMock()
        store.get_ticker_state.return_value = {}
        store.is_in_cooldown.return_value = False
        store.start_run.return_value = 1
        store.count_tier1_runs_today.return_value = runs_today
        with patch("watchy.tier1.compute_indicators", return_value=_bundle(210.0)), \
             patch("watchy.tier1.detect_signals", return_value=["rsi_oversold"]), \
             patch("watchy.tier1.run_pipeline", return_value={"summary": "ok"}) as run, \
             patch("watchy.tier1.get_advice", return_value={}), \
             patch("watchy.tier1.get_position_source"), \
             patch("watchy.tier1.monitor_schwab"):
            scan_ticker("AAPL", config, store, notifier)
        return store, notifier, run

    def test_capped_skips_pipeline(self):
        config = _config(max_tier1_pipelines_per_day=2)
        store, notifier, run = self._fire(config, runs_today=2)
        run.assert_not_called()                    # no paid pipeline
        notifier.rescan_capped.assert_called_once()
        notifier.signal_fired.assert_not_called()
        store.log_signal.assert_called_once()      # breach still recorded (cooldown intact)

    def test_under_cap_runs_pipeline(self):
        config = _config(max_tier1_pipelines_per_day=2)
        store, notifier, run = self._fire(config, runs_today=1)
        run.assert_called_once()
        notifier.rescan_capped.assert_not_called()
        notifier.signal_fired.assert_called_once()

    def test_no_cap_when_unset(self):
        config = _config()  # global None, no per-ticker → never capped
        config.max_tier1_pipelines_per_day = None
        store, notifier, run = self._fire(config, runs_today=99)
        run.assert_called_once()
        notifier.rescan_capped.assert_not_called()

    def test_per_ticker_override_beats_global(self):
        config = _config(max_tier1_pipelines_per_day=5)
        config.max_tier1_pipelines_per_day = 1  # global=1, per-ticker=5 → per-ticker wins
        store, notifier, run = self._fire(config, runs_today=3)
        run.assert_called_once()                   # 3 < 5, still runs
        notifier.rescan_capped.assert_not_called()


class TestTakeProfitZone:
    """Tier 1 take-profit zone-entry trigger (#28)."""

    def _config(self, enabled=True, floor=10.0):
        return WatchyConfig(
            watchlist=[TickerConfig(ticker="AAPL")],
            take_profit=TakeProfitConfig(enabled=enabled, floor_gain_pct=floor),
        )

    def _held(self, gain_pct, quantity=3):
        p = Position(
            ticker="AAPL", quantity=quantity, average_cost=100.0, current_price=189.0
        )
        p.unrealized_pnl_pct = gain_pct
        return p

    def _scan(self, config, *, prev_zone, gain, in_cooldown=False, digest=None,
              prev_qty=None, qty=3):
        """Drive a signal-free scan with the take-profit gate; return (store, notifier)."""
        store, notifier = MagicMock(), MagicMock()
        state = {}
        if prev_zone is not None:
            state["prev_take_profit_zone"] = prev_zone
        if prev_qty is not None:
            state["prev_quantity"] = prev_qty
        store.get_ticker_state.return_value = state
        store.is_in_cooldown.return_value = in_cooldown
        src = MagicMock()
        src.get_position.return_value = None if gain is None else self._held(gain, qty)
        src.format_position_context.return_value = "Current position in AAPL"
        with patch("watchy.tier1.compute_indicators", return_value=_bundle(189.0)), \
             patch("watchy.tier1.detect_signals", return_value=[]), \
             patch("watchy.tier1.get_position_source", return_value=src), \
             patch("watchy.tier1.load_digest", return_value=digest), \
             patch("watchy.tier1.get_advice", return_value={"decision": "TRIM",
                   "take_profit": "sell 1 share at 200"}) as adv:
            scan_ticker("AAPL", config, store, notifier)
        return store, notifier, adv

    def test_fires_on_zone_entry(self):
        store, notifier, adv = self._scan(self._config(), prev_zone=None, gain=15.7)
        adv.assert_called_once()
        assert adv.call_args.kwargs["indicator_bundle"] is not None
        notifier.take_profit_alert.assert_called_once()
        store.log_signal.assert_called_once_with("AAPL", "take_profit_zone",
                                                 store.log_signal.call_args.args[2])
        # zone membership persisted as 1
        saved = store.save_ticker_state.call_args.kwargs
        assert saved["prev_take_profit_zone"] == 1

    def test_no_fire_when_already_in_zone(self):
        store, notifier, adv = self._scan(self._config(), prev_zone=1, gain=15.7)
        adv.assert_not_called()
        notifier.take_profit_alert.assert_not_called()
        assert store.save_ticker_state.call_args.kwargs["prev_take_profit_zone"] == 1

    def test_no_fire_below_floor(self):
        store, notifier, adv = self._scan(self._config(floor=20.0), prev_zone=None, gain=15.7)
        adv.assert_not_called()
        notifier.take_profit_alert.assert_not_called()
        assert store.save_ticker_state.call_args.kwargs["prev_take_profit_zone"] == 0

    def test_no_fire_when_not_held(self):
        store, notifier, adv = self._scan(self._config(), prev_zone=None, gain=None)
        adv.assert_not_called()
        assert store.save_ticker_state.call_args.kwargs["prev_take_profit_zone"] == 0

    def test_no_fire_in_cooldown(self):
        store, notifier, adv = self._scan(
            self._config(), prev_zone=None, gain=15.7, in_cooldown=True
        )
        adv.assert_not_called()
        notifier.take_profit_alert.assert_not_called()

    def test_disabled_does_nothing(self):
        store, notifier, adv = self._scan(
            self._config(enabled=False), prev_zone=None, gain=15.7
        )
        adv.assert_not_called()
        # gate off → zone flag and share count untouched (None), not written
        saved = store.save_ticker_state.call_args.kwargs
        assert "prev_take_profit_zone" not in saved
        assert "prev_quantity" not in saved


class TestTakeProfitRearmOnFill:
    """A fill re-arms the zone-entry trigger (#28 follow-up).

    Under highest-cost-first lot accounting each trim strips the dearest lot and
    raises the reported gain on the shares left, so the zone flag latches at 1
    and the edge detector never fires twice. That leaves the position with no
    standing sell-limit and no intraday trigger for a full session. A drop in
    share count is the fill, so it re-arms.
    """

    _config = TestTakeProfitZone._config
    _held = TestTakeProfitZone._held
    _scan = TestTakeProfitZone._scan

    def test_fill_refires_while_still_in_zone(self):
        # Sold 1 of 3 shares; gain reads HIGHER afterwards (HIFO), so the zone
        # flag is still 1 — the fill is what must re-arm the trigger.
        store, notifier, adv = self._scan(
            self._config(), prev_zone=1, gain=24.3, prev_qty=3, qty=2
        )
        adv.assert_called_once()
        notifier.take_profit_alert.assert_called_once()
        assert store.save_ticker_state.call_args.kwargs["prev_quantity"] == 2

    def test_fractional_fill_refires(self):
        store, notifier, adv = self._scan(
            self._config(), prev_zone=1, gain=15.7, prev_qty=0.2, qty=0.1
        )
        adv.assert_called_once()

    def test_unchanged_quantity_stays_silent(self):
        store, notifier, adv = self._scan(
            self._config(), prev_zone=1, gain=15.7, prev_qty=3, qty=3
        )
        adv.assert_not_called()
        notifier.take_profit_alert.assert_not_called()

    def test_stale_snapshot_reporting_more_shares_is_not_a_fill(self):
        # A cached (pre-trim) position snapshot reports the larger, older count.
        # An increase must never read as a fill.
        store, notifier, adv = self._scan(
            self._config(), prev_zone=1, gain=15.7, prev_qty=2, qty=3
        )
        adv.assert_not_called()

    def test_no_baseline_does_not_refire(self):
        # First scan after the migration: prev_quantity is NULL → no comparison.
        store, notifier, adv = self._scan(
            self._config(), prev_zone=1, gain=15.7, prev_qty=None, qty=2
        )
        adv.assert_not_called()

    def test_fill_still_respects_cooldown(self):
        store, notifier, adv = self._scan(
            self._config(), prev_zone=1, gain=24.3, prev_qty=3, qty=2, in_cooldown=True
        )
        adv.assert_not_called()
        assert store.save_ticker_state.call_args.kwargs["prev_quantity"] == 2

    def test_quantity_persisted_below_floor(self):
        # Tracked out of the zone too, so a sell made below the floor isn't
        # replayed as a fresh fill on the next entry into it.
        store, notifier, adv = self._scan(
            self._config(floor=20.0), prev_zone=0, gain=15.7, prev_qty=3, qty=2
        )
        adv.assert_not_called()
        saved = store.save_ticker_state.call_args.kwargs
        assert saved["prev_take_profit_zone"] == 0
        assert saved["prev_quantity"] == 2

    def test_exit_records_zero_shares(self):
        store, notifier, adv = self._scan(
            self._config(), prev_zone=1, gain=None, prev_qty=3
        )
        adv.assert_not_called()
        assert store.save_ticker_state.call_args.kwargs["prev_quantity"] == 0.0


class TestAdviceLogWiring:
    """#31: both Tier 1 advisor call sites label themselves distinctly.

    The zone-entry re-advise is a different decision context from a signal-driven
    pipeline run, and forward-return scoring has to be able to separate them.
    """

    def test_pipeline_run_labels_itself_tier1(self):
        config = _config(max_tier1_pipelines_per_day=2)
        store, notifier = MagicMock(), MagicMock()
        store.get_ticker_state.return_value = {}
        store.is_in_cooldown.return_value = False
        store.start_run.return_value = 1
        store.count_tier1_runs_today.return_value = 0
        with patch("watchy.tier1.compute_indicators", return_value=_bundle(210.0)), \
             patch("watchy.tier1.detect_signals", return_value=["rsi_oversold"]), \
             patch("watchy.tier1.run_pipeline", return_value={"summary": "ok"}), \
             patch("watchy.tier1.get_advice", return_value={}) as adv, \
             patch("watchy.tier1.get_position_source"), \
             patch("watchy.tier1.monitor_schwab"):
            scan_ticker("AAPL", config, store, notifier)

        assert adv.call_args.kwargs["store"] is store
        assert adv.call_args.kwargs["source"] == "tier1"

    def test_zone_entry_labels_itself_take_profit_zone(self):
        zone = TestTakeProfitZone()
        store, notifier, adv = zone._scan(zone._config(), prev_zone=None, gain=15.7)
        assert adv.call_args.kwargs["store"] is store
        assert adv.call_args.kwargs["source"] == "take_profit_zone"
