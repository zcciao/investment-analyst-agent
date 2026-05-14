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

from agent.thesis_builder import get_thesis_build_graph
from agent.thesis_store import Thesis, list_theses, upsert_thesis
from pydantic import ValidationError as PydanticValidationError
from watcher.alerts import list_alerts, mark_read, unread_count
from watcher.engine import run_watcher

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


class ThesisBuildRequest(BaseModel):
    ticker: str
    purchase_price: float
    current_note: str


class ThesisSaveRequest(BaseModel):
    thesis: dict


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


@app.post("/thesis/build")
async def thesis_build(req: ThesisBuildRequest):
    return EventSourceResponse(_stream_thesis_build(req), sep="\n")


@app.post("/thesis/save")
async def thesis_save(req: ThesisSaveRequest) -> dict:
    try:
        t = Thesis(**req.thesis)
    except PydanticValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    upsert_thesis(t)
    log.info("[thesis saved] %s", t.ticker)
    return {"ok": True, "ticker": t.ticker}


@app.get("/thesis/dashboard")
async def thesis_dashboard() -> list[dict]:
    """Return all theses enriched with live quotes and P&L performance."""
    from agent.tools import get_stock_quote
    theses = list_theses()
    if not theses:
        return []
    quotes = await asyncio.gather(*[
        asyncio.to_thread(get_stock_quote.func, ticker=t.ticker)
        for t in theses
    ])
    result = []
    for t, q in zip(theses, quotes):
        current = q.get("last_price")
        entry = t.entry_price
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
            "thesis": t.model_dump(),
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
# Thesis builder streaming
# --------------------------------------------------------------------------- #

_THESIS_NODE_NAMES = {"draft_node", "research_node", "refine_node", "done_node"}
_THESIS_NODE_STEP = {n: n.replace("_node", "") for n in _THESIS_NODE_NAMES}


async def _stream_thesis_build(req: ThesisBuildRequest) -> AsyncIterator[dict]:
    initial: dict = {
        "ticker": req.ticker.upper(),
        "purchase_price": req.purchase_price,
        "current_note": req.current_note,
        "draft": {}, "quote": {}, "company_info": {},
        "financials": {}, "news": [], "web_snippets": [],
        "thesis": {}, "error": None, "steps_completed": [],
    }
    graph = get_thesis_build_graph()
    log.info("[thesis build] ticker=%s price=%s", initial["ticker"], req.purchase_price)
    try:
        async for event in graph.astream_events(initial, version="v2"):
            ev_kind = event["event"]
            ev_name = event.get("name", "")
            if ev_name not in _THESIS_NODE_NAMES:
                continue

            step = _THESIS_NODE_STEP[ev_name]

            if ev_kind == "on_chain_start":
                yield _sse("step_start", {"step": step})

            elif ev_kind == "on_chain_end":
                output = event.get("data", {}).get("output", {}) or {}
                if output.get("error"):
                    yield _sse("error", {"message": output["error"]})
                    yield _sse("done", {})
                    return

                yield _sse("step_done", {"step": step, "data": _wizard_summary(output, step)})

                if step == "done" and output.get("thesis"):
                    yield _sse("thesis_ready", {"thesis": output["thesis"]})

        yield _sse("done", {})

    except Exception as e:
        log.exception("thesis build stream failed")
        yield _sse("error", {"message": str(e)})
        yield _sse("done", {})


def _wizard_summary(output: dict, step: str) -> dict:
    """Return a small UI-friendly payload for each step — avoid large blobs."""
    if step == "draft":
        return {"pillar_count": len(output.get("draft", {}).get("pillars", []))}
    if step == "research":
        price = output.get("quote", {}).get("last_price")
        return {"price": price, "has_web": bool(output.get("web_snippets"))}
    if step == "refine":
        pillars = output.get("thesis", {}).get("pillars", [])
        filled = sum(1 for p in pillars if p.get("current_value"))
        return {"pillars_filled": filled, "total": len(pillars)}
    return {}


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
