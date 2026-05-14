"""Watcher engine: evaluates thesis pillars against live market data.

Each run:
  1. For every thesis, fetch quote + news concurrently (cheap, no LLM).
  2. For every pillar, ask a cheap LLM model to assess whether the
     threshold_break condition has been approached or triggered.
  3. If status changed, create an Alert and update the thesis in the store.
  4. Return a summary dict.

The watcher is intentionally a separate concern from the interactive
Analyst agent — different clock, different model, no conversation history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from agent.thesis_store import Thesis, list_theses, upsert_thesis
from agent.tools import get_recent_news, get_stock_quote
from watcher.alerts import Alert, append_alerts

log = logging.getLogger("watcher")

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PILLAR_EVAL_PROMPT = """\
You are a precise investment thesis monitor. Evaluate whether a thesis pillar
is still holding, wobbling, or broken based on current market data.

PILLAR
Name: {name}
Recorded value: {current_value}
Threshold break condition: {threshold_break}
Watch signals: {watch_signals}
Current status: {current_status}

CURRENT DATA FOR {ticker}
--- Quote ---
{quote_json}

--- Recent news (up to 10 headlines) ---
{news_json}

--- Web research ---
{web_json}

Based only on the data above, assess the pillar's status.

Respond with raw JSON only — no markdown fences:
{{"status": "intact"|"wobbling"|"broken", "current_value": "<updated one-line reading with source and date>", "reasoning": "<2-3 sentences citing specific data>", "confidence": <1-10>}}

Definitions:
- "intact"   : threshold condition clearly not triggered; pillar is holding.
- "wobbling" : threshold condition is being approached, or meaningful new risk has emerged.
- "broken"   : threshold condition appears to have been triggered.

Rules:
- current_value: write a fresh one-line factual reading using the data above, e.g.
  "Forward P/E ~23x (Yahoo Finance, 2026-05-14)". Include source and date.
  If you cannot extract a specific figure, copy the existing recorded value unchanged.
