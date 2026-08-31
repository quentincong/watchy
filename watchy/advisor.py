"""LLM-based position advisor.

Takes a TradingAgents analysis report + position data (from any PositionSource)
and produces actionable advice on whether to alter the position.

The advisor is a lightweight LLM call — it synthesizes, it doesn't re-analyze.
All the deep analysis is already done by TradingAgents.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from watchy.config import LLMConfig, WatchyConfig
# notify has no watchy imports of its own, so this cannot cycle. Reused rather
# than duplicated so "actionable Take-Profit" has exactly one definition.
from watchy.notify import _has_take_profit
from watchy.positions import PositionSource

logger = logging.getLogger(__name__)

ADVISOR_PROMPT = """You are a portfolio advisor. Below is a condensed analysis summary
from a team of financial analysts (market, sentiment, fundamentals, and risk) —
the final decision, risk assessment, trader plan, and each analyst's summary
table — plus the user's current position and portfolio overview.

Consider the user's overall portfolio composition when making your decision.

CRITICAL — concentration math: judge a position's weight against the TOTAL
ACCOUNT VALUE (equities PLUS cash and cash-equivalents like money-market/sweep),
never against the stock-only total. Use the "Total value" figure in the portfolio
overview as the denominator (it already includes cash); do NOT recompute weight
by summing only the stock positions. Uninvested cash is a risk buffer — a
position that looks heavy versus equities alone can be perfectly healthy versus
the full account (e.g. a $420 holding is 24% of $1,700 in stocks but only 12.6%
of a $3,340 account that also holds $1,700 cash). Do not advise a TRIM for
"over-concentration" unless the weight is high against the full account value.
NEVER add "buying power" or any margin/purchasing-power figure to the account
value to compute net worth or a concentration denominator — buying power is a
leveraged purchasing limit, not money you own. The "Total value" figure is the
sole denominator; it already includes cash and equivalents.

ODD-LOT / TINY-POSITION GUARD: a partial exit has to be placeable as a
WHOLE-SHARE SELL-LIMIT order — limit orders require whole shares, and a
fractional sell executes only as a MARKET order, which forfeits the pre-placed
limit that catches the intraday high. TRIM therefore only makes sense when the
REMAINING position is still a sensible whole-share size. If the position is
ALREADY fractional (a non-whole share count), a fractional MARKET sell is fine
— no new odd lot is created, and no limit order is given up that could have been
placed anyway. But for a whole-share position too small to split, do not force a
fractional sale: choose HOLD, or SELL to exit the entire share when the thesis is
genuinely bearish — never TRIM. The ONE exception: a single high-priced share
(roughly ≥ $1,000 per share) may be trimmed fractionally as a MARKET sell — say
explicitly that it is a market order and give NO limit price — when the analysis
strongly warrants taking money off the table.

TAKE-PROFIT / DON'T ROUND-TRIP A WINNER: protecting an existing gain matters as
much as finding an entry. When the position is IN PROFIT AND the analysis shows
the move is getting extended — price is at or into the resistance / upside-target
zone the analysts cite, momentum or volume is waning (weakening MACD, overbought
or rolling-over RSI, a low-volume bounce), or the remaining upside to the
analysts' target is small versus the downside to their stop — lean toward TRIM to
bank part of the gain rather than letting it fully round-trip. This is
deliberately NOT a fixed "up X% -> sell" rule: a strong, still-intact uptrend with
real upside left should be allowed to run (HOLD). The trigger is the COMBINATION
of an existing gain and a stalling / extended setup. Respect the guards above —
never force a fractional trim on a tiny whole-share position (follow the ODD-LOT
guard), and judge any resulting weight against full account value.

COST-BASIS CAVEAT: judge how extended a move is from PRICE, never from the
"Unrealized P&L" percentage in the position block. This account sells shares
highest-cost-first, so every trim removes the most expensive lot and MECHANICALLY
raises the reported gain % on the shares that remain, at a completely unchanged
price — a position trimmed twice can read +32% where the untrimmed one read +15%,
with the stock flat the whole time. That percentage only ever ratchets upward,
and each trim you recommend inflates it further, so its magnitude says nothing
about how far the move has run. Treat it as a yes/no fact — a gain exists — and
take every magnitude judgement (how extended, where to set a limit, how much to
sell) from price levels, ATR, and the analysts' cited targets.

{take_profit_guidance}
Respond in this exact format:

