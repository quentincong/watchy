"""Tests for the layered position source (#4): file backend, cache, fallback chain."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from watchy.positions import (
    AccountSummary,
    FilePositionSource,
    Position,
    PositionCache,
    PositionSource,
    RobustPositionSource,
    _format_age,
    get_position_source,
    render_portfolio,
    render_position,
)


# --- helpers ---

def _write_positions(tmp_path, body: str):
    p = tmp_path / "positions.yaml"
    p.write_text(body)
    return str(p)


class _FakeLive(PositionSource):
    """Stand-in for SchwabClient: returns a preset summary, or raises, or None."""

    def __init__(self, summary=None, raises=False):
        self._summary = summary
        self._raises = raises

    def get_position(self, ticker):  # pragma: no cover - unused by composite
        return None

    def get_all_positions(self):  # pragma: no cover - unused by composite
        return []

    def get_account_summary(self):
        if self._raises:
            raise RuntimeError("token expired")
        return self._summary


# --- FilePositionSource ---

class TestFilePositionSource:
    def test_missing_file_yields_nothing(self, tmp_path):
        src = FilePositionSource(str(tmp_path / "nope.yaml"), enrich=False)
        assert src.get_all_positions() == []
        assert src.get_position("NVDA") is None
        assert src.get_account_summary() is None

    def test_parses_positions_case_insensitive(self, tmp_path):
        path = _write_positions(tmp_path, """
positions:
  - ticker: nvda
    quantity: 100
    average_cost: 120.5
""")
        src = FilePositionSource(path, enrich=False)
        pos = src.get_position("NVDA")
        assert pos is not None
        assert pos.ticker == "NVDA"
        assert pos.quantity == 100
        assert pos.average_cost == 120.5

    def test_skips_malformed_entries(self, tmp_path):
        path = _write_positions(tmp_path, """
positions:
  - ticker: NVDA
    quantity: 100
    average_cost: 120.5
  - ticker: BROKEN          # missing quantity/average_cost
  - not_a_mapping
""")
        src = FilePositionSource(path, enrich=False)
        tickers = [p.ticker for p in src.get_all_positions()]
        assert tickers == ["NVDA"]

    def test_pinned_current_price_skips_fetch(self, tmp_path, monkeypatch):
        path = _write_positions(tmp_path, """
positions:
  - ticker: AAPL
    quantity: 10
    average_cost: 100.0
    current_price: 150.0
""")
        # Enrich on, but a pinned price must mean no live fetch.
        called = {"n": 0}
        monkeypatch.setattr(
            "watchy.positions._latest_price",
            lambda t: called.__setitem__("n", called["n"] + 1) or 999.0,
        )
        src = FilePositionSource(path, enrich=True)
        pos = src.get_position("AAPL")
        assert called["n"] == 0
        assert pos.current_price == 150.0
        assert pos.market_value == 1500.0
        assert pos.unrealized_pnl == 500.0
        assert pos.unrealized_pnl_pct == pytest.approx(50.0)

    def test_enrich_fetches_and_derives_pnl(self, tmp_path, monkeypatch):
        path = _write_positions(tmp_path, """
positions:
  - ticker: NVDA
    quantity: 100
    average_cost: 120.0
""")
        monkeypatch.setattr("watchy.positions._latest_price", lambda t: 130.0)
        src = FilePositionSource(path, enrich=True)
        pos = src.get_position("NVDA")
        assert pos.current_price == 130.0
        assert pos.market_value == 13000.0
        assert pos.unrealized_pnl == 1000.0
        assert pos.unrealized_pnl_pct == pytest.approx(8.333, abs=1e-2)

    def test_as_of_prefers_explicit_field(self, tmp_path):
        path = _write_positions(tmp_path, """
as_of: 2026-06-05
positions:
  - ticker: NVDA
    quantity: 10
    average_cost: 100.0
""")
        src = FilePositionSource(path, enrich=False)
        ts = src.as_of()
        assert ts is not None
        assert (ts.year, ts.month, ts.day) == (2026, 6, 5)
        assert ts.tzinfo is not None  # coerced to tz-aware UTC

    def test_as_of_falls_back_to_mtime(self, tmp_path):
        path = _write_positions(tmp_path, """
positions:
  - ticker: NVDA
    quantity: 10
    average_cost: 100.0
