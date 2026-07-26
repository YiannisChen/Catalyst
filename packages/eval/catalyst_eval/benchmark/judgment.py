"""EvidenceJudgment schema — per-chunk grade with rationale."""
from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class EvidenceJudgment(BaseModel):
    """A single annotator judgment for one retrieved chunk."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    grade: Literal[0, 1, 2]
    rationale: str
    annotator: str
    judged_at: datetime.datetime

    @field_validator("chunk_id")
    @classmethod
    def _non_empty_chunk_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("chunk_id must be non-empty")
        return v.strip()

    @field_validator("rationale")
    @classmethod
    def _strip_rationale(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("rationale must be non-empty after strip")
        if "\n" in stripped or "\r" in stripped:
            raise ValueError("rationale must be a single line (no CR/LF)")
        if len(stripped) > 500:
            raise ValueError("rationale must be ≤ 500 characters")
        return stripped

    @field_validator("annotator")
    @classmethod
    def _strip_annotator(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("annotator must be non-empty after strip")
        return stripped

    @field_validator("judged_at")
    @classmethod
    def _utc_aware(cls, v: datetime.datetime) -> datetime.datetime:
        if v.tzinfo is None or v.utcoffset() != datetime.timedelta(0):
            raise ValueError("judged_at must use UTC offset +00:00")
        return v