Ticker: {ticker}
Decision: <BUY / SELL / TRIM / ADD / HOLD>
Urgency: <HIGH / MEDIUM / LOW>
Target: <the entry / accumulation price level — where one would BUY or ADD to a
position — as a number like 215.50 (a range like 215-230 is fine). This is NOT a
stop-loss and NOT a take-profit; it's the level to watch for getting in. Write
N/A if the analysis gives no actionable entry level.>
Take-Profit: <the sell-limit price at which to BANK part of this gain — a level
ABOVE the current price the user pre-places as a limit order to catch an intraday
high — plus the WHOLE-share count to sell there, e.g. "sell 1 share at 192.50".
Write N/A when not taking profit (no meaningful gain, or holding the full position
with real upside left). Only meaningful for a held winner; see any TAKE-PROFIT
ZONE directive above.>

Then write a detailed paragraph (5-8 sentences) covering:
  - Specific entry/exit price target or range, referencing levels from the analysis
  - Suggested position size with rationale (e.g. "3% of portfolio / $5,000").
    Fractional shares are available, but only fall back to them when a single
    whole share is too large for the suggested allocation (i.e. one share costs
    more than the dollar amount you'd allocate); otherwise size in whole shares.
  - The 2-3 key reasons from the analysis that support this decision
  - The primary risk(s) that could invalidate this recommendation
  - Any conditions the user should watch for (e.g. "if it breaks below X, exit")

Be specific and data-driven — cite actual prices, indicator values, and analyst
findings from the report. Do NOT use JSON, markdown tables, or bullet points.

--- FULL ANALYSIS REPORT ---
Ticker: {ticker}
{analysis}

--- YOUR CURRENT POSITION ---
{position}

--- YOUR PORTFOLIO OVERVIEW ---
{portfolio}
"""


def _take_profit_guidance(
    ticker: str,
    analysis_text: str,
    position_source: PositionSource,
    config: WatchyConfig,
    indicator_bundle: Any,
) -> str:
    """Build the take-profit directive for the prompt, or "" when inactive (#28).

    Active only when take_profit is enabled AND the held position's unrealized
    gain has crossed the (per-ticker or global) floor. The current price / ATR
    come from indicator_bundle when given, else from the resolved position.
    """
    tp = config.take_profit
    if not tp.enabled:
        return ""
    from watchy import take_profit as tpmod

    try:
        pos = position_source.get_position(ticker)
    except Exception:  # noqa: BLE001
        logger.warning("take-profit: position lookup failed for %s", ticker, exc_info=True)
        return ""

    gain = tpmod.position_gain_pct(pos)
    floor = tpmod.effective_floor_pct(config.get_ticker_config(ticker), config)
    if not tpmod.is_in_zone(gain, floor):
        return ""

    # Anchor on the position's own live mark — the feed `gain` came from — so the
    # limit can't disagree with the gain (see tpmod.anchor_price). ATR still has
    # to come from the bundle; it has no per-feed equivalent.
    price = tpmod.anchor_price(pos, indicator_bundle)
    avg_atr = tpmod.bundle_avg_atr(indicator_bundle)
    upside = tpmod.extract_upside_level(analysis_text, price)
    # Share count decides which actions are even placeable (fractional -> market
    # only, 1 share -> full exit or nothing), so it has to reach the directive.
    shares = pos.quantity if pos is not None else None
    logger.info(
        "take-profit zone active for %s: gain=%.1f%% floor=%.1f%% price=%s "
        "upside=%s shares=%s",
        ticker, gain, floor, price, upside, shares,
    )
    # `gain` armed the gate and is logged above, but is deliberately not passed
    # on: its magnitude ratchets with every trim under HIFO and must not reach
    # the prompt as a sizing anchor (#30). See tpmod.build_guidance.
    return tpmod.build_guidance(
        ticker, price, avg_atr, upside, tp, shares=shares
    ) + "\n"


def get_advice(
    ticker: str,
    analysis_result: dict[str, Any],
    position_source: PositionSource,
    config: WatchyConfig,
    thinking_level: str = "off",
    *,
    indicator_bundle: Any = None,
) -> dict[str, str] | None:
    """Synthesize position-aware advice from analysis + portfolio.

    ``thinking_level`` (gemini only): off / minimal / low / medium / high. The
    caller passes the per-tier level (Tier 1 = cheap, Tier 2 = low); ignored by
    the non-gemini providers.

    ``indicator_bundle`` (optional) supplies the current price + ATR used by the
    take-profit gate (#28): when take_profit is enabled and the held position's
    unrealized gain has crossed the floor, an explicit take-profit directive is
    injected into the prompt so the advisor proposes a whole-share sell-limit.

    Returns a dict with keys: ticker, decision, urgency, target, take_profit,
    detail. Returns None if no LLM key is configured or the call fails.
    """
    llm = config.llm
    if not _effective_key(llm):
        field = "deepseek_api_key/api_key" if llm.provider == "deepseek" else "api_key"
        logger.info("No LLM %s configured — skipping advisor synthesis", field)
        return None

    position_text = position_source.format_position_context(ticker) or "No position held."
    portfolio_text = position_source.format_portfolio_context() or "Portfolio data unavailable."

    analysis_text = _format_analysis(analysis_result)

    take_profit_guidance = _take_profit_guidance(
        ticker, analysis_text, position_source, config, indicator_bundle
    )

    prompt = ADVISOR_PROMPT.format(
        ticker=ticker,
        analysis=analysis_text,
        position=position_text,
        portfolio=portfolio_text,
        take_profit_guidance=take_profit_guidance,
    )

    try:
        if llm.provider == "anthropic":
            result = _call_anthropic(prompt, llm)
        elif llm.provider in ("openai", "deepseek"):
            result = _call_openai_compatible(prompt, llm)
        elif llm.provider == "gemini":
            result = _call_gemini(prompt, llm, ticker, thinking_level)
        else:
            logger.warning("Unknown LLM provider: %s", llm.provider)
            return None

        parsed = _parse_advice(result.strip(), ticker)
        # #28: a Take-Profit line only means something when the mechanical
        # gain-gate actually armed the zone. With no zone the model fills the
        # field unprompted, and on a 1-share holding "sell 1 share at X" is a
        # FULL EXIT that notify.py would render under the Take-Profit heading
        # next to a HOLD decision. Drop it rather than surface a contradiction.
        if not take_profit_guidance and _has_take_profit(parsed.get("take_profit")):
            logger.info(
                "Advisor for %s: dropped volunteered Take-Profit (zone inactive): %s",
                ticker, parsed["take_profit"],
            )
            parsed["take_profit"] = ""
        logger.info(
            "Advisor for %s: decision=%s urgency=%s",
            ticker, parsed.get("decision"), parsed.get("urgency"),
        )
        return parsed
    except Exception:
        logger.exception("Advisor synthesis failed for %s", ticker)
        return None


def _parse_advice(raw: str, fallback_ticker: str) -> dict[str, str]:
    """Parse the structured advice output into a dict.

    Expected format::

        Ticker: NVDA
        Decision: BUY
        Urgency: HIGH

        <detail paragraph...>
    """
    parsed: dict[str, str] = {
        "ticker": fallback_ticker,
        "decision": "",
        "urgency": "",
        "target": "",
        "take_profit": "",
        "detail": "",
    }

    # Scan every line for the header fields (first match wins) rather than
    # stopping at the first non-header line — the model sometimes emits a blank
    # line or a short preamble before "Decision:", which previously dropped the
    # decision/urgency entirely. Non-header lines become the detail paragraph.
    got = {
        "ticker": False, "decision": False, "urgency": False,
        "target": False, "take_profit": False,
    }
    detail_lines: list[str] = []
    for line in raw.split("\n"):
        stripped = line.strip()
        low = stripped.lower()
        if not got["ticker"] and low.startswith("ticker:"):
            val = stripped.split(":", 1)[1].strip()
            if val:
                parsed["ticker"] = val
            got["ticker"] = True
        elif not got["decision"] and low.startswith("decision:"):
            parsed["decision"] = stripped.split(":", 1)[1].strip().upper()
            got["decision"] = True
        elif not got["urgency"] and low.startswith("urgency:"):
            parsed["urgency"] = stripped.split(":", 1)[1].strip().upper()
            got["urgency"] = True
        elif not got["take_profit"] and low.startswith("take-profit:"):
            # #28 sell-limit + whole-share count. Captured as a header so it
            # doesn't pollute the detail paragraph; shown verbatim in the alert.
            parsed["take_profit"] = stripped.split(":", 1)[1].strip()
            got["take_profit"] = True
        elif not got["target"] and low.startswith("target:"):
            # Captured as a header so it doesn't pollute the detail paragraph;
            # the numeric value (for #16's auto-target) is parsed via parse_price.
            parsed["target"] = stripped.split(":", 1)[1].strip()
            got["target"] = True
        elif stripped:
            detail_lines.append(stripped)

    parsed["detail"] = " ".join(detail_lines)
    return parsed


def parse_price(text: str | None) -> float | None:
    """Extract a numeric price from an advisor ``Target:`` value.

    Handles ``$215.50``, ``215.50``, ``215-230`` (→ midpoint), ``$3,000`` and
    returns None for ``N/A`` / empty / no-number strings. A range averages the
    first two numbers; a single value is returned as-is.
    """
    import re

    if not text:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))
    if not nums:
        return None
    vals = [float(n) for n in nums[:2]]
    return sum(vals) / len(vals)


def _analyst_summary_tail(text: str) -> str | None:
    """Extract an analyst's summary tail: its final Markdown table plus the
    conclusion that follows it (transaction proposal / final assessment / etc.),
    to the end of the report.

    Every analyst prompt instructs "append a Markdown table at the end … to
    organize key points"; the table is preceded by the long analytical prose
    (the token-heavy bulk we drop) and followed by a short actionable conclusion
    (kept — it carries the analyst's crisp "why"). Anchors on the LAST contiguous
    run of >=2 ``|``-rows and returns from there to the end. None if no table.
    """
    lines = text.splitlines()
    n = len(lines)
    last_table_start = None
    i = 0
    while i < n:
        if "|" in lines[i] and lines[i].strip():
            j = i
            while j < n and "|" in lines[j] and lines[j].strip():
                j += 1
            if j - i >= 2:  # header + separator at minimum
                last_table_start = i
            i = j
        else:
            i += 1
    if last_table_start is None:
        return None
    return "\n".join(lines[last_table_start:]).strip()


def _format_analysis(result: dict[str, Any]) -> str:
    """Build a compact analysis digest for the advisor LLM.

    Deliberately omits the full analyst prose — the dominant advisor input-token
    cost. The advisor synthesises an already-made decision, so it receives:

      - the decision chain (final decision + risk assessment + trader plan),
        which carries the rating and the concrete entry / stop / target levels;
      - each analyst's summary tail — its final table plus the conclusion that
        follows it — not the long analytical prose that precedes the table;
      - the SEPA stage.

    A report with no summary table falls back to its opening lines — and logs a
    greppable ``ADVISOR_TAIL_FALLBACK`` warning, because that substitutes the
    report's opening for its conclusion and is otherwise invisible. With no
    decision or reports at all, falls back to the truncated recommendations.
    """
    parts: list[str] = []

    # Decision chain — the rating and the concrete price levels live here.
    decision = result.get("_decision_raw") or ""
    if decision:
        parts.append(f"--- Final Decision ---\n{decision}")
    risk = result.get("risk_assessment")
    if risk:
        parts.append(f"--- Risk Assessment ---\n{risk}")
    trader = result.get("trader_plan")
    if trader:
        parts.append(f"--- Trader Plan ---\n{trader}")

    # Analyst signal: each report's trailing summary table, not the full prose.
    reports = result.get("_reports", {})
    for key, label in [
        ("market_report", "Market Analyst"),
        ("sentiment_report", "Sentiment Analyst"),
        ("news_report", "News Analyst"),
        ("fundamentals_report", "Fundamentals Analyst"),
    ]:
        text = reports.get(key) or ""
        if not text:
            continue
        tail = _analyst_summary_tail(text)
        if tail is None:
            # Every analyst prompt ends with "append a Markdown table at the end
            # of the report"; _analyst_summary_tail anchors on exactly that. If
            # the table goes missing the advisor silently receives the report's
            # OPENING instead of its conclusion — a quality regression with no
            # other symptom, so it gets a greppable warning. Expected triggers:
            # a model refresh (DeepSeek uses floating aliases and has shipped
            # snapshots unannounced) or lowered thinking effort, which costs
            # ~26 points of IFBench and is what this metric gates.
            logger.warning(
                "ADVISOR_TAIL_FALLBACK %s %s — no summary table in report, "
                "falling back to first 400 chars",
                result.get("ticker", "-"),
                label,
            )
        snippet = tail if tail else (text.strip()[:400] + " …")
        parts.append(f"--- {label} (summary) ---\n{snippet}")

    # Fallback: no decision and no reports → truncated recommendations field.
    if not parts:
        recs = result.get("recommendations", [])
        if recs:
            parts.append("Recommendations:\n" + "\n".join(recs))

    # SEPA stage context.
    stage = result.get("stage_context", {})
    if stage:
        sepa = stage.get("sepa_stage")
        if sepa:
            names = {1: "Basing", 2: "Advancing", 3: "Topping", 4: "Declining"}
            parts.append(f"SEPA Stage: {names.get(sepa, '?')} (stage {sepa})")

    if not parts:
        return json.dumps(result)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# LLM API call helpers
# ---------------------------------------------------------------------------


# Advice is a structured header + a 5-8 sentence paragraph with price targets,
# sizing, reasons, risks. 600 tokens truncated it mid-sentence (and on Gemini 2.5
# thinking models the budget is shared with hidden reasoning), so give it room.
_ADVICE_MAX_TOKENS = 1024
# Extra output headroom for the answer when Gemini thinking is enabled — thinking
# tokens share maxOutputTokens, so the visible answer needs its own room on top.
_GEMINI_THINK_HEADROOM = 2048

# Gemini prices, USD per 1M tokens (ai.google.dev/gemini-api/docs/pricing).
# Thinking tokens are billed at the output rate. Used only for the greppable
# GEMINICOST log estimate — the token counts logged are exact.
# Keyed by model so the estimate follows llm.model automatically: hardcoding one
# tier silently under-reported GEMINICOST by ~17% while the advisor ran 3.6.
# 3.6/3.7-flash are on promotional rates through 2026-12-31 and revert to
# $1.50/$7.50 on 2027-01-01 — revisit this table then.
_GEMINI_PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
}
# Unknown/unset model bills at the 3.5-flash tier, the advisor's historical default.
_GEMINI_PRICE_FALLBACK = (1.50, 9.00)
_GEMINI_PRICE_IN, _GEMINI_PRICE_OUT = _GEMINI_PRICE_FALLBACK


def _gemini_cost_usd(
    in_tok: int, out_tok: int, think_tok: int, model: str = ""
) -> float:
    """Approximate USD for one Gemini call (thinking billed as output).

    ``model`` picks the price tier; omitted or unrecognised falls back to the
    3.5-flash rates so older callers keep their previous numbers.
    """
    price_in, price_out = _GEMINI_PRICES.get(model, _GEMINI_PRICE_FALLBACK)
    return (in_tok * price_in + (out_tok + think_tok) * price_out) / 1_000_000


def _effective_key(llm: LLMConfig) -> str:
    """Resolve the API key for the configured provider.

    DeepSeek keys live in `deepseek_api_key` (so an Anthropic/OpenAI key can
    coexist in `api_key`); fall back to `api_key` if that field is empty.
    """
    if llm.provider == "deepseek":
        return llm.deepseek_api_key or llm.api_key
    return llm.api_key


# A single dropped connection used to cost a ticker its whole advice card AND
# its derived-target write (tier2 stores the #16 target off the advisor's Target
# field), because these calls had no retry at all — observed 2026-08-07 on
# NVT/TSM/COHR, all three failing in http.client._read_status. 30s was also tight
# for a long prompt plus thinking.
_HTTP_TIMEOUT = 60
_HTTP_ATTEMPTS = 3
_HTTP_BACKOFF_S = 2.0


def _post_json(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    timeout: int = _HTTP_TIMEOUT,
    attempts: int = _HTTP_ATTEMPTS,
) -> dict[str, Any]:
    """POST JSON and decode the reply, retrying transient failures.

    Retries socket timeouts, dropped/reset connections (the ``getresponse`` ->
    ``_read_status`` class, which urllib does NOT wrap in URLError), 429, and
    5xx. Any other 4xx is a genuine request error — raise at once, no retry.
    """
    import http.client
    import urllib.error

    req = urllib.request.Request(url, data=body, headers=headers)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
            last_exc = exc
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
        ) as exc:
            last_exc = exc
        if attempt < attempts:
            delay = _HTTP_BACKOFF_S * (2 ** (attempt - 1))
            logger.warning(
                "LLM HTTP attempt %d/%d failed (%s: %s) — retrying in %.0fs",
                attempt, attempts, type(last_exc).__name__, last_exc, delay,
            )
            time.sleep(delay)
    assert last_exc is not None  # unreachable: the loop either returns or sets it
    raise last_exc


def _call_anthropic(prompt: str, llm: LLMConfig) -> str:
    """Call Anthropic Messages API for advice synthesis."""

    url = llm.api_base or "https://api.anthropic.com/v1/messages"
    if not url.endswith("/messages"):
        url = url.rstrip("/") + "/messages"

    body = json.dumps({
        "model": llm.model,
        "max_tokens": _ADVICE_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    data = _post_json(url, body, {
        "Content-Type": "application/json",
        "x-api-key": _effective_key(llm),
        "anthropic-version": "2023-06-01",
    })
    return data["content"][0]["text"]


def _call_openai_compatible(prompt: str, llm: LLMConfig) -> str:
    """Call OpenAI-compatible Chat API (OpenAI, DeepSeek, etc.)."""

    default_bases = {
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
    }
    base = llm.api_base or default_bases.get(llm.provider, "https://api.openai.com/v1")
    url = base.rstrip("/") + "/chat/completions"

    body = json.dumps({
        "model": llm.model,
        "max_tokens": _ADVICE_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    data = _post_json(url, body, {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_effective_key(llm)}",
    })
    return data["choices"][0]["message"]["content"]


# thinkingLevel values each model accepts (ai.google.dev/gemini-api/docs/thinking).
# gemini-3.7-flash dropped "minimal" — sending it returns HTTP 400 — so "off" has
# to fall back to the cheapest level that model actually supports.
_GEMINI_THINKING_LEVELS: dict[str, tuple[str, ...]] = {
    "gemini-3.7-flash": ("low", "medium", "high"),
}
_GEMINI_THINKING_LEVELS_DEFAULT = ("minimal", "low", "medium", "high")


def _gemini_thinking_config(level: str, model: str = "") -> dict:
    """Map a thinking level to the gemini-3.x generateContent thinkingConfig.

    gemini-3.x uses ``thinkingLevel`` and REJECTS the legacy ``thinkingBudget``
    with HTTP 400 (verified on 3.6, 2026-07-21). Thinking can't be fully switched
    off, so "off" maps to the cheapest level the TARGET MODEL accepts: ``minimal``
    on 3.5/3.6, but ``low`` on 3.7, which rejects minimal outright (HTTP 400,
    verified 2026-08-31). An unsupported level warns and clamps rather than
    letting the call fail.
    """
    supported = _GEMINI_THINKING_LEVELS.get(model, _GEMINI_THINKING_LEVELS_DEFAULT)
    if level == "off":
        return {"thinkingLevel": supported[0]}
    if level not in supported:
        logger.warning(
            "thinking level %r not supported on %s — using %r",
            level, model or "<unset model>", supported[0],
        )
        return {"thinkingLevel": supported[0]}
    return {"thinkingLevel": level}


def _call_gemini(prompt: str, llm: LLMConfig, ticker: str = "", level: str = "off") -> str:
    """Call Google Gemini API for advice synthesis.

    Uses the Gemini REST API (not Vertex AI).
    API key from: https://aistudio.google.com/apikey
    """

    model = llm.model or "gemini-3.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={_effective_key(llm)}"

    # Thinking tokens share the output budget. On 3.6 every level (including "off"
    # → minimal) can emit thoughts, so always give the visible answer its own
    # headroom. maxOutputTokens is a ceiling, not a charge, so this is free.
    max_out = _ADVICE_MAX_TOKENS + _GEMINI_THINK_HEADROOM
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_out,
            "thinkingConfig": _gemini_thinking_config(level, model),
        },
    }).encode()

    data = _post_json(url, body, {"Content-Type": "application/json"})

    # Greppable cost line — the advisor (Gemini) is NOT covered by the DeepSeek
    # TOKENCOST callback, so log its usage here. thoughtsTokenCount is the
    # thinking slice (billed at the output rate); it's 0 when thinking is off.
    try:
        usage = data.get("usageMetadata", {})
        in_tok = int(usage.get("promptTokenCount", 0))
        out_tok = int(usage.get("candidatesTokenCount", 0))
        think_tok = int(usage.get("thoughtsTokenCount", 0))
        logger.info(
            "GEMINICOST %s model=%s think_level=%s in=%d out=%d think=%d usd=%.5f",
            ticker or "-", model, level, in_tok, out_tok, think_tok,
            _gemini_cost_usd(in_tok, out_tok, think_tok, model),
        )
    except Exception:
        logger.debug("GEMINICOST logging failed", exc_info=True)

    # With thinking on, skip any thought part and return the first answer text.
    parts = data["candidates"][0]["content"]["parts"]
    for part in parts:
        if part.get("text") and not part.get("thought"):
            return part["text"]
    return parts[0].get("text", "")
