"""Investment-analyst agent package.

This package contains the LangGraph definition (`graph`), the tools the
agent can call (`tools`), the system prompts (`prompts`), and a tiny
file-backed thesis store (`thesis_store`).

For now there is one agent — the Analyst — which is the interactive
"talk to me about my portfolio" agent. The four-agent vision from the
one-pager (Watcher / Analyst / Challenger / Coach) plugs in by adding
new graph nodes and routing between them; see README for guidance.
"""

from agent.graph import build_graph, get_graph

__all__ = ["build_graph", "get_graph"]
