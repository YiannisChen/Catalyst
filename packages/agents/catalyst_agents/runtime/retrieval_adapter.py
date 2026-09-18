"""Typed adapter from data-core V1.1 retrieval hits to the agent protocol (M4-0).

The ResearchExecutor-facing adapter consumes the V1.1 ``RetrievalHit`` contract
and emits ``RetrievedEvidence`` records carrying the complete data-owned
metadata required to construct ``EvidenceStateItem`` (amendment §1.5). The
legacy ``RetrievalResult`` list-shaped path is baseline-only for the sealed
M1/M3 four-arm exit and is not consumed here.
"""
from __future__ import annotations

import time
from typing import Any
from dataclasses import dataclass, field
from datetime import datetime, timezone

from catalyst_agents.attribution.provider import RetrievedEvidence
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval import v1_result
from catalyst_data.retrieval.hybrid import ProductionHybridRetriever
from catalyst_data.retrieval.result import RetrievalContractError
from catalyst_data.retrieval.v1_result import RetrievalHit

# Pre-rerank stages in preference order: the best available pre-rerank rank is
# the hit's rank before the reranker reordered it.
_PRE_RERANK_STAGES = ("fusion", "lexical", "dense")


@dataclass(frozen=True)
class RetrievalCallObservation:
    """Facts observed at the retrieval boundary for one retrieve() call.

    Every field is read or computed from what the retriever actually returned
    plus the request the adapter actually made. Fields that the retrieval
    contract cannot expose are ``None`` (typed unavailable) rather than an
    empty value that could be mistaken for "observed, none found".
    """

    requested_mode: str
    served_mode: str | None
    ordered_candidate_evidence_ids: tuple[str, ...] = ()
    ordered_final_ranked_evidence_ids: tuple[str, ...] = ()
    rank_changes: dict[str, int] = field(default_factory=dict)
    # The V1.1 result set exposes no post-dedup drop list, so this is typed
    # ``None`` and is never reported as "no duplicates were dropped".
    duplicate_drops: None = None
    measured_latency_ms: int | None = None
    ticker_violations: tuple[str, ...] = ()
    cutoff_violations: tuple[str, ...] = ()
    # Real degradation reasons reported by the retriever for this call; empty
    # means the retriever observed no degradation, never "unobserved".
    degradation_reasons: tuple[str, ...] = ()
    arm_top_k: int | None = None
    arm_names: tuple[str, ...] = ()


class RetrievalDegradedError(RuntimeError):
    """A real retriever degradation with its observation preserved."""

    def __init__(self, observation: Any) -> None:
        self.observation = observation
        reasons = tuple(observation.degradation_reasons) or (
            "retrieval_degraded_not_v1_representable",
        )
        super().__init__(
            "resolved retrieval degraded "
            f"(served_mode={observation.served_mode!r}): {', '.join(reasons)}"
        )


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


def _stage_rank(hit: RetrievalHit, stage: str) -> int | None:
    for entry in hit.ranks:
        if entry.stage == stage:
            return entry.value
    return None


def _pre_rerank_rank(hit: RetrievalHit) -> int | None:
    for stage in _PRE_RERANK_STAGES:
        rank = _stage_rank(hit, stage)
        if rank is not None:
            return rank
    return None


