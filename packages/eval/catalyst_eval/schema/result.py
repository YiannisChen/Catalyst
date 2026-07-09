from __future__ import annotations
from pydantic import BaseModel, Field


class PredictedCause(BaseModel):
    text: str
    category: str  # string, not enum — agent may predict unknown categories
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    direction: str = Field(default="negative", description="positive, negative, or neutral")


class RetrievedEvidence(BaseModel):
    """A single piece of evidence retrieved by the Miner and available to the Judge.

    Carries both the asset_id (for grounding checks against evidence_ids)
    and the content (for faithfulness / qualitative inspection).
    """
    asset_id: str
    content_md: str = ""
    source_type: str = ""
    rrf_score: float = 0.0


class AttributionResult(BaseModel):
    """Contract: any agent must return this to be evaluated."""
    ticker: str
    trade_date: str
    causes: list[PredictedCause]
    summary: str
    retrieved_evidence: list[RetrievedEvidence] = Field(default_factory=list)
    cost_breakdown: list[dict] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    cost_cap_triggered: bool = False
    stopped_after_case_id: str | None = None
    executed_case_count: int = 0
    cost_cap_policy: str = "strict_gt"
    # Arm-B fidelity: SHA-256 of the reconstructed evidence block
    evidence_block_sha256: str | None = Field(default=None)
