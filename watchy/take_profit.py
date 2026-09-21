"""Take-profit logic (#28) — mechanical gain-gate + ATR-runway helpers.

Pure, LLM-free functions. This protects unrealized gains on held winners — the
user's #1 pain: a position runs up, the gain isn't banked, and it round-trips.

The mechanical footprint is deliberately tiny. It decides only *when* to wake the
advisor up (unrealized gain crossed the floor) and hands it ground-truth facts —
the price, the ATR "runway" left to the analysts' upside level, and a reachable
sell-limit price. The LLM then sizes the trim (whole shares) and sets the limit.
The gain's magnitude is NOT among those facts: highest-cost-first selling makes
it ratchet upward with every trim at an unchanged price (#30), so it arms the
gate and nothing more.
It is NOT asked to detect the top itself; that judgement is inconsistent (the
same data reads HOLD on one model and ADD on another) and the analysis often
still calls a top "strong". Feeding a mechanical fact sidesteps that. See #28.

The system is advisory-only: it emits "sell N shares at $X" and the user places
the limit order. A pre-set sell-limit above the market is what actually catches
the intraday high, so the daily cadence is enough as long as the limit is set
early — the Tier 1 zone-entry trigger exists to set it promptly (#28).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from watchy.config import TakeProfitConfig, TickerConfig, WatchyConfig
    from watchy.indicators import IndicatorBundle
    from watchy.positions import Position


def effective_floor_pct(tc: "TickerConfig | None", config: "WatchyConfig") -> float:
    """Take-profit floor for a ticker: per-ticker override else the global."""
    if tc is not None and tc.take_profit_floor_gain_pct is not None:
        return tc.take_profit_floor_gain_pct
    return config.take_profit.floor_gain_pct


def is_in_zone(gain_pct: float | None, floor_pct: float) -> bool:
    """True once a held position's unrealized gain has crossed the floor."""
    return gain_pct is not None and gain_pct >= floor_pct


def position_gain_pct(pos: "Position | None") -> float | None:
    """Unrealized gain % for a position, or None when it can't be computed.

    Needs a cost basis (``average_cost``) and a resolved ``unrealized_pnl_pct``
    (filled by positions._derive_pnl). None when not held or no cost basis — the
    take-profit zone can't arm without knowing the gain.
    """
    if pos is None:
        return None
    return pos.unrealized_pnl_pct


# Share counts come back as floats (Schwab's longQuantity - shortQuantity), so
# compare with a tolerance rather than exactly. Real trims are >= 0.1 shares.
_QTY_EPSILON = 1e-6


def was_trimmed(prev_quantity: float | None, quantity: float | None) -> bool:
    """True when shares were sold since the last scan — i.e. a sell-limit filled.

    This re-arms the Tier 1 intraday zone-entry trigger, which is otherwise
    edge-triggered on zone membership alone and would never fire twice (#28).

    Why quantity and not the gain: the account sells highest-cost-first, so each
    trim strips the dearest lot and *raises* the reported gain % on the shares
    that remain, at an unchanged price. The zone flag therefore latches at 1 and
    only clears after a drawdown deep enough to drag the inflated gain back under
    the floor — by which point the winner has already round-tripped, which is the
    exact outcome this feature exists to prevent.

    A quantity drop is the precise event that matters, because the protection in
    this design is the *standing* sell-limit, not the alert: while an order is
    working, an intraday spike fills it without any new advice. You are only
    unprotected in two states — never entered the zone (the normal 0->1 edge
    covers it), and *just filled*, where the order is gone and the flag is
    latched. That second one is this function. A manual sell re-arms it too,
    correctly: you are equally unprotected either way.

    Returns False on a missing baseline (first scan after the migration) and on
    an increase — a stale cached position snapshot reports the pre-trim, larger
    quantity, and must never be read as a fill.
    """
    if prev_quantity is None or quantity is None:
        return False
    return quantity < prev_quantity - _QTY_EPSILON


