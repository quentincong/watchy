#!/usr/bin/env python
"""Replay the Research Manager / Portfolio Manager on frozen Weekly Full inputs:
DeepSeek (production) vs OpenRouter models (Qwen3.7 Max, GPT-6.1 Sol, Gemini 3.8
Flash) at several thinking budgets / effort levels.

Fixtures are the reports behind the current weekly digests
(``~/watchy/reports/*_weekly_digest.json`` -> ``result.report_path``). The RM
gets the saved bull/bear debate, the PM the saved risk debate plus the SAVED
RM plan and trader proposal, so each node is compared on identical input and
only the model changes (state reconstruction reused from
``compare_rm_pm_models.py``; same fidelity caveat about the interleaved debate
transcript).

Arms:  deepseek                  production client (deepseek-flash, TA factory)
       qwen:<budget>[:<method>]  qwen/qwen3.7-max, reasoning.max_tokens=<budget>
                                 (or "default" = reasoning on, no budget)
       sol:<effort>[:<method>]   openai/gpt-6.1-sol, reasoning.effort=<effort>
       gemini:<effort>[:<method>] google/gemini-3.8-flash, reasoning.effort=<effort>
                                 (any OpenRouter arm takes a budget, an effort
                                 name, or "default" in the second field)
       deepseek:struct           deepseek-flash, structured via function calling
                                 with tool_choice suppressed (the caps fix)
       deepseek:struct:max       same, reasoning_effort="max" (DeepSeek: high|max)
       method: tc   = TA default caps: function calling + forced tool_choice
                      (what a plain shim gets with no capability override)
               auto = function calling, tool_choice suppressed
               json = response_format json_schema
               free = no structured output (free text, like production DeepSeek)
The TA structured-output fallback (a failed structured call retried once as
free text) is detected from its WARNING and reported, together with the number
of LLM calls per node — a fallback that happens after generation is paid twice.

MANUAL: calls paid APIs. Run on the VPS with the trading pyenv:

    PY=/home/watchy/.pyenv/versions/3.11.9/envs/trading/bin/python
    $PY scripts/compare_rm_pm_qwen.py run --arms deepseek,qwen:4000 --only NVDA \\
        --out ~/abtest_qwen/rmpm_smoke.jsonl
    $PY scripts/compare_rm_pm_qwen.py report ~/abtest_qwen/rmpm.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from compare_rm_pm_models import faithfulness, parse_report, pm_state, rm_state  # noqa: E402

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("rmpm-qwen-ab")

OR_BASE = "https://openrouter.ai/api/v1"
# OpenRouter arms: family -> (slug, USD per 1M tokens). Reasoning bills as output.
# The price table is only the fallback for a response without usage.cost; cache
# rates for Sol/Gemini were not checked, so their cached input is priced as input.
_OR_MODELS = {
    "qwen": ("qwen/qwen3.7-max", {"in": 1.475, "cache": 0.295, "out": 4.425}),
    "sol": ("openai/gpt-6.1-sol", {"in": 2.0, "cache": 2.0, "out": 10.0}),
    "gemini": ("google/gemini-3.8-flash", {"in": 0.75, "cache": 0.75, "out": 3.75}),
}
# RM/PM answers run ~1-3k tokens; reasoning shares max_tokens on OpenRouter.
_ANSWER_TOKENS = 8192
_DEFAULT_REASON_CEILING = 32768


# The free-text path (production DeepSeek today, and any fallback) writes the
# rating many ways: "**Rating: Underweight NVDA**", "Rating:** Hold",
# "**Recommendation**: Overweight", "Final Trading Decision: Underweight",
# "**Research Manager Verdict: Underweight NVDA**",
# "rating for NVDA is **Underweight**". compare_rm_pm_models.RATING_RE only
# knows the rendered-schema form, so it returns "?" for all of these.
_RATING_RE = re.compile(
    r"(?:final\s+(?:trading\s+)?(?:decision|rating)|verdict|rating|recommendation)"
    r"[\s:*_]{0,6}(?:for\s+\S+\s+is\s+)?[\s*_]*"
    r"(Buy|Overweight|Hold|Underweight|Sell)\b",
    re.IGNORECASE,
)


def rating_of(text: str) -> str:
    m = _RATING_RE.search(text or "")
    return m.group(1).title() if m else "?"


def _prod_rating(report: str, node: str) -> str:
    try:
        sec = parse_report(Path(report).read_text(encoding="utf-8"))
    except OSError:
        return "?"
    return rating_of(sec.get("Research Manager" if node == "RM" else "Portfolio Manager", ""))


class _FallbackCatcher(logging.Handler):
    """Collects TA's 'structured-output invocation failed' warnings."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage()[:300])