""")
        src = FilePositionSource(path, enrich=False)
        ts = src.as_of()
        assert ts is not None
        # mtime ~ now; should be very recent and tz-aware.
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        assert 0 <= age < 60

    def test_as_of_none_when_missing_file(self, tmp_path):
        assert FilePositionSource(str(tmp_path / "nope.yaml")).as_of() is None

    def test_account_summary_totals_market_value(self, tmp_path, monkeypatch):
        path = _write_positions(tmp_path, """
positions:
  - ticker: NVDA
    quantity: 10
    average_cost: 100.0
    current_price: 120.0
  - ticker: AAPL
    quantity: 5
    average_cost: 100.0
    current_price: 200.0
""")
        src = FilePositionSource(path, enrich=False)
        summary = src.get_account_summary()
        assert summary.total_value == pytest.approx(1200.0 + 1000.0)
        assert summary.buying_power is None  # unknown for a file backend
        assert summary.cash_balance is None  # no cash field → not reported

    def test_account_summary_includes_cash_in_total(self, tmp_path, monkeypatch):
        # Cash is counted into total value so concentration uses the full account.
        path = _write_positions(tmp_path, """
cash: 1700.0
positions:
  - ticker: EMR
    quantity: 4
    average_cost: 100.0
    current_price: 105.0
""")
        src = FilePositionSource(path, enrich=False)
        summary = src.get_account_summary()
        assert summary.cash_balance == pytest.approx(1700.0)
        # stocks = 420, + 1700 cash = 2120 total
        assert summary.total_value == pytest.approx(420.0 + 1700.0)

    def test_cash_only_no_positions(self, tmp_path, monkeypatch):
        path = _write_positions(tmp_path, "cash: 500.0\n")
        src = FilePositionSource(path, enrich=False)
        summary = src.get_account_summary()
        assert summary is not None
        assert summary.total_value == pytest.approx(500.0)
        assert summary.positions == []

    def test_invalid_cash_ignored(self, tmp_path, monkeypatch):
        path = _write_positions(tmp_path, """
cash: "lots"
positions:
  - ticker: EMR
    quantity: 4
    average_cost: 100.0
    current_price: 105.0
""")
        src = FilePositionSource(path, enrich=False)
        summary = src.get_account_summary()
        assert summary.cash_balance is None
        assert summary.total_value == pytest.approx(420.0)

    def test_total_account_value_is_authoritative(self, tmp_path, monkeypatch):
        # Preferred input: a single account-total figure used directly as the
        # denominator; the buffer above equities surfaces as cash.
        path = _write_positions(tmp_path, """
total_account_value: 3340.0
positions:
  - ticker: EMR
    quantity: 4
    average_cost: 100.0
    current_price: 105.0
  - ticker: NVDA
    quantity: 8
    average_cost: 150.0
    current_price: 160.0
""")
        src = FilePositionSource(path, enrich=False)
        summary = src.get_account_summary()
        assert summary.total_value == pytest.approx(3340.0)
        assert summary.cash_balance == pytest.approx(1640.0)  # 3340 - 1700 equities
        assert "12.6%" in render_portfolio(summary)  # EMR 420 / 3340

    def test_total_account_value_wins_over_cash(self, tmp_path, monkeypatch):
        path = _write_positions(tmp_path, """
total_account_value: 3340.0
cash: 999.0
positions:
  - ticker: EMR
    quantity: 4
    average_cost: 100.0
    current_price: 105.0
""")
        summary = FilePositionSource(path, enrich=False).get_account_summary()
        assert summary.total_value == pytest.approx(3340.0)  # not 420 + 999

    def test_total_account_value_below_stocks_ignored(self, tmp_path, monkeypatch):
        # An inconsistent total (< equities) is dropped rather than producing
        # >100% weights; falls back to equities (+ cash if any).
        path = _write_positions(tmp_path, """
total_account_value: 100.0
positions:
  - ticker: EMR
    quantity: 4
    average_cost: 100.0
    current_price: 105.0
""")
        summary = FilePositionSource(path, enrich=False).get_account_summary()
        assert summary.total_value == pytest.approx(420.0)

    def test_total_account_value_cash_only(self, tmp_path, monkeypatch):
        path = _write_positions(tmp_path, "total_account_value: 500.0\n")
        summary = FilePositionSource(path, enrich=False).get_account_summary()
        assert summary is not None
        assert summary.total_value == pytest.approx(500.0)
        assert summary.cash_balance == pytest.approx(500.0)
        assert summary.positions == []

    def test_concentration_weight_uses_full_account(self, tmp_path, monkeypatch):
        # The user's scenario: $420 EMR, $1,280 other stocks ($1,700 equities),
        # $1,640 cash → $3,340 account. EMR is 24.7% of stocks-only but a healthy
        # 12.6% of the full account — render_portfolio must show the latter.
        path = _write_positions(tmp_path, """
