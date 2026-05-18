"""Linear 3-node LangGraph for plan back-check / review.

Graph layout:
    START → gather_node → analyze_node → done_node → END

gather_node    — pull historical price, current quote, financials, news,
                 watcher alerts for this plan. Concurrent fetch.
analyze_node   — single strong-model LLM call that produces a structured
                 review report (narrative + per-reason verdicts +
                 proposed plan updates).
done_node      — finalize: defaults missing fields, marks complete.

Mirrors the shape of agent/plan_builder.py so the same SSE pattern in
server.app applies. No checkpointer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from agent._llm import build_llm, extract_text, strip_fences
from agent.prompts import PLAN_REVIEW_PROMPT
from agent.plan_store import Plan, get_plan
from agent.tools import (
    get_financials_snapshot,
    get_price_history,
    get_recent_news,
    get_stock_quote,
)
from watcher.alerts import list_alerts

log = logging.getLogger("plan_reviewer")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class PlanReviewState(TypedDict):
    ticker: str
    plan: dict
    # gathered
    quote: dict
    history: dict
    financials: dict
    news: list
    alerts: list
    # output
    review: dict
    error: str | None
    steps_completed: list


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

async def gather_node(state: PlanReviewState) -> dict:
    """Pull all the data needed to back-check the plan."""
    ticker = state["ticker"]
    plan = state["plan"]
    period = _pick_period(plan.get("entry_date"))

    quote, history, fins, news = await asyncio.gather(
        asyncio.to_thread(get_stock_quote.func, ticker=ticker),
        asyncio.to_thread(get_price_history.func, ticker=ticker, period=period),
        asyncio.to_thread(get_financials_snapshot.func, ticker=ticker),
        asyncio.to_thread(get_recent_news.func, ticker=ticker, limit=15),
    )
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


def analyze_node(state: PlanReviewState) -> dict:
    """LLM produces the review report."""
    plan = state["plan"]
    prompt = PLAN_REVIEW_PROMPT.format(
        plan_json=json.dumps(plan, indent=2, default=str),
        history_json=json.dumps(state["history"], indent=2, default=str),
        quote_json=json.dumps(state["quote"], indent=2, default=str),
        financials_json=json.dumps(state["financials"], indent=2, default=str),
        news_json=json.dumps(state["news"], indent=2, default=str),
        alerts_json=json.dumps(state["alerts"], indent=2, default=str),
        journal_json=json.dumps(plan.get("journal", []), indent=2, default=str),
    )
    llm = build_llm("plain")
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = strip_fences(extract_text(response.content))
    try:
        review = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("analyze_node: JSON parse failed: %s\nraw=%s", e, raw[:500])
        return {
            "error": f"Reviewer returned non-JSON: {raw[:200]}",
            "steps_completed": state["steps_completed"] + ["analyze"],
        }
    log.info(
        "analyze_node: rec=%s reason_reviews=%d changed=%s",
        review.get("recommendation"),
        len(review.get("reason_reviews", []) or []),
        review.get("proposed_changes", {}).get("changed_fields"),
    )
    return {
        "review": review,
        "steps_completed": state["steps_completed"] + ["analyze"],
    }


def done_node(state: PlanReviewState) -> dict:
    """Finalize: default any missing fields so the frontend can render."""
    v = state.get("review") or {}
    v.setdefault("narrative", "")
    v.setdefault("performance_summary", {})
    v.setdefault("reason_reviews", [])
    v.setdefault("lessons", [])
    v.setdefault("proposed_changes", {"changed_fields": []})
    v["proposed_changes"].setdefault("changed_fields", [])
    v.setdefault("recommendation", "watch")
    v.setdefault("confidence", 5)
    return {
        "review": v,
        "steps_completed": state["steps_completed"] + ["done"],
    }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def _continue_or_abort(state: PlanReviewState) -> str:
    return "__end__" if state.get("error") else "done_node"


def build_review_graph():
    g = StateGraph(PlanReviewState)
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
def get_review_graph():
    return build_review_graph()


# ---------------------------------------------------------------------------
# Convenience: initial-state builder
# ---------------------------------------------------------------------------

def initial_state_for(ticker: str) -> dict | None:
    """Build the initial graph state. Returns None if plan doesn't exist."""
    t = get_plan(ticker)
    if t is None:
        return None
    return {
        "ticker": ticker.upper(),
        "plan": t.model_dump(),
        "quote": {}, "history": {}, "financials": {}, "news": [], "alerts": [],
        "review": {},
        "error": None,
        "steps_completed": [],
    }
