"""Tests for take-profit core logic (#28) — pure functions, no LLM."""

import re

from watchy.config import TakeProfitConfig, TickerConfig, WatchyConfig
from watchy.indicators import IndicatorBundle
from watchy.positions import Position
from watchy.take_profit import (
    anchor_price,
    atr_runway,
    build_guidance,
    bundle_avg_atr,
    effective_floor_pct,
    extract_upside_level,
    is_in_zone,
    position_gain_pct,
    suggest_limit,
    was_trimmed,
)


class TestEffectiveFloor:
    def test_global_default(self):
        cfg = WatchyConfig(take_profit=TakeProfitConfig(floor_gain_pct=10.0))
        assert effective_floor_pct(TickerConfig(ticker="NVDA"), cfg) == 10.0

    def test_per_ticker_override(self):
        cfg = WatchyConfig(take_profit=TakeProfitConfig(floor_gain_pct=10.0))
        tc = TickerConfig(ticker="NVDA", take_profit_floor_gain_pct=20.0)
        assert effective_floor_pct(tc, cfg) == 20.0

    def test_none_ticker_uses_global(self):
        cfg = WatchyConfig(take_profit=TakeProfitConfig(floor_gain_pct=12.0))
        assert effective_floor_pct(None, cfg) == 12.0


class TestIsInZone:
    def test_crosses_floor(self):
        assert is_in_zone(10.0, 10.0) is True
        assert is_in_zone(15.7, 10.0) is True

    def test_below_floor(self):
        assert is_in_zone(9.9, 10.0) is False

    def test_none_gain_never_in_zone(self):
        assert is_in_zone(None, 10.0) is False

    def test_loss_never_in_zone(self):
        assert is_in_zone(-5.0, 10.0) is False


class TestPositionGainPct:
    def test_reads_derived_pct(self):
        pos = Position(ticker="NVDA", quantity=3, average_cost=100.0)
        pos.unrealized_pnl_pct = 15.7
        assert position_gain_pct(pos) == 15.7

    def test_none_position(self):
        assert position_gain_pct(None) is None

    def test_no_cost_basis_gives_none(self):
        pos = Position(ticker="NVDA", quantity=3, average_cost=100.0)
        # _derive_pnl never ran → pct stays None → zone cannot arm
        assert position_gain_pct(pos) is None


class TestBundleAvgAtr:
    def test_prefers_20d(self):
        b = IndicatorBundle(ticker="X", atr=2.0, avg_atr_20d=5.0)
        assert bundle_avg_atr(b) == 5.0

    def test_falls_back_to_raw_atr(self):
        b = IndicatorBundle(ticker="X", atr=2.0, avg_atr_20d=None)
        assert bundle_avg_atr(b) == 2.0

    def test_none_bundle(self):
        assert bundle_avg_atr(None) is None


class TestAnchorPrice:
    """The limit must be anchored on the same feed the gain came from (#28)."""

    def test_prefers_the_position_mark_over_the_bundle(self):
        # Real 2026-07-27 EMR divergence: broker 148.72 vs yfinance 145.33.
        # Anchoring on the bundle put the sell-limit $3.40 too low.
        pos = Position(ticker="EMR", quantity=1, average_cost=139.0, current_price=148.72)
        b = IndicatorBundle(ticker="EMR", current_price=145.3261)
        assert anchor_price(pos, b) == 148.72

    def test_falls_back_to_bundle_when_position_has_no_price(self):
        pos = Position(ticker="EMR", quantity=1, average_cost=139.0)
        b = IndicatorBundle(ticker="EMR", current_price=145.33)
        assert anchor_price(pos, b) == 145.33

    def test_falls_back_to_bundle_when_position_price_is_zero(self):
        pos = Position(ticker="EMR", quantity=1, average_cost=139.0, current_price=0.0)
        b = IndicatorBundle(ticker="EMR", current_price=145.33)
        assert anchor_price(pos, b) == 145.33

    def test_no_bundle_uses_the_position(self):
        pos = Position(ticker="EMR", quantity=1, average_cost=139.0, current_price=148.72)
        assert anchor_price(pos, None) == 148.72

    def test_neither_gives_none(self):
        assert anchor_price(None, None) is None

    def test_anchor_shifts_the_suggested_limit(self):
        # The bug's user-visible effect: same ATR, two feeds, $3.40 of limit.
        atr = 4.0387
        assert abs(suggest_limit(145.3261, atr, 3.0) - 157.44) < 0.01
        assert abs(suggest_limit(148.72, atr, 3.0) - 160.84) < 0.01