cash: 1640.0
positions:
  - ticker: EMR
    quantity: 4
    average_cost: 100.0
    current_price: 105.0
  - ticker: NVDA
    quantity: 8
    average_cost: 150.0
    current_price: 160.0
""")
        src = FilePositionSource(path, enrich=False)
        text = render_portfolio(src.get_account_summary())
        assert "12.6%" in text   # 420 / 3340, not 420 / 1700 (24.7%)
        assert "24.7%" not in text
        assert "Cash: $1,640.00" in text


# --- PositionCache ---

class TestPositionCache:
    def test_write_then_read_roundtrips(self, tmp_path):
        cache = PositionCache(str(tmp_path / "cache.json"))
        summary = AccountSummary(
            account_id="X1",
            total_value=5000.0,
            buying_power=1000.0,
            cash_balance=500.0,
            positions=[Position(ticker="NVDA", quantity=10, average_cost=100.0,
                                market_value=1200.0)],
        )
        cache.write(summary)
        got = cache.read()
        assert got is not None
        restored, fetched_at = got
        assert restored.account_id == "X1"
        assert restored.total_value == 5000.0
        assert restored.positions[0].ticker == "NVDA"
        assert isinstance(fetched_at, datetime)

    def test_read_missing_returns_none(self, tmp_path):
        assert PositionCache(str(tmp_path / "absent.json")).read() is None

    def test_read_corrupt_returns_none(self, tmp_path):
        p = tmp_path / "cache.json"
        p.write_text("{not valid json")
        assert PositionCache(str(p)).read() is None


# --- RobustPositionSource fallback chain ---

def _summary(tag, mv=1000.0):
    return AccountSummary(
        account_id=tag,
        total_value=mv,
        positions=[Position(ticker="NVDA", quantity=10, average_cost=90.0,
                            market_value=mv, current_price=mv / 10)],
    )


class TestRobustFallbackChain:
    def test_live_wins_and_is_cached(self, tmp_path):
        cache = PositionCache(str(tmp_path / "c.json"))
        src = RobustPositionSource(
            live=_FakeLive(summary=_summary("live")),
            cache=cache,
            file_source=FilePositionSource(str(tmp_path / "none.yaml"), enrich=False),
        )
        assert src.get_account_summary().account_id == "live"
        # The successful fetch was persisted to the cache.
        assert cache.read()[0].account_id == "live"
        ctx = src.format_position_context("NVDA")
        assert "Schwab (live)" in ctx

    def test_falls_back_to_cache_when_live_unavailable(self, tmp_path):
        cache = PositionCache(str(tmp_path / "c.json"))
        cache.write(_summary("cached"))
        src = RobustPositionSource(
            live=_FakeLive(summary=None),
            cache=cache,
            file_source=FilePositionSource(str(tmp_path / "none.yaml"), enrich=False),
        )
        assert src.get_account_summary().account_id == "cached"
        assert "cache" in src.format_position_context("NVDA")

    def test_falls_back_to_cache_when_live_raises(self, tmp_path):
        cache = PositionCache(str(tmp_path / "c.json"))
        cache.write(_summary("cached"))
        src = RobustPositionSource(
            live=_FakeLive(raises=True),
            cache=cache,
            file_source=FilePositionSource(str(tmp_path / "none.yaml"), enrich=False),
        )
        assert src.get_account_summary().account_id == "cached"

    def test_falls_back_to_file_when_no_live_no_cache(self, tmp_path):
        path = _write_positions(tmp_path, """
positions:
  - ticker: NVDA
    quantity: 10
    average_cost: 90.0
    current_price: 100.0
""")
        src = RobustPositionSource(
            live=_FakeLive(summary=None),
            cache=PositionCache(str(tmp_path / "absent.json")),
            file_source=FilePositionSource(path, enrich=False),
        )
        summary = src.get_account_summary()
        assert summary.account_id == "manual"
        ctx = src.format_position_context("NVDA")
        assert "manual file" in ctx
        assert "old)" in ctx  # staleness age appended (from file mtime)

    def test_file_fallback_uses_explicit_as_of(self, tmp_path):
        path = _write_positions(tmp_path, """
as_of: 2026-06-05
positions:
  - ticker: NVDA
    quantity: 10
    average_cost: 90.0
    current_price: 100.0
