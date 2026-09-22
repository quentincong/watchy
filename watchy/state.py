"""SQLite state store for crossover detection, cooldown, and run history."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = os.path.expanduser("~/watchy/state.db")

# PRAGMA user_version after this build's migrations. 0 = any 1.x database (1.x
# never set it). Bumped only by additive, backward-compatible migrations.
SCHEMA_VERSION = 2


def _ensure_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


class StateStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        _ensure_dir(db_path)
        self.db_path = db_path
        # One connection shared across scheduler threads (check_same_thread=False);
        # serialize every access with a reentrant lock to avoid "database is locked".
        self._lock = threading.RLock()
        self.backup_path: str | None = None
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._refuse_newer_schema(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        try:
            self._backup_before_upgrade()
            self._init_schema()
            self._migrate()
        except Exception as exc:
            # Never recreate or discard a live database: stop loudly instead.
            self._conn.close()
            raise RuntimeError(
                f"state.db migration failed for {db_path}: {exc}. The database "
                "was left in place; restore the pre-upgrade backup next to it "
                "if needed and fix the error before restarting."
            ) from exc

    def _refuse_newer_schema(self, db_path: str) -> None:
        """Stop before touching a database written by a newer Watchy.

        Running older code against it would stamp ``user_version`` back down to
        this build's SCHEMA_VERSION and write rows the newer schema may not
        expect. Checked before any statement that can modify the file.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self._conn.close()
            raise RuntimeError(
                f"state.db at {db_path} has schema version {version}, newer than "
                f"this Watchy build supports ({SCHEMA_VERSION}). Refusing to start "
                "so the database is not downgraded; deploy the newer Watchy code "
                "(or restore a backup made for this version)."
            )

    def _backup_before_upgrade(self) -> None:
        """Copy an existing 1.x database aside once before the 2.0 migration.

        Only fires when the file already holds Watchy tables and an older
        user_version, so a fresh database (tests, new installs) never gets one.
        The copy uses SQLite's online backup API, which is safe under WAL.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        has_tables = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ticker_state'"
        ).fetchone()
        if not has_tables:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = f"{self.db_path}.v{version}-backup-{stamp}"
        dest = sqlite3.connect(backup_path)
        try:
            self._conn.backup(dest)
        finally:
            dest.close()
        self.backup_path = backup_path

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS ticker_state (
                ticker TEXT PRIMARY KEY,
                prev_sma_50_above_200 INTEGER,       -- bool: previous MA relationship
                prev_macd_above_signal INTEGER,       -- bool: previous MACD relationship
                prev_rsi REAL,                         -- last RSI value
                prev_atr REAL,                         -- last ATR value
                avg_volume_20d REAL,                   -- 20-day average volume
                avg_atr_20d REAL,                      -- 20-day average ATR
                -- transition flags for level-based signals (#8): bool of whether
                -- the condition held last scan, so we only fire on entry.
                prev_bollinger_above_upper INTEGER,
                prev_bollinger_below_lower INTEGER,
                prev_volume_anomaly INTEGER,
                prev_atr_spike INTEGER,
                last_full_analysis_ts TEXT,            -- ISO timestamp of last Tier 2 run
                derived_target_price REAL,             -- auto-derived target from analysis (#16)
                derived_target_ts TEXT,                -- when the derived target was last set
                -- take-profit zone membership last Tier 1 scan (#28): fire the
                -- intraday zone-entry trigger only on the transition into it.
                prev_take_profit_zone INTEGER,
                -- share count seen last Tier 1 scan (#28): a drop means a
                -- sell-limit filled, which re-arms the zone-entry trigger.
                prev_quantity REAL,
                updated_ts TEXT                        -- last update timestamp
            );

            CREATE TABLE IF NOT EXISTS signal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                fired_ts TEXT NOT NULL,
                details TEXT,                           -- JSON with signal context
                notified INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS run_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                tier TEXT NOT NULL,                     -- 'tier1' or 'tier2'
                trigger_type TEXT,                      -- signal type or 'scheduled'
                started_ts TEXT NOT NULL,
                completed_ts TEXT,
                success INTEGER DEFAULT 0,
                summary TEXT
            );

            -- Generic key/value scratch space for daemon-level state that isn't
            -- per-ticker (e.g. Schwab token-health dedup markers). A brand-new
            -- table, so CREATE IF NOT EXISTS also covers the live VPS db.
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_ts TEXT
            );

            -- Every advisor decision, with the book state that produced it, so
            -- decisions can be scored against forward returns later (#31). Four
            -- model evaluations have now been decided on proxies because nothing
            -- records what was advised, at what price, on what holding. Another
            -- brand-new table, so CREATE IF NOT EXISTS covers the live VPS db.
            CREATE TABLE IF NOT EXISTS advice_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                advised_ts TEXT NOT NULL,               -- ISO UTC, decision time
                ticker TEXT NOT NULL,
                source TEXT,                            -- tier1 / tier2 / take_profit_zone
                model TEXT,                             -- llm.model at call time
                thinking_level TEXT,                    -- effort at call time
                decision TEXT,                          -- BUY/ADD/HOLD/TRIM/SELL
                urgency TEXT,                           -- LOW/MEDIUM/HIGH
                target TEXT,                            -- raw Target field
                take_profit TEXT,                       -- post-gate Take-Profit field
                price REAL,                             -- mark the decision was made at
                quantity REAL,                          -- shares held (NULL = flat)
                average_cost REAL,
                gain_pct REAL,                          -- unrealized % at decision time
                zone_armed INTEGER                      -- was #28 guidance injected
            );

            -- Watchy 2.0 weekly plans and event overrides. History is kept:
            -- rows are superseded / deactivated, never overwritten or deleted.
            CREATE TABLE IF NOT EXISTS analysis_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                kind TEXT NOT NULL,                     -- weekly_base / event_override
                schema_version INTEGER NOT NULL,
                status TEXT NOT NULL,                   -- active/invalid/superseded/deactivated
                parent_plan_id INTEGER,                 -- override -> its weekly base
                decision TEXT,
                urgency TEXT,
                thesis TEXT,
                buy_zone_low REAL,
                buy_zone_high REAL,
                chase_ceiling REAL,
                invalidation_level REAL,
                invalidation_condition TEXT,
                trim_condition TEXT,
                resistance_low REAL,
                resistance_high REAL,
                take_profit_price REAL,
                guidance TEXT,
                dont_do TEXT,
                upstream_verdict TEXT,
                valid_from_session TEXT,
                expires_after_session TEXT,
                input_price REAL,
                input_price_ts TEXT,
                source_ref TEXT,                        -- JSON provenance
                validation_status TEXT,                 -- valid / invalid
                validation_errors TEXT,                 -- JSON list
                validation_warnings TEXT,               -- JSON list
                raw_output TEXT,                        -- advisor text, for diagnosis
                created_ts TEXT NOT NULL,
                activated_ts TEXT,
                superseded_ts TEXT,
                deactivated_ts TEXT
            );

            -- Last plan-relative state per ticker, so reminders fire only on
            -- transitions and stay deduplicated across daemon restarts.
            CREATE TABLE IF NOT EXISTS plan_reminder_state (
                ticker TEXT PRIMARY KEY,
                plan_id INTEGER,
                state TEXT,
                state_since_ts TEXT,
                notified TEXT,                          -- JSON {state: last notified ts}
                updated_ts TEXT
            );

            -- Paid triggered-analysis reservations, keyed by exchange session.
            -- Reserved atomically before a call, so budgets survive restarts
            -- and concurrent tickers cannot both take the last global slot.
            CREATE TABLE IF NOT EXISTS triggered_budget (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                kind TEXT NOT NULL,                     -- FAST_RECHECK / TRIGGERED_RISK
                reserved_ts TEXT NOT NULL,
                outcome TEXT                            -- ok / failed / NULL = running
            );

            -- One row per Tier 1 routing evaluation (shadow-mode observability
            -- and the zero-cost replay input).
            CREATE TABLE IF NOT EXISTS route_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluated_ts TEXT NOT NULL,
                session_date TEXT,
                ticker TEXT NOT NULL,
                position_state TEXT,
                route TEXT,                             -- router's chosen route
                effective_route TEXT,                   -- after enablement/budget
                plan_id INTEGER,
                plan_state TEXT,
                status TEXT,                            -- Telegram status, if sent
                llm_invoked INTEGER DEFAULT 0,
                payload TEXT                            -- full JSON record
            );

            CREATE INDEX IF NOT EXISTS idx_analysis_plan_ticker
                ON analysis_plan(ticker, kind, status, created_ts);
            CREATE INDEX IF NOT EXISTS idx_triggered_budget_session
                ON triggered_budget(session_date, ticker);
            CREATE INDEX IF NOT EXISTS idx_route_log_ticker
                ON route_log(ticker, evaluated_ts);
            CREATE INDEX IF NOT EXISTS idx_signal_log_ticker
                ON signal_log(ticker, signal_type);
            CREATE INDEX IF NOT EXISTS idx_signal_log_fired
                ON signal_log(fired_ts);
            CREATE INDEX IF NOT EXISTS idx_run_history_ticker
                ON run_history(ticker, started_ts);
            CREATE INDEX IF NOT EXISTS idx_advice_log_ticker
                ON advice_log(ticker, advised_ts);
        """)
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema to a pre-existing DB.

        `CREATE TABLE IF NOT EXISTS` never alters an existing table, so the live
        VPS `state.db` won't gain new columns from a schema bump. ALTER TABLE each
        missing column instead (idempotent — only adds what's absent). (#8)
        """
        new_columns = {
            "prev_bollinger_above_upper": "INTEGER",
            "prev_bollinger_below_lower": "INTEGER",
            "prev_volume_anomaly": "INTEGER",
            "prev_atr_spike": "INTEGER",
            # #16 auto-derived Tier 2 proximity target (and its freshness stamp).
            "derived_target_price": "REAL",
            "derived_target_ts": "TEXT",
            # #28 take-profit zone membership (for on-entry transition detection).
            "prev_take_profit_zone": "INTEGER",
            # #28 follow-up: last-seen share count, so a fill re-arms that trigger.
            "prev_quantity": "REAL",
        }
        with self._lock:
            existing = {
                row[1] for row in self._conn.execute("PRAGMA table_info(ticker_state)")
            }
            added = []
            for col, col_type in new_columns.items():
                if col not in existing:
                    self._conn.execute(
                        f"ALTER TABLE ticker_state ADD COLUMN {col} {col_type}"
                    )
                    added.append(col)
            if added:
                self._conn.commit()
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()

    # --- ticker state ---

    def get_ticker_state(self, ticker: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ticker_state WHERE ticker = ?", (ticker.upper(),)
            ).fetchone()
            if row is None:
                return {}
            cols = [d[0] for d in self._conn.execute(
                "SELECT * FROM ticker_state LIMIT 0"
            ).description]
            return dict(zip(cols, row))

    def save_ticker_state(self, ticker: str, **kwargs: Any) -> None:
        kwargs.setdefault("updated_ts", _now_iso())
        columns = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values())
        with self._lock:
            self._conn.execute(
                f"INSERT INTO ticker_state (ticker, {', '.join(kwargs)}) "
                f"VALUES (?, {', '.join('?' for _ in kwargs)}) "
                f"ON CONFLICT(ticker) DO UPDATE SET {columns}",
                [ticker.upper()] + vals + vals,
            )
            self._conn.commit()

    # --- signal cooldown ---

    def is_in_cooldown(self, ticker: str, signal_type: str, cooldown_hours: float) -> bool:
        since = (datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM signal_log WHERE ticker = ? AND signal_type = ? "
                "AND fired_ts > ? LIMIT 1",
                (ticker.upper(), signal_type, since),
            ).fetchone()
        return row is not None

    def log_signal(self, ticker: str, signal_type: str, details: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO signal_log (ticker, signal_type, fired_ts, details) "
                "VALUES (?, ?, ?, ?)",
                (
                    ticker.upper(),
                    signal_type,
                    _now_iso(),
                    json.dumps(details) if details else None,
                ),
            )
            self._conn.commit()

    def mark_notified(self, ticker: str, signal_type: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE signal_log SET notified = 1 "
                "WHERE ticker = ? AND signal_type = ? AND notified = 0",
                (ticker.upper(), signal_type),
            )
            self._conn.commit()

    # --- run history ---

    def start_run(self, ticker: str, tier: str, trigger_type: str = "scheduled") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO run_history (ticker, tier, trigger_type, started_ts) "
                "VALUES (?, ?, ?, ?)",
                (ticker.upper(), tier, trigger_type, _now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def count_tier1_runs_today(self, ticker: str) -> int:
        """Count Tier 1 pipeline runs launched for a ticker since UTC midnight.

        Used by the Tier 1 daily rescan cap (#23). Counts every launched run
        (start_run row) regardless of success, since each consumed LLM budget.
        """
        midnight = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM run_history "
                "WHERE ticker = ? AND tier = 'tier1' AND started_ts >= ?",
                (ticker.upper(), midnight),
            ).fetchone()
        return row[0] if row else 0

    def complete_run(self, run_id: int, success: bool, summary: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE run_history SET completed_ts = ?, success = ?, summary = ? "
                "WHERE id = ?",
                (_now_iso(), int(success), summary, run_id),
            )
            self._conn.commit()

    # --- advice log (#31) ---

    def log_advice(
        self,
        ticker: str,
        *,
        source: str = "",
        model: str = "",
        thinking_level: str = "",
        decision: str = "",
        urgency: str = "",
        target: str = "",
        take_profit: str = "",
        price: float | None = None,
        quantity: float | None = None,
        average_cost: float | None = None,
        gain_pct: float | None = None,
        zone_armed: bool = False,
    ) -> int:
        """Record one advisor decision with the book state that produced it.

        The row is the unit a forward-return score is computed over later, so it
        carries the *inputs* to the decision (price, shares, cost basis, gain,
        whether the take-profit zone was armed) alongside the decision itself —
        a decision can't be scored without knowing what it was made against.
        ``model`` and ``thinking_level`` are recorded per row so a later model
        switch splits the history cleanly instead of contaminating it.
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO advice_log ("
                "advised_ts, ticker, source, model, thinking_level, decision, "
                "urgency, target, take_profit, price, quantity, average_cost, "
                "gain_pct, zone_armed"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now_iso(),
                    ticker.upper(),
                    source,
                    model,
                    thinking_level,
                    decision,
                    urgency,
                    target,
                    take_profit,
                    price,
                    quantity,
                    average_cost,
                    gain_pct,
                    int(zone_armed),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_advice_log(
        self, ticker: str | None = None, since_ts: str | None = None
    ) -> list[dict[str, Any]]:
        """Read advice rows oldest-first, optionally filtered by ticker / start.

        Oldest-first because the consumer is a forward-return scorer walking
        decisions forward in time.
        """
        sql = "SELECT * FROM advice_log"
        clauses: list[str] = []
        params: list[Any] = []
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        if since_ts:
            clauses.append("advised_ts >= ?")
            params.append(since_ts)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY advised_ts ASC, id ASC"
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # --- generic key/value ---

    def get_kv(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value, updated_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_ts = excluded.updated_ts",
                (key, value, _now_iso()),
            )
            self._conn.commit()

    # --- Watchy 2.0: analysis plans ---

    _PLAN_COLUMNS = (
        "ticker", "kind", "schema_version", "status", "parent_plan_id",
        "decision", "urgency", "thesis", "buy_zone_low", "buy_zone_high",
        "chase_ceiling", "invalidation_level", "invalidation_condition",
        "trim_condition", "resistance_low", "resistance_high",
        "take_profit_price", "guidance", "dont_do", "upstream_verdict",
        "valid_from_session", "expires_after_session", "input_price",
        "input_price_ts", "created_ts", "activated_ts",
    )

    def insert_plan(self, plan: Any, raw_output: str = "") -> int:
        """Persist a WeeklyPlan (valid or not) as a new history row.

        A valid weekly base plan atomically supersedes the ticker's previous
        active weekly base; an invalid one leaves it untouched (its own expiry
        date then decides whether it is still usable — never extended here).
        Event overrides never touch the base plan.
        """
        now = _now_iso()
        values = {c: getattr(plan, c) for c in self._PLAN_COLUMNS}
        values["ticker"] = plan.ticker.upper()
        valid = not plan.validation_errors and plan.status == "active"
        if valid and not values["activated_ts"]:
            values["activated_ts"] = now
        cols = list(values) + [
            "source_ref", "validation_status", "validation_errors",
            "validation_warnings", "raw_output",
        ]
        vals = list(values.values()) + [
            json.dumps(plan.source_ref or {}, default=str),
            "valid" if not plan.validation_errors else "invalid",
            json.dumps(plan.validation_errors or []),
            json.dumps(plan.validation_warnings or []),
            raw_output,
        ]
        with self._lock:
            if valid and plan.kind == "weekly_base":
                self._conn.execute(
                    "UPDATE analysis_plan SET status = 'superseded', superseded_ts = ? "
                    "WHERE ticker = ? AND kind = 'weekly_base' AND status = 'active'",
                    (now, values["ticker"]),
                )
            cur = self._conn.execute(
                f"INSERT INTO analysis_plan ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                vals,
            )
            self._conn.commit()
            plan.id = cur.lastrowid
            plan.activated_ts = values["activated_ts"]
            return cur.lastrowid

    def get_active_plan(self, ticker: str, kind: str = "weekly_base") -> Any:
        """Latest row with status ``active`` (it may still be past its expiry
        date — callers classify freshness with plan.plan_freshness)."""
        return self._one_plan(
            "WHERE ticker = ? AND kind = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (ticker.upper(), kind),
        )

    def get_current_plan(self, ticker: str) -> Any:
        """The weekly base plan Tier 1 should reason about: the newest one that
        was ever activated (active, invalidated or deactivated), so a plan that
        was invalidated this week is shown as such instead of vanishing.
        Superseded and never-valid rows are skipped."""
        return self._one_plan(
            "WHERE ticker = ? AND kind = 'weekly_base' "
            "AND status IN ('active', 'invalidated', 'deactivated') "
            "ORDER BY id DESC LIMIT 1",
            (ticker.upper(),),
        )

    def get_latest_plan(self, ticker: str, kind: str = "weekly_base") -> Any:
        """Latest row of a kind regardless of status (e.g. a failed refresh)."""
        return self._one_plan(
            "WHERE ticker = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (ticker.upper(), kind),
        )

    def get_latest_override(self, ticker: str, parent_plan_id: int | None) -> Any:
        if parent_plan_id is None:
            return None
        return self._one_plan(
            "WHERE ticker = ? AND kind = 'event_override' AND parent_plan_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (ticker.upper(), parent_plan_id),
        )

    def get_plan(self, plan_id: int) -> Any:
        return self._one_plan("WHERE id = ?", (plan_id,))

    def get_plan_history(self, ticker: str, limit: int = 20) -> list[Any]:
        return self._plans(
            "WHERE ticker = ? ORDER BY id DESC LIMIT ?", (ticker.upper(), limit)
        )

    def get_plans_between(self, since_ts: str | None = None) -> list[Any]:
        """All plan rows oldest-first (replay input)."""
        if since_ts:
            return self._plans("WHERE created_ts >= ? ORDER BY id ASC", (since_ts,))
        return self._plans("ORDER BY id ASC", ())

    def deactivate_plan(self, plan_id: int, status: str = "deactivated") -> bool:
        """Take a plan out of force without deleting its history.

        ``deactivated`` = manual expiry; ``invalidated`` = the thesis broke
        (price crossed invalidation, or a watch-only death cross). Either way
        the row stays, and so does every older row.
        """
        if status not in ("deactivated", "invalidated"):
            raise ValueError(f"unsupported plan status {status!r}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE analysis_plan SET status = ?, deactivated_ts = ? "
                "WHERE id = ? AND status = 'active'",
                (status, _now_iso(), plan_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def _one_plan(self, where: str, params: tuple) -> Any:
        rows = self._plans(where, params)
        return rows[0] if rows else None

    def _plans(self, where: str, params: tuple) -> list[Any]:
        with self._lock:
            cur = self._conn.execute(f"SELECT * FROM analysis_plan {where}", params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return [row_to_plan(row) for row in rows]

    # --- Watchy 2.0: reminder state ---

    def get_reminder_state(self, ticker: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT plan_id, state, state_since_ts, notified, updated_ts "
                "FROM plan_reminder_state WHERE ticker = ?",
                (ticker.upper(),),
            ).fetchone()
        if row is None:
            return {}
        return {
            "plan_id": row[0],
            "state": row[1],
            "state_since_ts": row[2],
            "notified": _loads(row[3], {}),
            "updated_ts": row[4],
        }

    def save_reminder_state(
        self,
        ticker: str,
        *,
        plan_id: int | None,
        state: str,
        state_since_ts: str,
        notified: dict[str, str],
    ) -> None:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO plan_reminder_state "
                "(ticker, plan_id, state, state_since_ts, notified, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET "
                "plan_id = excluded.plan_id, state = excluded.state, "
                "state_since_ts = excluded.state_since_ts, "
                "notified = excluded.notified, updated_ts = excluded.updated_ts",
                (ticker.upper(), plan_id, state, state_since_ts,
                 json.dumps(notified), now),
            )
            self._conn.commit()

    # --- Watchy 2.0: paid triggered-analysis budget ---

    def count_triggered(self, session_date: str, ticker: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM triggered_budget WHERE session_date = ?"
        params: list[Any] = [session_date]
        if ticker:
            sql += " AND ticker = ?"
            params.append(ticker.upper())
        with self._lock:
            return self._conn.execute(sql, params).fetchone()[0]

    def try_reserve_triggered(
        self,
        session_date: str,
        ticker: str,
        kind: str,
        per_ticker_max: int,
        global_max: int,
    ) -> int | None:
        """Atomically reserve one paid analysis slot; None when a cap is hit.

        Check and insert happen under one lock and one transaction, so two
        tickers racing for the last global slot cannot both win.
        """
        with self._lock:
            n_ticker = self.count_triggered(session_date, ticker)
            n_global = self.count_triggered(session_date)
            if n_ticker >= per_ticker_max or n_global >= global_max:
                return None
            cur = self._conn.execute(
                "INSERT INTO triggered_budget (session_date, ticker, kind, reserved_ts) "
                "VALUES (?, ?, ?, ?)",
                (session_date, ticker.upper(), kind, _now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def finish_triggered(self, reservation_id: int, outcome: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE triggered_budget SET outcome = ? WHERE id = ?",
                (outcome, reservation_id),
            )
            self._conn.commit()

    # --- Watchy 2.0: route log ---

    def log_route(self, record: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO route_log (evaluated_ts, session_date, ticker, "
                "position_state, route, effective_route, plan_id, plan_state, "
                "status, llm_invoked, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.get("evaluated_ts") or _now_iso(),
                    record.get("session"),
                    str(record.get("ticker", "")).upper(),
                    record.get("position_state"),
                    record.get("route"),
                    record.get("effective_route"),
                    record.get("plan_id"),
                    record.get("plan_state"),
                    record.get("status"),
                    int(bool(record.get("llm_invoked"))),
                    json.dumps(record, default=str),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_route_log(
        self, ticker: str | None = None, since_ts: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM route_log"
        clauses: list[str] = []
        params: list[Any] = []
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        if since_ts:
            clauses.append("evaluated_ts >= ?")
            params.append(since_ts)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY evaluated_ts ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_loads(r[0], {}) for r in rows]

    def get_signal_log(self, since_ts: str | None = None) -> list[dict[str, Any]]:
        """Signal rows oldest-first with details decoded (replay input)."""
        sql = "SELECT ticker, signal_type, fired_ts, details FROM signal_log"
        params: list[Any] = []
        if since_ts:
            sql += " WHERE fired_ts >= ?"
            params.append(since_ts)
        sql += " ORDER BY fired_ts ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {"ticker": r[0], "signal_type": r[1], "fired_ts": r[2],
             "details": _loads(r[3], {})}
            for r in rows
        ]

    # --- housekeeping ---

    def close(self) -> None:
        self._conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def row_to_plan(row: dict[str, Any]) -> Any:
    """An analysis_plan row (as a dict) → WeeklyPlan. Shared with the
    read-only replay reader so both decode plans identically."""
    from watchy.plan import WeeklyPlan

    kwargs = {
        k: row[k] for k in WeeklyPlan.__dataclass_fields__
        if k in row and k not in ("source_ref", "validation_errors", "validation_warnings")
    }
    for k in ("thesis", "invalidation_condition", "trim_condition", "guidance",
              "dont_do", "upstream_verdict", "decision", "urgency", "input_price_ts",
              "valid_from_session", "expires_after_session"):
        if kwargs.get(k) is None:
            kwargs[k] = ""
    plan = WeeklyPlan(**kwargs)
    plan.source_ref = _loads(row.get("source_ref"), {})
    plan.validation_errors = _loads(row.get("validation_errors"), [])
    plan.validation_warnings = _loads(row.get("validation_warnings"), [])
    return plan
