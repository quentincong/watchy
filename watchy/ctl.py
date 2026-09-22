"""Watchy 2.0 operator controls (``scripts/watchy_ctl.py``).

Read-only unless stated:

  status                       effective mode, budgets, schema version
  plan show TICKER             current weekly plan, freshness, override, reminder state
  plan history TICKER          plan rows, newest first
  plan expire TICKER --yes     deactivate the active plan (history kept)          [writes db]
  route TICKER [...]           dry routing decision — no LLM, no Telegram, no db writes
  preview TICKER [...]         the Telegram message that route would send (not sent)
  weekly TICKER --yes          force a Weekly Full for one ticker — PAID, sends Telegram
  replay [...]                 zero-cost routing replay over the db (+ optional CSV)

Switching ``tier2_schedule`` (weekly/daily) and ``triggered_analysis.enabled``
is done in config.yaml followed by a daemon restart; ``status`` shows what the
daemon will read. Shadow mode is simply ``triggered_analysis.enabled: false``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watchy.plan import PositionState, plan_freshness

DEFAULT_DB = os.path.expanduser("~/watchy/state.db")
REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_config(path: str | None):
    from watchy.config import load_config

    return load_config(path)


def _ro(db: str):
    from watchy.replay import ReadOnlyStore

    if not Path(db).expanduser().exists():
        raise SystemExit(f"database not found: {db}")
    return ReadOnlyStore(db)


def _plan_lines(plan: Any, session) -> list[str]:
    if plan is None:
        return ["  (none)"]
    fresh = plan_freshness(plan, session)
    lines = [
        f"  id={plan.id} kind={plan.kind} status={plan.status} freshness={fresh.value}",
        f"  decision={plan.decision} urgency={plan.urgency} verdict={plan.upstream_verdict}",
        f"  buy_zone={plan.buy_zone_low}-{plan.buy_zone_high} chase={plan.chase_ceiling} "
        f"invalidation={plan.invalidation_level} ({plan.invalidation_condition or '-'})",
        f"  resistance={plan.resistance_low}-{plan.resistance_high} "
        f"take_profit={plan.take_profit_price} trim={plan.trim_condition or '-'}",
        f"  valid {plan.valid_from_session}..{plan.expires_after_session}  "
        f"price={plan.input_price}@{plan.input_price_ts}  created={plan.created_ts}",
        f"  thesis: {plan.thesis}",
        f"  guidance: {plan.guidance}",
        f"  dont_do: {plan.dont_do}",
    ]
    if plan.validation_errors:
        lines.append(f"  validation_errors: {plan.validation_errors}")
    if plan.source_ref:
        lines.append(f"  source: {json.dumps(plan.source_ref, default=str)}")
    return lines


def cmd_status(args) -> int:
    config = _load_config(args.config)
    ta, wp = config.triggered_analysis, config.weekly_plan
    print(f"tier2_schedule: {config.tier2_schedule}"
          + ("  (weekly plan + Tier 1 routing)" if config.weekly_mode
             else "  (ROLLBACK: 1.x daily Tier 2 + paid Tier 1 rescans)"))
    print(f"triggered_analysis.enabled: {ta.enabled}"
          + ("" if ta.enabled else "  (SHADOW MODE — no paid triggered calls)"))
    print(f"  budgets: per ticker {ta.max_per_ticker_per_trading_day}/session, "
          f"global {ta.max_global_per_trading_day}/session; bearish_shock_atr={ta.bearish_shock_atr}; "
          f"allow-list={ta.tickers or 'all'}")
    print(f"weekly_plan: approach_atr={wp.approach_atr} stale_move_atr={wp.stale_move_atr} "
          f"renotify_h={wp.renotify_h} market_data_max_age_min={wp.market_data_max_age_min}")
    print(f"take_profit.enabled: {config.take_profit.enabled}")
    print(f"watchlist: {[tc.ticker for tc in config.watchlist]}")
    db = Path(args.db).expanduser()
    if db.exists():
        import sqlite3

        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        print(f"state.db: {db} user_version={conn.execute('PRAGMA user_version').fetchone()[0]}")
        conn.close()
    else:
        print(f"state.db: {db} (not found)")
    return 0


def cmd_plan(args) -> int:
    from watchy.market_calendar import session_label

    session = session_label()
    if args.action == "expire":
        if not args.yes:
            print("refusing to expire without --yes (history is kept either way)")
            return 2
        from watchy.state import StateStore

        store = StateStore(os.path.expanduser(args.db))
        try:
            plan = store.get_active_plan(args.ticker)
            if plan is None:
                print(f"{args.ticker.upper()}: no active weekly plan")
                return 1
            store.deactivate_plan(plan.id)
            print(f"{args.ticker.upper()}: plan #{plan.id} deactivated (history kept)")
            return 0
        finally:
            store.close()

    store = _ro(args.db)
    try:
        if args.action == "history":
            for p in store.get_plan_history(args.ticker, args.limit):
                print("\n".join(_plan_lines(p, session)))
                print()
            return 0
        plan = store.get_current_plan(args.ticker)
        print(f"{args.ticker.upper()} — session {session}")
        print("weekly plan:")
        print("\n".join(_plan_lines(plan, session)))
        override = store.get_latest_override(args.ticker, plan.id) if plan else None
        print("latest event override:")
        print("\n".join(_plan_lines(override, session)))
        print(f"reminder state: {store.get_reminder_state(args.ticker) or '(none)'}")
        print(f"paid analyses this session: {store.count_triggered(session.isoformat(), args.ticker)}"
              f" (global {store.count_triggered(session.isoformat())})")
        return 0
    finally:
        store.close()


def _dry_route(args):
    """Build a routing decision + preview without side effects."""
    from watchy.indicators import IndicatorBundle, compute_indicators
    from watchy.monitor import build_reminder, evaluate_plan
    from watchy.router import RouterInput, route

    config = _load_config(args.config)
    store = _ro(args.db)
    try:
        now = datetime.now(timezone.utc)
        if args.price is not None:
            state = store.get_ticker_state(args.ticker)
            atr = args.atr or state.get("avg_atr_20d") or state.get("prev_atr")
            bundle = IndicatorBundle(ticker=args.ticker.upper(), current_price=args.price,
                                     prev_close=args.prev_close, atr=atr, avg_atr_20d=atr)
            bundle.fetched_at = now
        else:
            bundle = compute_indicators(args.ticker)
            if bundle is None:
                raise SystemExit(f"no indicator data for {args.ticker}")
        ev = evaluate_plan(args.ticker, bundle, config, store, now)
        if args.price is not None:
            from dataclasses import replace

            ev = replace(ev, data_stale=False)   # operator-supplied price
        pstate = PositionState(args.position) if args.position else PositionState.UNKNOWN
        ta = config.triggered_analysis
        transition = ev.transition.kind if ev.transition and ev.transition.notify else ""
        decision = route(RouterInput(
            ticker=args.ticker, position_state=pstate, plan=ev.plan, freshness=ev.freshness,
            plan_state=ev.state, plan_transition=transition, signals=list(args.signal or []),
            price=ev.price, prev_close=bundle.prev_close, atr=ev.atr,
            triggered_enabled=ta.enabled,
            ticker_allowed=not ta.tickers or args.ticker.upper() in {t.upper() for t in ta.tickers},
            digest_available=True,
            budget_ticker_used=store.count_triggered(ev.session, args.ticker),
            budget_global_used=store.count_triggered(ev.session),
            max_per_ticker=ta.max_per_ticker_per_trading_day,
            max_global=ta.max_global_per_trading_day,
            bearish_shock_atr=ta.bearish_shock_atr,
        ))
        status, text = build_reminder(
            ev, pstate, why_now=decision.reasons or ["manual preview"],
            route=decision.route, risk_trigger=decision.risk,
            stale_move_atr=config.weekly_plan.stale_move_atr,
            interpretation_pending=decision.route.value in ("FAST_RECHECK", "TRIGGERED_RISK")
            and decision.effective_route.value == "NOTIFY_ONLY",
        )
        return ev, decision, status, text
    finally:
        store.close()


def cmd_route(args) -> int:
    ev, decision, status, _ = _dry_route(args)
    out = {
        "ticker": args.ticker.upper(), "session": ev.session, "price": ev.price,
        "plan_id": ev.plan.id if ev.plan else None, "plan_freshness": ev.freshness.value,
        "plan_state": ev.state.value if ev.state else None,
        "plan_transition": ev.transition.kind if ev.transition else None,
        **decision.to_dict(), "preview_status": status.value,
        "note": "dry run — no LLM, no Telegram, no database writes",
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_preview(args) -> int:
    _, decision, status, text = _dry_route(args)
    print(f"[preview only — not sent] route={decision.route.value} "
          f"effective={decision.effective_route.value} status={status.value}\n")
    print(text)
    return 0


def cmd_weekly(args) -> int:
    if not args.yes:
        print("Weekly Full is a PAID pipeline run and sends Telegram; re-run with --yes")
        return 2
    from watchy.locks import TickerLockRegistry
    from watchy.notify import TelegramNotifier
    from watchy.pipeline_runner import create_tradingagents_runner
    from watchy.state import StateStore
    from watchy.tier2 import run_daily_scan

    config = _load_config(args.config)
    if config.get_ticker_config(args.ticker) is None:
        print(f"{args.ticker} is not on the watchlist")
        return 1
    store = StateStore(os.path.expanduser(args.db))
    try:
        notifier = TelegramNotifier(config.telegram.bot_token, config.telegram.chat_id)
        runner = create_tradingagents_runner(deepseek_api_key=config.llm.deepseek_api_key)
        results = run_daily_scan(
            config, store, notifier, pipeline_runner=runner,
            ticker_locks=TickerLockRegistry(), weekly=True, tickers=[args.ticker],
        )
        print(json.dumps(results, indent=2, default=str)[:4000])
        return 0
    finally:
        store.close()


def cmd_replay(args) -> int:
    from watchy.replay import (
        DEFAULT_COSTS,
        guard_output_path,
        render_text,
        run_replay,
        since_days,
        to_json,
    )
    from watchy.plan import Route

    guard_output_path(args.out, REPO_ROOT)
    config = _load_config(args.config) if args.config or os.path.exists(
        os.path.expanduser("~/watchy/config.yaml")) else None
    ta = config.triggered_analysis if config else None
    wp = config.weekly_plan if config else None
    costs = dict(DEFAULT_COSTS)
    if args.cost_fast_recheck is not None:
        costs[Route.FAST_RECHECK] = args.cost_fast_recheck
    if args.cost_triggered_risk is not None:
        costs[Route.TRIGGERED_RISK] = args.cost_triggered_risk
    store = _ro(args.db)
    try:
        report = run_replay(
            store,
            since=args.since or since_days(args.days),
            events_csv=args.events_csv,
            assume_enabled=not args.shadow,
            max_per_ticker=ta.max_per_ticker_per_trading_day if ta else 1,
            max_global=ta.max_global_per_trading_day if ta else 2,
            bearish_shock_atr=ta.bearish_shock_atr if ta else 0.75,
            approach_atr=wp.approach_atr if wp else 0.5,
            renotify_h=wp.renotify_h if wp else 6.0,
            costs=costs,
        )
    finally:
        store.close()
    text = to_json(report) if args.json else render_text(report)
    if args.out:
        Path(args.out).expanduser().write_text(text + "\n", encoding="utf-8")
        print(f"written: {args.out}")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="watchy_ctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None, help="config.yaml (default: daemon's path)")
    p.add_argument("--db", default=DEFAULT_DB, help=f"state.db (default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)

    pl = sub.add_parser("plan")
    pl.add_argument("action", choices=["show", "history", "expire"])
    pl.add_argument("ticker")
    pl.add_argument("--limit", type=int, default=10)
    pl.add_argument("--yes", action="store_true")
    pl.set_defaults(func=cmd_plan)

    for name, func in (("route", cmd_route), ("preview", cmd_preview)):
        r = sub.add_parser(name)
        r.add_argument("ticker")
        r.add_argument("--price", type=float, help="use this price instead of fetching live data")
        r.add_argument("--prev-close", type=float, dest="prev_close")
        r.add_argument("--atr", type=float)
        r.add_argument("--signal", action="append", help="simulate a technical trigger (repeatable)")
        r.add_argument("--position", choices=[s.value for s in PositionState])
        r.set_defaults(func=func)

    w = sub.add_parser("weekly")
    w.add_argument("ticker")
    w.add_argument("--yes", action="store_true")
    w.set_defaults(func=cmd_weekly)

    rp = sub.add_parser("replay")
    rp.add_argument("--since", help="ISO timestamp lower bound")
    rp.add_argument("--days", type=int, help="only the last N days")
    rp.add_argument("--events-csv", dest="events_csv",
                    help="explicitly provided read-only research export (never copied)")
    rp.add_argument("--shadow", action="store_true",
                    help="replay with paid analysis disabled (default assumes enabled)")
    rp.add_argument("--cost-fast-recheck", type=float, dest="cost_fast_recheck")
    rp.add_argument("--cost-triggered-risk", type=float, dest="cost_triggered_risk")
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--out", help="write the report here (refused inside the repository)")
    rp.set_defaults(func=cmd_replay)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "build_parser"]
