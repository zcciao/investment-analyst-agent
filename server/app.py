"""FastAPI server that serves the chat UI and streams agent responses.

Endpoints:
- GET  /                    → static index.html
- POST /chat                → SSE stream of agent events for one user turn
- GET  /health              → liveness

Streaming protocol (SSE event types):
- `token`     : a chunk of assistant text. data = {"text": "..."}
- `tool_call` : the model decided to call a tool. data = {"name": "...", "args": {...}}
- `tool_result`: a tool returned. data = {"name": "...", "result": ...}
- `done`      : the turn is complete. data = {}
- `error`     : something blew up. data = {"message": "..."}

Threads / sessions:
- The client picks a `thread_id` (any string; the UI uses a uuid in localStorage).
- LangGraph's MemorySaver keys conversation state by that thread_id, so messages
  accumulate within one session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent.graph import get_graph
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agent.plan_builder import get_plan_build_graph
from agent.plan_reviewer import get_review_graph, initial_state_for
from agent.plan_store import Plan, get_plan, list_plans, upsert_plan
from agent.tools import get_price_history, get_recent_news, get_stock_quote
from pydantic import ValidationError as PydanticValidationError
from watcher.alerts import list_alerts, mark_read, unread_count
from watcher.engine import run_watcher, run_watcher_for_ticker
from watcher.runs import last_run, list_runs

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("server")

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"

_scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    watcher_hour = int(os.getenv("WATCHER_HOUR", "7"))
    _scheduler.add_job(
        run_watcher,
        CronTrigger(hour=watcher_hour, minute=0),
        id="daily_watcher",
        replace_existing=True,
    )
    _scheduler.start()
    log.info("[scheduler] watcher scheduled daily at %02d:00", watcher_hour)
    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(title="Investment Analyst PoC", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class ChatRequest(BaseModel):
    thread_id: str
    message: str


class PlanBuildRequest(BaseModel):
    ticker: str
    purchase_price: float
    current_note: str


class PlanSaveRequest(BaseModel):
    plan: dict


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    provider = os.getenv("PROVIDER", "gemini").lower()
    if provider == "gemini":
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    else:
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    return {"ok": True, "provider": provider, "model": model}


@app.post("/chat")
async def chat(req: ChatRequest):
    provider = os.getenv("PROVIDER", "anthropic").lower()
    if provider == "gemini":
        if not os.getenv("GOOGLE_API_KEY"):
            raise HTTPException(status_code=400, detail="GOOGLE_API_KEY is not set.")
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY is not set.")
    return EventSourceResponse(_stream_turn(req), sep="\n")


@app.post("/plan/build")
async def plan_build(req: PlanBuildRequest):
    return EventSourceResponse(_stream_plan_build(req), sep="\n")


@app.post("/plan/{ticker}/review")
async def plan_review(ticker: str):
    """Stream a back-check / review report for an existing plan."""
    return EventSourceResponse(_stream_plan_review(ticker), sep="\n")


@app.post("/plan/save")
async def plan_save(req: PlanSaveRequest) -> dict:
    try:
        p = Plan(**req.plan)
    except PydanticValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    upsert_plan(p)
    log.info("[plan saved] %s", p.ticker)
    return {"ok": True, "ticker": p.ticker}


@app.get("/plan/{ticker}")
async def plan_detail(ticker: str, period: str = "1y", news_limit: int = 15) -> dict:
    """Lightweight read for the per-plan detail screen: price history + news.

    Picks a sensible default period based on the plan's entry_date if unset.
    """
    ticker = ticker.upper()
    p = get_plan(ticker)
    if p is None:
        raise HTTPException(status_code=404, detail=f"No plan for {ticker}")

    if period == "1y" and p.entry_date:
        try:
            entry = datetime.fromisoformat(p.entry_date.replace("Z", "+00:00")).date()
            days = (datetime.now(timezone.utc).date() - entry).days
            period = (
                "3mo" if days <= 30
                else "6mo" if days <= 90
                else "1y"  if days <= 365
                else "2y"  if days <= 730
                else "5y"
            )
        except Exception:
            pass

    history, news = await asyncio.gather(
        asyncio.to_thread(get_price_history.func, ticker=ticker, period=period),
        asyncio.to_thread(get_recent_news.func, ticker=ticker, limit=news_limit),
    )
    return {"ticker": ticker, "history": history, "news": news}


@app.get("/plans")
async def plans_dashboard() -> list[dict]:
    """Return all plans enriched with live quotes and P&L performance."""
    plans = list_plans()
    if not plans:
        return []
    quotes = await asyncio.gather(*[
        asyncio.to_thread(get_stock_quote.func, ticker=p.ticker)
        for p in plans
    ])
    result = []
    for p, q in zip(plans, quotes):
        current = q.get("last_price")
        entry = p.entry_price
        perf = None
        if entry and current:
            gain_abs = round(current - entry, 2)
            gain_pct = round((gain_abs / entry) * 100, 1)
            perf = {
                "entry_price": entry,
                "current_price": current,
                "gain_abs": gain_abs,
                "gain_pct": gain_pct,
            }
        result.append({
            "plan": p.model_dump(),
            "quote": q,
            "performance": perf,
        })
    return result


# --------------------------------------------------------------------------- #
# Watcher
# --------------------------------------------------------------------------- #

@app.post("/watcher/run")
async def watcher_run() -> dict:
    """Trigger a watcher run immediately. Returns a summary."""
    return await run_watcher()


@app.post("/watcher/run/{ticker}")
async def watcher_run_one(ticker: str) -> dict:
    """Trigger a watcher run for a single plan."""
    return await run_watcher_for_ticker(ticker)


@app.get("/watcher/alerts")
async def watcher_alerts(unread_only: bool = False) -> list[dict]:
    return [a.model_dump() for a in list_alerts(unread_only=unread_only)]


@app.get("/watcher/alerts/count")
async def watcher_alerts_count() -> dict:
    return {"unread": unread_count()}


class MarkReadRequest(BaseModel):
    ids: list[str] | None = None  # None = mark all


@app.post("/watcher/alerts/read")
async def watcher_mark_read(req: MarkReadRequest) -> dict:
    count = mark_read(req.ids)
    return {"marked_read": count}


@app.get("/watcher/runs")
async def watcher_runs(limit: int = 20) -> list[dict]:
    return [r.model_dump() for r in list_runs(limit=limit)]


@app.get("/watcher/status")
async def watcher_status() -> dict:
    r = last_run()
    return {"last_run": r.model_dump() if r else None}


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #

def _sse(event: str, data: dict) -> dict:
    """Format an SSE event for sse-starlette."""
    return {"event": event, "data": json.dumps(data)}


async def _stream_turn(req: ChatRequest) -> AsyncIterator[dict]:
    """Run one user turn through the graph and stream events to the client."""
    graph = get_graph()
    config = {"configurable": {"thread_id": req.thread_id}}
    inputs = {"messages": [HumanMessage(content=req.message)]}

    log.info("[turn start] thread=%s message=%r", req.thread_id, req.message)
    try:
        full_response: list[str] = []
        # `astream` with stream_mode="messages" gives us per-token AIMessageChunks
        # from the agent node, plus full ToolMessage objects from the tool node.
        async for chunk, metadata in graph.astream(
            inputs, config=config, stream_mode="messages"
        ):
            node = metadata.get("langgraph_node")
            log.debug("[chunk] node=%s type=%s content=%r", node, type(chunk).__name__, getattr(chunk, "content", None))

            if isinstance(chunk, AIMessageChunk):
                # Text deltas
                if chunk.content:
                    text = _extract_text(chunk.content)
                    if text:
                        full_response.append(text)
                        yield _sse("token", {"text": text})
                # Tool calls — Anthropic streams these as chunks; only emit when
                # we see the full call (chunk.tool_calls becomes non-empty on
                # the final assembly chunk).
                for tc in getattr(chunk, "tool_calls", []) or []:
                    if tc.get("name"):
                        log.info("[tool_call] %s args=%r", tc["name"], tc.get("args", {}))
                        yield _sse(
                            "tool_call",
                            {"name": tc["name"], "args": tc.get("args", {})},
                        )

            elif isinstance(chunk, ToolMessage):
                log.info("[tool_result] %s → %s", chunk.name, _truncate(chunk.content, 200))
                yield _sse(
                    "tool_result",
                    {"name": chunk.name, "result": _truncate(chunk.content)},
                )

        assembled = "".join(full_response)
        log.info("[turn done] response (%d chars): %s", len(assembled), assembled[:500] + ("…" if len(assembled) > 500 else ""))
        yield _sse("done", {})

    except Exception as e:  # noqa: BLE001
        log.exception("stream failed")
        yield _sse("error", {"message": str(e)})


def _extract_text(content) -> str:
    """Anthropic content can be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
            elif isinstance(block, str):
                out.append(block)
        return "".join(out)
    return ""


