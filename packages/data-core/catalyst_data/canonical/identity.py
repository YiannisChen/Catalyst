"""V1.1 data runtime identity contract (M2-1).

DataRuntimeIdentity is emitted once by data-core and passed unchanged through
retrieval, agents, and eval (Frozen §5.6; Final Migration TSD §14).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DataRuntimeIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data_snapshot_id: str
    corpus_manifest_id: str
    fts_index_version: str
    dense_index_version: str | None = None
    embedding_model_revision: str | None = None
    reranker_revision: str | None = None
    query_policy_version: str


__all__ = ["DataRuntimeIdentity"]
