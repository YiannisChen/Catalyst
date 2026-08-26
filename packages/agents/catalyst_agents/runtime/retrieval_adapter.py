"""Typed adapter from data-core V1.1 retrieval hits to the agent protocol (M4-0).

The ResearchExecutor-facing adapter consumes the V1.1 ``RetrievalHit`` contract
and emits ``RetrievedEvidence`` records carrying the complete data-owned
metadata required to construct ``EvidenceStateItem`` (amendment §1.5). The
legacy ``RetrievalResult`` list-shaped path is baseline-only for the sealed
M1/M3 four-arm exit and is not consumed here.
"""
from __future__ import annotations

from catalyst_agents.attribution.provider import RetrievedEvidence
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval import v1_result
from catalyst_data.retrieval.hybrid import ProductionHybridRetriever
from catalyst_data.retrieval.v1_result import RetrievalHit


def _scores_by_stage(hit: RetrievalHit) -> dict[str, float | None]:
    return {entry.stage: entry.value for entry in hit.scores}


def _ranks_by_stage(hit: RetrievalHit) -> dict[str, int | None]:
    return {entry.stage: entry.value for entry in hit.ranks}


def _to_retrieved_evidence(hit: RetrievalHit) -> RetrievedEvidence:
    """Map one V1.1 hit to the adapter evidence record (never fabricated)."""
    scores = _scores_by_stage(hit)
    ranks = _ranks_by_stage(hit)
    rank = (
        ranks.get("reranked")
        or ranks.get("fusion")
        or ranks.get("lexical")
        or ranks.get("dense")
        or 1
    )
    eligible_at = hit.eligible_at
    independence_status = (
        "KNOWN_GROUP"
        if hit.source_class.value == "reported_news" and hit.independence_group_id
        else "UNKNOWN"
    )
    return RetrievedEvidence(
        chunk_id=hit.chunk_id,
        fact_id=hit.fact_id,
        document_id=hit.corpus_document_id,
        content_text=hit.excerpt,
        available_at=eligible_at.isoformat(),
        source_class=hit.source_class.value,
        ticker_associations=hit.ticker_scope,
        dedup_cluster_id=hit.dedup_cluster_id,
        cluster_first_available_at=eligible_at.isoformat(),
        representative_document_id=hit.corpus_document_id,
        is_novel=False,
        lexical_raw_score=scores.get("lexical"),
        lexical_rank=ranks.get("lexical"),
        corpus_manifest_id=hit.data_runtime_identity.corpus_manifest_id,
        index_manifest_id=hit.data_runtime_identity.dense_index_version,
        mode_requested="reranked",
        mode_served="reranked",
        is_degraded=False,
        fallback_reason=None,
        fusion_score=scores.get("fusion"),
        reranker_score=scores.get("reranked"),
        reranker_rank=ranks.get("reranked"),
        dense_score=scores.get("dense"),
        dense_rank=ranks.get("dense"),
        temporal_center_date=hit.temporal_identity.session_date,
        query_date=None,
        query_date_conflict=False,
        query_date_decision=None,
        canonical_asset_id=hit.canonical_asset_id,
        canonical_content_version_id=hit.content_version_id,
        corpus_document_id=hit.corpus_document_id,
        section_key=hit.section_key,
        chunk_ordinal=hit.chunk_ordinal,
        asset_type=hit.asset_type.value if hasattr(hit.asset_type, "value") else hit.asset_type,
        content_hash=hit.content_hash,
        material_capability=hit.material_capability,
        serving_status=hit.serving_status,
        temporal_precision=hit.temporal_precision,
        independence_group_id=hit.independence_group_id,
        independence_status=independence_status,
        canonical_url=hit.canonical_url,
        publisher=hit.publisher,
        evidence_role=hit.evidence_role,
        parse_quality=hit.parse_quality,
        provider=hit.provider,
        content_state=hit.content_state.value if hasattr(hit.content_state, "value") else hit.content_state,
        eligible_at=eligible_at,
        temporal_identity=hit.temporal_identity,
        data_runtime_identity=hit.data_runtime_identity,
    )


class AgentRetrieverAdapter:
    """Expose the production facade through the agents' evidence protocol.

    The adapter consumes the V1.1 ``RetrievalResultSet``; the exact validated
    request ``TemporalIdentity`` is passed through to the production retriever
    and is never reconstructed from the cutoff.
    """

    def __init__(self, retriever: ProductionHybridRetriever) -> None:
        self._retriever = retriever

    def retrieve(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
        temporal_identity: TemporalIdentity | None = None,
        top_k: int = 8,
        candidate_depth: int = 20,
    ) -> tuple[RetrievedEvidence, ...]:
        result = self._retriever.retrieve(
            query,
            ticker=ticker,
            cutoff=cutoff,
            requested_manifest_id=requested_manifest_id,
            temporal_identity=temporal_identity,
            top_k=top_k,
            candidate_depth=candidate_depth,
        )
        if not isinstance(result, v1_result.RetrievalResultSet):
            raise TypeError("production retriever must return the V1.1 RetrievalResultSet")
        return tuple(_to_retrieved_evidence(hit) for hit in result.hits[:top_k])


__all__ = ["AgentRetrieverAdapter"]
