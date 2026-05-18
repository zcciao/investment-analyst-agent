"""Watcher engine: evaluates plan reasons against live market data.

Each run:
  1. For every plan, fetch quote + news concurrently (cheap, no LLM).
  2. For every reason, ask a cheap LLM to assess whether the
     walk_away_signal has been approached or triggered.
  3. If status changed, create an Alert and update the plan in the store.
  4. Return a summary dict.

The watcher is intentionally a separate concern from the interactive
Analyst agent — different clock, different model, no conversation history.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage

from agent._llm import build_llm, extract_text, gemini_search, strip_fences
from agent.plan_store import Plan, get_plan, list_plans, upsert_plan
from agent.tools import get_recent_news, get_stock_quote
from watcher.alerts import Alert, append_alerts
from watcher.runs import append_run

log = logging.getLogger("watcher")

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_REASON_EVAL_PROMPT = """\
You are a precise investment plan monitor. Evaluate the reason's status —
on_track, at_risk, off_track, or ahead — based on current market data.

The walk_away_signal describes the FAILURE condition (the bad direction).
Status moves AWAY from that direction = good. Status moves TOWARD it = bad.

REASON
Name: {name}
Recorded value: {current_value}
Walk-away signal: {walk_away_signal}
Data sources: {data_sources}
Current status: {current_status}

CURRENT DATA FOR {ticker}
--- Quote ---
{quote_json}

--- Recent news (up to 10 headlines) ---
{news_json}

--- Web research ---
{web_json}

Based only on the data above, assess the reason's status.

Respond with raw JSON only — no markdown fences:
{{"status": "on_track"|"at_risk"|"off_track"|"ahead", "current_value": "<updated one-line reading with source and date>", "reasoning": "<2-3 sentences citing specific data>", "confidence": <1-10>}}

Definitions:
- "on_track"  : within normal range; the walk-away signal is not at risk in either direction.
- "at_risk"   : the walk-away signal is being approached, or meaningful new risk has emerged.
- "off_track" : the walk-away signal appears to have been TRIGGERED — i.e., reality moved in the bad direction past the line.
- "ahead"     : reality is dramatically BETTER than expected — well clear of the signal in the favorable direction, with a material improvement vs. the recorded value. Reserved for genuine upside surprises, not routine performance.

Rules:
- "off_track" means adverse breach only. Numbers exceeding the bullish expectation are NEVER "off_track" — they are "ahead" or remain "on_track".
- "ahead" requires both: (a) the metric is far from the failure condition, AND (b) a meaningful improvement over the recorded value. Don't use it for normal-range readings.
- current_value: write a fresh one-line factual reading using the data above, e.g.
  "Forward P/E ~23x (Yahoo Finance, 2026-05-14)". Include source and date.
  If you cannot extract a specific figure, copy the existing recorded value unchanged.
