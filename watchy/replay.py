"""Watchy 2.0 zero-cost routing replay (no LLM, no network, read-only).

Re-runs the pure router over history Watchy already has — the ``signal_log``
(1.x and 2.0), the ``route_log`` of 2.0 shadow evaluations, the plan history
and the advice log's position snapshots — plus, only when explicitly given, a
read-only exported events CSV. It reports routing *volume and timing*:

* trigger and route counts by ticker and position state;
* the simultaneous-trigger collapse rate (triggers per routed scan);
* estimated paid-call count and cost, and budget suppressions;
* time between a trigger and the price leaving the plan's execution range;
* reminder repetition before vs after state deduplication;
* missing-plan and stale-plan frequencies.

It evaluates routing, not profitability, and must not be used to tune
thresholds toward historical returns. The database is opened with SQLite's
``mode=ro`` so a replay can never write, and nothing here imports the advisor
or the pipeline runner.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from watchy.market_calendar import session_label
from watchy.plan import PositionState, ReminderState, Route, plan_freshness
from watchy.plan_monitor import NOTIFY_STATES, detect_transition, plan_state
from watchy.router import PAID_ROUTES, RouterInput, route
from watchy.state import _loads, row_to_plan

# Rough per-call placeholders (USD). Replace with measured TOKENCOST/GEMINICOST
# figures before quoting; they only scale the estimate, never the routing.
DEFAULT_COSTS = {Route.FAST_RECHECK: 0.01, Route.TRIGGERED_RISK: 0.06}
SCAN_GROUP_SECONDS = 120


class ReadOnlyStore:
    """The slice of StateStore that routing needs, opened read-only.

    Tolerates a 1.x database: missing 2.0 tables read as empty.
    """

    def __init__(self, db_path: str) -> None:
        uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True)
        self._tables = {
            r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    def close(self) -> None:
        self._conn.close()

    def _rows(self, table: str, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if table not in self._tables:
            return []
        cur = self._conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # routing / inspection API (duck-typed like StateStore)
    def plans(self) -> list[Any]:
        return [row_to_plan(r) for r in self._rows(
            "analysis_plan", "SELECT * FROM analysis_plan ORDER BY id ASC")]

    def get_current_plan(self, ticker: str) -> Any:
        rows = self._rows(
            "analysis_plan",
            "SELECT * FROM analysis_plan WHERE ticker = ? AND kind = 'weekly_base' "
            "AND status IN ('active','invalidated','deactivated') ORDER BY id DESC LIMIT 1",
            (ticker.upper(),),
        )
        return row_to_plan(rows[0]) if rows else None

    def get_latest_override(self, ticker: str, parent_plan_id: int | None) -> Any:
        rows = self._rows(
            "analysis_plan",
            "SELECT * FROM analysis_plan WHERE ticker = ? AND kind = 'event_override' "
            "AND parent_plan_id = ? ORDER BY id DESC LIMIT 1",
            (ticker.upper(), parent_plan_id),
        )
        return row_to_plan(rows[0]) if rows else None

    def get_plan_history(self, ticker: str, limit: int = 20) -> list[Any]:
        return [row_to_plan(r) for r in self._rows(
            "analysis_plan",
            "SELECT * FROM analysis_plan WHERE ticker = ? ORDER BY id DESC LIMIT ?",
            (ticker.upper(), limit))]

    def get_reminder_state(self, ticker: str) -> dict[str, Any]:
        rows = self._rows(
            "plan_reminder_state",
            "SELECT plan_id, state, state_since_ts, notified FROM plan_reminder_state "
            "WHERE ticker = ?", (ticker.upper(),))
        if not rows:
            return {}
        r = rows[0]
        return {"plan_id": r["plan_id"], "state": r["state"],
                "state_since_ts": r["state_since_ts"], "notified": _loads(r["notified"], {})}

    def count_triggered(self, session_date: str, ticker: str | None = None) -> int:
        if "triggered_budget" not in self._tables:
            return 0
        sql = "SELECT COUNT(*) FROM triggered_budget WHERE session_date = ?"
        params: list[Any] = [session_date]
        if ticker:
            sql += " AND ticker = ?"
            params.append(ticker.upper())
        return self._conn.execute(sql, params).fetchone()[0]

    def get_ticker_state(self, ticker: str) -> dict[str, Any]:
        rows = self._rows("ticker_state", "SELECT * FROM ticker_state WHERE ticker = ?",
                          (ticker.upper(),))
        return rows[0] if rows else {}

    def signal_log(self, since: str | None) -> list[dict[str, Any]]:
        sql = "SELECT ticker, signal_type, fired_ts, details FROM signal_log"
        params: tuple = ()
        if since:
            sql += " WHERE fired_ts >= ?"
            params = (since,)
        rows = self._rows("signal_log", sql + " ORDER BY fired_ts ASC, id ASC", params)
        for r in rows:
            r["details"] = _loads(r["details"], {})
        return rows

    def route_log(self, since: str | None) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM route_log"
        params: tuple = ()
        if since:
            sql += " WHERE evaluated_ts >= ?"
            params = (since,)
        return [_loads(r["payload"], {}) for r in self._rows(
            "route_log", sql + " ORDER BY evaluated_ts ASC, id ASC", params)]

    def advice_positions(self) -> dict[str, list[tuple[str, float | None]]]:
        out: dict[str, list[tuple[str, float | None]]] = defaultdict(list)
        for r in self._rows("advice_log",
                            "SELECT ticker, advised_ts, quantity FROM advice_log ORDER BY advised_ts"):
            out[r["ticker"]].append((r["advised_ts"], r["quantity"]))
        return out


@dataclass
class ScanEvent:
    """One routed scan reconstructed from history."""

    ticker: str
    ts: datetime
    signals: list[str]
    price: float | None = None
    prev_close: float | None = None
    atr: float | None = None
    position_state: PositionState = PositionState.UNKNOWN
    source: str = "signal_log"


@dataclass
class ReplayReport:
    events: int = 0
    triggers: int = 0
    routes: Counter = field(default_factory=Counter)
    routes_by_ticker: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    routes_by_position: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    triggers_by_ticker: Counter = field(default_factory=Counter)
    paid_calls: Counter = field(default_factory=Counter)
    budget_suppressed: int = 0
    shadow_downgraded: int = 0
    plan_freshness: Counter = field(default_factory=Counter)
    reminders_without_dedup: int = 0
    reminders_with_dedup: int = 0
    minutes_to_leave_range: list[float] = field(default_factory=list)
    never_left_range: int = 0
    route_log_rows: int = 0
    route_log_mismatches: int = 0
    costs: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        est = sum(self.paid_calls[r] * self.costs.get(r, 0.0) for r in self.paid_calls)
        routed = self.events or 1
        leave = self.minutes_to_leave_range
        return {
            "events": self.events,
            "triggers": self.triggers,
            "simultaneous_trigger_collapse_rate": round(
                1 - (self.events / self.triggers), 4) if self.triggers else 0.0,
            "triggers_per_routed_scan": round(self.triggers / routed, 3),
            "routes": dict(self.routes),
            "routes_by_position_state": {k: dict(v) for k, v in self.routes_by_position.items()},
            "routes_by_ticker": {k: dict(v) for k, v in sorted(self.routes_by_ticker.items())},
            "triggers_by_ticker": dict(sorted(self.triggers_by_ticker.items())),
            "estimated_paid_calls": dict(self.paid_calls),
            "estimated_cost_usd": round(est, 4),
            "cost_assumptions_usd": dict(self.costs),
            "budget_suppressions": self.budget_suppressed,
            "shadow_downgrades": self.shadow_downgraded,
            "plan_freshness": dict(self.plan_freshness),
            "missing_plan_rate": round(self.plan_freshness.get("missing", 0) / routed, 4),
            "stale_plan_rate": round(
                (self.plan_freshness.get("expired", 0) + self.plan_freshness.get("invalid", 0))
                / routed, 4),
            "reminders_without_dedup": self.reminders_without_dedup,
            "reminders_with_dedup": self.reminders_with_dedup,
            "minutes_to_leave_execution_range": {
                "n": len(leave),
                "median": round(statistics.median(leave), 1) if leave else None,
                "mean": round(statistics.fmean(leave), 1) if leave else None,
                "never_left_in_data": self.never_left_range,
            },
            "route_log_rows_rechecked": self.route_log_rows,
            "route_log_mismatches": self.route_log_mismatches,
        }


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _position_at(history: list[tuple[str, float | None]], ts: datetime) -> PositionState:
    state = PositionState.UNKNOWN
    for advised_ts, qty in history:
        if _parse_ts(advised_ts) > ts:
            break
        state = PositionState.HELD if qty else PositionState.WATCH
    return state


def group_signal_log(rows: list[dict[str, Any]],
                     positions: dict[str, list[tuple[str, float | None]]]) -> list[ScanEvent]:
    """Collapse signal rows of one ticker fired within the same scan into one event."""
    events: list[ScanEvent] = []
    last: dict[str, ScanEvent] = {}
    for r in rows:
        if r["signal_type"] == "take_profit_zone":
            continue  # the take-profit path is its own trigger, not a router input here
        ts = _parse_ts(r["fired_ts"])
        t = r["ticker"].upper()
        d = r.get("details") or {}
        ev = last.get(t)
        if ev is not None and (ts - ev.ts).total_seconds() <= SCAN_GROUP_SECONDS:
            if r["signal_type"] not in ev.signals:
                ev.signals.append(r["signal_type"])
            continue
        ev = ScanEvent(
            ticker=t, ts=ts, signals=[r["signal_type"]],
            price=d.get("current_price"), prev_close=d.get("prev_close"),
            atr=d.get("avg_atr_20d") or d.get("atr"),
            position_state=_position_at(positions.get(t, []), ts),
        )
        last[t] = ev
        events.append(ev)
    return events


def load_events_csv(path: str) -> list[ScanEvent]:
    """Read-only research export: ticker, ts, signal[, price, prev_close, atr, position_state]."""
    groups: dict[tuple[str, str], ScanEvent] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["ticker"].upper(), row["ts"])
            ev = groups.get(key)
            if ev is None:
                def f(name):
                    v = (row.get(name) or "").strip()
                    return float(v) if v else None
                pos = (row.get("position_state") or "unknown").strip().lower()
                ev = ScanEvent(
                    ticker=key[0], ts=_parse_ts(row["ts"]), signals=[],
                    price=f("price"), prev_close=f("prev_close"), atr=f("atr"),
                    position_state=PositionState(pos) if pos in PositionState._value2member_map_
                    else PositionState.UNKNOWN,
                    source="csv",
                )
                groups[key] = ev
            if row.get("signal") and row["signal"] not in ev.signals:
                ev.signals.append(row["signal"])
    return sorted(groups.values(), key=lambda e: (e.ts, e.ticker))


def _plan_at(plans: list[Any], ticker: str, ts: datetime) -> Any:
    """The weekly base plan in force for a ticker at ``ts`` (latest valid one
    created at or before it)."""
    best = None
    for p in plans:
        if p.ticker != ticker or p.kind != "weekly_base" or p.validation_errors:
            continue
        try:
            if _parse_ts(p.created_ts) <= ts:
                best = p
        except ValueError:
            continue
    if best is None:
        return None
    from dataclasses import replace

    status = best.status
    if status == "superseded":
        status = "active"          # it was the plan in force at ts
    elif status in ("invalidated", "deactivated") and best.deactivated_ts:
        try:
            if _parse_ts(best.deactivated_ts) > ts:
                status = "active"  # withdrawn only later
        except ValueError:
            pass
    return replace(best, status=status)


def run_replay(
    store: ReadOnlyStore,
    *,
    since: str | None = None,
    events_csv: str | None = None,
    assume_enabled: bool = True,
    max_per_ticker: int = 1,
    max_global: int = 2,
    bearish_shock_atr: float = 0.75,
    approach_atr: float = 0.5,
    renotify_h: float = 6.0,
    costs: dict[Route, float] | None = None,
) -> ReplayReport:
    """Deterministic replay; the same inputs always produce the same report."""
    costs = costs or DEFAULT_COSTS
    report = ReplayReport(costs={r.value: c for r, c in costs.items()})
    plans = store.plans()

    events = group_signal_log(store.signal_log(since), store.advice_positions())
    if events_csv:
        events += load_events_csv(events_csv)
    events.sort(key=lambda e: (e.ts, e.ticker))

    budget_ticker: Counter = Counter()
    budget_global: Counter = Counter()
    for ev in events:
        session = session_label(ev.ts).isoformat()
        plan = _plan_at(plans, ev.ticker, ev.ts)
        fresh = plan_freshness(plan, session_label(ev.ts))
        st = plan_state(plan, fresh, ev.price, ev.atr, approach_atr=approach_atr)
        decision = route(RouterInput(
            ticker=ev.ticker, position_state=ev.position_state, plan=plan, freshness=fresh,
            plan_state=st, signals=list(ev.signals), price=ev.price, prev_close=ev.prev_close,
            atr=ev.atr, triggered_enabled=assume_enabled, digest_available=plan is not None,
            budget_ticker_used=budget_ticker[(session, ev.ticker)],
            budget_global_used=budget_global[session],
            max_per_ticker=max_per_ticker, max_global=max_global,
            bearish_shock_atr=bearish_shock_atr,
        ))
        report.events += 1
        report.triggers += len(ev.signals)
        report.triggers_by_ticker[ev.ticker] += len(ev.signals)
        report.routes[decision.route.value] += 1
        report.routes_by_ticker[ev.ticker][decision.route.value] += 1
        report.routes_by_position[ev.position_state.value][decision.route.value] += 1
        report.plan_freshness[fresh.value] += 1
        if decision.effective_route in PAID_ROUTES:
            report.paid_calls[decision.effective_route.value] += 1
            budget_ticker[(session, ev.ticker)] += 1
            budget_global[session] += 1
        elif decision.budget_result == "budget_exhausted":
            report.budget_suppressed += 1
        elif decision.budget_result == "disabled":
            report.shadow_downgraded += 1

    _replay_route_log(store.route_log(since), report, approach_atr=approach_atr,
                      renotify_h=renotify_h, plans={p.id: p for p in plans},
                      bearish_shock_atr=bearish_shock_atr)
    return report


def reroute_logged(record: dict[str, Any], plans: dict[int, Any],
                   bearish_shock_atr: float = 0.75) -> Route:
    """Recompute the router's (pre-downgrade) route from a logged ROUTE record."""
    from watchy.plan import PlanFreshness

    plan = plans.get(record.get("plan_id"))
    cooled = set(record.get("cooled_down") or [])
    transition = record.get("plan_transition")
    decision = route(RouterInput(
        ticker=record.get("ticker", ""),
        position_state=PositionState(record.get("position_state") or "unknown"),
        plan=plan,
        freshness=PlanFreshness(record.get("plan_freshness") or "missing"),
        plan_state=ReminderState(record["plan_state"]) if record.get("plan_state") else None,
        plan_transition=transition if transition in ("entered", "left_buy_zone") else "",
        signals=[s for s in record.get("triggers") or [] if s not in cooled],
        cooled_down=sorted(cooled),
        price=record.get("price"),
        prev_close=record.get("prev_close"),
        atr=record.get("atr"),
        take_profit_fire=bool(record.get("take_profit_fire")),
        triggered_enabled=True,
        digest_available=True,
        bearish_shock_atr=bearish_shock_atr,
    ))
    return decision.route


