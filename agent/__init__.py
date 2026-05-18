"""Investment-analyst agent package.

This package contains the LangGraph definition for the interactive
**Analyst** (`graph`), the tools the agent can call (`tools`), the
system prompts (`prompts`), a tiny file-backed plan store
(`plan_store`), and the on-demand Reviewer + plan-build graphs
(`plan_reviewer`, `plan_builder`).

The four-agent architecture from the one-pager:

- **Watcher** (`watcher.engine`) — continuous reason-status checks
- **Analyst** (`agent.graph`) — interactive chat; the only agent with a
  conversational thread
- **Reviewer** (`agent.plan_reviewer`) — on-demand retrospective grading
- **Supervisor** — quality gate; intended as a conditional edge between
  Analyst-proposed plan updates and the Plan store. Not yet implemented.

See `GLOSSARY.md` and `investment_ai_one_pager.md` for the design.
"""

from agent.graph import build_graph, get_graph

__all__ = ["build_graph", "get_graph"]