def _cutoff_datetime(cutoff: str) -> datetime:
    value = cutoff.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _violations(
    *, result: v1_result.RetrievalResultSet, ticker: str, cutoff: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Compute the ticker/cutoff violations actually observed on served hits."""
    cutoff_at = _cutoff_datetime(cutoff)
    ticker_violations: list[str] = []
    cutoff_violations: list[str] = []
    for hit in result.hits:
        if ticker not in tuple(hit.ticker_scope):
            ticker_violations.append(hit.evidence_id)
        if hit.eligible_at > cutoff_at:
            cutoff_violations.append(hit.evidence_id)
    return tuple(ticker_violations), tuple(cutoff_violations)


def _observe_from_v1_result_set(
    *,
    result: v1_result.RetrievalResultSet,
    ticker: str,
    cutoff: str,
    top_k: int,
    requested_mode: str,
    latency_ms: int,
) -> RetrievalCallObservation:
    """Fail-closed observation for a retriever that exposes only the V1 set.

    A V1.1 result set is intentionally lossy: it cannot prove which upstream
    stages executed, the complete candidate inventory, or a truthful served
    mode. Even its reranked ranks are not enough to reconstruct those facts.
    Evidence can still be consumed, but all process observations remain
    unavailable until the retriever exposes the observation boundary.
    """
    ticker_violations, cutoff_violations = _violations(
        result=result, ticker=ticker, cutoff=cutoff
    )
    return RetrievalCallObservation(
        requested_mode=requested_mode,
        served_mode=None,
        ordered_candidate_evidence_ids=(),
        ordered_final_ranked_evidence_ids=(),
        rank_changes={},
        duplicate_drops=None,
        measured_latency_ms=latency_ms,
        ticker_violations=ticker_violations,
        cutoff_violations=cutoff_violations,
        degradation_reasons=(),
        arm_top_k=top_k,
        arm_names=(),
    )


def _observe_call(
    *,
    observation: Any,
    result: v1_result.RetrievalResultSet,
    ticker: str,
    cutoff: str,
    top_k: int,
    requested_mode: str,
    latency_ms: int,
) -> RetrievalCallObservation:
    """Build the adapter observation from the retriever's real observation.

    Candidate order, final ranking, rank displacement, served arms and
    degradation reasons all come from the retriever's own observation captured
    before the lossy V1 conversion. The V1.1 hit contract writes the
    pre-rerank stage ranks as ``None``, so nothing here is inferred from the
    converted hits: a fact the runtime did not observe stays unavailable.
    """
    ticker_violations, cutoff_violations = _violations(
        result=result, ticker=ticker, cutoff=cutoff
    )
    final_ids = tuple(observation.ordered_final_ranked_evidence_ids)[:top_k]
    return RetrievalCallObservation(
        requested_mode=requested_mode,
        served_mode=observation.served_mode,
        ordered_candidate_evidence_ids=tuple(
            observation.ordered_candidate_evidence_ids
        ),
        ordered_final_ranked_evidence_ids=final_ids,
        rank_changes=dict(observation.rank_changes),
        duplicate_drops=None,
        measured_latency_ms=latency_ms,
        ticker_violations=ticker_violations,
        cutoff_violations=cutoff_violations,
        degradation_reasons=tuple(observation.degradation_reasons),
        arm_top_k=top_k,
        arm_names=tuple(observation.arm_names),
    )


class AgentRetrieverAdapter:
    """Expose the production facade through the agents' evidence protocol.

    The adapter consumes the V1.1 ``RetrievalResultSet``; the exact validated
    request ``TemporalIdentity`` is passed through to the production retriever
    and is never reconstructed from the cutoff.

    ``retrieve_with_observations`` additionally returns the observed retrieval
    facts for that call (ordered candidates, ordered final ranking, measured
    latency, computed ticker/cutoff violations). ``retrieve`` keeps the
    evidence-only signature for the agents' retriever protocol.
    """

    def __init__(self, retriever: ProductionHybridRetriever) -> None:
        self._retriever = retriever

    def _call(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
        temporal_identity: TemporalIdentity | None,
        top_k: int,
        candidate_depth: int,
    ) -> tuple[v1_result.RetrievalResultSet, RetrievedEvidence, int, Any]:
        started = time.monotonic()
        observe = getattr(self._retriever, "retrieve_with_observation", None)
        observation: Any = None
        if callable(observe):
            # The production retriever captures the real retrieval process
            # before the lossy V1 conversion; read it directly.
            observed = observe(
                query,
                ticker=ticker,
                cutoff=cutoff,
                requested_manifest_id=requested_manifest_id,
                temporal_identity=temporal_identity,
                top_k=top_k,
                candidate_depth=candidate_depth,
            )
            result = observed.result_set
            observation = observed.observation
            if result is None:
                # The retriever observed a degraded call (for example a
                # reranker fallback). The frozen V1.1 contract only represents
                # a served reranked set, so the call fails closed with the real
                # degradation reason rather than reporting an empty reranked
                # result as if the reranker had served.
                raise RetrievalDegradedError(observation)
        else:
            result = self._retriever.retrieve(
                query,
                ticker=ticker,
                cutoff=cutoff,
                requested_manifest_id=requested_manifest_id,
                temporal_identity=temporal_identity,
                top_k=top_k,
                candidate_depth=candidate_depth,
            )
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        if not isinstance(result, v1_result.RetrievalResultSet):
            raise TypeError(
                "production retriever must return the V1.1 RetrievalResultSet"
            )
        evidence = tuple(_to_retrieved_evidence(hit) for hit in result.hits[:top_k])
        return result, evidence, latency_ms, observation

    def retrieve_with_observations(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
        temporal_identity: TemporalIdentity | None = None,
        top_k: int = 8,
        candidate_depth: int = 20,
    ) -> tuple[tuple[RetrievedEvidence, ...], RetrievalCallObservation]:
        result, evidence, latency_ms, observed = self._call(
            query,
            ticker=ticker,
            cutoff=cutoff,
            requested_manifest_id=requested_manifest_id,
            temporal_identity=temporal_identity,
            top_k=top_k,
            candidate_depth=candidate_depth,
        )
        if observed is None:
            observation = _observe_from_v1_result_set(
                result=result,
                ticker=ticker,
                cutoff=cutoff,
                top_k=top_k,
                requested_mode="reranked",
                latency_ms=latency_ms,
            )
        else:
            observation = _observe_call(
                observation=observed,
                result=result,
                ticker=ticker,
                cutoff=cutoff,
                top_k=top_k,
                requested_mode=str(observed.requested_mode),
                latency_ms=latency_ms,
            )
        return evidence, observation

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
        evidence, _observation = self.retrieve_with_observations(
            query,
            ticker=ticker,
            cutoff=cutoff,
            requested_manifest_id=requested_manifest_id,
            temporal_identity=temporal_identity,
            top_k=top_k,
            candidate_depth=candidate_depth,
        )
        return evidence


__all__ = [
    "AgentRetrieverAdapter",
    "RetrievalCallObservation",
    "RetrievalDegradedError",
]