def _recorder_cls():
    from langchain_core.callbacks import BaseCallbackHandler

    from watchy.token_tracker import _extract_usage

    class Recorder(BaseCallbackHandler):
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.errors: list[str] = []

        def on_llm_end(self, response, **kwargs):  # noqa: D102
            i, c, o, r, model = _extract_usage(response)
            tu = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
            self.calls.append({"in": i, "cached": c, "out": o, "reason": r,
                               "model": model, "or_cost": tu.get("cost")})

        def on_llm_error(self, error, **kwargs):  # noqa: D102
            self.errors.append(f"{type(error).__name__}: {str(error)[:200]}")

    return Recorder


def _make_llm(arm: str, rec, keys: dict):
    """Build the chat model for an arm, the way the production shim would."""
    from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI

    if arm == "deepseek":
        from tradingagents.llm_clients.factory import create_llm_client

        os.environ.setdefault("DEEPSEEK_API_KEY", keys["deepseek"])
        return create_llm_client("deepseek", "deepseek-flash", callbacks=[rec]).get_llm()
    if arm in ("deepseek:struct", "deepseek:struct:max"):
        # Production DeepSeek with the capability fix: structured output via
        # function calling but no forced tool_choice (what TA's
        # _DEEPSEEK_THINKING caps do for deepseek-v4-*), so the structured path
        # actually runs instead of 400 -> free text.
        from tradingagents.llm_clients.openai_client import DeepSeekChatOpenAI

        class DeepSeekStruct(DeepSeekChatOpenAI):
            def with_structured_output(self, schema, *, method=None, **kwargs):
                kwargs.setdefault("tool_choice", None)
                return super().with_structured_output(
                    schema, method="function_calling", **kwargs)

        extra = {"reasoning_effort": "max"} if arm.endswith(":max") else {}
        return DeepSeekStruct(model="deepseek-flash", base_url="https://api.deepseek.com",
                              api_key=keys["deepseek"], callbacks=[rec],
                              timeout=300, max_retries=2, **extra)

    parts = arm.split(":")
    model = _OR_MODELS[parts[0]][0]
    budget = parts[1] if len(parts) > 1 else "default"
    method = parts[2] if len(parts) > 2 else "tc"
    if budget == "default":
        reasoning = {"enabled": True}
    elif budget.isdigit():
        reasoning = {"max_tokens": int(budget)}
    else:
        reasoning = {"effort": budget}
    ceiling = int(budget) if budget.isdigit() else _DEFAULT_REASON_CEILING

    class OpenRouterChat(NormalizedChatOpenAI):
        def with_structured_output(self, schema, *, method=None, **kwargs):
            if arm_method == "auto":
                kwargs.setdefault("tool_choice", None)
                return super().with_structured_output(schema, method="function_calling", **kwargs)
            if arm_method == "json":
                return super().with_structured_output(schema, method="json_schema", **kwargs)
            if arm_method == "free":
                # bind_structured catches this and the node runs free text,
                # the same path production DeepSeek takes today.
                raise NotImplementedError("free-text arm")
            return super().with_structured_output(schema, method=method, **kwargs)

    arm_method = method
    return OpenRouterChat(
        model=model, base_url=OR_BASE, api_key=keys["openrouter"],
        callbacks=[rec], timeout=300, max_retries=2,
        max_tokens=_ANSWER_TOKENS + ceiling,
        extra_body={
            "reasoning": reasoning,
            "provider": {"require_parameters": True, "allow_fallbacks": False},
            "usage": {"include": True},
        },
    )


def _usd(arm: str, calls: list[dict], when: datetime) -> float:
    if arm.startswith("deepseek"):
        from watchy.token_tracker import _cost_usd

        return sum(_cost_usd("deepseek-flash", c["in"], c["cached"], c["out"], when)
                   for c in calls)
    total = 0.0
    price = _OR_MODELS[arm.split(":")[0]][1]
    for c in calls:
        if c.get("or_cost") is not None:
            total += float(c["or_cost"])
        else:
            miss = max(c["in"] - c["cached"], 0)
            total += (miss * price["in"] + c["cached"] * price["cache"]
                      + c["out"] * price["out"]) / 1e6
    return total