class TestAtrRunway:
    def test_basic(self):
        # (200 - 188) / 5 = 2.4 ATRs of room
        assert abs(atr_runway(188.0, 200.0, 5.0) - 2.4) < 1e-9

    def test_at_or_above_ceiling_is_zero(self):
        assert atr_runway(200.0, 200.0, 5.0) == 0.0
        assert atr_runway(205.0, 200.0, 5.0) == 0.0

    def test_missing_inputs_return_none(self):
        assert atr_runway(None, 200.0, 5.0) is None
        assert atr_runway(188.0, None, 5.0) is None
        assert atr_runway(188.0, 200.0, None) is None
        assert atr_runway(188.0, 200.0, 0.0) is None


class TestSuggestLimit:
    def test_price_plus_mult_atr(self):
        assert suggest_limit(180.0, 5.0, 1.5) == 187.5
        assert suggest_limit(180.0, 5.0, 3.0) == 195.0

    def test_missing_inputs(self):
        assert suggest_limit(None, 5.0, 1.5) is None
        assert suggest_limit(180.0, None, 1.5) is None
        assert suggest_limit(180.0, 0.0, 1.5) is None


class TestExtractUpsideLevel:
    def test_price_target_above_current(self):
        text = "Market Analyst sees a price target of $200 on continued strength."
        assert extract_upside_level(text, 188.0) == 200.0

    def test_resistance_level(self):
        text = "Key resistance at $210 caps the near-term move."
        assert extract_upside_level(text, 188.0) == 210.0

    def test_picks_nearest_above_current(self):
        text = "Targets: resistance $195, then upside target $230."
        # nearest ceiling above current is the immediate one
        assert extract_upside_level(text, 188.0) == 195.0

    def test_ignores_levels_below_current(self):
        text = "Support target at $150 holds; stop below."
        assert extract_upside_level(text, 188.0) is None

    def test_ignores_absurd_hits(self):
        text = "Long-run target $9000 someday."
        assert extract_upside_level(text, 188.0) is None

    def test_no_match_returns_none(self):
        assert extract_upside_level("No levels cited here.", 188.0) is None
        assert extract_upside_level("", 188.0) is None
        assert extract_upside_level("target $200", None) is None


class TestWasTrimmed:
    """A share-count drop = a sell-limit filled → re-arm the trigger (#28)."""

    def test_drop_is_a_fill(self):
        assert was_trimmed(3, 2) is True

    def test_fractional_drop_is_a_fill(self):
        assert was_trimmed(0.2, 0.1) is True

    def test_full_exit_is_a_fill(self):
        assert was_trimmed(1, 0.0) is True

    def test_unchanged_is_not(self):
        assert was_trimmed(3, 3) is False

    def test_increase_is_not(self):
        # A stale cached snapshot serves the pre-trim, larger count — never a fill.
        assert was_trimmed(2, 3) is False

    def test_missing_baseline_is_not(self):
        # First scan after the migration: no prior count to diff against.
        assert was_trimmed(None, 2) is False
        assert was_trimmed(3, None) is False

    def test_float_noise_is_not_a_fill(self):
        assert was_trimmed(3.0, 3.0 - 1e-12) is False


