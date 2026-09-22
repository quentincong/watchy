"""Persist the latest analysis digest per ticker for cheap reuse (#28).

A full pipeline result (the dict the advisor consumes — decision chain, analyst
summaries, risk assessment, stage context) is produced by every Tier 2 run and
every Tier 1 rescan, then discarded once notified. The take-profit zone-entry
trigger (#28) wants to re-advise a held winner *intraday* without paying for a
fresh pipeline, so we stash the last result to disk and reload it as the advisor
input. Best-effort: a save/load failure never breaks a scan — the trigger just
falls back to a mechanical-facts-only prompt.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DIGEST_DIR = os.path.expanduser("~/watchy/reports")


def _path(ticker: str, digest_dir: str | None = None, kind: str = "") -> Path:
    suffix = f"_{kind}" if kind else ""
    return Path(digest_dir or DEFAULT_DIGEST_DIR) / f"{ticker.upper()}{suffix}_digest.json"


def save_digest(
    ticker: str,
    result: dict[str, Any],
    digest_dir: str | None = None,
    kind: str = "",
) -> str | None:
    """Persist a pipeline result as this ticker's latest digest (best-effort).

    ``kind="weekly"`` keeps a separate copy of the Weekly Full digest (Watchy
    2.0 Fast Recheck reuses it) so a later Tier 1 rescan cannot replace the
    analysis the weekly plan was built from. Returns the path, or None.
    """
    try:
        path = _path(ticker, digest_dir, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        # default=str so an unexpected non-JSON value degrades to its repr rather
        # than raising and losing the whole digest.
        path.write_text(json.dumps(payload, default=str), encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to save digest for %s", ticker)
        return None


def load_digest(
    ticker: str, digest_dir: str | None = None, kind: str = ""
) -> tuple[dict[str, Any], datetime] | None:
    """Return (result, saved_at) for a ticker's latest digest, or None if absent/bad."""
    path = _path(ticker, digest_dir, kind)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = payload["result"]
        saved_at = datetime.fromisoformat(payload["saved_at"])
        return result, saved_at
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read digest for %s", ticker)
        return None
