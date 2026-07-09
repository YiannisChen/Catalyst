from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field


class CauseCategory(str, Enum):
    EARNINGS = "earnings"
    MACRO = "macro"
    GEOPOLITICAL = "geopolitical"
    SECTOR = "sector"
    TECHNICAL = "technical"
    REGULATORY = "regulatory"


class Cause(BaseModel):
    text: str = Field(..., description="Description of the cause")
    category: CauseCategory
    evidence_ids: list[str] = Field(default_factory=list)
    # Per-cause provenance (§0.6)
    annotator: str | None = Field(default=None, description="Who created this cause")
    annotated_at: str | None = Field(default=None, description="ISO-8601 timestamp")
    supporting_asset_ids: list[str] = Field(default_factory=list, description="Evidence asset_ids backing this cause")
    notes: str | None = Field(default=None, description="Annotator notes")


class GoldenEvent(BaseModel):
    id: str
    ticker: str
    trade_date: str
    price_move_pct: float
    causes: list[Cause]
    should_refuse: bool = Field(default=False, description="True if the agent should refuse to answer")
