"""Watchy 2.0 Tier 1 trigger router — one pure function, one route per scan.

Given the ticker's position state, the current weekly plan, the scan's
triggers (technical signals, plan transitions, the take-profit gate), price
facts, cooldown and budget state, ``route()`` returns exactly one
highest-priority route and keeps every contributing reason::

    TAKE_PROFIT or invalidation > TRIGGERED_RISK > FAST_RECHECK > NOTIFY_ONLY > NO_ACTION

Paid routes (FAST_RECHECK / TRIGGERED_RISK) are then downgraded to the
deterministic NOTIFY_ONLY reminder when triggered analysis is disabled
(shadow mode), the ticker is not on the allow-list, Fast Recheck inputs are
missing, or a budget is exhausted. Take-profit is never budget-limited.

The routing rules and thresholds are an initial policy that still needs
prospective (shadow) validation — they are not tuned to historical returns.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from watchy.plan import (
    PlanFreshness,
    PositionState,
    ReminderState,
    Route,
    WeeklyPlan,
    is_bullish_buy_plan,
)

PAID_ROUTES = frozenset({Route.FAST_RECHECK, Route.TRIGGERED_RISK})

# Rank for arbitration; invalidation-driven Triggered Risk ties with TAKE_PROFIT.
_RANK = {
    Route.NO_ACTION: 0,
    Route.NOTIFY_ONLY: 1,
    Route.FAST_RECHECK: 2,
    Route.TRIGGERED_RISK: 3,
    Route.TAKE_PROFIT: 4,
}
_TOP_RANK = 4

DOWNSIDE_SIGNALS = frozenset({"bollinger_lower_breach", "rsi_oversold"})
BULLISH_SIGNALS = frozenset({"golden_cross", "macd_bullish_cross"})
VOLATILITY_SIGNALS = frozenset({"volume_anomaly_strong", "atr_spike"})
OVERBOUGHT_SIGNALS = frozenset({"rsi_overbought", "bollinger_upper_breach"})


@dataclass
class RouterInput:
    ticker: str
    position_state: PositionState
    plan: WeeklyPlan | None = None
    freshness: PlanFreshness = PlanFreshness.MISSING
    plan_state: ReminderState | None = None
    plan_transition: str = ""          # "", "entered", "left_buy_zone" (a notifying transition)
    signals: list[str] = field(default_factory=list)
    cooled_down: list[str] = field(default_factory=list)
    price: float | None = None
    prev_close: float | None = None
    atr: float | None = None
    take_profit_fire: bool = False
    triggered_enabled: bool = False
    ticker_allowed: bool = True
    digest_available: bool = False
    budget_ticker_used: int = 0
    budget_global_used: int = 0
    max_per_ticker: int = 1
    max_global: int = 2
    bearish_shock_atr: float = 0.75


@dataclass
class Candidate:
    route: Route
    trigger: str
    reason: str
    top: bool = False                  # invalidation-driven: ranks with TAKE_PROFIT


@dataclass
class RouteDecision:
    route: Route
    effective_route: Route
    reasons: list[str]
    candidates: list[Candidate]
    rejected: list[str]
    budget_result: str                 # n/a / ok / disabled / not_allowed / budget_exhausted / input_missing
    shadow: bool = False
    invalidate_plan: bool = False
    immediate_warning: bool = False
    negative_move_atr: float | None = None
    material_negative: bool = False

    @property
    def paid(self) -> bool:
        return self.effective_route in PAID_ROUTES

    @property
    def risk(self) -> bool:
        return self.route == Route.TRIGGERED_RISK

    def to_dict(self) -> dict:
        d = asdict(self)
        d["route"] = self.route.value
        d["effective_route"] = self.effective_route.value
        d["candidates"] = [
            {"route": c.route.value, "trigger": c.trigger, "reason": c.reason, "top": c.top}
            for c in self.candidates
        ]
        return d


def negative_move_atr(price: float | None, prev_close: float | None, atr: float | None) -> float | None:
    """Session move in ATR units (negative = down), or None without inputs."""
    if price is None or prev_close is None or not atr or atr <= 0:
        return None
    return (price - prev_close) / atr


def bullish_executable(inp: RouterInput) -> bool:
    """An active bullish buy plan whose entry range is still reachable."""
    if inp.freshness != PlanFreshness.ACTIVE or not is_bullish_buy_plan(inp.plan):
        return False
    if inp.plan_state in (ReminderState.ABOVE_CHASE, ReminderState.INVALIDATED, ReminderState.IN_TAKE_PROFIT):
        return False
    return inp.price is not None and inp.price <= inp.plan.chase_ceiling


def _signal_candidates(inp: RouterInput, held: bool, shock: bool, move: float | None) -> list[Candidate]:
    out: list[Candidate] = []
    shock_txt = f" with a {move:+.2f} ATR session move" if shock and move is not None else ""
    plan_active = inp.freshness == PlanFreshness.ACTIVE
    for sig in inp.signals:
        if sig == "death_cross":
            if held:
                out.append(Candidate(Route.TRIGGERED_RISK, sig, "death cross on a held position"))
            else:
                out.append(Candidate(Route.NOTIFY_ONLY, sig, "death cross — bullish plan withdrawn"
                                     if is_bullish_buy_plan(inp.plan) and plan_active else "death cross"))
        elif sig == "macd_bearish_cross":
            if held:
                route = Route.TRIGGERED_RISK if shock else Route.FAST_RECHECK
                out.append(Candidate(route, sig, "MACD bearish cross on a held position" + shock_txt))
            else:
                out.append(Candidate(Route.NOTIFY_ONLY, sig, "MACD bearish cross"))
        elif sig in DOWNSIDE_SIGNALS:
            label = "Bollinger lower-band breach" if sig == "bollinger_lower_breach" else "RSI oversold"
            if held:
                route = Route.TRIGGERED_RISK if shock else Route.FAST_RECHECK
                out.append(Candidate(route, sig, f"{label} on a held position" + shock_txt))
            elif bullish_executable(inp):
                out.append(Candidate(Route.FAST_RECHECK, sig, f"{label} while an active bullish buy plan is executable"))
            else:
                out.append(Candidate(Route.NOTIFY_ONLY, sig, label))
        elif sig in VOLATILITY_SIGNALS:
            label = "volume anomaly" if sig == "volume_anomaly_strong" else "ATR spike"
            if shock and held:
                out.append(Candidate(Route.TRIGGERED_RISK, sig, f"{label}{shock_txt} on a held position"))
            else:
                out.append(Candidate(Route.NOTIFY_ONLY, sig, label + shock_txt))
        elif sig in OVERBOUGHT_SIGNALS:
            label = "RSI overbought" if sig == "rsi_overbought" else "Bollinger upper-band breach"
            if held or plan_active:
                # A held winner past the floor is handled by the take-profit path.
                out.append(Candidate(Route.NOTIFY_ONLY, sig, label))
            else:
                out.append(Candidate(Route.NO_ACTION, sig, label + " on a watch-only name without a plan"))
        elif sig in BULLISH_SIGNALS:
            label = "golden cross" if sig == "golden_cross" else "MACD bullish cross"
            if bullish_executable(inp):
                out.append(Candidate(Route.FAST_RECHECK, sig, f"{label} with an executable bullish plan"))
            else:
                out.append(Candidate(Route.NOTIFY_ONLY, sig, label))
        else:
            out.append(Candidate(Route.NOTIFY_ONLY, sig, sig.replace("_", " ")))
    return out


def _plan_candidates(inp: RouterInput, held: bool) -> list[Candidate]:
    if not inp.plan_transition or inp.plan_state is None:
        return []
    st = inp.plan_state
    if inp.plan_transition == "left_buy_zone":
        return [Candidate(Route.NOTIFY_ONLY, "plan:left_buy_zone", "price left the planned buy zone")]
    if st == ReminderState.INVALIDATED:
        if held:
            return [Candidate(Route.TRIGGERED_RISK, "plan:invalidated",
                              "price crossed the plan's invalidation level", top=True)]
        return [Candidate(Route.NOTIFY_ONLY, "plan:invalidated",
                          "price crossed the plan's invalidation level — plan withdrawn")]
    if st == ReminderState.IN_TAKE_PROFIT:
        if held:
            return [Candidate(Route.NOTIFY_ONLY, "plan:take_profit_zone",
                              "price reached the plan's resistance / take-profit territory")]
        return [Candidate(Route.NO_ACTION, "plan:take_profit_zone", "watch-only name at resistance")]
    reasons = {
        ReminderState.IN_BUY_ZONE: "price entered the weekly buy zone",
        ReminderState.APPROACHING_BUY: "price is approaching the weekly buy zone",
        ReminderState.ABOVE_CHASE: "price moved above the chase ceiling",
        ReminderState.EXPIRED: "the weekly plan expired",
    }
    if st in reasons:
        return [Candidate(Route.NOTIFY_ONLY, f"plan:{st.value}", reasons[st])]
    return []


def route(inp: RouterInput) -> RouteDecision:
    """Pick exactly one route for this scan (pure; see module doc)."""
    # Unknown position: conservative like the 1.x Tier 2 gate — a name we might
    # own gets the held (risk-side) rules. Take-profit needs a known position
    # and is decided upstream by the take-profit gate.
    held = inp.position_state != PositionState.WATCH
    move = negative_move_atr(inp.price, inp.prev_close, inp.atr)
    shock = move is not None and move <= -abs(inp.bearish_shock_atr)

    cands: list[Candidate] = []
    if inp.take_profit_fire:
        cands.append(Candidate(Route.TAKE_PROFIT, "take_profit_zone",
                               "unrealized gain crossed the take-profit floor"))
    cands += _plan_candidates(inp, held)
    cands += _signal_candidates(inp, held, shock, move)

    rejected = [f"{s}: in cooldown" for s in inp.cooled_down]
    invalidate = any(
        c.trigger == "plan:invalidated" for c in cands
    ) or (
        not held
        and "death_cross" in inp.signals
        and inp.freshness == PlanFreshness.ACTIVE
        and is_bullish_buy_plan(inp.plan)
    )
    immediate = any(c.top for c in cands)

    if not cands:
        return RouteDecision(Route.NO_ACTION, Route.NO_ACTION, [], [], rejected, "n/a",
                             negative_move_atr=move, material_negative=shock)

    def rank(c: Candidate) -> tuple[int, int]:
        r = _TOP_RANK if c.top else _RANK[c.route]
        # tie at the top: keep the existing take-profit path first
        return (r, 1 if c.route == Route.TAKE_PROFIT else 0)

    best = max(cands, key=rank)
    chosen = best.route
    for c in cands:
        if c is not best and c.route != Route.NO_ACTION and _RANK[c.route] > _RANK[Route.NOTIFY_ONLY]:
            rejected.append(f"{c.trigger}: {c.route.value} superseded by {chosen.value}")
    reasons = [c.reason for c in cands if c.route != Route.NO_ACTION] or [best.reason]

    effective, budget, shadow = chosen, "n/a", False
    if chosen in PAID_ROUTES:
        if not inp.triggered_enabled:
            effective, budget, shadow = Route.NOTIFY_ONLY, "disabled", True
        elif not inp.ticker_allowed:
            effective, budget = Route.NOTIFY_ONLY, "not_allowed"
        elif chosen == Route.FAST_RECHECK and (
            inp.freshness != PlanFreshness.ACTIVE or not inp.digest_available
        ):
            effective, budget = Route.NOTIFY_ONLY, "input_missing"
        elif (
            inp.budget_ticker_used >= inp.max_per_ticker
            or inp.budget_global_used >= inp.max_global
        ):
            effective, budget = Route.NOTIFY_ONLY, "budget_exhausted"
        else:
            budget = "ok"
        if effective != chosen:
            rejected.append(f"{chosen.value}: downgraded to NOTIFY_ONLY ({budget})")

    return RouteDecision(
        route=chosen,
        effective_route=effective,
        reasons=reasons,
        candidates=cands,
        rejected=rejected,
        budget_result=budget,
        shadow=shadow,
        invalidate_plan=invalidate,
        immediate_warning=immediate,
        negative_move_atr=move,
        material_negative=shock,
    )
