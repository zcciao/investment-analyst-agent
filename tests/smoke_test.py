"""Structural smoke test — runs without an API key or network.

Verifies:
1. The Analyst graph compiles and exposes the expected nodes/edges.
2. The plan-builder + plan-reviewer graphs compile (LangGraph wiring is sane).
3. Tool schemas are well-formed (names unique, expected tools present).
4. The plan store loads at least one seeded plan.
5. yfinance is importable (we don't actually hit the network here).

This is intentionally NOT a behavioral test of the LLM — that requires
an API key and is out of scope for the PoC skeleton.

Run from the project root::

    python -m tests.smoke_test
"""

from __future__ import annotations

import sys


def check(label: str, cond: bool, detail: str = "") -> bool:
    mark = "OK  " if cond else "FAIL"
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")
    return cond


def main() -> int:
    ok = True

    # --- 1. Analyst graph compiles ---
    from agent.graph import build_graph

    graph = build_graph()
    nodes = set(graph.get_graph().nodes)
    ok &= check(
        "Analyst graph compiles with agent + tools nodes",
        {"agent", "tools"}.issubset(nodes),
        f"nodes={sorted(nodes)}",
    )

    # --- 2. plan-builder + plan-reviewer graphs compile ---
    from agent.plan_builder import build_plan_graph
    from agent.plan_reviewer import build_review_graph

    builder = build_plan_graph()
    builder_nodes = set(builder.get_graph().nodes)
    ok &= check(
        "Plan-builder graph compiles (4 nodes)",
        {"draft_node", "research_node", "refine_node", "done_node"}.issubset(builder_nodes),
        f"nodes={sorted(builder_nodes)}",
    )

    reviewer = build_review_graph()
    reviewer_nodes = set(reviewer.get_graph().nodes)
    ok &= check(
        "Plan-reviewer graph compiles (3 nodes)",
        {"gather_node", "analyze_node", "done_node"}.issubset(reviewer_nodes),
        f"nodes={sorted(reviewer_nodes)}",
    )

    # --- 3. tools well-formed ---
    from agent.tools import ALL_TOOLS

    names = [t.name for t in ALL_TOOLS]
    ok &= check("tool names are unique", len(names) == len(set(names)), str(names))
    ok &= check(
        "expected tools are registered",
        {
            "get_stock_quote",
            "get_company_info",
            "get_financials_snapshot",
            "get_recent_news",
            "list_plans",
            "get_plan",
        }.issubset(set(names)),
    )
    for t in ALL_TOOLS:
        ok &= check(
            f"  tool {t.name!r} has args_schema or no args",
            t.args_schema is not None or t.args == {},
        )

    # --- 4. plan store loads at least one plan ---
    from agent.plan_store import get_plan, list_plans

    plans = list_plans()
    ok &= check("at least one seeded plan", len(plans) >= 1, f"count={len(plans)}")
    if plans:
        first = plans[0]
        loaded = get_plan(first.ticker)
        ok &= check(
            f"plan {first.ticker!r} is parseable",
            loaded is not None and loaded.ticker == first.ticker,
        )
        ok &= check(
            f"plan {first.ticker!r} has reasons",
            loaded is not None and len(loaded.reasons) >= 1,
        )

    # --- 5. yfinance importable ---
    try:
        import yfinance  # noqa: F401

        ok &= check("yfinance imports", True)
    except Exception as e:  # noqa: BLE001
        ok &= check("yfinance imports", False, str(e))

    print()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
