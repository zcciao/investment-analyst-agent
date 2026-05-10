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

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("server")

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"

app = FastAPI(title="Investment Analyst PoC")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class ChatRequest(BaseModel):
    thread_id: str
    message: str


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
