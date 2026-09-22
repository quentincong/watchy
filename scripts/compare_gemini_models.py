#!/usr/bin/env python
"""Compare the Gemini advisor across MODELS — 3.6 vs 3.5 — WITHOUT re-running the
(expensive) TradingAgents pipeline (issue #18, the dropped model bake-off,
re-scoped to the upgrade we actually shipped: gemini-3.5-flash → gemini-3.6-flash,
commit 4bc1e8c).

The advisor only consumes the analysis *result*, so this reconstructs the
production prompt from a saved ``~/watchy/reports/*.md`` (ADVISOR_PROMPT +
_format_analysis digest + position/portfolio) and calls the models on the SAME
prompt — one live position snapshot shared by both cells, so the only variable is
the model.

Two modes:

  * BOTH FRESH (default): calls both the new model (gemini-3.6-flash) and the old
    model (gemini-3.5-flash) live. Cleanest A/B — identical prompt + position
    snapshot, only the model differs.
  * REUSE 3.6 (``--ref-file`` / stdin): paste the 3.6 Telegram advice you already
    received and only the 3.5 cell is billed. Handy to avoid a second 3.6 call, but
    the Telegram layout drops the ``Target:`` line (consumed for the #16 derived
    target), so the reused 3.6 cell shows target=N/A, and the pasted text MUST come
    from the same run as ``--report``.

Fidelity: the digest maps the report's "### Portfolio Manager" section (the
risk-debate judge_decision) to ``risk_assessment`` exactly as production does. The
one field the .md can't restore is the graph's short ``final_trade_decision``
(production's ``_decision_raw`` / the "Final Decision" block), so it's left empty —
identically for BOTH cells, so the model-vs-model comparison stays controlled even
though neither cell reproduces the live Tier 2 decision bit-for-bit.

MANUAL / needs the Gemini api_key in secrets.yaml. Run with the `trading` pyenv.

    # both fresh (recommended):
    python scripts/compare_gemini_models.py --ticker AVGO
    python scripts/compare_gemini_models.py --ticker AVGO --level low
    python scripts/compare_gemini_models.py --ticker AVGO \
        --new-model gemini-3.6-flash --old-model gemini-3.5-flash

    # reuse a pasted 3.6 Telegram advice (only 3.5 billed):
    python scripts/compare_gemini_models.py --ticker AVGO --ref-file /tmp/avgo_36.txt
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchy.advisor import (
    ADVISOR_PROMPT,
    _effective_key,
    _format_analysis,
    _parse_advice,
)
from watchy.config import load_config
from watchy.positions import get_position_source

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gemini-model-ab")

_AGENTS = {
    "Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst",
    "Bull Researcher", "Bear Researcher", "Research Manager", "Trader",
    "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager",
}
_DIVIDER_RE = re.compile(r"^##\s+[IVXLC]+\.\s")
_ADVICE_MAX_TOKENS = 4096  # visible-answer ceiling
# Thinking shares the output budget; at medium/high a level can spend ~4k tokens on
# hidden reasoning. Give it its own headroom so the visible answer isn't truncated
# (observed at level=high: think≈3933 + a stump 159-token answer that leaked raw
# reasoning). maxOutputTokens is a ceiling, not a charge, so this is free.
_THINK_HEADROOM = 4096

_DEFAULT_NEW_MODEL = "gemini-3.6-flash"
_DEFAULT_OLD_MODEL = "gemini-3.5-flash"
# gemini-flash list price, USD per 1M tokens. 3.5 and 3.6-flash share it as of
# 2026-07 (in $1.50 / out $7.50); update from ai.google.dev/pricing if it drifts.
# Thinking tokens are billed at the output rate.
_PRICE_IN = 1.50
_PRICE_OUT = 7.50

_DECISIONS = ("BUY", "SELL", "TRIM", "ADD", "HOLD")


def parse_report(text: str) -> dict[str, str]:
    """Map each ``### <agent>`` section to its body (see compare_gemini_thinking)."""
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("### ") and stripped[4:].strip() in _AGENTS:
            if current:
                out[current] = "\n".join(buf).strip()
            current = stripped[4:].strip()
            buf = []
        elif _DIVIDER_RE.match(stripped):
            if current:
                out[current] = "\n".join(buf).strip()
            current = None
            buf = []
        elif current:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf).strip()
    return out


