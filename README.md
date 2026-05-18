# Investment Analyst — PoC

A small but production-shaped skeleton for the personal investment AI described in [`investment_ai_one_pager.md`](./investment_ai_one_pager.md). Four agents share the work; one FastAPI server stitches them together; one HTML file is the UI.

If any of the vocabulary below looks unfamiliar (plan, reason, walk-away signal, on_track / at_risk / off_track / ahead), see [`GLOSSARY.md`](./GLOSSARY.md).

---

## What's here

```
.
├── agent/
│   ├── graph.py            # LangGraph: Analyst chat loop (agent ↔ tools)
│   ├── plan_builder.py     # LangGraph: build a Plan from raw notes (draft→research→refine→done)
│   ├── plan_reviewer.py    # LangGraph: back-check a Plan against history (gather→analyze→done)
│   ├── plan_store.py       # Pydantic Plan/Reason/Event models + JSON store + migration
│   ├── prompts.py          # System prompts: Analyst, plan-builder, plan-reviewer
│   └── tools.py            # @tool functions: yfinance quote/info/financials/news/history + plan lookup
├── watcher/
│   ├── engine.py           # Per-reason status checks against current data + recent news
│   ├── alerts.py           # Append-only alert log (data/alerts.json)
│   └── runs.py             # Per-run summary log (data/watcher_runs.json)
├── server/
│   └── app.py              # FastAPI: SSE streaming for chat, plan-build, plan-review, watcher
├── static/
│   └── index.html          # Mobile-first single-file UI: Briefing / Plans / Ask / You
├── data/
│   ├── plans.json          # Plans keyed by ticker
│   ├── alerts.json         # Watcher alerts (status changes)
│   └── watcher_runs.json   # Watcher run history
├── tests/
│   └── smoke_test.py       # No-network structural test
├── design/                 # UI mockups (history; not used by code)
├── requirements.txt
├── GLOSSARY.md             # Vocabulary + old↔new term map
└── investment_ai_one_pager.md
```

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set ANTHROPIC_API_KEY or GOOGLE_API_KEY
python -m tests.smoke_test      # should print PASS
python -m server.app            # opens http://127.0.0.1:8000
```

Pick your provider via `PROVIDER=anthropic` (default) or `PROVIDER=gemini` in `.env`.

Try in the UI:
- **Briefing** — daily digest; tap any alert card to drill in
- **Plans** — list of every plan; filter chips for issues / at risk / events / ahead
- **Plan detail** — price chart, reasons with status, timeline, and the action row (Review / Refresh checks / Ask / Edit)
- **Ask** — chat with the Analyst; try *"What's up with GOOG?"* or *"Bear case on my at-risk positions"*
- **You** → **Glossary** — in-app version of `GLOSSARY.md`

Tokens stream live, and every tool call / result is rendered inline so you can see *why* the model said what it said — important when the failure mode is hallucinated numbers.

---

## The four agents

| Agent | Lives in | When it runs | What it does |
|---|---|---|---|
| 🔭 **Watcher** | `watcher/engine.py` | Daily cron (set `WATCHER_HOUR`) + on-demand via UI | Forward-looking. Evaluates each reason's status against current data. Emits alerts on status changes. |
| 🔬 **Analyst** | `agent/graph.py` | Per chat turn | Conversational. Pulls live data, explains plan state, nudges revision on `off_track` / `ahead`. The only agent with thread state. |
| 🩺 **Reviewer** | `agent/plan_reviewer.py` | On-demand (Plan detail → 🔍 *Review this plan*) | Backward-looking. Pulls price history + fundamentals, grades reasons (`held_up / failed / set_too_low / set_too_high / not_enough_data`), proposes structured updates the user can apply. |
| 🛡️ **Supervisor** | _not yet implemented_ | Per Analyst output | Quality gate before any agent-proposed change touches the Plan store. Today the user is effectively the supervisor for Reviewer-proposed updates. |

A new plan is built via a 4th LangGraph in `agent/plan_builder.py` (paste notes → AI extracts reasons → you accept/edit).

---

## API surface

| Route | What it does |
|---|---|
| `GET /` | Static UI |
| `GET /health` | Provider + model |
| `POST /chat` | Analyst chat (SSE: `token` / `tool_call` / `tool_result` / `done` / `error`) |
| `POST /plan/build` | Plan-builder SSE (`step_start` / `step_done` / `plan_ready` / `done`) |
| `POST /plan/{ticker}/review` | Reviewer SSE (`step_start` / `step_done` / `review_ready` / `done`) |
| `GET  /plan/{ticker}` | Price history + recent news |
| `GET  /plans` | All plans + quotes + P&L |
| `POST /plan/save` | Upsert a plan |
| `POST /watcher/run` | Run watcher across all plans |
| `POST /watcher/run/{ticker}` | Run watcher for one plan |
| `GET  /watcher/alerts` | Alert log (`?unread_only=true` to filter) |
| `POST /watcher/alerts/read` | Mark alerts read |
| `GET  /watcher/runs` | Recent run summaries |
| `GET  /watcher/status` | Last run summary |

---

## Data model

`agent/plan_store.py` is the spine — every agent reads from or writes through it. The Pydantic models match `data/plans.json` 1:1 (see `GLOSSARY.md` for field-by-field meaning).

On startup, `_migrate_if_needed()` rewrites legacy `data/theses.json` (and old field names like `pillars` / `threshold_break` / `intact`) into the current schema. Idempotent; safe on every boot.

## Streaming protocol

All long-running endpoints stream Server-Sent Events:

| Event | Payload | Used by |
|---|---|---|
| `token` | `{text}` | `/chat` |
| `tool_call` | `{name, args}` | `/chat` |
| `tool_result` | `{name, result}` | `/chat` |
| `step_start` | `{step}` | `/plan/build`, `/plan/{t}/review` |
| `step_done` | `{step, data}` | `/plan/build`, `/plan/{t}/review` |
| `plan_ready` | `{plan}` | `/plan/build` |
| `review_ready` | `{ticker, plan, review}` | `/plan/{t}/review` |
| `done` | `{}` | all |
| `error` | `{message}` | all |

The UI uses `fetch()` + a streaming reader (so it can POST a body). See `static/index.html` → `readSSE`.

---

## What's intentionally NOT here

- **No Supervisor loop yet** — Watcher status changes commit directly. The plan is to wire Analyst-proposed changes through a Supervisor quality gate before they touch the Plan store.
- **No push channel** — Trigger/Push to email or native push is the natural follow-up to Supervisor.
- **No auth** — single-user, localhost / LAN only.
- **No real database** — JSON files are fine at PoC scale. Replace once schema stabilizes.
- **No behavioral evals** — only the structural smoke test (`tests/smoke_test.py`). Behavioral evals are the right thing to add before any production push.

## Landmines (from the one-pager, transferred into code)

- **Hallucinated numbers** → `agent/prompts.py` instructs the LLM to refuse if it doesn't have a tool result for a number. Verify this in evals.
- **Cost runaway** → watcher uses `WATCHER_MODEL` (cheap by default) and pre-filters before LLM calls. Reviewer is on-demand only.
- **Push fatigue** → not yet relevant; will be once `Trigger/Push` ships. Bar for interrupting > bar for committing to the Plan store.
- **Privacy** → `data/*.json` is your real portfolio. Keep `data/` out of any public repo before productizing.
