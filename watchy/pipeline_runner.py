"""TradingAgents bridge — maps Watchy PipelineSpec to TradingAgentsGraph calls.

This is the real pipeline runner that replaces the stub in orchestrator.py.
Each signal type gets the appropriate subset of analysts, debate rounds, and
risk management depth as defined in the graduated analysis matrix.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watchy.orchestrator import AnalystSet, DebateMode, PipelineSpec, RiskMode
from watchy.token_tracker import TokenCostTracker

logger = logging.getLogger(__name__)

# Default report output directory (overridable via extra_config).
DEFAULT_REPORTS_DIR = os.path.expanduser("~/watchy/reports")


def create_tradingagents_runner(
    *,
    llm_provider: str = "deepseek",
    deep_think_llm: str = "deepseek-flash",
    quick_think_llm: str = "deepseek-flash",
    backend_url: str | None = None,
    **extra_config: Any,
):
    """Factory that returns a ``PipelineRunner`` wired to real TradingAgents.

    DeepSeek V4.1 Flash is the default for both roles. DeepSeek retired the old
    Flash model and is routing V4 Pro to V4.1 Flash while V4.1 Pro is pending.

    Args:
        llm_provider: One of openai, google, anthropic, deepseek, etc.
        deep_think_llm: Model for complex reasoning (Research Manager, PM).
        quick_think_llm: Model for analysts, debaters, and trader.
        backend_url: Optional custom API endpoint.
        **extra_config: Passed through to the TradingAgents config dict
            (e.g. max_debate_rounds, data_cache_dir, ...).

    Returns:
        A callable suitable for ``PipelineRunner``.
    """

    # Ensure DEEPSEEK_API_KEY is set before TradingAgents imports its clients.
    # The key lives in secrets.yaml alongside the Gemini key — one secrets file,
    # no systemd Environment= needed.
    if "DEEPSEEK_API_KEY" not in os.environ and extra_config.get("deepseek_api_key"):
        os.environ["DEEPSEEK_API_KEY"] = extra_config.pop("deepseek_api_key")

    # Defer import so the module is importable even without TradingAgents
    # installed (e.g. during linting or unit tests on a different machine).
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    def runner(ticker: str, spec: PipelineSpec) -> dict[str, Any]:
        """Execute the TradingAgents pipeline for *ticker* per *spec*.

        Creates a fresh graph instance per call. This is intentional: each
        signal type gets a different analyst subset and debate/risk depth.
        Watchy fires at most a handful of signals per hour so the compile
        overhead (~200 ms) is negligible.
        """
        # ---- 1. Map PipelineSpec → TradingAgents config ------------------
        selected_analysts = _map_analysts(spec.analysts)
        if not selected_analysts:
            logger.info("No analysts selected for %s — skipping TA call", ticker)
            return {
                "ticker": ticker,
                "analysts_run": [],
                "debate": spec.debate.value,
                "risk_mode": spec.risk.value,
                "recommendations": [],
                "risk_assessment": None,
                "summary": f"[SKIP] No analysts requested for {ticker}.",
            }

        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = llm_provider
        config["deep_think_llm"] = deep_think_llm
        config["quick_think_llm"] = quick_think_llm
        if backend_url:
            config["backend_url"] = backend_url
        config["max_debate_rounds"] = _map_debate(spec.debate)
        config["max_risk_discuss_rounds"] = _map_risk(spec.risk)
        config.update(extra_config)

        # ---- 2. Run the graph -------------------------------------------
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        logger.info(
            "Launching TA graph for %s: analysts=%s debate_rounds=%d risk_rounds=%d",
            ticker,
            selected_analysts,
            config["max_debate_rounds"],
            config["max_risk_discuss_rounds"],
        )

        tracker = TokenCostTracker()
        ta = TradingAgentsGraph(
            selected_analysts=selected_analysts,
            debug=False,
            config=config,
            callbacks=[tracker],
        )

        final_state, decision = ta.propagate(ticker, today)

        # Per-component token/cost breakdown (measurement only; one greppable
        # TOKENCOST line per run). Label encodes the analyst set + risk depth so
        # FULL/subset and weekly-full-risk runs can be told apart in the journal.
        tracker.log_summary(ticker, f"{'+'.join(selected_analysts)}|risk{config['max_risk_discuss_rounds']}")

        # ---- 3. Save reports as markdown --------------------------------
        report_path = _save_report(final_state, ticker, config)

        # ---- 4. Format result for Watchy consumers ----------------------
        result = _format_result(ticker, spec, selected_analysts, final_state, decision)
        if report_path:
            result["report_path"] = str(report_path)
        return result

    return runner


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

# TradingAgents uses "social" as the wire key for Sentiment analyst
# (legacy naming — the method is create_sentiment_analyst but the key
# and tool node are "social").
_ANALYST_MAP: dict[AnalystSet, list[str]] = {
    AnalystSet.NONE: [],
    AnalystSet.MARKET_ONLY: ["market"],
    AnalystSet.MARKET_SENTIMENT: ["market", "social"],
    AnalystSet.MARKET_SENTIMENT_NEWS: ["market", "social", "news"],
    AnalystSet.FULL: ["market", "social", "news", "fundamentals"],
}

# Human-readable names (Watchy-facing, matches orchestrator._analyst_names).
_ANALYST_LABELS: dict[str, str] = {
    "market": "market",
    "social": "sentiment",
    "news": "news",
    "fundamentals": "fundamentals",
}


def _map_analysts(analyst_set: AnalystSet) -> list[str]:
    return _ANALYST_MAP.get(analyst_set, ["market", "social"])


def _map_debate(debate: DebateMode) -> int:
    """Map debate mode to max_debate_rounds.

    0 rounds means the conditional logic immediately exits to Research
    Manager after the first Bull Researcher response — no back-and-forth.
    """
    return 1 if debate == DebateMode.BULL_BEAR else 0


def _map_risk(risk: RiskMode) -> int:
    """Map risk mode to max_risk_discuss_rounds.

    0 rounds skips the 3-way Aggressive/Conservative/Neutral debate;
    the Portfolio Manager still evaluates the trader proposal directly.
    """
    if risk == RiskMode.FULL:
        return 1
    return 0  # NONE or SIMPLIFIED


# ---------------------------------------------------------------------------
# Report saving (markdown, same layout as TradingAgents CLI)
# ---------------------------------------------------------------------------


def _save_report(
    final_state: dict[str, Any],
    ticker: str,
    config: dict[str, Any],
) -> Path | None:
    """Save a single consolidated analysis report as markdown.

    File name format: ``{ticker}_{datetime}.md``
    Saved to ``<reports_dir>/`` (default: ``~/watchy/reports/``).
    """
    reports_dir = config.get("reports_dir", DEFAULT_REPORTS_DIR)
    out_dir = Path(reports_dir)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create report dir %s: %s", out_dir, exc)
        return None

    sections: list[str] = []

    # --- 1. Analysts ---
    analyst_parts: list[tuple[str, str]] = []
    for key, name in [
        ("market_report", "Market Analyst"),
        ("sentiment_report", "Sentiment Analyst"),
        ("news_report", "News Analyst"),
        ("fundamentals_report", "Fundamentals Analyst"),
    ]:
        text = final_state.get(key)
        if text:
            analyst_parts.append((name, str(text)))
    if analyst_parts:
        sections.append(
            "## I. Analyst Team Reports\n\n"
            + "\n\n".join(f"### {n}\n{t}" for n, t in analyst_parts)
        )

    # --- 2. Research (Bull/Bear debate) ---
    debate = final_state.get("investment_debate_state", {})
    if debate:
        research_parts: list[tuple[str, str]] = []
        for key, name in [
            ("bull_history", "Bull Researcher"),
            ("bear_history", "Bear Researcher"),
            ("judge_decision", "Research Manager"),
        ]:
            text = debate.get(key)
            if text:
                research_parts.append((name, str(text)))
        if research_parts:
            sections.append(
                "## II. Research Team Decision\n\n"
                + "\n\n".join(f"### {n}\n{t}" for n, t in research_parts)
            )

    # --- 3. Trading ---
    trader_plan = final_state.get("trader_investment_plan")
    if trader_plan:
        sections.append(
            f"## III. Trading Team Plan\n\n### Trader\n{trader_plan}"
        )

    # --- 4. Risk Management ---
    risk = final_state.get("risk_debate_state", {})
    if risk:
        risk_parts: list[tuple[str, str]] = []
        for key, name in [
            ("aggressive_history", "Aggressive Analyst"),
            ("conservative_history", "Conservative Analyst"),
            ("neutral_history", "Neutral Analyst"),
        ]:
            text = risk.get(key)
            if text:
                risk_parts.append((name, str(text)))
        if risk_parts:
            sections.append(
                "## IV. Risk Management Team Decision\n\n"
                + "\n\n".join(f"### {n}\n{t}" for n, t in risk_parts)
            )

        # --- 5. Portfolio Manager ---
        judge = risk.get("judge_decision")
        if judge:
            sections.append(
                f"## V. Portfolio Manager Decision\n\n### Portfolio Manager\n{judge}"
            )

    # --- Consolidated report ---
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    filename = f"{ticker}_{timestamp}.md"
    report_path = out_dir / filename

    header = (
        f"# Trading Analysis Report: {ticker}\n\n"
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
    )
    report_path.write_text(header + "\n\n".join(sections), encoding="utf-8")

    logger.info("Report saved: %s", report_path)
    return report_path


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


def _strip_preamble(content: str) -> str:
    """Drop an analyst report's conversational lead-in before its markdown body.

    TA analyst outputs frequently begin with a chatty preamble — e.g.
    "Excellent, I now have comprehensive data. Let me compile the full report." —
    followed by a ``---`` rule and/or a ``#`` heading. Start the snippet at the
    first such structural marker (skipping a leading rule); if none appears in the
    first several lines, return the content stripped unchanged.
    """
    lines = content.lstrip().splitlines()
    for i, line in enumerate(lines[:8]):
        stripped = line.strip()
        if stripped.startswith("#"):
            return "\n".join(lines[i:]).lstrip()
        if stripped.startswith("---"):
            # Skip the rule itself; the real content follows it.
            return "\n".join(lines[i + 1:]).lstrip()
    return content.strip()


def _format_result(
    ticker: str,
    spec: PipelineSpec,
    selected_analysts: list[str],
    final_state: dict[str, Any],
    decision: Any,
) -> dict[str, Any]:
    """Extract the parts Watchy cares about from the TA graph output."""

    reports: dict[str, str | None] = {
        "market_report": final_state.get("market_report"),
        "sentiment_report": final_state.get("sentiment_report"),
        "news_report": final_state.get("news_report"),
        "fundamentals_report": final_state.get("fundamentals_report"),
    }

    # Collect non-empty analyst reports as recommendations. Strip the LLM's
    # conversational lead-in first (reports often open with "Excellent, I now
    # have comprehensive data. Let me compile the full report." before the actual
    # markdown) so the snippet starts at real content, not filler.
    recommendations: list[str] = []
    for key, label in [
        ("market_report", "Market"),
        ("sentiment_report", "Sentiment"),
        ("news_report", "News"),
        ("fundamentals_report", "Fundamentals"),
    ]:
        content = reports.get(key)
        if content:
            cleaned = _strip_preamble(content)
            short = cleaned if len(cleaned) <= 300 else cleaned[:297] + "..."
            recommendations.append(f"[{label}] {short}")

    # Extract final decision text.
    decision_text = ""
    if isinstance(decision, dict):
        decision_text = decision.get("decision", decision.get("summary", str(decision)))
    elif isinstance(decision, str):
        decision_text = decision

    # Extract risk assessment from risk debate state. This is the Portfolio
    # Manager's final call (rating + entry/stop/targets); the notification shows
    # it in full and chunks it if needed, so no truncation here.
    risk_assessment = None
    risk_state = final_state.get("risk_debate_state", {})
    if risk_state:
        judge = risk_state.get("judge_decision", "")
        if judge:
            risk_assessment = str(judge).strip()

    # Summary: combine trader plan + final decision.
    trader_plan = final_state.get("trader_investment_plan", "")
    final_decision = final_state.get("final_trade_decision", "")
    summary_parts = []
    if trader_plan:
        summary_parts.append(str(trader_plan)[:400])
    if final_decision:
        summary_parts.append(f"Decision: {final_decision}")
    if decision_text:
        summary_parts.append(str(decision_text)[:300])

    analysts_run = [_ANALYST_LABELS.get(a, a) for a in selected_analysts]

    # Structured one-word verdict (#3) — the headline BUY/SELL/HOLD, surfaced so
    # notifications can show it without the reader parsing the whole summary.
    # Graduated Tier 1 subsets may not produce a final_trade_decision, so fall
    # through the trader plan and decision text too (e.g. a trader plan that opens
    # with "**Action**: Sell"), not just the final decision.
    verdict = _extract_verdict(
        "\n".join(filter(None, [str(final_decision), str(trader_plan), decision_text]))
    )

    return {
        "ticker": ticker,
        "analysts_run": analysts_run,
        "analyst_count": len(analysts_run),
        "verdict": verdict,
        "debate": spec.debate.value,
        "risk_mode": spec.risk.value,
        "recommendations": recommendations,
        # The Trader's concrete plan (action + entry/stop/targets), shown in the
        # notification in full alongside the Risk / Final Call. Distinct from the
        # Portfolio Manager decision in risk_assessment.
        "trader_plan": str(trader_plan).strip() if trader_plan else "",
        "risk_assessment": risk_assessment,
        "summary": "\n\n".join(summary_parts) if summary_parts else "Analysis complete.",
        # Full reports for downstream consumers (advisor, notifications).
        "_reports": reports,
        "_decision_raw": decision_text,
    }


def _extract_verdict(text: str) -> str:
    """Pull a one-word BUY / SELL / HOLD verdict from the final decision text.

    Prefers TradingAgents' explicit "FINAL TRANSACTION PROPOSAL: **BUY**" marker;
    falls back to the first standalone BUY/SELL/HOLD keyword. Returns "" if none.
    """
    if not text:
        return ""
    m = re.search(
        r"FINAL TRANSACTION PROPOSAL:\s*\*{0,2}\s*(BUY|SELL|HOLD)", text, re.IGNORECASE
    )
    if m:
        return m.group(1).upper()
    # Structured "Action:" / "Recommendation:" line (graduated subsets use these).
    m = re.search(
        r"(?:Action|Recommendation)\s*\*{0,2}\s*:\s*\*{0,2}\s*(BUY|SELL|HOLD)",
        text, re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()
    m = re.search(r"\b(BUY|SELL|HOLD)\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else ""