class TestBuildGuidance:
    def _cfg(self):
        return TakeProfitConfig(
            enabled=True, floor_gain_pct=10.0, limit_atr_mult=1.5,
            stretch_atr_mult=3.0, runway_near_atr=1.0, runway_far_atr=2.5,
        )

    def test_small_runway_says_bank_now(self):
        # price 199, ceiling 200, ATR 5 → runway 0.2 ATR (< 1) → at the ceiling
        g = build_guidance("NVDA", 199.0, 5.0, 200.0, self._cfg())
        assert "TAKE-PROFIT ZONE ACTIVE" in g
        assert "RUNWAY IS SMALL" in g
        assert "Take-Profit:" in g  # instructs filling the output line

    def test_large_runway_says_let_it_run(self):
        # price 180, ceiling 220, ATR 5 → runway 8 ATRs (> 2.5) → room to run
        g = build_guidance("NVDA", 180.0, 5.0, 220.0, self._cfg())
        assert "RUNWAY IS LARGE" in g
        assert "stretch limit" in g

    def test_moderate_runway(self):
        # price 188, ceiling 200, ATR 5 → runway 2.4 (between 1 and 2.5)
        g = build_guidance("NVDA", 188.0, 5.0, 200.0, self._cfg())
        assert "RUNWAY IS MODERATE" in g

    def test_unknown_upside_degrades_to_atr_limit(self):
        g = build_guidance("NVDA", 188.0, 5.0, None, self._cfg())
        assert "runway is unknown" in g.lower()
        assert "good-day-reachable" in g

    def test_whole_share_guard_present(self):
        g = build_guidance("NVDA", 199.0, 5.0, 200.0, self._cfg())
        assert "WHOLE SHARES ONLY" in g

    def test_unknown_shares_keeps_whole_share_wording(self):
        # shares=None (the historical call) must not change behaviour.
        g = build_guidance("NVDA", 199.0, 5.0, 200.0, self._cfg())
        assert "WHOLE SHARES ONLY" in g
        assert "SINGLE-SHARE" not in g
        assert "FRACTIONAL POSITION" not in g


class TestGainMagnitudeIsNotADecisionInput:
    """The gain % arms the gate and goes no further (#30).

    HIFO selling makes it ratchet upward with every trim at an unchanged price,
    so it cannot anchor a limit or a tranche size.
    """

    def _cfg(self):
        return TakeProfitConfig(
            enabled=True, floor_gain_pct=10.0, limit_atr_mult=1.5,
            stretch_atr_mult=3.0, runway_near_atr=1.0, runway_far_atr=2.5,
        )

    def test_no_gain_percentage_anywhere_in_the_directive(self):
        # Every branch: at the ceiling, room to run, and no ceiling found.
        for upside in (200.0, 260.0, None):
            for shares in (None, 0.2, 1, 3):
                g = build_guidance("NVDA", 199.0, 5.0, upside, self._cfg(), shares=shares)
                # The floor itself may be quoted; a measured gain never is.
                pcts = set(re.findall(r"[-+]?\d+(?:\.\d+)?%", g)) - {"+10%", "10%"}
                assert not pcts, f"gain magnitude leaked: {pcts}"

    def test_floor_crossing_is_still_stated(self):
        # Dropping the magnitude must not drop the urgency — the user's pain is
        # selling too late, so the directive still demands a resolution.
        g = build_guidance("NVDA", 199.0, 5.0, 200.0, self._cfg())
        assert "+10% take-profit floor" in g
        assert "RESOLVE take-profit" in g
        assert "not an option" in g

    def test_explains_why_the_position_block_percentage_is_untrustworthy(self):
        # The number is still visible in the injected position block, so the
        # directive has to neutralise it explicitly or the LLM will use it.
        g = build_guidance("NVDA", 199.0, 5.0, 200.0, self._cfg())
        assert "highest-cost-first" in g
        assert "position block must not drive your answer" in g

    def test_no_ceiling_fallback_does_not_reason_from_the_gain(self):
        g = build_guidance("NVDA", 188.0, 5.0, None, self._cfg())
        assert "given the gain" not in g
        assert "runway is unknown" in g.lower()
        assert "floor is already crossed" in g


