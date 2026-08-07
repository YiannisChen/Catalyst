from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Protocol


@dataclass(frozen=True)
class ContextInputs:
    ticker: str
    session_date: date
    cutoff: str
    target_close: float | None
    previous_target_close: float | None
    target_volume: float | None
    expected_prior_sessions: tuple[str, ...]
    prior_volumes_by_session: Mapping[str, float | None]
    benchmark_ticker: str | None
    benchmark_return_pct: float | None
    sector_ticker: str | None
    sector_return_pct: float | None
    peer_returns_by_ticker: Mapping[str, float | None]


class ContextProvider(Protocol):
    def load_context_inputs(self, *, ticker: str, session_date: str, cutoff: str) -> ContextInputs:
        ...


@dataclass(frozen=True)
class RetrievedEvidence:
    chunk_id: str
    document_id: str
    content_text: str
    available_at: str
    source_class: str
    ticker_associations: tuple[str, ...]
    dedup_cluster_id: str | None
    cluster_first_available_at: str
    representative_document_id: str
    is_novel: bool
    lexical_raw_score: float | None
    lexical_rank: int
    corpus_manifest_id: str
    index_manifest_id: str | None
    mode_requested: str
    mode_served: str
    is_degraded: bool
    fallback_reason: str | None
    fusion_score: float | None = None


class Retriever(Protocol):
    def retrieve(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
        top_k: int = 8,
        candidate_depth: int = 20,
    ) -> tuple[RetrievedEvidence, ...]:
        ...
