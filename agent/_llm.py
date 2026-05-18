"""Shared LLM helpers — used by plan_builder, plan_reviewer, and watcher.

These four functions used to be copied across three modules. Centralising
them here keeps provider-switching, fence-stripping, and Gemini-grounded
web search logic in one place. The leading underscore in the module name
signals "internal to the agent package".
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI

log = logging.getLogger(__name__)

Role = Literal["plain", "watcher"]


def build_llm(role: Role = "plain"):
    """Build an LLM for the configured ``PROVIDER`` env var.

    ``role`` picks the model tier:

    - ``"plain"``    → strong model (Sonnet / Gemini 2.0-flash).
                       For Analyst, plan-builder, Reviewer.
    - ``"watcher"``  → cheap model (Haiku / Gemini 2.0-flash, with the
                       ``WATCHER_MODEL`` env var taking precedence).

    No tools bound. Callers wanting tool-binding should do it themselves.
    """
    provider = os.getenv("PROVIDER", "anthropic").lower()
    if provider == "gemini":
        if role == "watcher":
            model = os.getenv("WATCHER_MODEL") or os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        else:
            model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        return ChatGoogleGenerativeAI(model=model, temperature=0)
    if role == "watcher":
        model = os.getenv("WATCHER_MODEL") or "claude-haiku-4-5-20251001"
    else:
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    return ChatAnthropic(model=model, temperature=0)


def strip_fences(raw: str) -> str:
    """Remove markdown code fences some LLMs add despite instructions."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        inner = parts[1] if len(parts) > 1 else raw
        if inner.startswith("json"):
            inner = inner[4:]
        return inner.strip()
    return raw


def extract_text(content) -> str:
    """Anthropic content can be a string or a list of blocks. Normalize."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else b
            for b in content
        )
    return ""


def gemini_search(ticker: str, role: Role = "plain") -> list[str]:
    """Use Gemini's built-in Google Search grounding for recent context.

    Returns an empty list if ``GOOGLE_API_KEY`` is absent or the call fails —
    callers should treat web research as best-effort, not load-bearing.

    ``role="watcher"`` prefers the ``WATCHER_MODEL`` override for cost.
    """
    if not os.getenv("GOOGLE_API_KEY"):
        return []
    try:
        gemini_default = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        model = (
            os.getenv("WATCHER_MODEL") or gemini_default
            if role == "watcher"
            else gemini_default
        )
        llm = ChatGoogleGenerativeAI(model=model, temperature=0)
        llm_with_search = llm.bind(tools=[{"google_search": {}}])
        query = (
            f"Latest analyst views and risks for {ticker} stock — "
            f"any earnings surprises, guidance changes, or competitive threats in the past week. "
            f"Be factual and concise."
        )
        response = llm_with_search.invoke([HumanMessage(content=query)])
        text = response.content if isinstance(response.content, str) else ""
        return [text] if text else []
    except Exception:
        log.debug("Gemini search skipped for %s", ticker, exc_info=True)
        return []
