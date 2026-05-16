"""Persistent log of watcher runs.

Each run gets a record on disk so the UI can show "last ran X min ago"
and a recent-activity feed. Same crude JSON-file approach as alerts.py —
swap for a DB when volume warrants.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

_RUNS_PATH = Path(__file__).resolve().parent.parent / "data" / "watcher_runs.json"
_MAX_RUNS = 200  # rolling cap


class WatcherRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ran_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    checked: int = 0
    new_alerts: int = 0
    results: list[dict] = Field(default_factory=list)


def _load() -> list[dict]:
    if not _RUNS_PATH.exists():
        return []
    with _RUNS_PATH.open() as f:
        return json.load(f)


def _save(runs: list[dict]) -> None:
    _RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _RUNS_PATH.open("w") as f:
        json.dump(runs, f, indent=2)


def append_run(summary: dict) -> WatcherRun:
    """Record a watcher run. `summary` is the dict returned by run_watcher()."""
    record = WatcherRun(
        ran_at=summary.get("ran_at") or datetime.now(timezone.utc).isoformat(),
        checked=summary.get("checked", 0),
        new_alerts=summary.get("new_alerts", 0),
        results=summary.get("results", []),
    )
    raw = _load()
    raw.append(record.model_dump())
    # Keep most-recent _MAX_RUNS
    if len(raw) > _MAX_RUNS:
        raw = raw[-_MAX_RUNS:]
    _save(raw)
    return record


def list_runs(limit: int = 20) -> list[WatcherRun]:
    raw = _load()
    raw.sort(key=lambda r: r.get("ran_at", ""), reverse=True)
    return [WatcherRun(**r) for r in raw[:limit]]


def last_run() -> WatcherRun | None:
    runs = list_runs(limit=1)
    return runs[0] if runs else None