def _truncate(s, limit: int = 4000) -> str:
    """Keep tool-result payloads small enough for the wire."""
    s = s if isinstance(s, str) else json.dumps(s, default=str)
    return s if len(s) <= limit else s[:limit] + f"… [truncated, {len(s)} chars]"


# --------------------------------------------------------------------------- #
# Generic graph-streaming helper
# --------------------------------------------------------------------------- #

async def _stream_graph(
    graph,
    initial_state: dict,
    *,
    node_names: set[str],
    summary_fn,
    ready_event: tuple[str, str] | None = None,
    ready_payload_fn=None,
) -> AsyncIterator[dict]:
    """Stream a LangGraph as SSE events.

    Emits ``step_start`` / ``step_done`` for every node in ``node_names``.
    On ``error`` in any node's output, emits ``error`` + ``done`` and stops.
    On the final ``done`` step, optionally emits a custom *ready* event:

        ready_event       = (event_name, output_key)
                            e.g. ("plan_ready", "plan")
        ready_payload_fn  = (output, initial_state) -> payload dict
                            defaults to {output_key: output[output_key]}

    Step names are derived from node names by stripping the ``_node`` suffix
    (so ``draft_node`` becomes step ``"draft"``).
    """
    step_of = {n: n.replace("_node", "") for n in node_names}
    try:
        async for event in graph.astream_events(initial_state, version="v2"):
            ev_name = event.get("name", "")
            if ev_name not in node_names:
                continue
            step = step_of[ev_name]
            ev_kind = event["event"]

            if ev_kind == "on_chain_start":
                yield _sse("step_start", {"step": step})

            elif ev_kind == "on_chain_end":
                output = event.get("data", {}).get("output", {}) or {}
                if output.get("error"):
                    yield _sse("error", {"message": output["error"]})
                    yield _sse("done", {})
                    return

                yield _sse("step_done", {"step": step, "data": summary_fn(output, step)})

                if (
                    ready_event
                    and step == "done"
                    and output.get(ready_event[1])
                ):
                    name, key = ready_event
                    payload = (
                        ready_payload_fn(output, initial_state)
                        if ready_payload_fn
                        else {key: output[key]}
                    )
                    yield _sse(name, payload)

        yield _sse("done", {})

    except Exception as e:
        log.exception("graph stream failed")
        yield _sse("error", {"message": str(e)})
        yield _sse("done", {})