def bundle_avg_atr(bundle: "IndicatorBundle | None") -> float | None:
    """The ATR to size the trail/limit against — 20d average, else the raw ATR."""
    if bundle is None:
        return None
    return bundle.avg_atr_20d or bundle.atr


def anchor_price(
    pos: "Position | None", bundle: "IndicatorBundle | None"
) -> float | None:
    """The price the sell-limit and ATR runway are anchored to.

    Must come from the *same* feed as the unrealized gain that armed the gate.
    The gain is the broker's live mark (``positions._derive_pnl``); the indicator
    bundle is a separate yfinance quote that can lag it. Mixing them emits a
    limit that silently disagrees with the stated gain — observed 2026-07-27 on
    EMR: broker last $148.72 vs bundle $145.33, a 0.84xATR gap that put the
    suggested sell-limit $3.40 too low. That direction is the dangerous one: a
    low limit fills sooner and banks the winner cheaper than intended, the exact
    error this feature exists to prevent. Falls back to the bundle when the
    position carries no price (manual file source with no quote resolved).
    """
    if pos is not None and pos.current_price:
        return pos.current_price
    return bundle.current_price if bundle is not None else None


def atr_runway(
    price: float | None,
    upside_level: float | None,
    avg_atr: float | None,
) -> float | None:
    """ATRs of room left to the ceiling: (upside_level - price) / ATR.

    "How many typical trading days of movement remain before the cited upside /
    resistance level." None when the inputs are missing (→ runway unknown, the
    caller degrades to a pure ``price + k x ATR`` limit). 0.0 when price is at or
    above the level (already at the ceiling).
    """
    if not price or not avg_atr or avg_atr <= 0 or upside_level is None:
        return None
    if upside_level <= price:
        return 0.0
    return (upside_level - price) / avg_atr


def suggest_limit(price: float | None, avg_atr: float | None, mult: float) -> float | None:
    """A reachable sell-limit at ``price + mult x ATR``; None if inputs missing."""
    if not price or not avg_atr or avg_atr <= 0:
        return None
    return price + mult * avg_atr


