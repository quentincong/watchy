"""Position data — the robust position source for Watchy (#4).

Watchy needs the user's holdings to give position-aware advice. The primary
backend is the Schwab API, but Schwab's OAuth needs a 7-day refresh-token reauth
that's hostile to an unattended daemon — so the source is layered to keep working
when a live fetch can't happen:

    Schwab live  →  on-disk cached last-good snapshot (flagged stale)  →  manual file

Every successful live fetch is cached to ``~/watchy_config/positions_cache.json``.
If a later fetch fails (token lapsed, API down, network), the cached snapshot is
served and labelled with its age; if there's no cache yet, a manually-maintained
``~/watchy_config/positions.yaml`` is the final backstop. The rest of the system
(advisor, tier1/tier2, e2e) depends only on the ``PositionSource`` contract, never
on a concrete backend; ``get_position_source`` wires the layers together.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from watchy.config import WatchyConfig

logger = logging.getLogger(__name__)

DEFAULT_POSITIONS_PATH = "~/watchy_config/positions.yaml"
DEFAULT_CACHE_PATH = "~/watchy_config/positions_cache.json"


@dataclass
class Position:
    ticker: str
    quantity: float
    average_cost: float
    market_value: float | None = None
    unrealized_pnl: float | None = None
    unrealized_pnl_pct: float | None = None
    current_price: float | None = None


@dataclass
class AccountSummary:
    account_id: str
    total_value: float
    # Buying power / cash are unknown for a positions-only backend (e.g. the
    # manual file lists holdings, not the account's cash), so they're optional.
    buying_power: float | None = None
    cash_balance: float | None = None
    positions: list[Position] = field(default_factory=list)


# --- shared rendering (pure functions so every backend reads identically) ---

def render_position(pos: Position) -> str:
    shares = (
        f"{pos.quantity:.0f}" if pos.quantity == int(pos.quantity)
        else f"{pos.quantity}"
    )
    lines = [
        f"Current position in {pos.ticker}:",
        f"  Shares: {shares}",
        f"  Average cost: ${pos.average_cost:.2f}",
    ]
    if pos.current_price is not None:
        lines.append(f"  Current price: ${pos.current_price:.2f}")
    if pos.market_value is not None:
        lines.append(f"  Market value: ${pos.market_value:,.2f}")
    if pos.unrealized_pnl is not None:
        pct = (
            f" ({pos.unrealized_pnl_pct:+.1f}%)"
            if pos.unrealized_pnl_pct is not None else ""
        )
        lines.append(f"  Unrealized P&L: ${pos.unrealized_pnl:,.2f}{pct}")
    return "\n".join(lines)


def mask_account_id(account_id: str) -> str:
    """Last four characters of a real account number, e.g. ``****4734``.

    The rendered portfolio goes into the advisor prompt and so to the LLM
    vendor (Gemini, and Qwen via OpenRouter/Alibaba); the advisor never needs
    the brokerage account number. Labels without digits ("manual") and ids of
    four characters or fewer are left as they are.
    """
    text = str(account_id)
    if len(text) <= 4 or not any(ch.isdigit() for ch in text):
        return text
    return "****" + text[-4:]


def render_portfolio(summary: AccountSummary) -> str:
    lines = [
        f"Account: {mask_account_id(summary.account_id)}",
        f"  Total value: ${summary.total_value:,.2f}",
    ]
    # Deliberately NOT rendering buying_power (#22): on a margin account it's the
    # leveraged purchasing limit (here ~3x the account), not net worth. The LLM
    # kept adding it to Total value to fabricate an inflated denominator, badly
    # understating concentration (a 32% position rendered as 7.7%) — the unsafe
    # direction. Total value (= liquidationValue, already includes cash) is the
    # only concentration denominator. buying_power stays on AccountSummary for
    # caching/round-trips; it is just never shown to the advisor.
    if summary.cash_balance is not None:
        lines.append(f"  Cash: ${summary.cash_balance:,.2f}")
    lines.append(f"  Positions held: {len(summary.positions)}")

    # Per-ticker weight breakdown so the advisor can judge over-concentration.
    total = summary.total_value
    for pos in summary.positions:
        mv = pos.market_value
        if mv is None:
            lines.append(f"    {pos.ticker}: {pos.quantity:g} sh")
            continue
        weight = f" ({mv / total * 100:.1f}%)" if total else ""
        lines.append(f"    {pos.ticker}: ${mv:,.2f}{weight}")
    return "\n".join(lines)


class PositionSource(ABC):
    """Backend-agnostic source of position and account data.

    Subclasses implement the three getters; the ``format_*_context`` helpers are
    shared so every backend renders LLM context identically.
    """

    @abstractmethod
    def get_position(self, ticker: str) -> Position | None:
        """Return the current position for a ticker, or None if not held."""

    @abstractmethod
    def get_all_positions(self) -> list[Position]:
        """Return all current positions."""

    @abstractmethod
    def get_account_summary(self) -> AccountSummary | None:
        """Return account balances and positions, or None if unavailable."""

    def format_position_context(self, ticker: str) -> str | None:
        """Human-readable summary of a single position, or None if not held."""
        pos = self.get_position(ticker)
        return render_position(pos) if pos is not None else None

    def format_portfolio_context(self) -> str | None:
        """Concentration-aware summary of the account, or None if unavailable."""
        summary = self.get_account_summary()
        return render_portfolio(summary) if summary is not None else None


# --- enrichment helper ---

def _latest_price(ticker: str) -> float | None:
    """Latest close for ``ticker`` via the same cached fetch the scanner uses.

    Reuses ``indicators._fetch_history`` (yfinance-cache with rate-limit-aware
    fallback) so enrichment shares the scanner's caching and never opens a second
    path to Yahoo. Returns None on any failure — enrichment is best-effort.
    """
    try:
        from watchy.indicators import _fetch_history

        df = _fetch_history(ticker)
    except Exception:  # noqa: BLE001
        logger.exception("Price fetch failed for %s", ticker)
        return None
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return None
    try:
        close = df["Close"]
        if hasattr(close, "columns"):  # multiindex (yf.download) → first column
            close = close.iloc[:, 0]
        return float(close.iloc[-1])
    except Exception:  # noqa: BLE001
        logger.exception("Could not read latest close for %s", ticker)
        return None


def _derive_pnl(pos: Position) -> None:
    """Fill derived market value / unrealized P&L from current_price in place."""
    if pos.current_price is None:
        return
    pos.market_value = pos.current_price * pos.quantity
    cost_basis = pos.average_cost * pos.quantity
    pos.unrealized_pnl = pos.market_value - cost_basis
    if cost_basis:
        pos.unrealized_pnl_pct = pos.unrealized_pnl / cost_basis * 100.0


class FilePositionSource(PositionSource):
    """Positions from a manually-maintained YAML file (the final backstop).

    File schema (``~/watchy_config/positions.yaml``)::

        positions:
          - ticker: NVDA
            quantity: 100
            average_cost: 120.50
          - ticker: AAPL
            quantity: 50
            average_cost: 190.00
            current_price: 195.00   # optional; pins price, skips the live fetch

    When ``enrich`` is True (default) each position's current price is fetched
    live (unless pinned in the file) to derive market value and unrealized P&L.
    """

    def __init__(self, path: str | None = None, enrich: bool = True) -> None:
        self.path = Path(os.path.expanduser(path or DEFAULT_POSITIONS_PATH))
        self.enrich = enrich

    def _load_raw(self) -> dict[str, Position]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path) as f:
                raw: dict[str, Any] = yaml.safe_load(f) or {}
        except Exception:  # noqa: BLE001
            logger.exception("Failed to read positions file %s", self.path)
            return {}

        out: dict[str, Position] = {}
        for item in raw.get("positions", []) or []:
            if not isinstance(item, dict):
                logger.warning("Skipping non-mapping position entry: %r", item)
                continue
            try:
                ticker = str(item["ticker"]).upper()
                cp = item.get("current_price")
                pos = Position(
                    ticker=ticker,
                    quantity=float(item["quantity"]),
                    average_cost=float(item["average_cost"]),
                    current_price=float(cp) if cp is not None else None,
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping malformed position entry: %r", item)
                continue
            out[ticker] = pos
        return out

    def _resolve(self, pos: Position) -> None:
        if pos.current_price is None and self.enrich:
            pos.current_price = _latest_price(pos.ticker)
        _derive_pnl(pos)

    def as_of(self) -> datetime | None:
        """When the manual data is current 'as of', for staleness labelling.

        Prefers an explicit top-level ``as_of:`` field in the file (the user's
        own statement of when the holdings were accurate); otherwise falls back
        to the file's modification time (zero-upkeep default). None if no file.
        """
        if not self.path.exists():
            return None
        try:
            with open(self.path) as f:
                raw = yaml.safe_load(f) or {}
            explicit = _parse_as_of(raw.get("as_of"))
            if explicit is not None:
                return explicit
        except Exception:  # noqa: BLE001
            logger.exception("Failed to read as_of from %s", self.path)
        try:
            return datetime.fromtimestamp(self.path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    def get_position(self, ticker: str) -> Position | None:
        pos = self._load_raw().get(ticker.upper())
        if pos is None:
            return None
        self._resolve(pos)
        return pos

    def get_all_positions(self) -> list[Position]:
        positions = list(self._load_raw().values())
        for pos in positions:
            self._resolve(pos)
        return positions

    def _load_account_fields(self) -> tuple[float | None, float | None]:
        """``(total_account_value, cash)`` from the file; None each if absent/bad.

        ``total_account_value`` is the preferred, authoritative input — the single
        figure you read straight off your broker (equities + cash + equivalents),
        used directly as the concentration denominator. ``cash`` is the
        alternative: state just the buffer and let Watchy add it to the live stock
        value. Both feed the account total so concentration is judged against the
        full account, not the stock-only sum.
        """
        if not self.path.exists():
            return None, None
        try:
            with open(self.path) as f:
                raw = yaml.safe_load(f) or {}
        except Exception:  # noqa: BLE001
            return None, None
        return _coerce_float(raw.get("total_account_value")), _coerce_float(raw.get("cash"))

    def get_account_summary(self) -> AccountSummary | None:
        positions = self.get_all_positions()
        explicit_total, cash = self._load_account_fields()
        cash = cash or 0.0
        stocks = sum(p.market_value for p in positions if p.market_value is not None)

        if explicit_total is not None and explicit_total >= stocks:
            # Authoritative account total wins; the remainder above equities is the
            # cash buffer, surfaced for the advisor's concentration math.
            total = explicit_total
            buffer = explicit_total - stocks
            cash_balance = buffer if buffer > 0 else None
        else:
            if explicit_total is not None:
                logger.warning(
                    "total_account_value (%.2f) below stock value (%.2f) in %s — "
                    "ignoring it, using equities + cash",
                    explicit_total, stocks, self.path,
                )
            total = stocks + cash
            cash_balance = cash if cash else None

        if not positions and not total:
            return None
        return AccountSummary(
            account_id="manual",
            total_value=total,
            cash_balance=cash_balance,
            positions=positions,
        )


class PositionCache:
    """On-disk snapshot of the last good live fetch, for stale-but-real fallback."""

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(os.path.expanduser(path or DEFAULT_CACHE_PATH))

    def write(self, summary: AccountSummary) -> None:
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "account": asdict(summary),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write position cache %s", self.path)

    def read(self) -> tuple[AccountSummary, datetime] | None:
        """Return (summary, fetched_at) from the cache, or None if absent/bad."""
        if not self.path.exists():
            return None
        try:
            with open(self.path) as f:
                payload = json.load(f)
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            acct = payload["account"]
            summary = AccountSummary(
                account_id=acct["account_id"],
                total_value=acct["total_value"],
                buying_power=acct.get("buying_power"),
                cash_balance=acct.get("cash_balance"),
                positions=[Position(**p) for p in acct.get("positions", [])],
            )
            return summary, fetched_at
        except Exception:  # noqa: BLE001
            logger.exception("Failed to read position cache %s", self.path)
            return None


def _coerce_float(val: Any) -> float | None:
    """Best-effort float from a YAML scalar; None (with a warning) on non-numeric."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        logger.warning("Ignoring non-numeric account value: %r", val)
        return None


