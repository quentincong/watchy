"""Watchy-side adjustments to the installed TradingAgents LLM wiring.

TradingAgents is vendored separately (``~/TradingAgents``) and never edited; the
two fixes below are applied at runtime by ``install()``, which the pipeline
runner calls once before it builds a graph.

1. Capability entry for the canonical ``deepseek-flash`` id. TA's capability
   table knows ``deepseek-v4-*`` (``_BY_ID`` and the ``^deepseek-v\\d`` pattern)
   but not the V4.1 canonical ids adopted on 2026-09-10, so they fell through to
   ``_DEFAULT`` — function calling WITH a forced ``tool_choice``. DeepSeek's
   thinking mode rejects that (``400 Thinking mode does not support this
   tool_choice``), so every structured node (Sentiment, RM, Trader, PM) silently
   fell back to free text. The rejection happens before generation, so nothing
   was billed twice; the structured path was simply off. Registering the ids as
   ``_DEEPSEEK_THINKING`` restores the pre-9/10 behaviour.

2. Per-role reasoning effort. TA builds the deep (Research Manager, Portfolio
   Manager) and quick clients through two ``create_llm_client`` calls with the
   same kwargs, and both roles use the same model id, so a call cannot be told
   apart by model. The runner therefore tags the deep id as ``<model>@<effort>``
   (e.g. ``deepseek-flash@max``); the wrapper strips the tag and passes
   ``reasoning_effort`` to that one client. TA reads ``deep_think_llm`` only at
   that call, so the tag never reaches the API.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

EFFORT_SEP = "@"
# DeepSeek V4.1 thinking accepts only these (high is the default).
DEEPSEEK_EFFORTS = ("high", "max")
_DEEPSEEK_CANONICAL_IDS = ("deepseek-flash", "deepseek-pro")


def tag_effort(model: str, effort: str | None) -> str:
    """``model`` with a reasoning-effort tag, or unchanged when effort is empty."""
    effort = (effort or "").strip().lower()
    return f"{model}{EFFORT_SEP}{effort}" if effort else model


def split_effort(model: str) -> tuple[str, str | None]:
    """Inverse of ``tag_effort``: ``("deepseek-flash", "max")`` or ``(model, None)``."""
    if EFFORT_SEP not in model:
        return model, None
    base, effort = model.rsplit(EFFORT_SEP, 1)
    return base, (effort.strip().lower() or None)


def register_deepseek_capabilities(caps_module: Any = None) -> list[str]:
    """Map the canonical V4.1 ids to TA's DeepSeek-thinking capabilities.

    Returns the ids added (already-known ids are left as TA defines them).
    """
    if caps_module is None:
        from tradingagents.llm_clients import capabilities as caps_module
    added = []
    for model_id in _DEEPSEEK_CANONICAL_IDS:
        if model_id not in caps_module._BY_ID:
            caps_module._BY_ID[model_id] = caps_module._DEEPSEEK_THINKING
            added.append(model_id)
    return added


def wrap_create_llm_client(original):
    """Return a ``create_llm_client`` that honours ``<model>@<effort>`` ids."""
    if getattr(original, "_watchy_shim", False):
        return original

    def create_llm_client(provider, model, base_url=None, **kwargs):
        base, effort = split_effort(model)
        if effort:
            kwargs["reasoning_effort"] = effort
        return original(provider, base, base_url, **kwargs)

    create_llm_client._watchy_shim = True
    create_llm_client.__wrapped__ = original
    return create_llm_client


def install() -> None:
    """Apply both adjustments to the installed TradingAgents (idempotent)."""
    import tradingagents.graph.trading_graph as tg

    added = register_deepseek_capabilities()
    if added:
        logger.info("TA capabilities: registered %s as DeepSeek thinking models", added)
    tg.create_llm_client = wrap_create_llm_client(tg.create_llm_client)
