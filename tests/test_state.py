"""Tests for SQLite state store: CRUD, cooldown, run history."""

import os
import sqlite3
import tempfile
import threading

import pytest

from watchy.state import StateStore


@pytest.fixture
def store():
    """Create a StateStore backed by a temporary SQLite file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = StateStore(path)
    yield s
    s.close()
    os.unlink(path)


class TestTickerState:
    def test_initial_state_is_empty(self, store):
        assert store.get_ticker_state("NVDA") == {}

    def test_save_and_retrieve(self, store):
        store.save_ticker_state("NVDA", prev_rsi=45.5, prev_sma_50_above_200=1)
        state = store.get_ticker_state("NVDA")
        assert state["prev_rsi"] == 45.5
        assert state["prev_sma_50_above_200"] == 1

    def test_update_existing(self, store):
        store.save_ticker_state("AAPL", prev_rsi=60.0)
        store.save_ticker_state("AAPL", prev_rsi=30.0)
        state = store.get_ticker_state("AAPL")
        assert state["prev_rsi"] == 30.0

    def test_ticker_case_insensitive(self, store):
        store.save_ticker_state("nvda", prev_rsi=50.0)
        assert store.get_ticker_state("NVDA")["prev_rsi"] == 50.0

    def test_multiple_tickers(self, store):
        store.save_ticker_state("A", prev_rsi=1.0)
        store.save_ticker_state("B", prev_rsi=2.0)
        assert store.get_ticker_state("A")["prev_rsi"] == 1.0
        assert store.get_ticker_state("B")["prev_rsi"] == 2.0


class TestSignalLog:
    def test_log_and_check_cooldown(self, store):
        store.log_signal("NVDA", "rsi_oversold", {"rsi": 25.0})

        # Should be in cooldown for 12 hours
        assert store.is_in_cooldown("NVDA", "rsi_oversold", 12.0) is True

        # Should NOT be in cooldown for 0 hours (already expired)
        assert store.is_in_cooldown("NVDA", "rsi_oversold", 0.0) is False

    def test_different_signal_types_are_independent(self, store):
        store.log_signal("NVDA", "rsi_oversold")
        assert store.is_in_cooldown("NVDA", "macd_bullish_cross", 24.0) is False

    def test_different_tickers_are_independent(self, store):
        store.log_signal("NVDA", "rsi_oversold")
        assert store.is_in_cooldown("TSLA", "rsi_oversold", 12.0) is False


class TestMigration:
    """The live VPS state.db predates the #8 level-signal columns; _migrate must
    ALTER TABLE them in (CREATE TABLE IF NOT EXISTS won't)."""

    NEW_COLS = [
        "prev_bollinger_above_upper",
        "prev_bollinger_below_lower",
        "prev_volume_anomaly",
        "prev_atr_spike",
        # #28 take-profit zone membership
        "prev_take_profit_zone",
    ]

    def _make_pre_migration_db(self, path):
        """Create a ticker_state table with the *old* schema (no #8 columns)."""
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE ticker_state (
                ticker TEXT PRIMARY KEY,
                prev_sma_50_above_200 INTEGER,
                prev_macd_above_signal INTEGER,
                prev_rsi REAL,
                prev_atr REAL,
                avg_volume_20d REAL,
                avg_atr_20d REAL,
                last_full_analysis_ts TEXT,
                updated_ts TEXT
            );
        """)
        conn.execute(
            "INSERT INTO ticker_state (ticker, prev_rsi) VALUES ('NVDA', 55.0)"
        )
        conn.commit()
        conn.close()

    def test_migrate_adds_missing_columns_to_existing_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            self._make_pre_migration_db(path)
            # Opening via StateStore should migrate in place.
            store = StateStore(path)
            cols = {row[1] for row in store._conn.execute("PRAGMA table_info(ticker_state)")}
            for c in self.NEW_COLS:
                assert c in cols, f"{c} not added by migration"
            # existing data survives
            assert store.get_ticker_state("NVDA")["prev_rsi"] == 55.0
            # and the new columns are writable/readable
            store.save_ticker_state("NVDA", prev_volume_anomaly=1)
            assert store.get_ticker_state("NVDA")["prev_volume_anomaly"] == 1
            store.close()
        finally:
            os.unlink(path)

    def test_migrate_is_idempotent(self, store):
        """Running migrate again on an already-migrated DB is a no-op, no error."""
        store._migrate()
        store._migrate()
        cols = {row[1] for row in store._conn.execute("PRAGMA table_info(ticker_state)")}
        for c in self.NEW_COLS:
            assert c in cols


class TestConcurrency:
    def test_concurrent_writes_no_lock_error(self, store):
        """Many threads writing the shared connection must not raise
        'database is locked' — the RLock serializes access (#9)."""
        errors: list[Exception] = []
        barrier = threading.Barrier(16)

        def worker(n: int):
            try:
                barrier.wait()
                for i in range(20):
                    store.save_ticker_state(f"T{n}", prev_rsi=float(i))
                    store.log_signal(f"T{n}", "rsi_oversold")
                    store.is_in_cooldown(f"T{n}", "rsi_oversold", 1.0)
                    rid = store.start_run(f"T{n}", "tier1")
                    store.complete_run(rid, success=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # final write of each ticker landed
        assert store.get_ticker_state("T0")["prev_rsi"] == 19.0


class TestRunHistory:
    def test_start_and_complete_run(self, store):
        run_id = store.start_run("NVDA", "tier1", "rsi_oversold")
        assert isinstance(run_id, int)
        assert run_id > 0

        store.complete_run(run_id, success=True, summary="All good")
        # No error = success

    def test_run_ids_are_sequential(self, store):
        id1 = store.start_run("A", "tier1")
        id2 = store.start_run("B", "tier2")
        assert id2 > id1


class TestTier1RunCount:
    """count_tier1_runs_today backs the Tier 1 daily rescan cap (#23)."""

    def test_counts_only_tier1_for_ticker(self, store):
        store.start_run("NVDA", "tier1", "rsi_oversold")
        store.start_run("NVDA", "tier1", "atr_spike")
        store.start_run("NVDA", "tier2", "scheduled_daily")  # tier2 excluded
        store.start_run("AAPL", "tier1", "rsi_oversold")     # other ticker excluded
        assert store.count_tier1_runs_today("NVDA") == 2
        assert store.count_tier1_runs_today("AAPL") == 1
        assert store.count_tier1_runs_today("TSLA") == 0

    def test_case_insensitive(self, store):
        store.start_run("nvda", "tier1")
        assert store.count_tier1_runs_today("NVDA") == 1

    def test_excludes_earlier_utc_days(self, store):
        # A run stamped yesterday must not count toward today's cap.
        store._conn.execute(
            "INSERT INTO run_history (ticker, tier, trigger_type, started_ts) "
            "VALUES ('NVDA', 'tier1', 'rsi_oversold', '2000-01-01T12:00:00+00:00')"
        )
        store._conn.commit()
        store.start_run("NVDA", "tier1")  # today
        assert store.count_tier1_runs_today("NVDA") == 1


class TestAdviceLog:
    """#31: every advisor decision is recorded with the book state behind it."""

    def test_round_trip(self, store):
        store.log_advice(
            "NVDA",
            source="tier2",
            model="gemini-3.5-flash",
            thinking_level="low",
            decision="ADD",
            urgency="LOW",
            target="200.00 - 210.00",
            take_profit="",
            price=189.0,
            quantity=3.0,
            average_cost=163.33,
            gain_pct=15.7,
            zone_armed=False,
        )
        rows = store.get_advice_log()
        assert len(rows) == 1
        row = rows[0]
        assert row["ticker"] == "NVDA"
        assert row["source"] == "tier2"
        assert row["model"] == "gemini-3.5-flash"
        assert row["thinking_level"] == "low"
        assert row["decision"] == "ADD"
        assert row["urgency"] == "LOW"
        assert row["price"] == 189.0
        assert row["quantity"] == 3.0
        assert row["average_cost"] == 163.33
        assert row["gain_pct"] == 15.7
        assert row["zone_armed"] == 0
        assert row["advised_ts"]

    def test_flat_position_logs_nulls_not_zeros(self, store):
        # A ticker with no holding must be distinguishable from a zeroed one:
        # forward-return scoring treats "not held" and "held 0 shares" alike
        # only if NULL survives the write.
        store.log_advice("AMD", decision="BUY", price=140.0)
        row = store.get_advice_log()[0]
        assert row["quantity"] is None
        assert row["average_cost"] is None
        assert row["gain_pct"] is None

    def test_zone_armed_stored_as_int(self, store):
        store.log_advice("NVDA", decision="TRIM", zone_armed=True)
        assert store.get_advice_log()[0]["zone_armed"] == 1

    def test_ticker_filter_is_case_insensitive(self, store):
        store.log_advice("NVDA", decision="HOLD")
        store.log_advice("AMD", decision="BUY")
        rows = store.get_advice_log(ticker="nvda")
        assert [r["ticker"] for r in rows] == ["NVDA"]

    def test_since_ts_filter(self, store):
        store._conn.execute(
            "INSERT INTO advice_log (advised_ts, ticker, decision) "
            "VALUES ('2000-01-01T12:00:00+00:00', 'NVDA', 'HOLD')"
        )
        store._conn.commit()
        store.log_advice("NVDA", decision="ADD")
        rows = store.get_advice_log(since_ts="2020-01-01T00:00:00+00:00")
        assert [r["decision"] for r in rows] == ["ADD"]

    def test_ordered_oldest_first(self, store):
        # The consumer walks decisions forward in time against later prices.
        store._conn.execute(
            "INSERT INTO advice_log (advised_ts, ticker, decision) "
            "VALUES ('2000-01-01T12:00:00+00:00', 'NVDA', 'OLD')"
        )
        store._conn.commit()
        store.log_advice("NVDA", decision="NEW")
        assert [r["decision"] for r in store.get_advice_log()] == ["OLD", "NEW"]

    def test_table_is_created_on_a_preexisting_db(self):
        # The live VPS state.db predates this table. It's a brand-new table, so
        # CREATE TABLE IF NOT EXISTS covers it with no ALTER migration — but
        # only if reopening an existing file actually re-runs the schema.
        import os
        import tempfile

        from watchy.state import StateStore

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = StateStore(path)
            first._conn.execute("DROP TABLE advice_log")
            first._conn.commit()
            first.close()

            reopened = StateStore(path)
            reopened.log_advice("NVDA", decision="HOLD")
            assert len(reopened.get_advice_log()) == 1
            reopened.close()
        finally:
            os.unlink(path)


# --- Watchy 2.0 persistence (Phase 1) ---

from tests.fixtures_v2 import make_plan  # noqa: E402
from watchy.state import SCHEMA_VERSION  # noqa: E402


_V1_SCHEMA = """
CREATE TABLE ticker_state (
    ticker TEXT PRIMARY KEY, prev_sma_50_above_200 INTEGER,
    prev_macd_above_signal INTEGER, prev_rsi REAL, prev_atr REAL,
    avg_volume_20d REAL, avg_atr_20d REAL, last_full_analysis_ts TEXT,
    updated_ts TEXT
);
CREATE TABLE signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
    signal_type TEXT NOT NULL, fired_ts TEXT NOT NULL, details TEXT,
    notified INTEGER DEFAULT 0
);
CREATE TABLE run_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, tier TEXT NOT NULL,
    trigger_type TEXT, started_ts TEXT NOT NULL, completed_ts TEXT,
    success INTEGER DEFAULT 0, summary TEXT
);
INSERT INTO ticker_state (ticker, prev_rsi, updated_ts) VALUES ('NVDA', 55.5, '2026-06-01');
INSERT INTO signal_log (ticker, signal_type, fired_ts, details)
    VALUES ('NVDA', 'rsi_oversold', '2026-06-01T14:00:00+00:00', '{"current_price": 120.0}');
INSERT INTO run_history (ticker, tier, trigger_type, started_ts, success)
    VALUES ('NVDA', 'tier2', 'scheduled_daily', '2026-06-01T10:02:00+00:00', 1);
"""


@pytest.fixture
def v1_db(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(_V1_SCHEMA)
    conn.commit()
    conn.close()
    return path


class TestV2Migration:
    def test_upgrade_preserves_data_and_adds_tables(self, v1_db):
        s = StateStore(str(v1_db))
        try:
            assert s.get_ticker_state("NVDA")["prev_rsi"] == 55.5
            assert s.get_signal_log()[0]["details"]["current_price"] == 120.0
            tables = {r[0] for r in s._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"analysis_plan", "plan_reminder_state", "triggered_budget",
                    "route_log", "advice_log", "kv"} <= tables
            cols = {r[1] for r in s._conn.execute("PRAGMA table_info(ticker_state)")}
            assert {"prev_take_profit_zone", "prev_quantity"} <= cols
            assert s._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            s.close()

    def test_upgrade_takes_one_backup(self, v1_db):
        s = StateStore(str(v1_db))
        backup = s.backup_path
        s.close()
        assert backup and os.path.exists(backup)
        conn = sqlite3.connect(backup)
        assert conn.execute("SELECT prev_rsi FROM ticker_state").fetchone()[0] == 55.5
        conn.close()
        s2 = StateStore(str(v1_db))  # already migrated → no second backup
        assert s2.backup_path is None
        s2.close()

    def test_fresh_db_has_no_backup(self, store):
        assert store.backup_path is None

    def test_migration_failure_is_loud_and_keeps_db(self, v1_db, monkeypatch):
        def boom(self):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(StateStore, "_migrate", boom)
        with pytest.raises(RuntimeError, match="left in place"):
            StateStore(str(v1_db))
        conn = sqlite3.connect(v1_db)
        assert conn.execute("SELECT prev_rsi FROM ticker_state").fetchone()[0] == 55.5
        conn.close()


class TestPlanPersistence:
    def test_round_trip(self, store):
        plan = make_plan(source_ref={"run_id": 7})
        pid = store.insert_plan(plan, raw_output="raw")
        got = store.get_active_plan("nvda")
        assert got.id == pid and got.buy_zone_low == 121.0
        assert got.source_ref == {"run_id": 7}
        assert got.validation_errors == [] and got.activated_ts

    def test_new_weekly_supersedes_previous(self, store):
        first = store.insert_plan(make_plan())
        second = store.insert_plan(make_plan(valid_from_session="2026-09-28",
                                             expires_after_session="2026-10-02"))
        assert store.get_active_plan("NVDA").id == second
        assert store.get_plan(first).status == "superseded"
        assert len(store.get_plan_history("NVDA")) == 2  # history kept

    def test_invalid_refresh_keeps_previous_row(self, store):
        good = store.insert_plan(make_plan())
        bad = store.insert_plan(make_plan(decision="MAYBE"))
        assert store.get_active_plan("NVDA").id == good
        assert store.get_latest_plan("NVDA").id == bad
        assert store.get_plan(bad).validation_errors

    def test_override_never_touches_base(self, store):
        base = store.insert_plan(make_plan())
        override = make_plan(kind="event_override", parent_plan_id=base)
        oid = store.insert_plan(override)
        assert store.get_active_plan("NVDA").id == base
        assert store.get_latest_override("NVDA", base).id == oid

    def test_deactivate_keeps_history(self, store):
        pid = store.insert_plan(make_plan())
        assert store.deactivate_plan(pid) is True
        assert store.get_active_plan("NVDA") is None
        assert store.get_plan(pid).status == "deactivated"
        assert store.deactivate_plan(pid) is False


class TestReminderAndBudget:
    def test_reminder_state_round_trip(self, store):
        assert store.get_reminder_state("NVDA") == {}
        store.save_reminder_state("NVDA", plan_id=1, state="inside_buy_zone",
                                  state_since_ts="t0", notified={"inside_buy_zone": "t0"})
        got = store.get_reminder_state("nvda")
        assert got["state"] == "inside_buy_zone" and got["notified"] == {"inside_buy_zone": "t0"}

    def test_budget_caps(self, store):
        assert store.try_reserve_triggered("2026-09-21", "NVDA", "FAST_RECHECK", 1, 2)
        assert store.try_reserve_triggered("2026-09-21", "NVDA", "FAST_RECHECK", 1, 2) is None
        assert store.try_reserve_triggered("2026-09-21", "AMZN", "TRIGGERED_RISK", 1, 2)
        assert store.try_reserve_triggered("2026-09-21", "TSM", "FAST_RECHECK", 1, 2) is None
        # next exchange session resets
        assert store.try_reserve_triggered("2026-09-22", "TSM", "FAST_RECHECK", 1, 2)

    def test_budget_is_atomic_under_concurrency(self, store):
        wins = []

        def worker(t):
            if store.try_reserve_triggered("2026-09-21", t, "FAST_RECHECK", 1, 2):
                wins.append(t)

        threads = [threading.Thread(target=worker, args=(f"T{i}",)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(wins) == 2

    def test_route_log_round_trip(self, store):
        store.log_route({"ticker": "nvda", "route": "NOTIFY_ONLY", "session": "2026-09-21",
                         "triggers": ["macd_bearish_cross"]})
        rows = store.get_route_log("NVDA")
        assert rows[0]["triggers"] == ["macd_bearish_cross"]
