#!/usr/bin/env python
"""Run the full Weekly Full pipeline with different quick/deep model pairs.

Unlike ``compare_rm_pm_qwen.py`` (isolated RM/PM replay on frozen inputs) this
runs every node, so a model change in the quick slot (analysts, Bull/Bear,
Trader, risk debaters) or the deep slot (Research Manager, Portfolio Manager)
propagates to the final rating. Data tools fetch live, so run all arms in one
sitting, ideally with the market closed.

Model ids:  deepseek-flash            production client (TA deepseek provider)
            or:<slug>[@<effort>]      OpenRouter, reasoning.effort=<effort>
                                      (no effort = reasoning on, provider default);
                                      structured output via json_schema

Production state is not touched: reports, TA logs and a COPY of the TA memory
log go under ``<out-dir>/<arm>/``; nothing is sent to Telegram or state.db.

MANUAL: calls paid APIs. Run on the VPS with the trading pyenv, one process
per arm if you want them in parallel (each arm writes its own jsonl):

    PY=/home/watchy/.pyenv/versions/3.11.9/envs/trading/bin/python
    $PY scripts/compare_pipeline_models.py run --arms ds,m3+sol --only NVDA \\
        --out-dir ~/abtest_qwen/pipe
    $PY scripts/compare_pipeline_models.py report ~/abtest_qwen/pipe
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from compare_rm_pm_models import faithfulness, parse_report  # noqa: E402
from compare_rm_pm_qwen import OR_BASE, _FallbackCatcher, rating_of  # noqa: E402

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("pipeline-ab")

OR_PREFIX = "or:"
SOL = "or:openai/gpt-6.1-sol@low"
# arm -> (quick slot, deep slot)
ARMS = {
    "ds": ("deepseek-flash", "deepseek-flash"),
    "ds+sol": ("deepseek-flash", SOL),
    "luna+sol": ("or:openai/gpt-6-luna@max", SOL),
    "m3+sol": ("or:minimax/minimax-m3", SOL),
}
_ANALYSTS = ("Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst")
_SCORE = {"Sell": -2, "Underweight": -1, "Hold": 0, "Overweight": 1, "Buy": 2}
# Reasoning shares max_tokens on OpenRouter; analyst reports run to ~10k tokens.
_MAX_TOKENS = 8192 + 32768
_PROD_MEMORY_LOG = "~/.tradingagents/memory/trading_memory.md"


def _recorder_cls():
    from langchain_core.callbacks import BaseCallbackHandler

    from watchy.token_tracker import _cost_usd, _extract_usage

    class Recorder(BaseCallbackHandler):
        """Per-run usage by model, plus every tool result the models were shown."""

        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.errors: list[str] = []
            self.tool_text: dict[str, None] = {}

        def on_chat_model_start(self, serialized, messages, **kwargs):  # noqa: D102
            for batch in messages:
                for m in batch:
                    if getattr(m, "type", "") == "tool":
                        self.tool_text[str(m.content)] = None

        def on_llm_end(self, response, **kwargs):  # noqa: D102
            i, c, o, r, model = _extract_usage(response)
            tu = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
            cost = tu.get("cost")
            if cost is None:
                cost = _cost_usd("deepseek-flash", i, c, o, datetime.now(timezone.utc))
            self.calls.append({"model": model, "in": i, "cached": c, "out": o,
                               "reason": r, "usd": float(cost)})

        def on_llm_error(self, error, **kwargs):  # noqa: D102
            self.errors.append(f"{type(error).__name__}: {str(error)[:200]}")

    return Recorder


class _Client:
    """The one method TradingAgentsGraph needs from an LLM client."""

    def __init__(self, llm) -> None:
        self._llm = llm

    def get_llm(self):
        return self._llm


def install_router(state: dict, or_key: str) -> None:
    """Route ``or:`` model ids to OpenRouter; everything else to the TA factory.

    ``state["rec"]`` (the current run's Recorder) is attached to every client.
    """
    import tradingagents.graph.trading_graph as tg
    from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI

    from watchy import llm_shim

    llm_shim.install()
    inner = tg.create_llm_client

    class OpenRouterChat(NormalizedChatOpenAI):
        # Function calling with these endpoints 404s under require_parameters
        # (2026-10-03); json_schema is the method that works.
        def with_structured_output(self, schema, *, method=None, **kwargs):
            return super().with_structured_output(schema, method="json_schema", **kwargs)

    def create_llm_client(provider, model, base_url=None, **kwargs):
        kwargs["callbacks"] = list(kwargs.get("callbacks") or []) + [state["rec"]]
        if not model.startswith(OR_PREFIX):
            return inner(provider, model, base_url, **kwargs)
        slug, _, effort = model[len(OR_PREFIX):].partition("@")
        return _Client(OpenRouterChat(
            model=slug, base_url=OR_BASE, api_key=or_key,
            callbacks=kwargs["callbacks"], timeout=300, max_retries=2,
            max_tokens=_MAX_TOKENS,
            extra_body={
                "reasoning": {"effort": effort} if effort else {"enabled": True},
                "provider": {"require_parameters": True, "allow_fallbacks": False},
                "usage": {"include": True},
            },
        ))

    # Marked so llm_shim.install() (called again by every runner) leaves it alone.
    create_llm_client._watchy_shim = True
    tg.create_llm_client = create_llm_client


def _keys() -> dict:
    import yaml

    from watchy.config import load_config

    with open(os.path.expanduser("~/watchy_config/secrets.yaml"), encoding="utf-8") as f:
        secrets = yaml.safe_load(f) or {}
    return {"deepseek": load_config().llm.deepseek_api_key
            or os.environ.get("DEEPSEEK_API_KEY", ""),
            "openrouter": ((secrets.get("openrouter") or {}).get("api_key") or "").strip()}


def _frac(pair) -> str:
    return "%d/%d" % tuple(pair) if pair else "-"


def cmd_run(args) -> int:
    from watchy.advisor import _analyst_summary_tail
    from watchy.orchestrator import AnalystSet, DebateMode, PipelineSpec, RiskMode
    from watchy.pipeline_runner import create_tradingagents_runner

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        print(f"unknown arms: {unknown} (have {list(ARMS)})", file=sys.stderr)
        return 2
    tickers = [t.strip().upper() for t in args.only.split(",") if t.strip()]
    keys = _keys()
    if not keys["openrouter"] or not keys["deepseek"]:
        print("deepseek / openrouter key missing", file=sys.stderr)
        return 2
    os.environ.setdefault("DEEPSEEK_API_KEY", keys["deepseek"])

    Recorder = _recorder_cls()
    state = {"rec": Recorder()}
    install_router(state, keys["openrouter"])
    catcher = _FallbackCatcher()
    logging.getLogger("tradingagents.agents.utils.structured").addHandler(catcher)
    spec = PipelineSpec(AnalystSet.FULL, DebateMode.BULL_BEAR, RiskMode.FULL)
    out_dir = Path(os.path.expanduser(args.out_dir))
    spent = 0.0

    for arm in arms:
        quick, deep = ARMS[arm]
        arm_dir = out_dir / arm
        arm_dir.mkdir(parents=True, exist_ok=True)
        memory = arm_dir / "trading_memory.md"
        prod_memory = Path(os.path.expanduser(_PROD_MEMORY_LOG))
        if not memory.exists() and prod_memory.exists():
            shutil.copy(prod_memory, memory)
        runner = create_tradingagents_runner(
            quick_think_llm=quick, deep_think_llm=deep,
            reports_dir=str(arm_dir / "reports"), results_dir=str(arm_dir / "logs"),
            memory_log_path=str(memory))
        out = out_dir / f"{arm}.jsonl"
        done = set()
        if out.exists():
            done = {r["ticker"] for r in map(json.loads, out.read_text(
                encoding="utf-8").splitlines()) if not r.get("error")}
        with out.open("a", encoding="utf-8") as fh:
            for ticker in tickers:
                if ticker in done:
                    continue
                if args.max_usd and spent >= args.max_usd:
                    print(f"STOP: spent ${spent:.3f} >= --max-usd {args.max_usd}")
                    return 0
                rec = state["rec"] = Recorder()
                catcher.messages.clear()
                row = {"ticker": ticker, "arm": arm, "quick": quick, "deep": deep,
                       "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                t0 = time.time()
                try:
                    result = runner(ticker, spec)
                    report = Path(result["report_path"]).read_text(encoding="utf-8")
                    sec = parse_report(report)
                    pm = sec.get("Portfolio Manager", "")
                    analysts = "\n".join(sec.get(a, "") for a in _ANALYSTS)
                    row.update({
                        "report": result["report_path"], "verdict": result.get("verdict"),
                        "rm": rating_of(sec.get("Research Manager", "")),
                        "pm": rating_of(pm), "pm_text": pm,
                        "tables": sum(_analyst_summary_tail(sec.get(a, "")) is not None
                                      for a in _ANALYSTS),
                        "faith_analysts": faithfulness(analysts, "\n".join(rec.tool_text)),
                        "faith_pm": faithfulness(pm, report.replace(pm, "")),
                    })
                except Exception as exc:  # noqa: BLE001
                    row["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                by_model: dict = defaultdict(lambda: Counter())
                for c in rec.calls:
                    by_model[c["model"]].update(
                        {k: c[k] for k in ("in", "cached", "out", "reason", "usd")})
                    by_model[c["model"]]["calls"] += 1
                row.update({"secs": round(time.time() - t0, 1),
                            "usd": sum(c["usd"] for c in rec.calls),
                            "models": {m: dict(v) for m, v in by_model.items()},
                            "call_errors": rec.errors,
                            "fallbacks": list(catcher.messages)})
                spent += row["usd"]
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                print(f"  {arm:9} {ticker:5} "
                      f"{row.get('error') or 'RM=%-11s PM=%-11s' % (row['rm'], row['pm'])} "
                      f"tables={row.get('tables', '-')}/4 fb={len(row['fallbacks'])} "
                      f"faithA={_frac(row.get('faith_analysts'))} "
                      f"faithPM={_frac(row.get('faith_pm'))} "
                      f"${row['usd']:.3f} {row['secs']:.0f}s", flush=True)
    print(f"spent this invocation: ${spent:.3f}")
    return 0


def cmd_report(args) -> int:
    rows = []
    for path in sorted(glob.glob(os.path.join(os.path.expanduser(args.dir), "*.jsonl"))):
        rows += [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()
                 if l.strip()]
    arms = [a for a in ARMS if any(r["arm"] == a for r in rows)]
    ok = {a: [r for r in rows if r["arm"] == a and not r.get("error")] for a in arms}
    base = {r["ticker"]: r for r in ok.get("ds", [])}

    def pct(rs, key):
        pairs = [r[key] for r in rs if r.get(key)]
        total = sum(p[1] for p in pairs)
        return f"{sum(p[0] for p in pairs) / total:5.0%}" if total else "    -"

    print(f"  {'arm':9} {'n':>2} {'err':>3} {'fb':>3} {'tables':>7} {'faithA':>6} {'faithPM':>7} "
          f"{'min/tkr':>7} {'$/tkr':>6} {'PM mean':>7} {'PM=ds':>5} {'RM=ds':>5}")
    for a in arms:
        rs = ok[a]
        if not rs:
            print(f"  {a:9}  0 {sum(r['arm'] == a for r in rows):3d}")
            continue
        same = [(r["pm"] == base[r["ticker"]]["pm"], r["rm"] == base[r["ticker"]]["rm"])
                for r in rs if r["ticker"] in base]
        scores = [_SCORE[r["pm"]] for r in rs if r["pm"] in _SCORE]
        print(f"  {a:9} {len(rs):2d} {sum(r['arm'] == a for r in rows) - len(rs):3d} "
              f"{sum(len(r['fallbacks']) for r in rs):3d} "
              f"{sum(r['tables'] for r in rs):3d}/{4 * len(rs):<3d} "
              f"{pct(rs, 'faith_analysts'):>6} {pct(rs, 'faith_pm'):>7} "
              f"{statistics.mean(r['secs'] for r in rs) / 60:7.1f} "
              f"{statistics.mean(r['usd'] for r in rs):6.3f} "
              f"{(statistics.mean(scores) if scores else float('nan')):+7.2f} "
              f"{sum(s[0] for s in same):2d}/{len(same):<2d} "
              f"{sum(s[1] for s in same):2d}/{len(same):<2d}")
    for node in ("pm", "rm"):
        print(f"\n  {node.upper()} per ticker: " + "  ".join(arms))
        for t in sorted({r["ticker"] for r in rows}):
            cells = {r["arm"]: r[node] for r in rows if r["ticker"] == t and not r.get("error")}
            print(f"    {t:6} " + "  ".join(f"{cells.get(a, '-'):11}" for a in arms))
    errors = [(r["arm"], r["ticker"], r["error"][:120]) for r in rows if r.get("error")]
    if errors:
        print("\n  errors:")
        for e in errors:
            print("   ", *e)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--arms", required=True)
    r.add_argument("--only", required=True)
    r.add_argument("--out-dir", required=True)
    r.add_argument("--max-usd", type=float, default=5.0)
    p = sub.add_parser("report")
    p.add_argument("dir")
    args = ap.parse_args()
    return {"run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