- If the available data is insufficient to judge status, preserve the current status "{current_status}".
- Cite specific figures from the data provided. Never invent numbers.
- Keep reasoning to 2-3 sentences maximum.
"""


# ---------------------------------------------------------------------------
# LLM helper (cheap model for watcher)
# ---------------------------------------------------------------------------

def _build_watcher_llm():
    provider = os.getenv("PROVIDER", "anthropic").lower()
    if provider == "gemini":
        # Use WATCHER_MODEL if set, else fall back to configured model
        model = os.getenv("WATCHER_MODEL") or os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        return ChatGoogleGenerativeAI(model=model, temperature=0)
    model = os.getenv("WATCHER_MODEL") or "claude-haiku-4-5-20251001"
    return ChatAnthropic(model=model, temperature=0)


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        inner = parts[1] if len(parts) > 1 else raw
        if inner.startswith("json"):
            inner = inner[4:]
        return inner.strip()
    return raw


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return ""


def _gemini_search(ticker: str) -> list[str]:
    if not os.getenv("GOOGLE_API_KEY"):
        return []
    try:
        llm = ChatGoogleGenerativeAI(
            model=os.getenv("WATCHER_MODEL") or os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
            temperature=0,
        )
        llm_s = llm.bind(tools=[{"google_search": {}}])
        query = (
            f"Latest analyst views and risks for {ticker} stock — "
            f"any earnings surprises, guidance changes, or competitive threats in the past week."
        )
        resp = llm_s.invoke([HumanMessage(content=query)])
        text = resp.content if isinstance(resp.content, str) else ""
        return [text] if text else []
    except Exception:
        log.debug("Gemini search skipped for %s", ticker)
        return []


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def _evaluate_pillar_sync(
    pillar,
    ticker: str,
    quote: dict,
    news: list,
    web_snippets: list,
) -> dict:
    """Synchronous LLM call — wraps into asyncio.to_thread at call site."""
    prompt = _PILLAR_EVAL_PROMPT.format(
        name=pillar.name,
        current_value=pillar.current_value,
        threshold_break=pillar.threshold_break,
        watch_signals=", ".join(pillar.watch_signals) if pillar.watch_signals else "none specified",
        current_status=pillar.status,
        ticker=ticker,
        quote_json=json.dumps(quote, indent=2),
        news_json=json.dumps(news, indent=2),
        web_json=json.dumps(web_snippets, indent=2),
    )
    llm = _build_watcher_llm()
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = _strip_fences(_extract_text(response.content))
    try:
        result = json.loads(raw)
        # Validate status field
        if result.get("status") not in ("intact", "wobbling", "broken"):
            result["status"] = pillar.status
        return result
    except json.JSONDecodeError:
        log.warning("Pillar eval returned non-JSON for %s/%s: %s", ticker, pillar.name, raw[:200])
        return {"status": pillar.status, "reasoning": "Parse error — status unchanged.", "confidence": 1}


async def check_thesis(thesis: Thesis) -> list[Alert]:
    """Evaluate all pillars for one thesis. Returns new alerts (status changes only)."""
    ticker = thesis.ticker

    # Fetch data concurrently
    quote, news = await asyncio.gather(
        asyncio.to_thread(get_stock_quote.func, ticker=ticker),
        asyncio.to_thread(get_recent_news.func, ticker=ticker, limit=10),
    )
    web = await asyncio.to_thread(_gemini_search, ticker)

    alerts: list[Alert] = []
    updated_pillars = []

    for pillar in thesis.pillars:
        result = await asyncio.to_thread(
            _evaluate_pillar_sync, pillar, ticker, quote, news, web
        )
        new_status = result.get("status", pillar.status)

        if new_status != pillar.status:
            alerts.append(Alert(
                ticker=ticker,
                pillar_name=pillar.name,
                old_status=pillar.status,
                new_status=new_status,
                reasoning=result.get("reasoning", ""),
                confidence=result.get("confidence", 5),
            ))
            log.info(
                "[watcher] %s / %s: %s → %s (conf %s)",
                ticker, pillar.name, pillar.status, new_status,
                result.get("confidence", "?"),
            )

        new_value = result.get("current_value", "").strip()
        update = {"status": new_status}
        if new_value and new_value != pillar.current_value:
            update["current_value"] = new_value
        updated_pillars.append(pillar.model_copy(update=update))

    # Persist updated pillars always (current_value refreshes even without status changes)
    value_refreshed = any(
        p_new.current_value != p_old.current_value
        for p_new, p_old in zip(updated_pillars, thesis.pillars)
    )
    if alerts:
        changes = ", ".join(
            f"{a.pillar_name}: {a.old_status}→{a.new_status}" for a in alerts
        )
        journal_entry = (
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}: "
            f"Watcher update — {changes}."
        )
        updated = thesis.model_copy(update={
            "pillars": updated_pillars,
            "journal": [*thesis.journal, journal_entry],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })
        upsert_thesis(updated)
    elif value_refreshed:
        # Status unchanged but at least one current_value was updated — persist quietly
        updated = thesis.model_copy(update={
            "pillars": updated_pillars,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })
        upsert_thesis(updated)
        log.info("[watcher] %s: pillar values refreshed, status unchanged", ticker)
    else:
        log.info("[watcher] %s: all pillars unchanged", ticker)

    return alerts


async def run_watcher() -> dict:
    """Run the watcher for every thesis. Returns a summary dict."""
    theses = list_theses()
    if not theses:
        log.info("[watcher] no theses to check")
        return {"checked": 0, "new_alerts": 0, "results": []}

    all_alerts: list[Alert] = []
    results = []

    for thesis in theses:
        try:
            alerts = await check_thesis(thesis)
            all_alerts.extend(alerts)
            results.append({"ticker": thesis.ticker, "alerts": len(alerts), "ok": True})
        except Exception as e:
            log.exception("[watcher] %s failed", thesis.ticker)
            results.append({"ticker": thesis.ticker, "ok": False, "error": str(e)})

    append_alerts(all_alerts)

    summary = {
        "checked": len(theses),
        "new_alerts": len(all_alerts),
        "results": results,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info("[watcher] done — %d theses, %d new alerts", len(theses), len(all_alerts))
    return summary
