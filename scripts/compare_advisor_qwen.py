#!/usr/bin/env python
"""Offline advisor comparison on frozen Weekly Full inputs: Gemini vs Qwen3.7 Max.

Three subcommands, run on the VPS as ``watchy`` with the trading pyenv:

  freeze  Build one frozen advisor prompt per ticker from the weekly digests
          (``~/watchy/reports/*_weekly_digest.json``). The prompt comes out of
          the REAL ``advisor.get_advice`` (plan_request=True, as Weekly Full
          calls it) with the LLM call stubbed, so it is byte-for-byte what
          production would send today. Frozen alongside it:
            - price: the mark the production advisor saw for that digest
              (``advice_log`` row that followed the digest), used as the plan's
              input price and the position mark;
            - indicators/ATR: daily bars through ``--asof`` (last full session
              before the run), current price overridden with the mark above;
            - positions: the ``advice_log`` snapshot per ticker; cash is the
              current Schwab cache cash with later trades unwound at the logged
              mark (approximate, identical for every arm).
  run     Call each arm on every frozen prompt, ``--repeats`` times, appending
          one JSON line per call (resumable: finished cells are skipped). Each
          reply is parsed with the production parsers and turned into a
          validated ``WeeklyPlan``.
  report  Summarise a run file: validity, levels vs price in ATR, decision
          agreement and run-to-run stability, tokens, cost, latency.

Arms:  gemini:<level>            gemini advisor model at thinking <level>
       qwen:default              OpenRouter qwen/qwen3.7-max, reasoning on, no budget
       qwen:<N>                  same with reasoning.max_tokens = N
       qwen:off                  reasoning disabled

MANUAL: calls paid APIs (never from tests). Needs the gemini key in secrets
``llm:`` and ``openrouter: {api_key}`` as a top-level secrets section.

    PY=/home/watchy/.pyenv/versions/3.11.9/envs/trading/bin/python
    $PY scripts/compare_advisor_qwen.py freeze --out ~/abtest_qwen/frozen
    $PY scripts/compare_advisor_qwen.py run --frozen ~/abtest_qwen/frozen \\
        --arms gemini:medium,qwen:default --repeats 2 --out ~/abtest_qwen/stage1.jsonl
    $PY scripts/compare_advisor_qwen.py report ~/abtest_qwen/stage1.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.request  # advisor._post_json relies on it being imported elsewhere
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("advisor-qwen-ab")

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
QWEN_MODEL = "qwen/qwen3.7-max"
# Visible answer (header + paragraph + WEEKLY PLAN block) is ~600-900 tokens.
# Reasoning shares max_tokens on OpenRouter, so a budget arm gets its budget on
# top; the unbudgeted arm gets a wide ceiling. A ceiling is not a charge.
_ANSWER_TOKENS = 4096
_DEFAULT_REASON_CEILING = 32768
_QWEN_TIMEOUT = 300


# --------------------------------------------------------------------------- freeze


class FrozenPositionSource:
    """A fixed book, rendered exactly like the live RobustPositionSource."""

    def __init__(self, summary, provenance: str = "Schwab (live)") -> None:
        self.summary = summary
        self.provenance = provenance

    def get_position(self, ticker):
        for p in self.summary.positions:
            if p.ticker.upper() == ticker.upper():
                return p
        return None

    def get_all_positions(self):
        return list(self.summary.positions)

    def get_account_summary(self):
        return self.summary

    def format_position_context(self, ticker):
        from watchy.positions import render_position

        pos = self.get_position(ticker)
        if pos is None:
            return None
        return f"{render_position(pos)}\n  (source: {self.provenance})"

    def format_portfolio_context(self):
        from watchy.positions import render_portfolio

        return f"{render_portfolio(self.summary)}\n  (source: {self.provenance})"


def _advice_row_for_digest(db: sqlite3.Connection, ticker: str, saved_at: str):
    """The weekly_full advice_log row written right after this digest was saved."""
    return db.execute(
        "SELECT advised_ts, price, quantity, average_cost, zone_armed FROM advice_log "
        "WHERE ticker=? AND source='weekly_full' AND advised_ts>=? "
        "ORDER BY advised_ts LIMIT 1",
        (ticker, saved_at[:19]),
    ).fetchone()


def cmd_freeze(args) -> int:
    import pandas as pd

    from watchy import advisor
    from watchy.config import load_config
    from watchy.indicators import _fetch_history, compute_indicators
    from watchy.positions import AccountSummary, Position, PositionCache

    out = Path(os.path.expanduser(args.out))
    out.mkdir(parents=True, exist_ok=True)
    config = load_config()
    db = sqlite3.connect(os.path.expanduser(args.db))
    asof = pd.Timestamp(args.asof).date()

    digests = sorted(glob.glob(os.path.join(os.path.expanduser(args.reports_dir),
                                            "*_weekly_digest.json")))
    rows: dict[str, dict] = {}
    for path in digests:
        ticker = os.path.basename(path).split("_", 1)[0].upper()
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        r = _advice_row_for_digest(db, ticker, payload["saved_at"])
        if r is None:
            logger.warning("%s: no weekly_full advice row after digest %s — skipped",
                           ticker, payload["saved_at"])
            continue
        advised_ts, price, qty, avg, zone = r
        rows[ticker] = {"digest": path, "saved_at": payload["saved_at"],
                        "result": payload["result"], "advised_ts": advised_ts,
                        "price": float(price), "quantity": qty, "average_cost": avg,
                        "prod_zone_armed": bool(zone)}

    # The book as of the digests: advice_log quantities/costs at the logged mark.
    positions = []
    for t, row in rows.items():
        if row["quantity"]:
            q, a, p = float(row["quantity"]), float(row["average_cost"]), row["price"]
            positions.append(Position(
                ticker=t, quantity=q, average_cost=a, market_value=round(q * p, 2),
                unrealized_pnl=round(q * (p - a), 2),
                unrealized_pnl_pct=(p - a) / a * 100 if a else None, current_price=p,
            ))
    cached = PositionCache().read()
    if cached is None:
        print("no Schwab position cache — cannot derive cash", file=sys.stderr)
        return 2
    now_summary, fetched_at = cached
    # A holding with no weekly digest (not on the watchlist) keeps its cached row.
    positions += [p for p in now_summary.positions if p.ticker.upper() not in rows]
    now_qty = {p.ticker.upper(): p.quantity for p in now_summary.positions}
    then_qty = {p.ticker.upper(): p.quantity for p in positions}
    cash = float(now_summary.cash_balance or 0.0)
    unwound = []
    for t in sorted(set(now_qty) | set(then_qty)):
        d = now_qty.get(t, 0.0) - then_qty.get(t, 0.0)
        if abs(d) > 1e-9:
            px = rows[t]["price"] if t in rows else next(
                p.current_price for p in now_summary.positions if p.ticker.upper() == t)
            cash += d * px          # a later buy spent cash: add it back
            unwound.append(f"{t} {d:+g}@{px:.2f}")
    # Keep the live cache's ordering so the portfolio block reads like production.
    order = {p.ticker.upper(): i for i, p in enumerate(now_summary.positions)}
    positions.sort(key=lambda p: order.get(p.ticker, 99))
    total = round(cash + sum(p.market_value for p in positions), 2)
    # The advisor never needs the brokerage account number; don't ship it to a
    # new provider. Masked identically for every arm, so it can't bias the test.
    acct = str(now_summary.account_id)
    summary = AccountSummary(account_id="****" + acct[-4:], total_value=total,
                             cash_balance=round(cash, 2), positions=positions)
    ps = FrozenPositionSource(summary)
    logger.info("Frozen book: %d positions, cash %.2f (cache %s, unwound: %s), total %.2f",
                len(positions), cash, fetched_at.isoformat(timespec="minutes"),
                ", ".join(unwound) or "none", total)

    # Capture the exact production prompt: get_advice with the Gemini call stubbed.
    captured: list[str] = []
    real_call = advisor._call_gemini
    advisor._call_gemini = lambda prompt, *a, **k: captured.append(prompt) or ""
    config.llm.provider = "gemini"
    config.llm.api_key = config.llm.api_key or "offline"
    manifest = {"asof": str(asof), "created": datetime.now(timezone.utc).isoformat(),
                "book": {"cash": summary.cash_balance, "total": total,
                         "unwound": unwound, "cache_fetched_at": fetched_at.isoformat()},
                "tickers": []}
    try:
        for t, row in sorted(rows.items()):
            hist = _fetch_history(t)
            if hist is None or hist.empty:
                logger.warning("%s: no history — skipped", t)
                continue
            idx = pd.to_datetime(hist.index)
            if idx.tz is not None:
                idx = idx.tz_convert("America/New_York")
            hist = hist[[d.date() <= asof for d in idx]]
            bundle = compute_indicators(t, hist)
            if bundle is None:
                logger.warning("%s: no indicator bundle — skipped", t)
                continue
            last_bar = str(bundle.timestamp)[:10]
            bundle.current_price = row["price"]
            bundle.fetched_at = datetime.fromisoformat(row["advised_ts"])
            captured.clear()
            advisor.get_advice(t, row["result"], ps, config, "medium",
                               indicator_bundle=bundle, plan_request=True)
            if len(captured) != 1:
                logger.warning("%s: prompt capture failed — skipped", t)
                continue
            prompt = captured[0]
            held = ps.get_position(t) is not None
            item = {
                "ticker": t, "held": held, "price": row["price"],
                "price_ts": row["advised_ts"], "last_bar": last_bar,
                "atr": bundle.atr, "avg_atr_20d": bundle.avg_atr_20d,
                "zone_armed": "TAKE-PROFIT ZONE ACTIVE" in prompt,
                "prod_zone_armed": row["prod_zone_armed"],
                "verdict": row["result"].get("verdict", ""),
                "digest": row["digest"], "digest_saved_at": row["saved_at"],
                "prompt_file": f"{t}.prompt.txt", "prompt_chars": len(prompt),
            }
            (out / item["prompt_file"]).write_text(prompt, encoding="utf-8")
            manifest["tickers"].append(item)
            logger.info("%s held=%s price=%.2f last_bar=%s atr20=%.2f zone=%s chars=%d",
                        t, held, row["price"], last_bar, bundle.avg_atr_20d or 0,
                        item["zone_armed"], len(prompt))
            time.sleep(args.throttle)
    finally:
        advisor._call_gemini = real_call
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"froze {len(manifest['tickers'])} prompts -> {out}")
    return 0


# --------------------------------------------------------------------------- run


def _openrouter_key() -> str:
    import yaml

    with open(os.path.expanduser("~/watchy_config/secrets.yaml"), encoding="utf-8") as f:
        secrets = yaml.safe_load(f) or {}
    return ((secrets.get("openrouter") or {}).get("api_key") or "").strip()


_THINK_TAG_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


def call_qwen(prompt: str, key: str, setting: str) -> dict:
    from watchy.advisor import _post_json

    body: dict = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "usage": {"include": True},
        # Fail loudly rather than silently drop the reasoning parameter or route
        # to another host (Alibaba is the only provider today).
        "provider": {"require_parameters": True, "allow_fallbacks": False},
    }
    if setting == "off":
        body["reasoning"] = {"enabled": False}
        body["max_tokens"] = _ANSWER_TOKENS
    elif setting == "default":
        body["reasoning"] = {"enabled": True}
        body["max_tokens"] = _ANSWER_TOKENS + _DEFAULT_REASON_CEILING
    else:
        budget = int(setting)
        body["reasoning"] = {"max_tokens": budget}
        body["max_tokens"] = _ANSWER_TOKENS + budget
    t0 = time.time()
    d = _post_json(OR_URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Authorization": f"Bearer {key}",
    }, timeout=_QWEN_TIMEOUT)
    secs = time.time() - t0
    if d.get("error") and not d.get("choices"):
        raise RuntimeError(str(d["error"])[:300])
    ch = d["choices"][0]
    msg = ch.get("message") or {}
    text = msg.get("content") or ""
    think_in_content = bool(_THINK_TAG_RE.match(text))
    text = _THINK_TAG_RE.sub("", text)
    u = d.get("usage") or {}
    reason = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    comp = int(u.get("completion_tokens") or 0)
    return {
        "text": text, "in": int(u.get("prompt_tokens") or 0),
        "cached": int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
        "out": comp - reason, "think": reason, "usd": float(u.get("cost") or 0.0),
        "secs": round(secs, 1), "finish": ch.get("finish_reason"),
        "native_finish": ch.get("native_finish_reason"),
        "truncated": ch.get("finish_reason") == "length",
        "resp_model": d.get("model"), "resp_provider": d.get("provider"),
        "reasoning_chars": len(msg.get("reasoning") or ""),
        "think_in_content": think_in_content,
    }


def call_gemini(prompt: str, llm, level: str) -> dict:
    from watchy.advisor import (
        _ADVICE_MAX_TOKENS,
        _GEMINI_THINK_HEADROOM,
        _effective_key,
        _gemini_cost_usd,
        _gemini_thinking_config,
        _post_json,
    )

    model = llm.model or "gemini-3.5-flash"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={_effective_key(llm)}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": _ADVICE_MAX_TOKENS + _GEMINI_THINK_HEADROOM,
            "thinkingConfig": _gemini_thinking_config(level, model),
        },
    }).encode()
    t0 = time.time()
    d = _post_json(url, body, {"Content-Type": "application/json"})
    secs = time.time() - t0
    cand = d["candidates"][0]
    text = ""
    for part in cand.get("content", {}).get("parts", []):
        if part.get("text") and not part.get("thought"):
            text = part["text"]
            break
    u = d.get("usageMetadata", {})
    i, o, k = (int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0)),
               int(u.get("thoughtsTokenCount", 0)))
    return {
        "text": text, "in": i, "cached": int(u.get("cachedContentTokenCount", 0)),
        "out": o, "think": k, "usd": _gemini_cost_usd(i, o, k, model),
        "secs": round(secs, 1), "finish": cand.get("finishReason"),
        "truncated": cand.get("finishReason") == "MAX_TOKENS",
        "resp_model": d.get("modelVersion") or model,
    }


def evaluate(item: dict, text: str) -> dict:
    """Production parse + weekly-plan validation of one reply (pure)."""
    from watchy.advisor import _parse_advice
    from watchy.notify import _has_take_profit
    from watchy.plan import parse_plan_block, strip_plan_block
    from watchy.weekly import build_weekly_plan

    raw = (text or "").strip()
    block = parse_plan_block(raw)
    adv = _parse_advice(strip_plan_block(raw), item["ticker"])
    if not item["zone_armed"] and _has_take_profit(adv.get("take_profit")):
        adv["take_profit"] = ""
    adv["_plan_block"] = block
    plan = build_weekly_plan(
        item["ticker"], adv, {"verdict": item.get("verdict", "")},
        held=item["held"], input_price=item["price"], input_price_ts=item["price_ts"],
        now=datetime.fromisoformat(item["price_ts"]),
    )
    atr = item.get("avg_atr_20d") or item.get("atr")
    px = item["price"]

    def gap(level):
        return round((px - level) / atr, 2) if (level is not None and atr) else None

    return {
        "decision": adv.get("decision", ""), "urgency": adv.get("urgency", ""),
        "target": adv.get("target", ""), "take_profit": adv.get("take_profit", ""),
        "plan_decision": plan.decision, "valid": plan.is_valid,
        "errors": plan.validation_errors, "warnings": plan.validation_warnings,
        "block_found": block.found,
        "inv": plan.invalidation_level, "inv_gap_atr": gap(plan.invalidation_level),
        "inv_below_price": (plan.invalidation_level < px)
        if plan.invalidation_level is not None else None,
        "zone_lo": plan.buy_zone_low, "zone_hi": plan.buy_zone_high,
        "zone_hi_gap_atr": gap(plan.buy_zone_high),
        "chase": plan.chase_ceiling, "chase_gap_atr": gap(plan.chase_ceiling),
        "tp_price": plan.take_profit_price,
        "detail_chars": len(adv.get("detail", "")),
    }


def cmd_run(args) -> int:
    from watchy.config import load_config

    frozen = Path(os.path.expanduser(args.frozen))
    manifest = json.loads((frozen / "manifest.json").read_text(encoding="utf-8"))
    items = manifest["tickers"]
    if args.only:
        only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        items = [i for i in items if i["ticker"] in only]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    llm = load_config().llm
    orkey = _openrouter_key() if any(a.startswith("qwen:") for a in arms) else ""
    if any(a.startswith("qwen:") for a in arms) and not orkey:
        print("openrouter.api_key missing from secrets.yaml", file=sys.stderr)
        return 2

    out = Path(os.path.expanduser(args.out))
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if not r.get("error"):
                done.add((r["ticker"], r["arm"], r["rep"]))
    cells = [(i, a, rep) for rep in range(args.repeats) for i in items for a in arms]
    todo = [c for c in cells if (c[0]["ticker"], c[1], c[2]) not in done]
    print(f"{len(items)} tickers x {len(arms)} arms x {args.repeats} = {len(cells)} "
          f"cells, {len(todo)} to run -> {out}")
    spent = 0.0
    with out.open("a", encoding="utf-8") as fh:
        for item, arm, rep in todo:
            if args.max_usd and spent >= args.max_usd:
                print(f"STOP: spent ${spent:.3f} >= --max-usd {args.max_usd}")
                break
            prompt = (frozen / item["prompt_file"]).read_text(encoding="utf-8")
            kind, setting = arm.split(":", 1)
            row = {"ticker": item["ticker"], "arm": arm, "rep": rep,
                   "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            try:
                res = call_gemini(prompt, llm, setting) if kind == "gemini" \
                    else call_qwen(prompt, orkey, setting)
            except Exception as exc:  # noqa: BLE001
                detail = ""
                if hasattr(exc, "read"):
                    try:
                        detail = exc.read().decode()[:300]
                    except Exception:  # noqa: BLE001
                        pass
                row["error"] = f"{type(exc).__name__}: {exc} {detail}".strip()[:400]
                print(f"  {item['ticker']:5} {arm:14} r{rep} ERROR {row['error'][:120]}")
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                continue
            row.update(res)
            row.update(evaluate(item, res["text"]))
            spent += row["usd"]
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            print(f"  {item['ticker']:5} {arm:14} r{rep} {row['decision'] or '<none>':5}/"
                  f"{row['urgency'] or '-':6} valid={'Y' if row['valid'] else 'N'} "
                  f"in={row['in']:5} out={row['out']:4} think={row['think']:5} "
                  f"${row['usd']:.4f} {row['secs']:5.1f}s"
                  f"{' TRUNC' if row['truncated'] else ''}"
                  f"{'' if row['valid'] else '  ' + '; '.join(row['errors'])[:90]}")
    print(f"spent this invocation: ${spent:.3f}")
    return 0


# --------------------------------------------------------------------------- report


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _fmt(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


def cmd_report(args) -> int:
    rows = [json.loads(l) for l in Path(os.path.expanduser(args.file))
            .read_text(encoding="utf-8").splitlines() if l.strip()]
    ok = [r for r in rows if not r.get("error")]
    arms = sorted({r["arm"] for r in rows}, key=lambda a: (not a.startswith("gemini"), a))
    by_arm = defaultdict(list)
    for r in ok:
        by_arm[r["arm"]].append(r)
    errs = Counter(r["arm"] for r in rows if r.get("error"))

    print(f"\n{'arm':15} {'n':>3} {'err':>3} {'valid':>6} {'trunc':>5} {'in':>6} {'out':>5} "
          f"{'think':>6} {'think_max':>9} {'$/call':>7} {'secs':>5} {'inv_gap':>7}")
    for a in arms:
        rs = by_arm[a]
        if not rs:
            print(f"{a:15} {0:3d} {errs[a]:3d}")
            continue
        n = len(rs)
        print(f"{a:15} {n:3d} {errs[a]:3d} {sum(r['valid'] for r in rs)/n:6.0%} "
              f"{sum(bool(r['truncated']) for r in rs):5d} "
              f"{statistics.mean(r['in'] for r in rs):6.0f} "
              f"{statistics.mean(r['out'] for r in rs):5.0f} "
              f"{statistics.mean(r['think'] for r in rs):6.0f} "
              f"{max(r['think'] for r in rs):9d} "
              f"{statistics.mean(r['usd'] for r in rs):7.4f} "
              f"{statistics.mean(r['secs'] for r in rs):5.1f} "
              f"{_fmt(_med([r['inv_gap_atr'] for r in rs])):>7}")

    print("\nper-arm detail")
    for a in arms:
        rs = by_arm[a]
        if not rs:
            continue
        dec = Counter(r["decision"] or "<none>" for r in rs)
        urg = Counter(r["urgency"] or "-" for r in rs)
        errc = Counter(e.split(" ")[0] if ":" not in e else e.split(":")[0]
                       for r in rs for e in r["errors"])
        models = Counter(str(r.get("resp_model")) for r in rs)
        inv_above = sum(1 for r in rs if r["inv_below_price"] is False)
        print(f"  {a}: decisions {dict(dec)} urgency {dict(urg)}")
        print(f"      invalidation >= price: {inv_above}; zone_hi gap med "
              f"{_fmt(_med([r['zone_hi_gap_atr'] for r in rs]))} ATR; chase gap med "
              f"{_fmt(_med([r['chase_gap_atr'] for r in rs]))} ATR")
        print(f"      validation errors: {dict(errc) or 'none'}")
        print(f"      response model: {dict(models)}")
        if any(r.get("think_in_content") for r in rs):
            print("      WARNING: <think> found inside content")

    # Stability (same arm, different reps) and agreement (arms vs first arm).
    cell = defaultdict(dict)
    for r in ok:
        cell[(r["ticker"], r["arm"])][r["rep"]] = r
    tickers = sorted({r["ticker"] for r in ok})
    print("\nrun-to-run stability (decision identical across reps)")
    for a in arms:
        pairs = [cell[(t, a)] for t in tickers if len(cell[(t, a)]) >= 2]
        same = sum(1 for p in pairs if len({x["decision"] for x in p.values()}) == 1)
        print(f"  {a:15} {same}/{len(pairs)}")
    base = arms[0]
    print(f"\ndecision agreement vs {base} (all rep pairs)")
    for a in arms[1:]:
        agree = total = 0
        for t in tickers:
            for x in cell[(t, base)].values():
                for y in cell[(t, a)].values():
                    total += 1
                    agree += x["decision"] == y["decision"]
        print(f"  {a:15} {agree}/{total}")

    print(f"\nper ticker (decision/urgency valid, per rep)")
    print(f"{'ticker':6} " + " ".join(f"{a:>26}" for a in arms))
    for t in tickers:
        cols = []
        for a in arms:
            reps = cell[(t, a)]
            cols.append(" ".join(
                f"{(reps[k]['decision'] or '?')[:4]}/{(reps[k]['urgency'] or '-')[:1]}"
                f"{'' if reps[k]['valid'] else '!'}" for k in sorted(reps)))
        print(f"{t:6} " + " ".join(f"{c:>26}" for c in cols))
    print("  ('!' = plan failed validation)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--out", required=True)
    f.add_argument("--reports-dir", default="~/watchy/reports")
    f.add_argument("--db", default="~/watchy/state.db")
    f.add_argument("--asof", default="2026-09-25",
                   help="last daily bar used for indicators/ATR")
    f.add_argument("--throttle", type=float, default=1.0)
    r = sub.add_parser("run")
    r.add_argument("--frozen", required=True)
    r.add_argument("--arms", required=True)
    r.add_argument("--repeats", type=int, default=2)
    r.add_argument("--only", default="")
    r.add_argument("--out", required=True)
    r.add_argument("--max-usd", type=float, default=5.0,
                   help="stop once this invocation has spent this much (0 = no cap)")
    p = sub.add_parser("report")
    p.add_argument("file")
    args = ap.parse_args()
    return {"freeze": cmd_freeze, "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
