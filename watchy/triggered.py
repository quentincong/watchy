"""Watchy 2.0 paid triggered analysis — Fast Recheck and Triggered Risk.

Reached only when the router picked a paid route AND triggered analysis is
enabled AND the ticker is allowed AND the budget had room. Before spending,
the slot is reserved atomically in ``triggered_budget`` (keyed by exchange
session), so a restart or two tickers racing cannot exceed the caps.

* **Fast Recheck** — advisor only, on the saved *weekly* digest plus an event
  context block (trigger, plan-relative state, freshness). No pipeline.
* **Triggered Risk** — market + sentiment + news analysts, bull/bear debate,
  simplified risk, then the advisor.

Either produces an ``event_override`` row (the base plan's levels copied, the
advisor's decision/urgency/guidance on top) and never rewrites the weekly base
plan. The message status comes from the deterministic guards after a price
refresh. Any failure falls back to the deterministic reminder, labelled
"analysis failed" — an LLM problem never suppresses a risk notification.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from watchy.advisor import get_advice
from watchy.digest_store import load_digest, save_digest
from watchy.guards import StatusInputs, classify_alignment, revalidate, select_status
from watchy.messages import MessageContext, format_et, render_plan_card
from watchy.orchestrator import AnalystSet, DebateMode, PipelineSpec, RiskMode, run_pipeline
from watchy.plan import (
    NUMERIC_FIELDS,
    PlanFreshness,
    PlanKind,
    Route,
    WeeklyPlan,
    normalize_decision,
    validate_plan,
)

logger = logging.getLogger(__name__)

TRIGGERED_RISK_SPEC = PipelineSpec(
    analysts=AnalystSet.MARKET_SENTIMENT_NEWS,
    debate=DebateMode.BULL_BEAR,
    risk=RiskMode.SIMPLIFIED,
)


def event_context(decision: Any, ev: Any, pstate: Any, now: datetime,
                  digest_saved_at: datetime | None = None) -> str:
    """The block injected into the advisor prompt for a triggered analysis."""
    plan = ev.plan
    lines = [
        "EVENT CONTEXT — this is an intraday re-check, not the weekly review.",
        f"Why now: {'; '.join(decision.reasons)}.",
        f"Current price: {ev.price} as of {format_et(ev.price_ts) or 'unknown'}"
        + (f"; session move {decision.negative_move_atr:+.2f} ATR"
           if decision.negative_move_atr is not None else "") + ".",
        f"Position state: {pstate.value}.",
    ]
    if plan is not None and ev.freshness == PlanFreshness.ACTIVE:
        levels = ", ".join(
            f"{k}={getattr(plan, k)}" for k in NUMERIC_FIELDS if getattr(plan, k) is not None
        )
        lines.append(
            f"Active weekly plan #{plan.id} ({plan.valid_from_session}..{plan.expires_after_session}): "
            f"decision {plan.decision}; {levels}; thesis: {plan.thesis}"
        )
        lines.append(f"Price vs plan: {ev.state.value if ev.state else 'unknown'}.")
    else:
        lines.append(f"Weekly plan: {ev.freshness.value} — no plan levels are in force.")
    if digest_saved_at is not None:
        lines.append(f"The analysis below is the weekly digest saved {format_et(digest_saved_at, True)}.")
    lines.append(
        "Decide whether this event changes the plan for THIS session. Do not move the "
        "plan's buy zone, chase ceiling or invalidation level; say HOLD when no new "
        "action is needed."
    )
    return "\n".join(lines)


def build_override(
    base: WeeklyPlan | None,
    advice: dict[str, Any],
    *,
    ticker: str,
    session: str,
    price: float | None,
    price_ts: datetime | None,
    held: bool | None,
    source_ref: dict[str, Any],
    now: datetime,
    upstream_verdict: str = "",
) -> WeeklyPlan:
    """Temporary override for one session; the base plan's levels are copied
    so the advisor can reinterpret the event but never move the boundaries."""
    override = WeeklyPlan(
        ticker=ticker.upper(),
        kind=PlanKind.EVENT_OVERRIDE.value,
        parent_plan_id=base.id if base is not None else None,
        decision=normalize_decision(advice.get("decision"), held),
        urgency=str(advice.get("urgency") or "").upper(),
        guidance=str(advice.get("detail") or "")[:1500],
        upstream_verdict=upstream_verdict or (base.upstream_verdict if base else ""),
        valid_from_session=session,
        expires_after_session=session,
        input_price=price,
        input_price_ts=price_ts.isoformat() if price_ts else "",
        created_ts=now.isoformat(),
        source_ref=source_ref,
    )
    if base is not None:
        override.thesis = base.thesis
        override.dont_do = base.dont_do
        override.invalidation_condition = base.invalidation_condition
        override.trim_condition = base.trim_condition
        for key in NUMERIC_FIELDS:
            setattr(override, key, getattr(base, key))
    return validate_plan(override)


def execute(
    decision: Any,
    ev: Any,
    pstate: Any,
    bundle: Any,
    config: Any,
    store: Any,
    notifier: Any,
    position_source: Any,
    *,
    pipeline_runner: Any = None,
    ticker_locks: Any = None,
    now: datetime | None = None,
    price_fetcher: Any = None,
) -> dict[str, Any]:
    """Run the routed paid analysis; returns fields merged into the ROUTE record.

    ``effective_route`` comes back as NOTIFY_ONLY whenever the analysis did not
    produce a message, so the caller sends the deterministic reminder.
    """
    now = now or datetime.now(timezone.utc)
    kind = decision.effective_route
    ticker = ev.ticker
    ta = config.triggered_analysis

    digest = None
    if kind == Route.FAST_RECHECK:
        digest = load_digest(ticker, kind="weekly")
        if digest is None or ev.plan is None or ev.freshness != PlanFreshness.ACTIVE:
            logger.info("Fast Recheck %s: weekly digest/plan unavailable — Notify Only", ticker)
            return {"effective_route": Route.NOTIFY_ONLY.value, "budget_result": "input_missing"}

    lock = ticker_locks.get(ticker) if ticker_locks is not None else None
    if lock is not None and not lock.acquire(blocking=False):
        logger.info("%s %s: another analysis holds the ticker lock — Notify Only", kind.value, ticker)
        return {"effective_route": Route.NOTIFY_ONLY.value, "budget_result": "busy"}
    try:
        reservation = store.try_reserve_triggered(
            ev.session, ticker, kind.value,
            ta.max_per_ticker_per_trading_day, ta.max_global_per_trading_day,
        )
        if reservation is None:
            logger.info("%s %s: budget exhausted at reservation", kind.value, ticker)
            return {"effective_route": Route.NOTIFY_ONLY.value, "budget_result": "budget_exhausted"}

        out: dict[str, Any] = {"budget_result": "ok", "reservation_id": reservation}
        if decision.immediate_warning:
            _send_immediate_warning(decision, ev, pstate, config, notifier)
            out["immediate_warning_sent"] = True
        started = time.monotonic()
        try:
            if kind == Route.FAST_RECHECK:
                out.update(_fast_recheck(decision, ev, pstate, bundle, config, store,
                                         notifier, position_source, digest, now, price_fetcher))
            else:
                out.update(_triggered_risk(decision, ev, pstate, bundle, config, store,
                                           notifier, position_source, pipeline_runner, now,
                                           price_fetcher))
            store.finish_triggered(reservation, "ok")
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s failed for %s", kind.value, ticker)
            store.finish_triggered(reservation, "failed")
            out.update({
                "effective_route": Route.NOTIFY_ONLY.value,
                "analysis_error": f"{type(exc).__name__}: {exc}"[:300],
                "llm_invoked": True,
            })
        out["analysis_latency_s"] = round(time.monotonic() - started, 3)
        out["analysis_model"] = config.llm.model
        out["analysis_thinking"] = config.llm.gemini_thinking_tier1
        return out
    finally:
        if lock is not None:
            lock.release()


def _send_immediate_warning(decision: Any, ev: Any, pstate: Any, config: Any, notifier: Any) -> None:
    from watchy.monitor import build_reminder

    _, text = build_reminder(
        ev, pstate, why_now=decision.reasons, route=decision.route,
        risk_trigger=True, stale_move_atr=config.weekly_plan.stale_move_atr,
        notes=["Triggered Risk analysis is running — follow-up message to come"],
        interpretation_pending=True,
    )
    notifier.send(text)


def _fast_recheck(decision, ev, pstate, bundle, config, store, notifier,
                  position_source, digest, now, price_fetcher) -> dict[str, Any]:
    result, saved_at = digest
    advice = get_advice(
        ev.ticker, result, position_source, config,
        thinking_level=config.llm.gemini_thinking_tier1,
        indicator_bundle=bundle, store=store, source="fast_recheck",
        event_context=event_context(decision, ev, pstate, now, saved_at),
    )
    if advice is None:
        raise RuntimeError("advisor produced no advice")
    return _finish(
        decision, ev, pstate, bundle, config, store, notifier, advice,
        mode="Fast Recheck — advisor re-read the weekly digest",
        source=f"weekly digest from {format_et(saved_at, True)}",
        verdict=ev.plan.upstream_verdict if ev.plan else "",
        components=["advisor"],
        source_ref={"mode": "fast_recheck", "digest_saved_at": saved_at.isoformat()},
        now=now, price_fetcher=price_fetcher,
    )


def _triggered_risk(decision, ev, pstate, bundle, config, store, notifier,
                    position_source, pipeline_runner, now, price_fetcher) -> dict[str, Any]:
    from watchy.schwab_health import monitor_schwab

    monitor_schwab(config, store, notifier, position_source)
    run_id = store.start_run(ev.ticker, "tier1", "triggered_risk")
    try:
        result = run_pipeline(ev.ticker, TRIGGERED_RISK_SPEC, runner=pipeline_runner)
    except Exception as exc:
        store.complete_run(run_id, success=False, summary=str(exc))
        raise
    store.complete_run(run_id, success=True, summary=result.get("summary", ""))
    # Latest digest only (the take-profit trigger reads it); the weekly digest
    # that Fast Recheck uses stays the one the weekly plan was built from.
    save_digest(ev.ticker, result)
    advice = get_advice(
        ev.ticker, result, position_source, config,
        thinking_level=config.llm.gemini_thinking_tier1,
        indicator_bundle=bundle, store=store, source="triggered_risk",
        event_context=event_context(decision, ev, pstate, now),
    )
    out = _finish(
        decision, ev, pstate, bundle, config, store, notifier, advice or {},
        mode="Triggered Risk — market+sentiment+news, bull/bear, simplified risk, advisor",
        source=f"fresh analysis {format_et(now)}",
        verdict=str(result.get("verdict") or ""),
        components=["market", "sentiment", "news", "bull_bear", "simplified_risk", "advisor"],
        source_ref={"mode": "triggered_risk", "run_id": run_id,
                    "report_path": result.get("report_path")},
        now=now, price_fetcher=price_fetcher, result=result, position_source=position_source,
        advisor_failed=advice is None,
    )
    return out


def _finish(decision, ev, pstate, bundle, config, store, notifier, advice, *, mode, source,
            verdict, components, source_ref, now, price_fetcher, result=None,
            position_source=None, advisor_failed=False) -> dict[str, Any]:
    """Persist the override, re-check the price, pick the status, send."""
    from watchy.monitor import fetch_live_price
    from watchy.plan import PositionState

    fetch = price_fetcher or fetch_live_price
    post_price, post_ts = fetch(ev.ticker)
    wp = config.weekly_plan
    reval = revalidate(
        ev.plan if ev.freshness == PlanFreshness.ACTIVE else None, ev.freshness,
        pre_price=ev.price, pre_ts=ev.price_ts, post_price=post_price, post_ts=post_ts,
        atr=ev.atr, now=datetime.now(timezone.utc),
        approach_atr=wp.approach_atr, max_age_min=wp.market_data_max_age_min,
    )
    held = {PositionState.HELD: True, PositionState.WATCH: False}.get(pstate)
    override_id = None
    if advice:
        override = build_override(
            ev.plan if ev.freshness == PlanFreshness.ACTIVE else None, advice,
            ticker=ev.ticker, session=ev.session, price=reval.price, price_ts=reval.price_ts,
            held=held, source_ref={**source_ref, "reasons": decision.reasons,
                                   "advice_log_id": advice.get("_advice_log_id")},
            now=now, upstream_verdict=verdict,
        )
        override_id = store.insert_plan(override)
    else:
        override = None

    decision_txt = str(advice.get("decision") or "")
    status = select_status(StatusInputs(
        position_state=pstate,
        freshness=ev.freshness,
        plan_state=reval.state,
        plan=ev.plan if ev.freshness == PlanFreshness.ACTIVE else None,
        data_stale=reval.stale,
        route=decision.route,
        risk_trigger=decision.risk,
        advisor_decision=decision_txt,
        advisor_urgency=str(advice.get("urgency") or ""),
        verdict=verdict,
        price_moved_atr=reval.moved_atr,
        stale_move_atr=wp.stale_move_atr,
    ))
    notes = []
    if reval.reason:
        notes.append(reval.reason)
    if reval.moved_atr is not None and reval.moved_atr > wp.stale_move_atr:
        notes.append(f"price moved {reval.moved_atr:.2f} ATR while the analysis ran — recheck")
    if advisor_failed:
        notes.append("advisor failed — analyst verdict only; see the attached report")
    if override is not None and override.validation_errors:
        notes.append("event override failed validation: " + "; ".join(override.validation_errors[:2]))
    shown_ev = replace(ev, state=reval.state)
    ctx = MessageContext(
        ticker=ev.ticker,
        status=status,
        price=reval.price,
        price_ts=reval.price_ts,
        why_now=decision.reasons,
        plan=ev.plan,
        plan_freshness=ev.freshness,
        plan_state=shown_ev.state,
        override=None,
        guidance=str(advice.get("detail") or ""),
        take_profit=str(advice.get("take_profit") or ""),
        dont_do=(ev.plan.dont_do if ev.plan and ev.freshness == PlanFreshness.ACTIVE else
                 "do not open or add a position from this alert alone"),
        verdict=verdict,
        advisor_decision=decision_txt,
        advisor_urgency=str(advice.get("urgency") or ""),
        alignment=classify_alignment(verdict, decision_txt),
        mode=mode,
        source_freshness=source,
        notes=notes,
    )
    card = render_plan_card(ctx)
    if result is not None:
        position_text = position_source.format_position_context(ev.ticker) if position_source else None
        sent = notifier.pipeline_result(
            ev.ticker, "triggered_risk", result,
            position_text=position_text, advice=advice or None, plan_card=card,
        )
    else:
        sent = notifier.send(card)
    logger.info(
        "TRIGGERED %s route=%s status=%s decision=%s pre=%s post=%s override=%s",
        ev.ticker, decision.route.value, status.value, decision_txt, ev.price, post_price, override_id,
    )
    return {
        "effective_route": decision.effective_route.value,
        "llm_invoked": True,
        "analysis_components": components,
        "status": status.value,
        "notified": bool(sent),
        "post_price": post_price,
        "post_price_ts": post_ts.isoformat() if post_ts else None,
        "price_moved_atr": reval.moved_atr,
        "override_id": override_id,
        "advisor_decision": decision_txt,
        "advisor_urgency": str(advice.get("urgency") or ""),
        "upstream_verdict": verdict,
    }
