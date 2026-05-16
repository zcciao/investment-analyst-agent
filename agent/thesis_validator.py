"""Linear 3-node LangGraph for thesis back-check / validation.

Graph layout:
    START → gather_node → analyze_node → done_node → END

gather_node    — pull historical price, current quote, financials, news,
                 watcher alerts for this thesis. Concurrent fetch.
analyze_node   — single strong-model LLM call that produces a structured
                 validation report (narrative + per-pillar verdicts +
                 proposed thesis updates).
done_node      — finalize: defaults missing fields, marks complete.

Mirrors the shape of agent/thesis_builder.py so the same SSE pattern in
server.app applies. No checkpointer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph

from agent.prompts import THESIS_VALIDATE_PROMPT
from agent.thesis_store import Thesis, get_thesis
from agent.tools import (
    get_financials_snapshot,
    get_price_history,
    get_recent_news,
    get_stock_quote,
)
from watcher.alerts import list_alerts

log = logging.getLogger("thesis_validator")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class ThesisValidateState(TypedDict):
    ticker: str
    thesis: dict
    # gathered
    quote: dict
    history: dict
    financials: dict
    news: list
    alerts: list
    # output
    validation: dict
    error: str | None
    steps_completed: list


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def _build_plain_llm():
    provider = os.getenv("PROVIDER", "anthropic").lower()
    if provider == "gemini":
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        return ChatGoogleGenerativeAI(model=model, temperature=0)
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    return ChatAnthropic(model=model, temperature=0)


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        inner = parts[1] if len(parts) > 1 else raw
        if inner.startswith("json"):
            inner = inner[4:]
        return inner.strip()
    return raw


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return ""


def _pick_period(entry_date: str | None) -> str:
    """Choose a yfinance period covering the holding window with some margin."""
    if not entry_date:
        return "1y"
    try:
        entry = datetime.fromisoformat(entry_date.replace("Z", "+00:00")).date()
    except Exception:
        return "1y"
    days = (datetime.now(timezone.utc).date() - entry).days
    if days <= 30:   return "3mo"
    if days <= 90:   return "6mo"
    if days <= 365:  return "1y"
    if days <= 730:  return "2y"
    return "5y"


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def gather_node(state: ThesisValidateState) -> dict:
    """Pull all the data needed to back-check the thesis."""
    ticker = state["ticker"]
    thesis = state["thesis"]
    period = _pick_period(thesis.get("entry_date"))

    quote, history, fins, news = await asyncio.gather(
        asyncio.to_thread(get_stock_quote.func, ticker=ticker),
        asyncio.to_thread(get_price_history.func, ticker=ticker, period=period),
        asyncio.to_thread(get_financials_snapshot.func, ticker=ticker),
        asyncio.to_thread(get_recent_news.func, ticker=ticker, limit=15),
    )
    # Alerts are local — no need for to_thread but keep symmetry cheap.
    raw_alerts = list_alerts(unread_only=False)
    alerts = [a.model_dump() for a in raw_alerts if a.ticker == ticker][:20]

    log.info(
        "gather_node: %s history_period=%s closes=%d alerts=%d",
        ticker, period, len(history.get("closes", []) or []), len(alerts),
    )
    return {
        "quote": quote,
        "history": history,
        "financials": fins,
        "news": news,
        "alerts": alerts,
        "steps_completed": state["steps_completed"] + ["gather"],
    }


def analyze_node(state: ThesisValidateState) -> dict:
    """LLM produces the validation report."""
    thesis = state["thesis"]
    prompt = THESIS_VALIDATE_PROMPT.format(
        thesis_json=json.dumps(thesis, indent=2, default=str),
        history_json=json.dumps(state["history"], indent=2, default=str),
        quote_json=json.dumps(state["quote"], indent=2, default=str),
        financials_json=json.dumps(state["financials"], indent=2, default=str),
        news_json=json.dumps(state["news"], indent=2, default=str),
        alerts_json=json.dumps(state["alerts"], indent=2, default=str),
        journal_json=json.dumps(thesis.get("journal", []), indent=2, default=str),
    )
    llm = _build_plain_llm()
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = _strip_fences(_extract_text(response.content))
    try:
        validation = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("analyze_node: JSON parse failed: %s\nraw=%s", e, raw[:500])
        return {
            "error": f"Validator returned non-JSON: {raw[:200]}",
            "steps_completed": state["steps_completed"] + ["analyze"],
        }
    log.info(
        "analyze_node: rec=%s pillar_reviews=%d changed=%s",
        validation.get("recommendation"),
        len(validation.get("pillar_reviews", []) or []),
        validation.get("proposed_changes", {}).get("changed_fields"),
    )
    return {
        "validation": validation,
        "steps_completed": state["steps_completed"] + ["analyze"],
    }


def done_node(state: ThesisValidateState) -> dict:
    """Finalize: default any missing fields so the frontend can render."""
    v = state.get("validation") or {}
    # Soft defaults — never abort on missing fields
    v.setdefault("narrative", "")
    v.setdefault("performance_summary", {})
    v.setdefault("pillar_reviews", [])
    v.setdefault("lessons", [])
    v.setdefault("proposed_changes", {"changed_fields": []})
    v["proposed_changes"].setdefault("changed_fields", [])
    v.setdefault("recommendation", "watch")
    v.setdefault("confidence", 5)
    return {
        "validation": v,
        "steps_completed": state["steps_completed"] + ["done"],
    }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def _continue_or_abort(state: ThesisValidateState) -> str:
    return "__end__" if state.get("error") else "done_node"


def build_validate_graph():
    g = StateGraph(ThesisValidateState)
    g.add_node("gather_node", gather_node)
    g.add_node("analyze_node", analyze_node)
    g.add_node("done_node", done_node)

    g.add_edge(START, "gather_node")
    g.add_edge("gather_node", "analyze_node")
    g.add_conditional_edges(
        "analyze_node",
        _continue_or_abort,
        {"done_node": "done_node", END: END},
    )
    g.add_edge("done_node", END)
    return g.compile()


@lru_cache(maxsize=1)
def get_validate_graph():
    return build_validate_graph()


# ---------------------------------------------------------------------------
# Convenience: initial-state builder
# ---------------------------------------------------------------------------

def initial_state_for(ticker: str) -> dict | None:
    """Build the initial graph state. Returns None if thesis doesn't exist."""
    t = get_thesis(ticker)
    if t is None:
        return None
    return {
        "ticker": ticker.upper(),
        "thesis": t.model_dump(),
        "quote": {}, "history": {}, "financials": {}, "news": [], "alerts": [],
        "validation": {},
        "error": None,
        "steps_completed": [],
    }
