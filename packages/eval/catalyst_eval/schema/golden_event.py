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
    weight: float = Field(..., ge=0.0, le=1.0, description="Contribution weight, must sum to ~1.0")
    temporal_anchor: str = Field(..., description="When cause became known: pre-market, intraday, after-hours")
    evidence_ids: list[str] = Field(default_factory=list)


class GoldenEvent(BaseModel):
    id: str
    ticker: str
    trade_date: str
    price_move_pct: float
    causes: list[Cause]