# Labelled upside levels the analysts cite. Matches "price target", "target",
# "resistance", "upside" followed (within a short span) by a $-amount. Kept
# conservative on purpose: a miss degrades to a pure ATR limit, a false hit
# would mislead the runway, so we only trust clearly-labelled numbers.
_UPSIDE_RE = re.compile(
    r"(?:price\s+target|target|resistance|upside)[^.\n]{0,40}?"
    r"\$?\s*(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)


def extract_upside_level(analysis_text: str, current_price: float | None) -> float | None:
    """Best-effort nearest upside/resistance level strictly ABOVE current price.

    Scans the analyst digest for clearly-labelled target/resistance prices and
    returns the closest one above the current price (the immediate ceiling for
    runway). None when nothing qualifies — the caller then falls back to a pure
    ``price + k x ATR`` limit rather than a runway-based one (#28). Conservative
    by design: fragile parsing here only ever costs precision, never safety.
    """
    if not analysis_text or not current_price:
        return None
    candidates: list[float] = []
    for m in _UPSIDE_RE.finditer(analysis_text):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        # Only levels above current price are a ceiling; ignore absurd hits
        # (>3x current is almost certainly a mis-parse, not a near-term target).
        if current_price < val <= current_price * 3:
            candidates.append(val)
    return min(candidates) if candidates else None


def _sizing_directive(
    shares: float | None,
    runway: float | None,
    cfg: "TakeProfitConfig",
    limit: float | None = None,
    stretch: float | None = None,
) -> str:
    """The action-space block: which sizes are actually executable (#28).

    Position size decides what the user can place, and getting it wrong emits
    advice they cannot act on. Three tiers:

    * ``< 1`` — a fractional remainder. A sell-LIMIT needs whole shares, so only
      MARKET sells (partial or full) are executable. That forfeits the
      pre-placed limit that normally catches the intraday high, so it only asks
      for action when the runway says price is at the ceiling.
    * ``== 1`` — a partial trim is arithmetically impossible, so the tranche is
      the whole position. It still gets a resting sell-limit: near the price
      when the runway says it's at the ceiling, otherwise at the stretch limit
      (price + stretch_atr_mult x ATR) so it only fills on a spike and a winner
      with room left isn't cashed out at today's price. (Before 2026-09-21 the
      runway case wrote N/A, which left 1-share winners with no exit at all.)
    * ``>= 2`` — the normal case: trim a whole-share tranche at a limit.

    An unknown ``shares`` (None) falls through to the whole-share wording, which
    is the historical behaviour.
    """
    at_ceiling = runway is not None and runway < cfg.runway_near_atr

    if shares is not None and shares < 1:
        act = (
            "The runway above says price is at the ceiling, so asking for the "
            "sale now is justified"
            if at_ceiling
            else "There is still runway above, so prefer holding unless the "
            "analysis argues the top is already in"
        )
        return (
            f"- FRACTIONAL POSITION ({shares:g} shares): this holding is smaller "
            "than one share, so a sell-LIMIT order CANNOT be placed on it — limit "
            "orders require whole shares. Do NOT propose a limit price here. The "
            "only executable actions are a MARKET sell of part or all of the "
            f"fractional position, or holding. {act}. Fill the 'Take-Profit:' line "
            "with a plain market instruction and NO price (e.g. 'market-sell all "
            "0.2 shares' or 'market-sell 0.1 of 0.2 shares'), else write N/A. Note "
            "this forfeits the pre-placed-limit safety net — the user must act by "
            "hand — so only ask for it when it is genuinely warranted."
        )

    if shares is not None and shares < 2:
        if at_ceiling:
            at = f"about ${limit:.2f}" if limit is not None else "roughly"
            return (
                "- SINGLE-SHARE POSITION: exactly 1 share, so a partial trim is "
                "arithmetically impossible — the take-profit tranche is the ENTIRE "
                "position. The runway above says price is at the ceiling, so fill "
                "the 'Take-Profit:' line with 'sell the whole 1-share position at "
                f"<limit>', using {at} the good-day-reachable level (a limit ABOVE "
                "the current price). Write N/A only if you would genuinely rather "
                "hold."
            )
        at = (
            f"about ${stretch:.2f}" if stretch is not None
            else f"the stretch level (price + {cfg.stretch_atr_mult:g}xATR)"
        )
        return (
            "- SINGLE-SHARE POSITION: exactly 1 share, so a partial trim is "
            "arithmetically impossible — the take-profit tranche is the ENTIRE "
            "position. Price is not at the ceiling, so do NOT sell it at today's "
            "price; instead give a resting STRETCH sell-limit that only fills on a "
            "spike: fill the 'Take-Profit:' line with 'sell the whole 1-share "
            f"position at <limit>', using {at}. This keeps the winner running "
            "while still banking the gain if price overshoots. Write N/A only if "
            "the analysis gives a concrete reason that even the stretch level is "
            "too low to sell at."
        )

    return (
        "- WHOLE SHARES ONLY: this position is sized in whole shares and the user "
        "does not want a fractional tranche of it. Your take-profit tranche must be "
        "a whole-share count (sell 1 share / 2 shares / or the whole position); "
        "never propose a fractional sale.\n"
        "- Fill in the 'Take-Profit:' output line with a concrete sell-limit price "
        "AND the whole-share count to sell there (e.g. 'sell 1 share at 192.50'). "
        "This is a limit ABOVE the current price the user will pre-place to catch "
        "an intraday high. Write N/A only if you are genuinely recommending to hold "
        "the entire position with meaningful upside intact."
    )


def build_guidance(
    ticker: str,
    price: float | None,
    avg_atr: float | None,
    upside_level: float | None,
    cfg: "TakeProfitConfig",
    shares: float | None = None,
) -> str:
    """The explicit take-profit directive injected into the advisor prompt (#28).

    Carries the mechanical facts (price, ATR, a reachable sell-limit, and the ATR
    runway when an upside level is known) and forces the advisor to resolve
    take-profit — bank part of the gain, or justify holding with concrete
    remaining upside — rather than staying silent.

    The SIZE of the gain is deliberately not a parameter (#30). Whether the floor
    was crossed is the arming condition and stays in the caller; the magnitude is
    not a decision input, because highest-cost-first selling means each trim
    strips the dearest lot and inflates the reported gain on the shares that
    remain at an unchanged price — it ratchets up with every trim this directive
    recommends. Keeping it out of the signature makes that structural rather than
    a convention. The journal still records it (advisor._take_profit_guidance).

    ``shares`` sizes the action space via _sizing_directive: a fractional or
    single-share position cannot be trimmed the normal way, and telling the
    advisor otherwise produces advice the user cannot place.
    """
    runway = atr_runway(price, upside_level, avg_atr)
    limit = suggest_limit(price, avg_atr, cfg.limit_atr_mult)
    stretch = suggest_limit(price, avg_atr, cfg.stretch_atr_mult)

    lines = [
        "TAKE-PROFIT ZONE ACTIVE (mechanical trigger — ground truth, not an "
        "analyst opinion):",
        "- This position is a HELD WINNER: its unrealized gain has crossed the "
        f"+{cfg.floor_gain_pct:.0f}% take-profit floor. Do not let it fully "
        "round-trip. You must RESOLVE take-profit here — either bank part of the "
        "gain at a concrete limit, or justify holding with specific remaining "
        "upside. Staying silent on it is not an option.",
        "- The SIZE of that gain is deliberately not quoted, and the percentage "
        "in the position block must not drive your answer: this account sells "
        "highest-cost-first, so each trim strips the dearest lot and mechanically "
        "inflates the reported gain on the shares that remain, at an unchanged "
        "price. Size the tranche and set the limit from the price / ATR / runway "
        "facts below ONLY.",
    ]
    if price is not None:
        lines.append(f"- Current price ${price:.2f}.")
    if avg_atr:
        lines.append(
            f"- ATR (typical daily move) ≈ ${avg_atr:.2f}. A 'good-day-reachable' "
            f"sell-limit is about ${limit:.2f} (price + {cfg.limit_atr_mult:g}xATR); "
            f"a stretch limit if you let it run is about ${stretch:.2f} "
            f"(+ {cfg.stretch_atr_mult:g}xATR)."
        )

    if runway is not None and upside_level is not None:
        lines.append(
            f"- ATR RUNWAY to the cited upside ${upside_level:.2f} is "
            f"{runway:.1f} ATRs of room. Interpret it:"
        )
        if runway < cfg.runway_near_atr:
            lines.append(
                f"    RUNWAY IS SMALL (< {cfg.runway_near_atr:g} ATR) — price is "
                "basically at the ceiling. Bank most or all of the gain now; set "
                "the sell-limit close to the current price."
            )
        elif runway > cfg.runway_far_atr:
            lines.append(
                f"    RUNWAY IS LARGE (> {cfg.runway_far_atr:g} ATR) — there is real "
                "room left. Prefer HOLD, or take only a single share at the higher "
                "stretch limit and let the rest run."
            )
        else:
            lines.append(
                "    RUNWAY IS MODERATE — trim one share into strength; set the "
                "sell-limit around the good-day-reachable level above."
            )
    else:
        lines.append(
            "- No clear upside/resistance level was found in the analysis, so ATR "
            "runway is unknown. Read the analysis for any ceiling; if none, set the "
            "sell-limit around the good-day-reachable level above and take a single "
            "share into strength — the floor is already crossed, so banking "
            "something is the default when no ceiling can be identified."
        )

    lines.append(_sizing_directive(shares, runway, cfg, limit=limit, stretch=stretch))
    return "\n".join(lines)