def _fixtures(reports_dir: str, only: set[str]) -> list[tuple[str, Path]]:
    out = []
    for path in sorted(glob.glob(os.path.join(os.path.expanduser(reports_dir),
                                              "*_weekly_digest.json"))):
        ticker = os.path.basename(path).split("_", 1)[0].upper()
        if only and ticker not in only:
            continue
        rp = json.loads(Path(path).read_text(encoding="utf-8"))["result"].get("report_path")
        if rp and Path(rp).exists():
            out.append((ticker, Path(rp)))
        else:
            logger.warning("%s: digest has no readable report_path (%s)", ticker, rp)
    return out


def _keys() -> dict:
    import yaml

    from watchy.config import load_config

    with open(os.path.expanduser("~/watchy_config/secrets.yaml"), encoding="utf-8") as f:
        secrets = yaml.safe_load(f) or {}
    llm = load_config().llm
    return {"deepseek": llm.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY", ""),
            "openrouter": ((secrets.get("openrouter") or {}).get("api_key") or "").strip()}


def cmd_run(args) -> int:
    from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
    from tradingagents.agents.managers.research_manager import create_research_manager

    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    fixtures = _fixtures(args.reports_dir, only)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    nodes = [n.strip().upper() for n in args.nodes.split(",") if n.strip()]
    keys = _keys()
    unknown = [a for a in arms if not a.startswith("deepseek")
               and a.split(":")[0] not in _OR_MODELS]
    if unknown:
        print(f"unknown arms: {unknown}", file=sys.stderr)
        return 2
    if any(not a.startswith("deepseek") for a in arms) and not keys["openrouter"]:
        print("openrouter.api_key missing from secrets.yaml", file=sys.stderr)
        return 2
    Recorder = _recorder_cls()
    catcher = _FallbackCatcher()
    logging.getLogger("tradingagents.agents.utils.structured").addHandler(catcher)

    out = Path(os.path.expanduser(args.out))
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if not r.get("error"):
                done.add((r["ticker"], r["node"], r["arm"], r["rep"]))
    cells = [(t, p, n, a, rep) for rep in range(args.repeats) for t, p in fixtures
             for n in nodes for a in arms]
    todo = [c for c in cells if (c[0], c[2], c[3], c[4]) not in done]
    print(f"{len(fixtures)} fixtures x {len(nodes)} nodes x {len(arms)} arms x "
          f"{args.repeats} = {len(cells)} cells, {len(todo)} to run -> {out}", flush=True)
    spent = 0.0
    with out.open("a", encoding="utf-8") as fh:
        for ticker, rpath, node, arm, rep in todo:
            if args.max_usd and spent >= args.max_usd:
                print(f"STOP: spent ${spent:.3f} >= --max-usd {args.max_usd}")
                break
            source = rpath.read_text(encoding="utf-8")
            sec = parse_report(source)
            state = rm_state(ticker, sec) if node == "RM" else pm_state(ticker, sec)
            prod_text = sec.get("Research Manager" if node == "RM" else "Portfolio Manager", "")
            rec = Recorder()
            catcher.messages.clear()
            row = {"ticker": ticker, "node": node, "arm": arm, "rep": rep,
                   "report": str(rpath),
                   "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            t0 = time.time()
            try:
                llm = _make_llm(arm, rec, keys)
                factory = create_research_manager if node == "RM" else create_portfolio_manager
                result = factory(llm)(state)
                text = str(result.get("investment_plan" if node == "RM"
                                      else "final_trade_decision", ""))
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                text = ""
            secs = time.time() - t0
            fth = faithfulness(text, source)
            row.update({
                "secs": round(secs, 1), "text": text, "rating": rating_of(text),
                "prod_rating": rating_of(prod_text), "chars": len(text),
                "calls": len(rec.calls), "call_errors": rec.errors,
                "fallback": bool(catcher.messages), "fallback_msgs": list(catcher.messages),
                "in": sum(c["in"] for c in rec.calls),
                "cached": sum(c["cached"] for c in rec.calls),
                "out": sum(c["out"] for c in rec.calls),
                "reason": sum(c["reason"] for c in rec.calls),
                "resp_models": sorted({c["model"] for c in rec.calls}),
                "usd": _usd(arm, rec.calls, datetime.now(timezone.utc)),
                "faithful": list(fth) if fth else None,
            })
            spent += row["usd"]
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            print(f"  {ticker:5} {node} {arm:16} r{rep} "
                  f"{row.get('error') or row['rating']:11} prod={row['prod_rating']:11} "
                  f"calls={row['calls']} fb={'Y' if row['fallback'] else 'n'} "
                  f"in={row['in']:6} out={row['out']:5} think={row['reason']:5} "
                  f"${row['usd']:.4f} {row['secs']:5.1f}s "
                  f"fth={'%d/%d' % tuple(fth) if fth else '-'}", flush=True)
    print(f"spent this invocation: ${spent:.3f}")
    return 0


def cmd_report(args) -> int:
    rows = [json.loads(l) for l in Path(os.path.expanduser(args.file))
            .read_text(encoding="utf-8").splitlines() if l.strip()]
    arms = sorted({r["arm"] for r in rows}, key=lambda a: (not a.startswith("deepseek"), a))
    # "=ds" compares against production DeepSeek: the free-text arm when it was
    # run, else the structured one (production since the 2026-09-29 caps fix).
    base_arm = "deepseek" if "deepseek" in arms else "deepseek:struct"
    prod_cache: dict = {}
    for r in rows:
        r["rating"] = rating_of(r.get("text", ""))
        key = (r["report"], r["node"])
        if key not in prod_cache:
            prod_cache[key] = _prod_rating(r["report"], r["node"])
        r["prod_rating"] = prod_cache[key]
    for node in ("RM", "PM"):
        nr = [r for r in rows if r["node"] == node]
        if not nr:
            continue
        base = {(r["ticker"], r["rep"]): r["rating"] for r in nr
                if r["arm"] == base_arm and not r.get("error")}
        print(f"\n[{node}]")
        print(f"  {'arm':18} {'n':>3} {'err':>3} {'fb':>3} {'calls':>5} {'in':>6} {'out':>5} "
              f"{'think':>6} {'$/call':>7} {'secs':>5} {'=ds':>5} {'=prod':>5} {'faith':>6}")
        for a in arms:
            rs = [r for r in nr if r["arm"] == a]
            ok = [r for r in rs if not r.get("error")]
            if not ok:
                print(f"  {a:18} {len(rs):3d} {len(rs):3d}")
                continue
            agree = [r["rating"] == base[(r["ticker"], r["rep"])] for r in ok
                     if (r["ticker"], r["rep"]) in base]
            prod = [r["rating"] == r["prod_rating"] for r in ok]
            fp = sum(r["faithful"][0] for r in ok if r["faithful"])
            ft = sum(r["faithful"][1] for r in ok if r["faithful"])
            print(f"  {a:18} {len(rs):3d} {len(rs) - len(ok):3d} "
                  f"{sum(r['fallback'] for r in ok):3d} "
                  f"{statistics.mean(r['calls'] for r in ok):5.2f} "
                  f"{statistics.mean(r['in'] for r in ok):6.0f} "
                  f"{statistics.mean(r['out'] for r in ok):5.0f} "
                  f"{statistics.mean(r['reason'] for r in ok):6.0f} "
                  f"{statistics.mean(r['usd'] for r in ok):7.4f} "
                  f"{statistics.mean(r['secs'] for r in ok):5.1f} "
                  f"{(f'{sum(agree)}/{len(agree)}' if agree else '-'):>5} "
                  f"{sum(prod)}/{len(prod):<3} "
                  f"{(fp / ft if ft else 0):6.0%}")
        print(f"  ratings:")
        for a in arms:
            c = Counter(r["rating"] for r in nr if r["arm"] == a and not r.get("error"))
            print(f"    {a:18} {dict(c)}")
        fbm = Counter(m.split("(")[1][:90] if "(" in m else m[:90]
                      for r in nr for m in r.get("fallback_msgs", []))
        if fbm:
            print("  fallback reasons:")
            for m, n in fbm.most_common(5):
                print(f"    {n:3d}x {m}")
        per = defaultdict(dict)
        for r in nr:
            if not r.get("error"):
                per[r["ticker"]][(r["arm"], r["rep"])] = r["rating"]
        print(f"  per ticker: " + "  ".join(arms))
        for t in sorted(per):
            print(f"    {t:6} " + "  ".join(
                "/".join(v for (a2, _), v in sorted(per[t].items()) if a2 == a) or "-"
                for a in arms))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--arms", required=True)
    r.add_argument("--nodes", default="RM,PM")
    r.add_argument("--only", default="")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--reports-dir", default="~/watchy/reports")
    r.add_argument("--out", required=True)
    r.add_argument("--max-usd", type=float, default=8.0)
    p = sub.add_parser("report")
    p.add_argument("file")
    args = ap.parse_args()
    return {"run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