""")
        src = RobustPositionSource(
            live=_FakeLive(summary=None),
            cache=PositionCache(str(tmp_path / "absent.json")),
            file_source=FilePositionSource(path, enrich=False),
        )
        ctx = src.format_position_context("NVDA")
        assert "manual file, as of 2026-06-05" in ctx

    def test_returns_none_when_everything_empty(self, tmp_path):
        src = RobustPositionSource(
            live=_FakeLive(summary=None),
            cache=PositionCache(str(tmp_path / "absent.json")),
            file_source=FilePositionSource(str(tmp_path / "none.yaml"), enrich=False),
        )
        assert src.get_account_summary() is None
        assert src.format_position_context("NVDA") is None
        assert src.format_portfolio_context() is None

    def test_snapshot_memoized_single_live_fetch(self, tmp_path):
        live = _FakeLive(summary=_summary("live"))
        calls = {"n": 0}
        orig = live.get_account_summary

        def counting():
            calls["n"] += 1
            return orig()

        live.get_account_summary = counting
        src = RobustPositionSource(
            live=live,
            cache=PositionCache(str(tmp_path / "c.json")),
            file_source=FilePositionSource(str(tmp_path / "none.yaml"), enrich=False),
        )
        src.format_position_context("NVDA")
        src.format_portfolio_context()
        src.get_all_positions()
        assert calls["n"] == 1  # memoized across multiple renders


# --- factory ---

def test_get_position_source_returns_robust(monkeypatch):
    from watchy.config import WatchyConfig

    src = get_position_source(WatchyConfig())
    assert isinstance(src, RobustPositionSource)


# --- rendering & age ---

class TestRendering:
    def test_render_position_includes_pnl_pct(self):
        pos = Position(ticker="NVDA", quantity=10, average_cost=100.0,
                       current_price=120.0, market_value=1200.0,
                       unrealized_pnl=200.0, unrealized_pnl_pct=20.0)
        text = render_position(pos)
        assert "NVDA" in text
        assert "+20.0%" in text
        assert "$1,200.00" in text

    def test_render_portfolio_shows_weights(self):
        summary = AccountSummary(
            account_id="manual", total_value=2000.0,
            positions=[
                Position(ticker="NVDA", quantity=10, average_cost=100.0, market_value=1500.0),
                Position(ticker="AAPL", quantity=5, average_cost=100.0, market_value=500.0),
            ],
        )
        text = render_portfolio(summary)
        assert "75.0%" in text
        assert "25.0%" in text
        assert "Buying power" not in text  # omitted when None

    def test_render_portfolio_never_shows_buying_power(self):
        # #22: buying power is a leveraged purchasing limit, not net worth. The
        # advisor must never see it, or it adds it to Total value and fabricates an
        # inflated concentration denominator (a 32% position rendered as 7.7%).
        # Weight must be computed against Total value alone.
        summary = AccountSummary(
            account_id="X1", total_value=4590.72, buying_power=14421.64,
            positions=[Position(ticker="AMZN", quantity=6, average_cost=240.0,
                                market_value=1476.0)],
        )
        text = render_portfolio(summary)
        assert "Buying power" not in text
        assert "14,421" not in text
        assert "32.2%" in text  # 1476 / 4590.72, NOT 1476 / 19012.36 (7.8%)

    def test_format_age_days_and_hours(self):
        now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
        assert _format_age(now - timedelta(days=3, hours=4), now) == "3d 4h old"
        assert _format_age(now - timedelta(hours=5, minutes=2), now) == "5h 2m old"
        assert _format_age(now - timedelta(minutes=10), now) == "10m old"


class TestAccountMasking:
    """The portfolio block reaches LLM vendors; the account number must not."""

    def test_real_account_number_is_masked(self):
        from watchy.positions import mask_account_id

        assert mask_account_id("40424734") == "****4734"

    def test_labels_and_short_ids_unchanged(self):
        from watchy.positions import mask_account_id

        assert mask_account_id("manual") == "manual"
        assert mask_account_id("X1") == "X1"

    def test_masking_is_idempotent(self):
        from watchy.positions import mask_account_id

        assert mask_account_id("****4734") == "****4734"

    def test_render_portfolio_never_shows_the_full_number(self):
        summary = AccountSummary(account_id="40424734", total_value=5949.48,
                                 cash_balance=2618.55, positions=[])
        text = render_portfolio(summary)
        assert "40424734" not in text
        assert "Account: ****4734" in text
