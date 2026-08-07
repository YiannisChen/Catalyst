"""Typed adapter from data-core retrieval results to the agent protocol."""

from __future__ import annotations

from catalyst_agents.attribution.provider import RetrievedEvidence
from catalyst_data.retrieval.hybrid import ProductionHybridRetriever
from catalyst_data.retrieval.result import RetrievalResult


def _to_retrieved_evidence(result: RetrievalResult) -> RetrievedEvidence:
    rank = result.reranker_rank or result.fusion_rank or result.lexical_rank or result.dense_rank or 1
    return RetrievedEvidence(
        chunk_id=result.chunk_id,
        document_id=result.document_id,
        content_text=result.content_text or "",
        available_at=result.available_at,
        source_class=result.source_class,
        ticker_associations=result.ticker_associations or (result.filters_applied.ticker,),
        dedup_cluster_id=result.dedup_cluster_id,
        cluster_first_available_at=result.cluster_first_available_at or result.available_at,
        representative_document_id=result.representative_document_id or result.document_id,
        is_novel=result.is_novel,
        lexical_raw_score=result.lexical_raw_score,
        lexical_rank=rank,
        corpus_manifest_id=result.corpus_manifest_id,
        index_manifest_id=result.index_manifest_id,
        mode_requested=result.mode_requested,
        mode_served=result.mode_served,
        is_degraded=result.is_degraded,
        fallback_reason=result.fallback_reason,
        fusion_score=result.fusion_score,
    )


class AgentRetrieverAdapter:
    """Expose the production facade through the agents' evidence protocol."""

    def __init__(self, retriever: ProductionHybridRetriever) -> None:
        self._retriever = retriever

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
        results = self._retriever.retrieve(
            query,
            ticker=ticker,
            cutoff=cutoff,
            requested_manifest_id=requested_manifest_id,
            top_k=top_k,
            candidate_depth=candidate_depth,
        )
        return tuple(_to_retrieved_evidence(result) for result in results)


__all__ = ["AgentRetrieverAdapter"]
