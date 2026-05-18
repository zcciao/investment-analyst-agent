"""Linear 4-node LangGraph for guided plan creation.

Graph layout (no loops):
    START → draft_node →(abort on error)→ research_node → refine_node → done_node → END

Each node writes to a different slice of PlanBuildState.
No checkpointer — each /plan/build call is a single ephemeral run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from agent._llm import build_llm, extract_text, gemini_search, strip_fences
from agent.prompts import PLAN_DRAFT_PROMPT, PLAN_REFINE_PROMPT
from agent.plan_store import Plan
from agent.tools import (
    get_company_info,
    get_financials_snapshot,
    get_recent_news,
    get_stock_quote,
)

log = logging.getLogger("plan_builder")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class PlanBuildState(TypedDict):
    # Inputs (set at entry, never mutated)
    ticker: str
    purchase_price: float
    current_note: str
    # Step 1: draft skeleton
    draft: dict
    # Step 2: research bundle
    quote: dict
    company_info: dict
    financials: dict
    news: list
    web_snippets: list
    # Step 3+4: final plan
    plan: dict
    # Control
    error: str | None
    steps_completed: list


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def draft_node(state: PlanBuildState) -> dict:
    """Step 1: LLM produces a skeleton plan from user notes (no live data)."""
    now = datetime.now(timezone.utc)
    prompt = PLAN_DRAFT_PROMPT.format(
        ticker=state["ticker"],
        purchase_price=state["purchase_price"],
        current_note=state["current_note"],
        today=now.strftime("%Y-%m-%d"),
        now=now.isoformat(),
    )
    llm = build_llm("plain")
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = strip_fences(extract_text(response.content))
    try:
        draft = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("draft_node: JSON parse failed: %s\nraw=%s", e, raw[:500])
        return {
            "error": f"Draft LLM returned non-JSON: {raw[:200]}",
            "steps_completed": state["steps_completed"] + ["draft"],
        }
    log.info("draft_node: %d reasons drafted", len(draft.get("reasons", [])))
    return {"draft": draft, "steps_completed": state["steps_completed"] + ["draft"]}


async def research_node(state: PlanBuildState) -> dict:
    """Step 2: Fetch live market data concurrently + optional Gemini web search."""
    ticker = state["ticker"]
    quote, company, fins, news = await asyncio.gather(
        asyncio.to_thread(get_stock_quote.func, ticker=ticker),
        asyncio.to_thread(get_company_info.func, ticker=ticker),
        asyncio.to_thread(get_financials_snapshot.func, ticker=ticker),
        asyncio.to_thread(get_recent_news.func, ticker=ticker, limit=10),
    )
    snippets = await asyncio.to_thread(gemini_search, ticker, "plain")
    log.info(
        "research_node: price=%s has_web=%s",
        quote.get("last_price"), bool(snippets),
    )
    return {
        "quote": quote,
        "company_info": company,
        "financials": fins,
        "news": news,
        "web_snippets": snippets,
        "steps_completed": state["steps_completed"] + ["research"],
    }


def refine_node(state: PlanBuildState) -> dict:
    """Step 3: LLM fills all blanks in the draft using the research bundle."""
    prompt = PLAN_REFINE_PROMPT.format(
        draft_json=json.dumps(state["draft"], indent=2),
        quote_json=json.dumps(state["quote"], indent=2),
        company_json=json.dumps(state["company_info"], indent=2),
        financials_json=json.dumps(state["financials"], indent=2),
        news_json=json.dumps(state["news"], indent=2),
        web_json=json.dumps(state["web_snippets"], indent=2),
    )
    llm = build_llm("plain")
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = strip_fences(extract_text(response.content))
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("refine_node: JSON parse failed: %s\nraw=%s", e, raw[:500])
        # Fall back to the draft so the flow doesn't completely fail
        plan = state["draft"]
    filled = sum(1 for r in plan.get("reasons", []) if r.get("current_value"))
    log.info("refine_node: %d/%d reasons filled", filled, len(plan.get("reasons", [])))
    return {"plan": plan, "steps_completed": state["steps_completed"] + ["refine"]}


def done_node(state: PlanBuildState) -> dict:
    """Step 4: Pydantic-validate the refined plan. Best-effort — never aborts."""
    try:
        validated = Plan(**state["plan"])
        plan = validated.model_dump()
    except ValidationError as e:
        log.warning("done_node: validation warning (passing through): %s", e)
        plan = state["plan"]
    return {"plan": plan, "steps_completed": state["steps_completed"] + ["done"]}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _continue_or_abort(state: PlanBuildState) -> Literal["research_node", "__end__"]:
    return "__end__" if state.get("error") else "research_node"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_plan_graph():
    g = StateGraph(PlanBuildState)
    g.add_node("draft_node", draft_node)
    g.add_node("research_node", research_node)
    g.add_node("refine_node", refine_node)
    g.add_node("done_node", done_node)

    g.add_edge(START, "draft_node")
    g.add_conditional_edges(
        "draft_node",
        _continue_or_abort,
        {"research_node": "research_node", END: END},
    )
    g.add_edge("research_node", "refine_node")
    g.add_edge("refine_node", "done_node")
    g.add_edge("done_node", END)

    return g.compile()


@lru_cache(maxsize=1)
def get_plan_build_graph():
    return build_plan_graph()
