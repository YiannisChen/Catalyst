"""Typed retrieval trace contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RetrievalTrace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_row_count: int = Field(ge=0)
    eligible_row_count: int = Field(ge=0)
    matched_row_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    final_count: int = Field(ge=0)
    filter_ms: float = Field(ge=0)
    score_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)
    mode_requested: Literal["lexical"]
    mode_served: Literal["fts5", "sql_like"]
    fallback_reason: Literal[
        "empty_query", "fts5_unavailable", "fts5_missing", "fts5_stale"
    ] | None = None

    @model_validator(mode="after")
    def _validate_counts(self) -> "RetrievalTrace":
        counts = (
            self.manifest_row_count,
            self.eligible_row_count,
            self.matched_row_count,
            self.candidate_count,
            self.final_count,
        )
        if any(left < right for left, right in zip(counts, counts[1:])):
            raise ValueError("retrieval trace counts must be monotonically non-increasing")
        if self.total_ms < self.filter_ms or self.total_ms < self.score_ms:
            raise ValueError("total_ms must cover each stage timing")
        return self