# --------------------------------------------------------------------------- #
# Plan builder streaming
# --------------------------------------------------------------------------- #

_PLAN_NODE_NAMES = {"draft_node", "research_node", "refine_node", "done_node"}


def _wizard_summary(output: dict, step: str) -> dict:
    """Per-step payload for the build wizard's progress strip."""
    if step == "draft":
        return {"reason_count": len(output.get("draft", {}).get("reasons", []))}
    if step == "research":
        price = output.get("quote", {}).get("last_price")
        return {"price": price, "has_web": bool(output.get("web_snippets"))}
    if step == "refine":
        reasons = output.get("plan", {}).get("reasons", [])
        filled = sum(1 for r in reasons if r.get("current_value"))
        return {"reasons_filled": filled, "total": len(reasons)}
    return {}


async def _stream_plan_build(req: PlanBuildRequest) -> AsyncIterator[dict]:
    initial: dict = {
        "ticker": req.ticker.upper(),
        "purchase_price": req.purchase_price,
        "current_note": req.current_note,
        "draft": {}, "quote": {}, "company_info": {},
        "financials": {}, "news": [], "web_snippets": [],
        "plan": {}, "error": None, "steps_completed": [],
    }
    log.info("[plan build] ticker=%s price=%s", initial["ticker"], req.purchase_price)
    async for ev in _stream_graph(
        get_plan_build_graph(),
        initial,
        node_names=_PLAN_NODE_NAMES,
        summary_fn=_wizard_summary,
        ready_event=("plan_ready", "plan"),
    ):
        yield ev


# --------------------------------------------------------------------------- #
# Plan reviewer streaming
# --------------------------------------------------------------------------- #

_REVIEW_NODE_NAMES = {"gather_node", "analyze_node", "done_node"}


def _review_summary(output: dict, step: str) -> dict:
    """Per-step payload for the review report's progress strip."""
    if step == "gather":
        hist = output.get("history") or {}
        return {
            "period": hist.get("period"),
            "history_points": len(hist.get("closes") or []),
            "alerts": len(output.get("alerts") or []),
        }
    if step == "analyze":
        v = output.get("review") or {}
        return {
            "recommendation": v.get("recommendation"),
            "reason_reviews": len(v.get("reason_reviews") or []),
        }
    return {}


async def _stream_plan_review(ticker: str) -> AsyncIterator[dict]:
    state = initial_state_for(ticker)
    if state is None:
        yield _sse("error", {"message": f"No plan on file for {ticker.upper()}."})
        yield _sse("done", {})
        return

    log.info("[plan review] ticker=%s", ticker.upper())
    async for ev in _stream_graph(
        get_review_graph(),
        state,
        node_names=_REVIEW_NODE_NAMES,
        summary_fn=_review_summary,
        ready_event=("review_ready", "review"),
        ready_payload_fn=lambda out, st: {
            "ticker": st["ticker"],
            "plan": st["plan"],
            "review": out["review"],
        },
    ):
        yield ev


# --------------------------------------------------------------------------- #
# Entrypoint: `python -m server.app`
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server.app:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
