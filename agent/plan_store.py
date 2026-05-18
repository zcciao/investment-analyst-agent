"""Tiny file-backed plan store.

For the PoC, plans live in `data/plans.json`. This is intentionally crude —
replace with a real DB once the schema stabilizes.

The Pydantic models here mirror the Plan Object described in the strategic
one-pager. They are the spine of the product, so we want type-checked access
from the start.

Renaming history (kept for context):
  Thesis → Plan
  Pillar → Reason
  Catalyst → Event
  ExitPlan → ExitTriggers
  PillarStatus → ReasonStatus
  conviction → confidence
  threshold_break → walk_away_signal
  watch_signals → data_sources
  exit_plan → exit_triggers
  thesis_break → plan_break  (field inside exit_triggers)
  pillars → reasons
  catalysts → events
Status values: intact|wobbling|broken|strengthening
            →  on_track|at_risk|off_track|ahead

`_migrate_if_needed()` runs at import time and converts the old format
in-place. Idempotent; safe on every boot.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

log = logging.getLogger("plan_store")

ReasonStatus = Literal["on_track", "at_risk", "off_track", "ahead"]
Direction = Literal["long", "short"]

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_PATH = _DATA_DIR / "plans.json"
_OLD_PLANS_PATH = _DATA_DIR / "theses.json"
_ALERTS_PATH = _DATA_DIR / "alerts.json"


class Reason(BaseModel):
    """One load-bearing claim of a plan."""

    name: str
    current_value: str = Field(
        description="Latest reading, e.g. 'Cloud growth 63% YoY'."
    )
    walk_away_signal: str = Field(
        description="The condition that would invalidate this reason, "
        "e.g. '<40% for 2 quarters'."
    )
    data_sources: list[str] = Field(
        default_factory=list,
        description="Sources that move this reason (filings, calls, indices, etc.).",
    )
    status: ReasonStatus = "on_track"


class Event(BaseModel):
    name: str
    expected_date: str | None = None  # ISO date or 'Q3 2026' style


class ExitTriggers(BaseModel):
    take_profit: str | None = None
    plan_break: str | None = None
    time_stop: str | None = None


class Plan(BaseModel):
    ticker: str
    one_liner: str
    direction: Direction = "long"
    confidence: int = Field(ge=1, le=10)
    time_horizon: str  # free-form: "12-18 months"
    entry_price: float | None = None
    entry_date: str | None = None
    reasons: list[Reason] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    exit_triggers: ExitTriggers = Field(default_factory=ExitTriggers)
    source_material: str | None = None
    journal: list[str] = Field(default_factory=list)
    last_updated: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #

_STATUS_MAP = {
    "intact":        "on_track",
    "wobbling":      "at_risk",
    "broken":        "off_track",
    "strengthening": "ahead",
}


def _migrate_plan_dict(t: dict) -> dict:
    """Old-shape thesis dict → new-shape plan dict."""
    pillars = t.get("pillars") or []
    reasons = []
    for p in pillars:
        reasons.append({
            "name":             p.get("name", ""),
            "current_value":    p.get("current_value", ""),
            "walk_away_signal": p.get("walk_away_signal") or p.get("threshold_break", ""),
            "data_sources":     p.get("data_sources") or p.get("watch_signals", []),
            "status":           _STATUS_MAP.get(p.get("status", "intact"), p.get("status", "on_track")),
        })

    old_exit = t.get("exit_plan") or t.get("exit_triggers") or {}
    exit_triggers = {
        "take_profit": old_exit.get("take_profit"),
        "plan_break":  old_exit.get("plan_break") or old_exit.get("thesis_break"),
        "time_stop":   old_exit.get("time_stop"),
    }

    return {
        "ticker":          t.get("ticker"),
        "one_liner":       t.get("one_liner", ""),
        "direction":       t.get("direction", "long"),
        "confidence":      t.get("confidence", t.get("conviction", 5)),
        "time_horizon":    t.get("time_horizon", ""),
        "entry_price":     t.get("entry_price"),
        "entry_date":      t.get("entry_date"),
        "reasons":         reasons,
        "events":          t.get("events") or t.get("catalysts", []),
        "exit_triggers":   exit_triggers,
        "source_material": t.get("source_material"),
        "journal":         t.get("journal", []),
        "last_updated":    t.get("last_updated", datetime.utcnow().isoformat()),
    }


def _migrate_alerts_in_place() -> int:
    """Rewrite data/alerts.json: pillar_name → reason_name, status string values."""
    if not _ALERTS_PATH.exists():
        return 0
    with _ALERTS_PATH.open() as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        return 0
    changed = 0
    for a in raw:
        if "pillar_name" in a and "reason_name" not in a:
            a["reason_name"] = a.pop("pillar_name")
            changed += 1
        for k in ("old_status", "new_status"):
            v = a.get(k)
            if v in _STATUS_MAP:
                a[k] = _STATUS_MAP[v]
                changed += 1
    if changed:
        with _ALERTS_PATH.open("w") as f:
            json.dump(raw, f, indent=2)
    return changed


def _migrate_if_needed() -> None:
    """Convert legacy data files to the new schema. Idempotent."""
    if _DATA_PATH.exists():
        # Already on the new schema. Still try to migrate alerts (idempotent).
        n = _migrate_alerts_in_place()
        if n:
            log.info("[migration] alerts.json: %d fields updated", n)
        return

    if not _OLD_PLANS_PATH.exists():
        # Fresh install — nothing to migrate.
        return

    try:
        with _OLD_PLANS_PATH.open() as f:
            old = json.load(f)
        new = {ticker: _migrate_plan_dict(t) for ticker, t in old.items()}
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _DATA_PATH.open("w") as f:
            json.dump(new, f, indent=2)
        # Keep a one-cycle backup; don't delete.
        shutil.move(str(_OLD_PLANS_PATH), str(_OLD_PLANS_PATH) + ".bak")
        log.info("[migration] %d plans migrated → %s", len(new), _DATA_PATH.name)
        n = _migrate_alerts_in_place()
        if n:
            log.info("[migration] alerts.json: %d fields updated", n)
    except Exception:
        log.exception("[migration] failed — aborting boot to avoid half-state")
        raise


# Run at import. Cheap if no-op.
_migrate_if_needed()


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def _load_raw() -> dict[str, dict]:
    if not _DATA_PATH.exists():
        return {}
    with _DATA_PATH.open() as f:
        return json.load(f)


def list_plans() -> list[Plan]:
    return [Plan(**t) for t in _load_raw().values()]


def get_plan(ticker: str) -> Plan | None:
    raw = _load_raw().get(ticker.upper())
    return Plan(**raw) if raw else None


def upsert_plan(plan: Plan) -> None:
    """Persist a plan."""
    _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = _load_raw()
    raw[plan.ticker.upper()] = plan.model_dump()
    with _DATA_PATH.open("w") as f:
        json.dump(raw, f, indent=2)
