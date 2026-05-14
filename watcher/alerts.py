"""Alert persistence for the Watcher.

Alerts are written to data/alerts.json — a flat append-only list.
The store is intentionally simple: for the PoC, linear scans over a
small file are fine. Replace with a DB once alert volume warrants it.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

_ALERTS_PATH = Path(__file__).resolve().parent.parent / "data" / "alerts.json"


class Alert(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ticker: str
    pillar_name: str
    old_status: str
    new_status: str
    reasoning: str
    confidence: int = 5
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    read: bool = False


def _load() -> list[dict]:
    if not _ALERTS_PATH.exists():
        return []
    with _ALERTS_PATH.open() as f:
        return json.load(f)


def _save(alerts: list[dict]) -> None:
    _ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _ALERTS_PATH.open("w") as f:
        json.dump(alerts, f, indent=2)


def append_alerts(new: list[Alert]) -> None:
    if not new:
        return
    raw = _load()
    raw.extend([a.model_dump() for a in new])
    _save(raw)


def list_alerts(unread_only: bool = False) -> list[Alert]:
    raw = _load()
    if unread_only:
        raw = [r for r in raw if not r.get("read")]
    # Newest first
    raw.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return [Alert(**r) for r in raw]


def mark_read(ids: list[str] | None = None) -> int:
    """Mark alerts read. Pass ids=None to mark all. Returns count updated."""
    raw = _load()
    count = 0
    for r in raw:
        if ids is None or r["id"] in ids:
            if not r.get("read"):
                r["read"] = True
                count += 1
    _save(raw)
    return count


def unread_count() -> int:
    return sum(1 for r in _load() if not r.get("read"))