def _parse_as_of(val: Any) -> datetime | None:
    """Coerce a YAML ``as_of`` value (datetime / date / ISO string) to UTC datetime."""
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
    elif isinstance(val, date):
        dt = datetime(val.year, val.month, val.day)
    elif isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val)
        except ValueError:
            logger.warning("Unparseable as_of value: %r", val)
            return None
    else:
        logger.warning("Unsupported as_of type: %r", val)
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_age(fetched_at: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    delta = now - fetched_at
    secs = max(int(delta.total_seconds()), 0)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h old"
    if hours:
        return f"{hours}h {minutes}m old"
    return f"{minutes}m old"


@dataclass
class _Snapshot:
    summary: AccountSummary | None
    provenance: str | None  # e.g. "Schwab (live)", "Schwab cache, ... (3d 4h old)"


class RobustPositionSource(PositionSource):
    """Layered source: Schwab live → cached snapshot → manual file.

    The chosen snapshot is memoized for the lifetime of the instance, so a single
    scan (which formats position + portfolio context several times) triggers at
    most one live fetch. Call sites create a fresh source per scan, so the
    memoized snapshot never goes stale within its useful lifetime.
    """

    def __init__(
        self,
        live: PositionSource,
        cache: PositionCache,
        file_source: FilePositionSource,
    ) -> None:
        self.live = live
        self.cache = cache
        self.file = file_source
        self._snap: _Snapshot | None = None

    def _snapshot(self) -> _Snapshot:
        if self._snap is not None:
            return self._snap

        # 1. Live Schwab — authoritative when it returns data; cache it.
        try:
            summary = self.live.get_account_summary()
        except Exception:  # noqa: BLE001
            logger.exception("Live position fetch raised; falling back")
            summary = None
        if summary is not None:
            self.cache.write(summary)
            self._snap = _Snapshot(summary, "Schwab (live)")
            return self._snap

        # 2. Last good cached snapshot — real data, flagged with its age.
        cached = self.cache.read()
        if cached is not None:
            summary, fetched_at = cached
            prov = f"Schwab cache, as of {fetched_at:%Y-%m-%d %H:%M} UTC ({_format_age(fetched_at)})"
            logger.info("Serving stale position data from cache (%s)", prov)
            self._snap = _Snapshot(summary, prov)
            return self._snap

        # 3. Manual file — final backstop (labelled with its age too).
        summary = self.file.get_account_summary()
        if summary is not None:
            as_of = self.file.as_of()
            if as_of is not None:
                prov = f"manual file, as of {as_of:%Y-%m-%d %H:%M} UTC ({_format_age(as_of)})"
            else:
                prov = "manual file"
            self._snap = _Snapshot(summary, prov)
            return self._snap

        self._snap = _Snapshot(None, None)
        return self._snap

    def get_position(self, ticker: str) -> Position | None:
        snap = self._snapshot()
        if snap.summary is None:
            return None
        t = ticker.upper()
        for pos in snap.summary.positions:
            if pos.ticker.upper() == t:
                return pos
        return None

    def get_all_positions(self) -> list[Position]:
        snap = self._snapshot()
        return list(snap.summary.positions) if snap.summary else []

    def get_account_summary(self) -> AccountSummary | None:
        return self._snapshot().summary

    def provenance(self) -> str | None:
        """Source label for the resolved snapshot ('Schwab (live)' | cache | file | None).

        Lets callers detect a fallback off live data (e.g. an expired Schwab token)
        without re-fetching — the snapshot is memoized.
        """
        return self._snapshot().provenance

    # Append provenance so stale/fallback data is never silently presented as live.

    def format_position_context(self, ticker: str) -> str | None:
        text = super().format_position_context(ticker)
        if text is None:
            return None
        return f"{text}\n  (source: {self._snapshot().provenance})"

    def format_portfolio_context(self) -> str | None:
        text = super().format_portfolio_context()
        if text is None:
            return None
        return f"{text}\n  (source: {self._snapshot().provenance})"


def get_position_source(config: WatchyConfig) -> PositionSource:
    """Build the layered position source: Schwab live → cache → manual file."""
    # Lazy import avoids a positions <-> schwab import cycle.
    from watchy.schwab import SchwabClient

    return RobustPositionSource(
        live=SchwabClient(config.schwab),
        cache=PositionCache(),
        file_source=FilePositionSource(),
    )
