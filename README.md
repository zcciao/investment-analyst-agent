# Investment Analyst — PoC

A small but production-shaped skeleton for the personal investment AI
described in `investment_ai_one_pager.md`. One agent (the **Analyst**),
two pluggable tool families (market data + thesis lookup), a FastAPI
server, and a single-file HTML chat UI with token streaming.

## What's here

```
.
├── agent/
│   ├── graph.py          # LangGraph: agent ↔ tools loop, with checkpointer
│   ├── tools.py          # @tool functions: quote / info / financials / news / theses
│   ├── thesis_store.py   # Pydantic Thesis model + JSON file store
│   └── prompts.py        # System prompts (shared ground rules + Analyst)
├── server/
│   └── app.py            # FastAPI; POST /chat streams via SSE
├── static/
│   └── index.html        # Minimal chat UI, no build step
├── data/
│   └── theses.json       # Seed thesis (GOOG) — edit freely
├── tests/
│   └── smoke_test.py     # No-network structural test
├── requirements.txt
├── .env.example
└── investment_ai_one_pager.md   # the strategic vision
```

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then put your ANTHROPIC_API_KEY in .env
python -m tests.smoke_test      # should print PASS
python -m server.app            # opens http://127.0.0.1:8000
```

Try these in the UI:
- "What positions am I tracking?"
- "What's up with GOOG?" — pulls quote + news, and surfaces your GOOG
  thesis pillars; flags wobbling ones.
- "Give me a fundamentals snapshot of MSFT"

The UI streams tokens, and renders each tool call/result inline so you
can see *why* the model said what it said — important when the
landmine is hallucinated numbers.

## How the graph works

```
START → agent ──tool_calls?── tools → agent → … → END
```

`agent/graph.py` defines:
- `MessagesState` — built-in state with a `messages` list reduced via
  `add_messages`. This is the conversation history.
- `agent` node — calls Claude with the system prompt + history + bound tools.
- `tools` node — `ToolNode(ALL_TOOLS)` dispatches the model's tool calls.
- `_should_continue` — routes to `tools` if the LLM emitted tool calls,
  otherwise to `END`.
- `MemorySaver` checkpointer — keyed by `thread_id`, so the UI's stable
  `thread_id` (stored in `localStorage`) keeps history across turns.

Swap `MemorySaver` for `SqliteSaver` / `PostgresSaver` to persist threads.

## Extending toward the four-agent vision

The one-pager describes Watcher / Analyst / Challenger / Coach. Here's
the minimum-rewrite path from this skeleton:

| Agent | How to add |
|---|---|
| **Watcher** | A scheduled script (cron, APScheduler, or LangGraph's `interrupt`) that calls a small model over each thesis's `watch_signals` and writes candidates to a queue. It's a *separate process*, not a node in this graph. |
| **Challenger** | Add a `challenger` node parallel to `agent`, with its own bear-case system prompt and a higher-quality model. Route to it from a behavior trigger ("user is adding to a position with wobbly pillars"). |
| **Coach** | Weekly job that reads journal entries + trades and writes a digest. Closer to a batch tool than an interactive agent. |

The thesis object in `agent/thesis_store.py` is the spine that all
four agents share — keep that schema stable as you add agents.

## Tools

Market data uses **yfinance** (no API key, somewhat flaky, fine for PoC):
- `get_stock_quote(ticker)` — last price, day range, 52w range, market cap
- `get_company_info(ticker)` — name, sector, industry, business summary
- `get_financials_snapshot(ticker)` — latest income statement + key ratios
- `get_recent_news(ticker, limit)` — headlines

Thesis tools read from `data/theses.json`:
- `list_theses()` — summary of every thesis on file
- `get_thesis(ticker)` — full thesis object including pillars and threshold_break

When you outgrow yfinance: drop in Polygon, FMP, or Alpha Vantage by
adding a new file in `agent/` that exposes the same `@tool` interface,
then add the tools to `ALL_TOOLS`.

## Streaming protocol

`POST /chat` returns Server-Sent Events:

| Event | Payload | Meaning |
|---|---|---|
| `token` | `{text}` | Assistant text delta |
| `tool_call` | `{name, args}` | Model decided to call a tool |
| `tool_result` | `{name, result}` | Tool returned (truncated to 4 KB) |
| `done` | `{}` | Turn complete |
| `error` | `{message}` | Something failed |

The UI uses `fetch()` + a streaming reader (rather than `EventSource`)
so it can POST a body. See `static/index.html` → `readSSE`.

## What's intentionally NOT here

- No real Watcher loop — that needs a scheduler and is out of scope for a skeleton.
- No auth — single-user, localhost only.
- No persistence beyond the JSON thesis file — `MemorySaver` is in-process.
- No bull/bear debate orchestration — that's the Challenger work.
- No tests beyond the structural smoke test — adding behavioral evals
  is the natural next step before any production push.

## Known landmines (from the one-pager, transferred into code)

- **Hallucinated numbers** → `prompts.py` instructs Claude to refuse if
  it doesn't have a tool result for a number. Verify this holds in
  your evals.
- **Cost runaway** → keep `temperature=0` and the tool list short. The
  Watcher (when added) must use a cheap model and pre-filter.
- **Privacy** → `data/theses.json` is gitignored under `*.local.json`;
  rename your real thesis file to `theses.local.json` once you stop
  wanting to commit it.
