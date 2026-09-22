"""Watchy 2.0 Phase 8 — zero-cost replay (deterministic, no LLM) and operator CLI."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tests.fixtures_v2 import make_plan
from watchy.replay import ReadOnlyStore, guard_output_path, render_text, run_replay, to_json
from watchy.state import StateStore

MON = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)


def _seed(path):
    s = StateStore(str(path))
    pid = s.insert_plan(make_plan(created_ts=(MON - timedelta(hours=3)).isoformat()))
    rows = [
        ("NVDA", "macd_bearish_cross", MON, {"current_price": 122.0, "prev_close": 124.0,
                                              "avg_atr_20d": 3.0}),
        ("NVDA", "atr_spike", MON + timedelta(seconds=5), {"current_price": 122.0}),
        ("NVDA", "bollinger_lower_breach", MON + timedelta(hours=2),
         {"current_price": 121.0, "prev_close": 124.5, "avg_atr_20d": 3.0}),
        ("AMZN", "golden_cross", MON + timedelta(hours=1), {"current_price": 200.0, "atr": 4.0}),
        ("AMZN", "take_profit_zone", MON + timedelta(hours=1), {}),
    ]
    for t, sig, ts, det in rows:
        s._conn.execute("INSERT INTO signal_log (ticker, signal_type, fired_ts, details) "
                        "VALUES (?, ?, ?, ?)", (t, sig, ts.isoformat(), json.dumps(det)))
    s._conn.execute("INSERT INTO advice_log (advised_ts, ticker, quantity) VALUES (?, 'NVDA', 3)",
                    ((MON - timedelta(days=1)).isoformat(),))
    s._conn.commit()
    # 2.0 shadow evaluations: in zone for 2 scans, then out, then back (flapping)
    states = ["inside_buy_zone", "inside_buy_zone", "outside", "inside_buy_zone", "above_chase_ceiling"]
    for i, st in enumerate(states):
        s.log_route({"evaluated_ts": (MON + timedelta(minutes=30 * i)).isoformat(),
                     "ticker": "NVDA", "session": "2026-09-21", "plan_id": pid,
                     "plan_freshness": "active", "plan_state": st, "position_state": "held",
                     "triggers": ["macd_bearish_cross"] if i == 0 else [], "cooled_down": [],
                     "plan_transition": "entered" if i == 0 else "unchanged",
                     "price": 122.0, "atr": 3.0, "route": "FAST_RECHECK" if i == 0 else "NO_ACTION"})
    s.close()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    _seed(path)
    return str(path)


class TestReplay:
    def test_report_contents(self, db):
        store = ReadOnlyStore(db)
        rep = run_replay(store).as_dict()
        store.close()
        assert rep["events"] == 3 and rep["triggers"] == 4       # TP rows excluded, pair collapsed
        assert rep["simultaneous_trigger_collapse_rate"] == 0.25
        assert rep["routes_by_position_state"]["held"]           # from advice_log quantity
        # -0.67 ATR move is below the 0.75 shock threshold → Fast Recheck, and the
        # second NVDA trigger that session hits the 1/ticker cap
        assert rep["estimated_paid_calls"] == {"FAST_RECHECK": 1}
        assert rep["estimated_cost_usd"] == 0.01
        assert rep["budget_suppressions"] == 1
        assert rep["reminders_without_dedup"] == 4
        assert rep["reminders_with_dedup"] < rep["reminders_without_dedup"]
        assert rep["minutes_to_leave_execution_range"]["n"] >= 1
        assert rep["route_log_mismatches"] == 0
        assert "plan_freshness" in rep and rep["missing_plan_rate"] < 1

    def test_deterministic(self, db):
        a = ReadOnlyStore(db)
        b = ReadOnlyStore(db)
        assert to_json(run_replay(a)) == to_json(run_replay(b))
        a.close()
        b.close()

    def test_makes_no_llm_or_network_calls_and_no_writes(self, db, tmp_path):
        import os
        before = os.path.getmtime(db)
        boom = AssertionError("replay must not call an LLM / pipeline / network")
        with patch("watchy.advisor.get_advice", side_effect=boom), \
             patch("watchy.advisor._call_gemini", side_effect=boom), \
             patch("watchy.orchestrator.run_pipeline", side_effect=boom), \
             patch("watchy.indicators.compute_indicators", side_effect=boom), \
             patch("urllib.request.urlopen", side_effect=boom):
            store = ReadOnlyStore(db)
            text = render_text(run_replay(store))
            with pytest.raises(Exception):
                store._conn.execute("INSERT INTO kv (key, value) VALUES ('x', 'y')")
            store.close()
        assert "volume and timing only" in text
        assert os.path.getmtime(db) == before

    def test_shadow_replay_counts_downgrades(self, db):
        store = ReadOnlyStore(db)
        rep = run_replay(store, assume_enabled=False).as_dict()
        store.close()
        assert rep["estimated_paid_calls"] == {} and rep["shadow_downgrades"] >= 1

    def test_1x_database_without_v2_tables(self, tmp_path):
        import sqlite3
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE ticker_state (ticker TEXT PRIMARY KEY);"
            "CREATE TABLE signal_log (id INTEGER PRIMARY KEY, ticker TEXT, signal_type TEXT,"
            " fired_ts TEXT, details TEXT, notified INTEGER);"
            "INSERT INTO signal_log (ticker, signal_type, fired_ts, details) VALUES"
            " ('NVDA', 'death_cross', '2026-06-01T15:00:00+00:00', '{\"current_price\": 100}');")
        conn.commit()
        conn.close()
        store = ReadOnlyStore(str(path))
        rep = run_replay(store).as_dict()
        store.close()
        assert rep["events"] == 1 and rep["missing_plan_rate"] == 1.0
        assert rep["routes"] == {"TRIGGERED_RISK": 1}   # unknown position → held rules

    def test_events_csv_read_only(self, db, tmp_path):
        csv_path = tmp_path / "events.csv"
        csv_path.write_text("ticker,ts,signal,price,prev_close,atr,position_state\n"
                            "TSM,2026-09-21T15:00:00+00:00,death_cross,150,151,3,held\n",
                            encoding="utf-8")
        store = ReadOnlyStore(db)
        rep = run_replay(store, events_csv=str(csv_path)).as_dict()
        store.close()
        assert rep["triggers_by_ticker"]["TSM"] == 1

    def test_output_guard(self, tmp_path):
        from watchy.ctl import REPO_ROOT
        with pytest.raises(ValueError):
            guard_output_path(str(REPO_ROOT / "reports" / "x.json"), REPO_ROOT)
        guard_output_path(str(tmp_path / "x.json"), REPO_ROOT)


class TestCtl:
    def test_plan_show_and_expire(self, db, capsys):
        from watchy.ctl import main
        assert main(["--db", db, "plan", "show", "NVDA"]) == 0
        out = capsys.readouterr().out
        assert "buy_zone=121.0-123.0" in out and "reminder state" in out
        assert main(["--db", db, "plan", "expire", "NVDA"]) == 2        # needs --yes
        assert main(["--db", db, "plan", "expire", "NVDA", "--yes"]) == 0
        s = StateStore(db)
        assert s.get_active_plan("NVDA") is None and s.get_plan_history("NVDA")
        s.close()

    def test_route_and_preview_are_dry(self, db, tmp_path, capsys):
        import os
        from watchy.ctl import main
        cfg = tmp_path / "c.yaml"
        cfg.write_text("watchlist: [NVDA]\ntier2_schedule: weekly\n", encoding="utf-8")
        before = os.path.getmtime(db)
        boom = AssertionError("dry run must not call an LLM or send")
        with patch("watchy.advisor._call_gemini", side_effect=boom), \
             patch("watchy.notify.TelegramNotifier._post", side_effect=boom), \
             patch("watchy.config._merge_secrets", side_effect=lambda c, p: c):
            assert main(["--config", str(cfg), "--db", db, "route", "NVDA", "--price", "122",
                         "--signal", "macd_bearish_cross", "--position", "held"]) == 0
            out = json.loads(capsys.readouterr().out)
            assert out["route"] == "FAST_RECHECK" and out["effective_route"] == "NOTIFY_ONLY"
            assert main(["--config", str(cfg), "--db", db, "preview", "NVDA", "--price", "126",
                         "--position", "watch_only"]) == 0
            text = capsys.readouterr().out
        assert "not sent" in text and "NVDA —" in text
        assert os.path.getmtime(db) == before

    def test_status_shows_mode(self, db, tmp_path, capsys):
        from watchy.ctl import main
        cfg = tmp_path / "c.yaml"
        cfg.write_text("watchlist: [NVDA]\ntier2_schedule: daily\n", encoding="utf-8")
        with patch("watchy.config._merge_secrets", side_effect=lambda c, p: c):
            assert main(["--config", str(cfg), "--db", db, "status"]) == 0
        out = capsys.readouterr().out
        assert "ROLLBACK" in out and "SHADOW MODE" in out and "user_version=2" in out

    def test_weekly_requires_yes(self, db, capsys):
        from watchy.ctl import main
        assert main(["--db", db, "weekly", "NVDA"]) == 2
        assert "PAID" in capsys.readouterr().out

    def test_replay_cli(self, db, tmp_path, capsys):
        from watchy.ctl import main
        cfg = tmp_path / "c.yaml"
        cfg.write_text("watchlist: [NVDA]\n", encoding="utf-8")
        with patch("watchy.config._merge_secrets", side_effect=lambda c, p: c):
            assert main(["--config", str(cfg), "--db", db, "replay", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["events"] == 3