- If the available data is insufficient to judge status, preserve the current status "{current_status}".
- Cite specific figures from the data provided. Never invent numbers.
- Keep reasoning to 2-3 sentences maximum.
"""


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

_VALID_STATUSES = ("on_track", "at_risk", "off_track", "ahead")


def _evaluate_reason_sync(
    reason,
    ticker: str,
    quote: dict,
    news: list,
    web_snippets: list,
) -> dict:
    """Synchronous LLM call — wraps into asyncio.to_thread at call site."""
    prompt = _REASON_EVAL_PROMPT.format(
        name=reason.name,
        current_value=reason.current_value,
        walk_away_signal=reason.walk_away_signal,
        data_sources=", ".join(reason.data_sources) if reason.data_sources else "none specified",
        current_status=reason.status,
        ticker=ticker,
        quote_json=json.dumps(quote, indent=2),
        news_json=json.dumps(news, indent=2),
        web_json=json.dumps(web_snippets, indent=2),
    )
    llm = build_llm("watcher")
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = strip_fences(extract_text(response.content))
    try:
        result = json.loads(raw)
        if result.get("status") not in _VALID_STATUSES:
            result["status"] = reason.status
        return result
    except json.JSONDecodeError:
        log.warning("Reason eval returned non-JSON for %s/%s: %s", ticker, reason.name, raw[:200])
        return {"status": reason.status, "reasoning": "Parse error — status unchanged.", "confidence": 1}


async def check_plan(plan: Plan) -> list[Alert]:
    """Evaluate all reasons for one plan. Returns new alerts (status changes only)."""
    ticker = plan.ticker

    quote, news = await asyncio.gather(
        asyncio.to_thread(get_stock_quote.func, ticker=ticker),
        asyncio.to_thread(get_recent_news.func, ticker=ticker, limit=10),
    )
    web = await asyncio.to_thread(gemini_search, ticker, "watcher")

    alerts: list[Alert] = []
    updated_reasons = []

    for reason in plan.reasons:
        result = await asyncio.to_thread(
            _evaluate_reason_sync, reason, ticker, quote, news, web
        )
        new_status = result.get("status", reason.status)

        if new_status != reason.status:
            alerts.append(Alert(
                ticker=ticker,
                reason_name=reason.name,
                old_status=reason.status,
                new_status=new_status,
                reasoning=result.get("reasoning", ""),
                confidence=result.get("confidence", 5),
            ))
            log.info(
                "[watcher] %s / %s: %s → %s (conf %s)",
                ticker, reason.name, reason.status, new_status,
                result.get("confidence", "?"),
            )

        new_value = result.get("current_value", "").strip()
        update = {"status": new_status}
        if new_value and new_value != reason.current_value:
            update["current_value"] = new_value
        updated_reasons.append(reason.model_copy(update=update))

    # Persist updated reasons always (current_value refreshes even without status changes)
    value_refreshed = any(
        r_new.current_value != r_old.current_value
        for r_new, r_old in zip(updated_reasons, plan.reasons)
    )
    if alerts:
        changes = ", ".join(
            f"{a.reason_name}: {a.old_status}→{a.new_status}" for a in alerts
        )
        journal_entry = (
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}: "
            f"Watcher update — {changes}."
        )
        updated = plan.model_copy(update={
            "reasons": updated_reasons,
            "journal": [*plan.journal, journal_entry],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })
        upsert_plan(updated)
    elif value_refreshed:
        updated = plan.model_copy(update={
            "reasons": updated_reasons,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })
        upsert_plan(updated)
        log.info("[watcher] %s: reason values refreshed, status unchanged", ticker)
    else:
        log.info("[watcher] %s: all reasons unchanged", ticker)

    return alerts


async def run_watcher_for_ticker(ticker: str) -> dict:
    """Run the watcher for a single plan. Returns a summary dict."""
    ticker = ticker.upper()
    plan = get_plan(ticker)
    if plan is None:
        return {"checked": 0, "new_alerts": 0, "results": [], "error": f"No plan for {ticker}"}

    try:
        alerts = await check_plan(plan)
        result = {"ticker": ticker, "alerts": len(alerts), "ok": True}
    except Exception as e:
        log.exception("[watcher] %s failed", ticker)
        alerts = []
        result = {"ticker": ticker, "ok": False, "error": str(e)}

    append_alerts(alerts)

    summary = {
        "checked": 1,
        "new_alerts": len(alerts),
        "results": [result],
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    append_run(summary)
    log.info("[watcher] %s done — %d new alerts", ticker, len(alerts))
    return summary


async def run_watcher() -> dict:
    """Run the watcher for every plan. Returns a summary dict."""
    plans = list_plans()
    if not plans:
        log.info("[watcher] no plans to check")
        return {"checked": 0, "new_alerts": 0, "results": []}

    all_alerts: list[Alert] = []
    results = []

    for plan in plans:
        try:
            alerts = await check_plan(plan)
            all_alerts.extend(alerts)
            results.append({"ticker": plan.ticker, "alerts": len(alerts), "ok": True})
        except Exception as e:
            log.exception("[watcher] %s failed", plan.ticker)
            results.append({"ticker": plan.ticker, "ok": False, "error": str(e)})

    append_alerts(all_alerts)

    summary = {
        "checked": len(plans),
        "new_alerts": len(all_alerts),
        "results": results,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    append_run(summary)
    log.info("[watcher] done — %d plans, %d new alerts", len(plans), len(all_alerts))
    return summary
