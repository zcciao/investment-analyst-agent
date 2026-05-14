"""Linear 4-node LangGraph for guided thesis creation.

Graph layout (no loops):
    START → draft_node →(abort on error)→ research_node → refine_node → done_node → END

Each node writes to a different slice of ThesisBuildState.
No checkpointer — each /thesis/build call is a single ephemeral run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from agent.prompts import THESIS_DRAFT_PROMPT, THESIS_REFINE_PROMPT
from agent.thesis_store import Thesis
from agent.tools import (
    get_company_info,
    get_financials_snapshot,
    get_recent_news,
    get_stock_quote,
)

log = logging.getLogger("thesis_builder")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class ThesisBuildState(TypedDict):
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
    # Step 3+4: final thesis
    thesis: dict
    # Control
    error: str | None
    steps_completed: list


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _build_plain_llm():
    """Build LLM without tools — for structured JSON output."""
    provider = os.getenv("PROVIDER", "anthropic").lower()
    if provider == "gemini":
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        return ChatGoogleGenerativeAI(model=model, temperature=0)
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    return ChatAnthropic(model=model, temperature=0)


def _gemini_search(ticker: str) -> list[str]:
    """Use Gemini's built-in Google Search grounding to fetch web context.

    Returns empty list if GOOGLE_API_KEY is absent or the call fails.
    """
    if not os.getenv("GOOGLE_API_KEY"):
        return []
    try:
        llm = ChatGoogleGenerativeAI(
            model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
            temperature=0,
        )
        llm_with_search = llm.bind(tools=[{"google_search": {}}])
        query = (
            f"Summarise recent analyst sentiment, earnings outlook, and key risks "
            f"for {ticker} stock. Focus on AI-driven demand trends and memory chip "
            f"market dynamics. Be factual and concise."
        )
        response = llm_with_search.invoke([HumanMessage(content=query)])
        text = response.content if isinstance(response.content, str) else ""
        return [text] if text else []
    except Exception:
        log.exception("Gemini search failed — skipping web snippets")
        return []


def _strip_fences(raw: str) -> str:
    """Remove markdown code fences that some LLMs add despite instructions."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        # parts[1] is the fenced block (may start with "json\n")
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
            b.get("text", "") if isinstance(b, dict) else b
            for b in content
        )
    return ""


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def draft_node(state: ThesisBuildState) -> dict:
    """Step 1: LLM produces a skeleton thesis from user notes (no live data)."""
    now = datetime.now(timezone.utc)
    prompt = THESIS_DRAFT_PROMPT.format(
        ticker=state["ticker"],
        purchase_price=state["purchase_price"],
        current_note=state["current_note"],
        today=now.strftime("%Y-%m-%d"),
        now=now.isoformat(),
    )
    llm = _build_plain_llm()
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = _strip_fences(_extract_text(response.content))
    try:
        draft = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("draft_node: JSON parse failed: %s\nraw=%s", e, raw[:500])
        return {
            "error": f"Draft LLM returned non-JSON: {raw[:200]}",
            "steps_completed": state["steps_completed"] + ["draft"],
        }
    log.info("draft_node: %d pillars drafted", len(draft.get("pillars", [])))
    return {"draft": draft, "steps_completed": state["steps_completed"] + ["draft"]}


async def research_node(state: ThesisBuildState) -> dict:
    """Step 2: Fetch live market data concurrently + optional Gemini web search."""
    ticker = state["ticker"]
    quote, company, fins, news = await asyncio.gather(
        asyncio.to_thread(get_stock_quote.func, ticker=ticker),
        asyncio.to_thread(get_company_info.func, ticker=ticker),
        asyncio.to_thread(get_financials_snapshot.func, ticker=ticker),
        asyncio.to_thread(get_recent_news.func, ticker=ticker, limit=10),
    )
    snippets = await asyncio.to_thread(_gemini_search, ticker)
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


def refine_node(state: ThesisBuildState) -> dict:
    """Step 3: LLM fills all blanks in the draft using the research bundle."""
    prompt = THESIS_REFINE_PROMPT.format(
        draft_json=json.dumps(state["draft"], indent=2),
        quote_json=json.dumps(state["quote"], indent=2),
        company_json=json.dumps(state["company_info"], indent=2),
        financials_json=json.dumps(state["financials"], indent=2),
        news_json=json.dumps(state["news"], indent=2),
        web_json=json.dumps(state["web_snippets"], indent=2),
    )
    llm = _build_plain_llm()
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = _strip_fences(_extract_text(response.content))
    try:
        thesis = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("refine_node: JSON parse failed: %s\nraw=%s", e, raw[:500])
        # Fall back to the draft so the flow doesn't completely fail
        thesis = state["draft"]
    filled = sum(1 for p in thesis.get("pillars", []) if p.get("current_value"))
    log.info("refine_node: %d/%d pillars filled", filled, len(thesis.get("pillars", [])))
    return {"thesis": thesis, "steps_completed": state["steps_completed"] + ["refine"]}


def done_node(state: ThesisBuildState) -> dict:
    """Step 4: Pydantic-validate the refined thesis. Best-effort — never aborts."""
    try:
        validated = Thesis(**state["thesis"])
        thesis = validated.model_dump()
    except ValidationError as e:
        log.warning("done_node: validation warning (passing through): %s", e)
        thesis = state["thesis"]
    return {"thesis": thesis, "steps_completed": state["steps_completed"] + ["done"]}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _continue_or_abort(state: ThesisBuildState) -> Literal["research_node", "__end__"]:
    return "__end__" if state.get("error") else "research_node"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_thesis_graph():
    g = StateGraph(ThesisBuildState)
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

    return g.compile()  # no checkpointer — ephemeral runs only


@lru_cache(maxsize=1)
def get_thesis_build_graph():
    return build_thesis_graph()
