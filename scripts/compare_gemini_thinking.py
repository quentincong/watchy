#!/usr/bin/env python
"""Compare the Gemini advisor across thinking levels — WITHOUT re-running the
(expensive) TradingAgents pipeline (issue #18 / the ③ thinking A/B).

The advisor only consumes the analysis *result*, so this reconstructs it from a
saved ``~/watchy/reports/*.md`` and calls Gemini once per thinking level, using
the same prompt production builds (ADVISOR_PROMPT + _format_analysis digest +
position/portfolio). You get token usage (prompt / candidates / **thoughts**),
cost, and the advice text per level — for a handful of Gemini calls, no DeepSeek.

Fidelity note: the report's "### Portfolio Manager" section is the risk-debate
judge_decision, mapped to ``risk_assessment`` so _format_analysis labels it the
same way production does. The one field the .md can't restore is the graph's short
``final_trade_decision`` (production's ``_decision_raw`` / the "Final Decision"
block); it's left empty. Everything else matches the live advisor input, so the
decision should now track live Tier 2 at the same model + thinking level.

gemini-3.x controls thinking with ``thinkingConfig.thinkingLevel`` and REJECTS
the legacy ``thinkingBudget`` with HTTP 400. The level table is model-dependent
— 3.5/3.6 take minimal|low|medium|high, while 3.7 and 3.8 take low|medium|high
and 400 on minimal — so the "off" cell is resolved by the advisor's own
``_gemini_thinking_config`` rather than mirrored here, which is what kept this
script honest when 3.7 landed. maxOutputTokens is set generously so thinking
can't truncate the answer. Each level runs in its own try/except so an
unsupported field on one cell doesn't abort the sweep.

MANUAL / needs the Gemini api_key in secrets.yaml. Run with the `trading` pyenv.

    python scripts/compare_gemini_thinking.py --ticker MOD
    python scripts/compare_gemini_thinking.py --ticker MOD --levels off,low,medium,high

Pass ``--model gemini-3.8-flash`` to pin the model regardless of the config/secrets
default (the repo default reverted to gemini-3.5-flash on 2026-07-22).
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
    _gemini_cost_usd,
    _gemini_thinking_config,
    _parse_advice,
    _take_profit_guidance,
)
from watchy.config import load_config
from watchy.positions import get_position_source

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gemini-ab")

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


def parse_report(text: str) -> dict[str, str]:
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
    # (labelled "Risk Assessment" by _format_analysis) — NOT in `_decision_raw`.
    # `_decision_raw` is the graph's short final_trade_decision, which is NOT
    # persisted to the .md, so it's left empty. Previously the PM call was fed
    # under the wrong "Final Decision" label with no Risk Assessment block, which
    # flipped the advisor's *decision* vs live Tier 2 on the same model/level.
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


def call_gemini(prompt: str, llm, level: str, model: str = "") -> tuple[str, dict]:
    model = model or llm.model or "gemini-3.5-flash"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={_effective_key(llm)}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": _ADVICE_MAX_TOKENS + _THINK_HEADROOM,
            "thinkingConfig": _gemini_thinking_config(level, model),
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", default="", help="use the newest report for this ticker")
    ap.add_argument("--report", default="", help="explicit report .md path")
    ap.add_argument("--reports-dir", default="~/watchy/reports")
    ap.add_argument("--levels", default="off,low,medium,high",
                    help="comma-separated: off, minimal, low, medium, high")
    ap.add_argument("--model", default="",
                    help="force the Gemini model (e.g. gemini-3.8-flash), "
                         "independent of the config/secrets default")
    args = ap.parse_args()

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

    config = load_config()
    if config.llm.provider != "gemini":
        print(f"advisor provider is {config.llm.provider!r}, not gemini", file=sys.stderr)
        return 2
    llm = config.llm
    active_model = args.model or llm.model or "gemini-3.5-flash"
    logger.info("Gemini model under test: %s", active_model)
    ps = get_position_source(config)

    analysis_text = _format_analysis(result)
    # #28: the take-profit directive is part of the prompt under test. No
    # indicator bundle offline, so the limit anchors on the position's own mark
    # and the ATR runway is omitted — the gate itself still arms off live gain.
    prompt = ADVISOR_PROMPT.format(
        ticker=ticker,
        analysis=analysis_text,
        position=ps.format_position_context(ticker) or "No position held.",
        portfolio=ps.format_portfolio_context() or "Portfolio data unavailable.",
        take_profit_guidance=_take_profit_guidance(ticker, analysis_text, ps, config, None),
    )

    levels = [x.strip() for x in args.levels.split(",") if x.strip()]
    rows: list[tuple] = []
    outputs: dict[str, str] = {}
    for level in levels:
        print(f"\n{'='*68}\nthinking level: {level}\n{'='*68}")
        try:
            text, usage = call_gemini(prompt, llm, level, model=active_model)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            continue
        in_tok = int(usage.get("promptTokenCount", 0))
        out_tok = int(usage.get("candidatesTokenCount", 0))
        think_tok = int(usage.get("thoughtsTokenCount", 0))
        usd = _gemini_cost_usd(in_tok, out_tok, think_tok, active_model)
        parsed = _parse_advice((text or "").strip(), ticker)
        outputs[level] = text
        rows.append((level, in_tok, out_tok, think_tok, usd, parsed.get("decision"), parsed.get("urgency")))
        print(f"  in={in_tok} out={out_tok} think={think_tok} usd=${usd:.5f} "
              f"decision={parsed.get('decision')!r} urgency={parsed.get('urgency')!r}")

    print(f"\n{'#'*68}\nSUMMARY ({ticker} · {active_model})\n{'#'*68}")
    print(f"{'level':8} {'in':>6} {'out':>6} {'think':>6} {'usd':>9}  decision")
    for lvl, i, o, t, u, dec, urg in rows:
        print(f"{lvl:8} {i:6d} {o:6d} {t:6d} {u:9.5f}  {dec}/{urg}")

    print(f"\n{'#'*68}\nADVICE TEXT PER LEVEL ({ticker})\n{'#'*68}")
    for lvl in levels:
        if lvl in outputs:
            print(f"\n----- {lvl} -----\n{outputs[lvl]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