def _replay_route_log(rows: list[dict[str, Any]], report: ReplayReport, *,
                      approach_atr: float, renotify_h: float,
                      plans: dict[int, Any] | None = None,
                      bearish_shock_atr: float = 0.75) -> None:
    """Dedup and timing statistics from 2.0 shadow evaluations, plus a
    determinism re-check of each logged route."""
    plans = plans or {}
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("ticker") and r.get("evaluated_ts"):
            by_ticker[r["ticker"]].append(r)
    in_range = {ReminderState.IN_BUY_ZONE.value, ReminderState.APPROACHING_BUY.value}
    for ticker, seq in by_ticker.items():
        prev: dict[str, Any] = {}
        for i, r in enumerate(seq):
            report.route_log_rows += 1
            try:
                if r.get("route") and reroute_logged(r, plans, bearish_shock_atr).value != r["route"]:
                    report.route_log_mismatches += 1
            except (ValueError, KeyError):
                report.route_log_mismatches += 1
            state = r.get("plan_state")
            if state is None:
                continue
            ts = _parse_ts(r["evaluated_ts"])
            if ReminderState(state) in NOTIFY_STATES:
                report.reminders_without_dedup += 1
            t = detect_transition(prev, r.get("plan_id"), ReminderState(state), ts,
                                  renotify_h=renotify_h)
            if t.notify:
                report.reminders_with_dedup += 1
            prev = {"plan_id": r.get("plan_id"), "state": state,
                    "state_since_ts": t.state_since_ts, "notified": t.notified}
            triggered = bool(r.get("triggers")) or (t.notify and state in in_range)
            if triggered and state in in_range:
                left = next(
                    (_parse_ts(n["evaluated_ts"]) for n in seq[i + 1:]
                     if n.get("plan_state") not in in_range),
                    None,
                )
                if left is None:
                    report.never_left_range += 1
                else:
                    report.minutes_to_leave_range.append((left - ts).total_seconds() / 60)


