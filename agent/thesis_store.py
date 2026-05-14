"""Tiny file-backed thesis store.

For the PoC, theses live in `data/theses.json`. This is intentionally
crude — replace with a real DB once the schema stabilizes.

The Pydantic models here mirror the Thesis Object described in the
strategic one-pager. They are the spine of the product, so we want
type-checked access from the start.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

PillarStatus = Literal["intact", "wobbling", "broken"]
Direction = Literal["long", "short"]

# Resolve the data file relative to the project root, not the cwd.
_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "theses.json"


class Pillar(BaseModel):
    """One load-bearing claim of a thesis."""

    name: str
    current_value: str = Field(
        description="Latest reading, e.g. 'Cloud growth 63% YoY'."
    )
    threshold_break: str = Field(
        description="The condition that would invalidate this pillar, "
        "e.g. '<40% for 2 quarters'."
    )
    watch_signals: list[str] = Field(
        default_factory=list,
        description="Data sources that move this pillar.",
    )
    status: PillarStatus = "intact"


class Catalyst(BaseModel):
    name: str
    expected_date: str | None = None  # ISO date or 'Q3 2026' style


class ExitPlan(BaseModel):
    take_profit: str | None = None
    thesis_break: str | None = None
    time_stop: str | None = None


class Thesis(BaseModel):
    ticker: str
    one_liner: str
    direction: Direction = "long"
    conviction: int = Field(ge=1, le=10)
    time_horizon: str  # free-form: "12-18 months"
    entry_price: float | None = None   # purchase price; drives dashboard P&L
    entry_date: str | None = None      # ISO date of purchase
    pillars: list[Pillar] = Field(default_factory=list)
    catalysts: list[Catalyst] = Field(default_factory=list)
    exit_plan: ExitPlan = Field(default_factory=ExitPlan)
    source_material: str | None = None
    journal: list[str] = Field(default_factory=list)
    last_updated: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


def _load_raw() -> dict[str, dict]:
    if not _DATA_PATH.exists():
        return {}
    with _DATA_PATH.open() as f:
        return json.load(f)


def list_theses() -> list[Thesis]:
    return [Thesis(**t) for t in _load_raw().values()]


def get_thesis(ticker: str) -> Thesis | None:
    raw = _load_raw().get(ticker.upper())
    return Thesis(**raw) if raw else None


def upsert_thesis(thesis: Thesis) -> None:
    """Persist a thesis. Used by future flows (paste-to-pillars, journal)."""
    _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = _load_raw()
    raw[thesis.ticker.upper()] = thesis.model_dump()
    with _DATA_PATH.open("w") as f:
        json.dump(raw, f, indent=2)