class TestSizingDirective:
    """Position size decides which actions are actually placeable (#28)."""

    def _cfg(self):
        return TakeProfitConfig(
            enabled=True, floor_gain_pct=10.0, limit_atr_mult=1.5,
            stretch_atr_mult=3.0, runway_near_atr=1.0, runway_far_atr=2.5,
        )

    def test_two_shares_uses_normal_whole_share_trim(self):
        g = build_guidance("NVDA", 199.0, 5.0, 200.0, self._cfg(), shares=3)
        assert "WHOLE SHARES ONLY" in g
        assert "sell 1 share at 192.50" in g  # the worked example stays

    def test_single_share_at_ceiling_allows_full_exit(self):
        # runway 0.2 ATR (< runway_near_atr 1.0) → price is at the ceiling
        g = build_guidance("APH", 199.0, 5.0, 200.0, self._cfg(), shares=1)
        assert "SINGLE-SHARE POSITION" in g
        assert "sell the whole 1-share position" in g
        assert "WHOLE SHARES ONLY" not in g

    def test_single_share_with_runway_gets_stretch_exit(self):
        # runway 8 ATRs → real room left. This is the EMR 2026-08-07 case
        # (+12.9%, 1 share, upside far away) that used to write N/A and leave
        # the winner with no exit. Now: a full-exit limit at the stretch level
        # (180 + 3x5 = 195), not at today's price.
        g = build_guidance("EMR", 180.0, 5.0, 220.0, self._cfg(), shares=1)
        assert "SINGLE-SHARE POSITION" in g
        assert "sell the whole 1-share position" in g
        assert "STRETCH sell-limit" in g
        assert "$195.00" in g.split("SINGLE-SHARE POSITION")[1]
        assert "write N/A on" not in g

    def test_single_share_at_ceiling_uses_reachable_limit(self):
        # runway 0.2 ATR → at the ceiling → limit at 199 + 1.5x5 = 206.50.
        g = build_guidance("APH", 199.0, 5.0, 200.0, self._cfg(), shares=1)
        assert "$206.50" in g.split("SINGLE-SHARE POSITION")[1]
        assert "STRETCH" not in g

    def test_unknown_runway_single_share_is_not_treated_as_ceiling(self):
        # No upside level → runway None → stretch exit, not a near-price sale.
        g = build_guidance("EMR", 180.0, 5.0, None, self._cfg(), shares=1)
        assert "SINGLE-SHARE POSITION" in g
        assert "STRETCH sell-limit" in g
        assert "$195.00" in g.split("SINGLE-SHARE POSITION")[1]

    def test_single_share_without_atr_still_asks_for_exit(self):
        g = build_guidance("EMR", 180.0, None, None, self._cfg(), shares=1)
        assert "sell the whole 1-share position" in g
        assert "price + 3xATR" in g

    def test_fractional_position_forbids_a_limit_price(self):
        # ASML 0.2 shares: a sell-limit needs whole shares, so market only.
        g = build_guidance("ASML", 199.0, 5.0, 200.0, self._cfg(), shares=0.2)
        assert "FRACTIONAL POSITION (0.2 shares)" in g
        assert "CANNOT be placed" in g
        assert "Do NOT propose a limit price" in g
        assert "market-sell" in g
        assert "WHOLE SHARES ONLY" not in g

    def test_fractional_partial_sell_is_offered(self):
        g = build_guidance("ASML", 199.0, 5.0, 200.0, self._cfg(), shares=0.2)
        assert "part or all of the" in g          # trim OR full exit
        assert "market-sell 0.1 of 0.2 shares" in g

    def test_fractional_with_runway_prefers_holding(self):
        g = build_guidance("ASML", 180.0, 5.0, 220.0, self._cfg(), shares=0.2)
        assert "prefer holding" in g

    def test_no_longer_claims_user_never_trades_fractional(self):
        # That premise became false (a real 0.2-share ASML position), and it
        # contradicted "or the whole position" for a fractional holding.
        for shares in (None, 0.2, 1, 3):
            g = build_guidance("X", 199.0, 5.0, 200.0, self._cfg(), shares=shares)
            assert "does not trade fractional shares" not in g