def result_from_report(sec: dict[str, str]) -> dict:
    # Map report sections to the SAME result fields production's pipeline_runner
    # builds, so _format_analysis produces the same labelled digest the live
    # advisor sees. The report's "### Portfolio Manager" section is the
    # risk-debate judge_decision, which production carries in `risk_assessment`
    # (labelled "Risk Assessment") — NOT in `_decision_raw`. `_decision_raw` (the
    # graph's short final_trade_decision) isn't persisted to the .md, so it's left
    # empty. Feeding the PM call under the wrong "Final Decision" label with no
    # Risk Assessment block previously flipped the advisor's decision.
    return {
        "_reports": {
            "market_report": sec.get("Market Analyst", ""),
            "sentiment_report": sec.get("Sentiment Analyst", ""),
            "news_report": sec.get("News Analyst", ""),
            "fundamentals_report": sec.get("Fundamentals Analyst", ""),
        },
        "trader_plan": sec.get("Trader", ""),
        "risk_assessment": sec.get("Portfolio Manager", ""),
        "_decision_raw": "",
        "recommendations": [],
    }


def _thinking_config(level: str) -> dict:
    """Mirror advisor._gemini_thinking_config: "off" → minimal (cheapest tier)."""
    return {"thinkingLevel": "minimal" if level == "off" else level}


def call_gemini(prompt: str, llm, model: str, level: str) -> tuple[str, dict]:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={_effective_key(llm)}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": _ADVICE_MAX_TOKENS + _THINK_HEADROOM,
            "thinkingConfig": _thinking_config(level),
        },
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read())
    text = ""
    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if part.get("text") and not part.get("thought"):
            text = part["text"]
            break
    return text, data.get("usageMetadata", {})


def _cost(usage: dict) -> tuple[int, int, int, float]:
    in_tok = int(usage.get("promptTokenCount", 0))
    out_tok = int(usage.get("candidatesTokenCount", 0))
    think_tok = int(usage.get("thoughtsTokenCount", 0))
    usd = (in_tok * _PRICE_IN + (out_tok + think_tok) * _PRICE_OUT) / 1_000_000
    return in_tok, out_tok, think_tok, usd


