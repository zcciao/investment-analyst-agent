"""Structural smoke test — runs without an Anthropic key or network.

Verifies:
1. The graph compiles and exposes the expected nodes/edges.
2. Tool schemas are well-formed (Pydantic accepts them, names are unique).
3. The thesis store loads the seed GOOG thesis.
4. yfinance is importable (we don't actually hit the network here).

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

    # --- 1. graph compiles ---
    from agent.graph import build_graph

    graph = build_graph()
    nodes = set(graph.get_graph().nodes)
    ok &= check(
        "graph compiles with agent + tools nodes",
        {"agent", "tools"}.issubset(nodes),
        f"nodes={sorted(nodes)}",
    )

    # --- 2. tools well-formed ---
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
            "list_theses",
            "get_thesis",
        }.issubset(set(names)),
    )
    for t in ALL_TOOLS:
        ok &= check(
            f"  tool {t.name!r} has args_schema or no args",
            t.args_schema is not None or t.args == {},
        )

    # --- 3. thesis store loads seed ---
    from agent.thesis_store import get_thesis, list_theses

    theses = list_theses()
    ok &= check("at least one seeded thesis", len(theses) >= 1, f"count={len(theses)}")
    goog = get_thesis("GOOG")
    ok &= check("GOOG thesis is parseable", goog is not None and goog.ticker == "GOOG")
    ok &= check(
        "GOOG thesis has pillars",
        goog is not None and len(goog.pillars) >= 1,
    )

    # --- 4. yfinance importable ---
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
