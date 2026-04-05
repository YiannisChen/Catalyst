from __future__ import annotations
from pydantic import BaseModel, Field


class PredictedCause(BaseModel):
    text: str
    category: str  # string, not enum — agent may predict unknown categories
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    direction: str = Field(default="negative", description="positive, negative, or neutral")


class AttributionResult(BaseModel):
    """Contract: any agent must return this to be evaluated."""
    ticker: str
    trade_date: str
    causes: list[PredictedCause]
    summary: str
    retrieved_chunks: list[str] = Field(default_factory=list)
    cost_breakdown: list[dict] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