def parse_telegram_advice(raw: str, fallback_ticker: str) -> dict[str, str]:
    """Extract decision/urgency/detail from a pasted Telegram advice message.

    Telegram renders (notify.pipeline_result)::

        Position Advice — $MOD: 🟢 BUY (LOW urgency)
        <detail paragraph...>

    optionally preceded by a "Your Position — $MOD\\n<position>" block. The
    ``Target:`` line is NOT in Telegram (consumed for the #16 derived target), so
    target is left blank. If no "Position Advice" header is found we fall back to
    the raw advisor format (_parse_advice), so a pasted RAW advisor reply also works.
    """
    lines = raw.splitlines()
    hdr_idx = next(
        (i for i, ln in enumerate(lines) if "position advice" in ln.lower()),
        None,
    )
    if hdr_idx is None:
        return _parse_advice(raw.strip(), fallback_ticker)

    header = lines[hdr_idx]
    decision = next((d for d in _DECISIONS if re.search(rf"\b{d}\b", header, re.I)), "")
    m = re.search(r"\(([A-Za-z]+)\s+urgency\)", header, re.I)
    urgency = m.group(1).upper() if m else ""
    detail = "\n".join(lines[hdr_idx + 1:]).strip()
    return {
        "ticker": fallback_ticker,
        "decision": decision.upper(),
        "urgency": urgency,
        "target": "",
        "detail": detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", default="", help="use the newest report for this ticker")
    ap.add_argument("--report", default="", help="explicit report .md path")
    ap.add_argument("--reports-dir", default="~/watchy/reports")
    ap.add_argument("--new-model", default=_DEFAULT_NEW_MODEL,
                    help=f"new model, called fresh unless a 3.6 reply is pasted "
                         f"(default: {_DEFAULT_NEW_MODEL})")
    ap.add_argument("--old-model", default=_DEFAULT_OLD_MODEL,
                    help=f"old model, always called fresh (default: {_DEFAULT_OLD_MODEL})")
    ap.add_argument("--ref-file", default="",
                    help="optional: file with a pasted 3.6 Telegram advice to REUSE "
                         "instead of calling --new-model fresh. Omit to run both fresh.")
    ap.add_argument("--level", default="low",
                    help="thinking level for BOTH calls (off/minimal/low/medium/high; "
                         "default low = production Tier 2)")
    ap.add_argument("--position-file", default="",
                    help="OVERRIDE the live position/portfolio context with a static "
                         "positions.yaml, bypassing Schwab/cache. Use this to replay a "
                         "past winner you no longer hold: set average_cost + pin "
                         "current_price so 'Unrealized P&L' reflects the gain at that "
                         "time, and total_account_value for a realistic weight — "
                         "otherwise the take-profit clause can't see the gain.")
    args = ap.parse_args()

    # --- resolve the report (same rules as compare_gemini_thinking) ---
    if args.report:
        report_path = Path(os.path.expanduser(args.report))
    else:
        files = sorted(glob.glob(os.path.join(os.path.expanduser(args.reports_dir), "*.md")),
                       key=os.path.getmtime, reverse=True)
        if args.ticker:
            files = [f for f in files if os.path.basename(f).split("_", 1)[0].upper() == args.ticker.upper()]
        if not files:
            print(f"No report found (ticker={args.ticker!r}, dir={args.reports_dir})", file=sys.stderr)
            return 1
        report_path = Path(files[0])

    ticker = report_path.name.split("_", 1)[0]
    sec = parse_report(report_path.read_text(encoding="utf-8"))
    result = result_from_report(sec)
    logger.info("Report: %s  (ticker=%s)", report_path.name, ticker)

    # --- optional pasted 3.6 reference: --ref-file, or piped stdin ---
    ref_raw = ""
    if args.ref_file:
        ref_raw = Path(os.path.expanduser(args.ref_file)).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        ref_raw = sys.stdin.read()
    ref = parse_telegram_advice(ref_raw, ticker) if ref_raw.strip() else None

    # --- config / prompt (production-identical, ONE shared position snapshot) ---
    config = load_config()
    if config.llm.provider != "gemini":
        print(f"advisor provider is {config.llm.provider!r}, not gemini", file=sys.stderr)
        return 2
    llm = config.llm
    if args.position_file:
        from watchy.positions import FilePositionSource
        ps = FilePositionSource(os.path.expanduser(args.position_file))
        logger.info("Position context OVERRIDDEN from %s (Schwab bypassed)", args.position_file)
    else:
        ps = get_position_source(config)
    prompt = ADVISOR_PROMPT.format(
        ticker=ticker,
        analysis=_format_analysis(result),
        position=ps.format_position_context(ticker) or "No position held.",
        portfolio=ps.format_portfolio_context() or "Portfolio data unavailable.",
        take_profit_guidance="",
        event_context="",
        plan_instructions="",
    )

    # --- new-model cell: reuse pasted 3.6, or call fresh ---
    new_usd = 0.0
    new_source = "reused (TG)"
    new_target = "N/A (TG)"
    new_text = ""
    if ref is not None:
        new = ref
        logger.info("Reusing pasted %s advice (not billed)", args.new_model)
    else:
        print(f"\n{'='*68}\ncalling {args.new_model}  (thinking level: {args.level})\n{'='*68}")
        try:
            new_text, usage = call_gemini(prompt, llm, args.new_model, args.level)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        i, o, t, new_usd = _cost(usage)
        new = _parse_advice((new_text or "").strip(), ticker)
        new_source = "fresh"
        new_target = new.get("target") or "N/A"
        print(f"  in={i} out={o} think={t} usd=${new_usd:.5f}")

    # --- old-model cell: always fresh ---
    print(f"\n{'='*68}\ncalling {args.old_model}  (thinking level: {args.level})\n{'='*68}")
    try:
        old_text, usage = call_gemini(prompt, llm, args.old_model, args.level)
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    i, o, t, old_usd = _cost(usage)
    old = _parse_advice((old_text or "").strip(), ticker)
    print(f"  in={i} out={o} think={t} usd=${old_usd:.5f}")

    # --- comparison ---
    agree = old.get("decision") == new.get("decision") and bool(new.get("decision"))
    print(f"\n{'#'*68}\nSUMMARY ({ticker} · level={args.level})\n{'#'*68}")
    print(f"{'model':18} {'decision':9} {'urgency':8} {'target':>10}  cost")
    print(f"{args.new_model:18} {new.get('decision','?'):9} {new.get('urgency','?'):8} "
          f"{new_target:>10}  ${new_usd:.5f} ({new_source})")
    print(f"{args.old_model:18} {old.get('decision','?'):9} {old.get('urgency','?'):8} "
          f"{(old.get('target') or 'N/A'):>10}  ${old_usd:.5f} (fresh)")
    print(f"\ndecision match: {'YES' if agree else 'NO'}"
          f"   ({args.new_model}={new.get('decision','?')} vs "
          f"{args.old_model}={old.get('decision','?')})")

    print(f"\n{'#'*68}\n{args.new_model} ADVICE ({new_source}) — {ticker}\n{'#'*68}")
    print((new_text or new.get("detail") or "(no detail parsed)"))
    print(f"\n{'#'*68}\n{args.old_model} ADVICE (fresh) — {ticker}\n{'#'*68}")
    print(old_text or "(empty)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
