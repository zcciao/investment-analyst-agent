"""LangGraph definition for the Analyst agent.

The graph is intentionally minimal: a single ReAct-style loop with
Anthropic + tool calling. The shape is set up so that the eventual
Watcher / Challenger / Coach agents from the one-pager can be added as
sibling nodes with a router on top, without rewriting this file.

Layout::

    ┌──────────┐  tool calls?   ┌────────┐
    │  agent   │ ───── yes ────►│ tools  │
    └────┬─────┘ ◄──────────────┴────────┘
         │ no
         ▼
        END

`MessagesState` is the prebuilt state class — its `messages` field is
appended-to (reducer = `add_messages`), which is exactly what we want
for chat history.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from agent.prompts import ANALYST_SYSTEM_PROMPT
from agent.tools import ALL_TOOLS

# In-process checkpointer so each session keeps its own thread of messages.
# Replace with `SqliteSaver` or `PostgresSaver` for persistence.
_CHECKPOINTER = MemorySaver()


def _build_llm():
    provider = os.getenv("PROVIDER", "gemini").lower()
    # temperature=0 keeps numeric reasoning stable; bump for brainstorming nodes.
    if provider == "gemini":
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        return ChatGoogleGenerativeAI(model=model, temperature=0).bind_tools(ALL_TOOLS)
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    return ChatAnthropic(model=model, temperature=0).bind_tools(ALL_TOOLS)


def _agent_node(state: MessagesState) -> dict:
    """Call the LLM with the current message history."""
    llm = _build_llm()
    # Prepend the system prompt every call. Cheaper than persisting it in state.
    messages = [SystemMessage(content=ANALYST_SYSTEM_PROMPT), *state["messages"]]
    response = llm.invoke(messages)
    return {"messages": [response]}


def _should_continue(state: MessagesState) -> Literal["tools", "__end__"]:
    """Route: if the last AI message asked for tools, run them; else stop."""
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    return END


def build_graph():
    """Build a fresh compiled graph. Use `get_graph()` for the cached one."""
    g = StateGraph(MessagesState)
    g.add_node("agent", _agent_node)
    g.add_node("tools", ToolNode(ALL_TOOLS))

    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", _should_continue, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")

    return g.compile(checkpointer=_CHECKPOINTER)


@lru_cache(maxsize=1)
def get_graph():
    """Return a process-wide singleton of the compiled graph."""
    return build_graph()