def render_text(report: ReplayReport) -> str:
    d = report.as_dict()
    lines = [
        "Watchy 2.0 routing replay — volume and timing only (not profitability)",
        f"routed scans: {d['events']}   triggers: {d['triggers']}   "
        f"collapse rate: {d['simultaneous_trigger_collapse_rate']:.1%}",
        f"routes: {d['routes']}",
        f"by position state: {d['routes_by_position_state']}",
        f"estimated paid calls: {d['estimated_paid_calls']}   "
        f"estimated cost: ${d['estimated_cost_usd']:.2f} (assumptions {d['cost_assumptions_usd']})",
        f"budget suppressions: {d['budget_suppressions']}   shadow downgrades: {d['shadow_downgrades']}",
        f"plan freshness at trigger: {d['plan_freshness']}   missing-plan rate: "
        f"{d['missing_plan_rate']:.1%}   stale/invalid-plan rate: {d['stale_plan_rate']:.1%}",
        f"reminders without dedup: {d['reminders_without_dedup']}   with dedup: "
        f"{d['reminders_with_dedup']}",
        f"minutes to leave execution range: {d['minutes_to_leave_execution_range']}",
        "per ticker:",
    ]
    for t, routes in d["routes_by_ticker"].items():
        lines.append(f"  {t}: triggers={d['triggers_by_ticker'].get(t, 0)} routes={routes}")
    return "\n".join(lines)


def to_json(report: ReplayReport) -> str:
    return json.dumps(report.as_dict(), indent=2, sort_keys=True)


def guard_output_path(out: str | None, repo_root: Path) -> None:
    """Refuse to write a replay report inside the repository (it can contain
    private position-derived data)."""
    if not out:
        return
    target = Path(out).expanduser().resolve()
    root = repo_root.resolve()
    if target == root or root in target.parents:
        raise ValueError(f"refusing to write replay output inside the repository: {target}")


def since_days(days: int | None) -> str | None:
    if not days:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


__all__ = [
    "ReadOnlyStore", "ReplayReport", "ScanEvent", "run_replay", "render_text",
    "to_json", "load_events_csv", "group_signal_log", "guard_output_path", "since_days",
]
