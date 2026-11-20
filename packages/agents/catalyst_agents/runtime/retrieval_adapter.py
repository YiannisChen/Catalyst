"""Typed adapter from data-core V1.1 retrieval hits to the agent protocol (M4-0).

The ResearchExecutor-facing adapter consumes the V1.1 ``RetrievalHit`` contract
and emits ``RetrievedEvidence`` records carrying the complete data-owned
metadata required to construct ``EvidenceStateItem`` (amendment §1.5). The
legacy ``RetrievalResult`` list-shaped path is baseline-only for the sealed
M1/M3 four-arm exit and is not consumed here.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval import v1_result
from catalyst_data.retrieval.v1_result import RetrievalHit

from catalyst_agents.attribution.provider import RetrievedEvidence

# Pre-rerank stages in preference order: the best available pre-rerank rank is
# the hit's rank before the reranker reordered it.
_PRE_RERANK_STAGES = ("fusion", "lexical", "dense")

# WP-B B4: stage/arm names that declare a rerank stage. Only a retriever whose
# observation declares one of these may be described as "the reranker did not
# serve" (Frozen-A3 §2.1 removes the reranker from V1.1).
_RERANK_STAGE_NAMES = ("reranked", "rerank", "reranker")


@runtime_checkable
class RetrieverProtocol(Protocol):
    """The retriever surface ``AgentRetrieverAdapter`` wraps.

    Both V1.1 retrievers expose the same two entry points: the evidence-only
    ``retrieve`` and the ``retrieve_with_observation`` call that also returns
    the retriever's own observation. The adapter is typed to this protocol, not
    to any concrete class, so the V1.1 lexical path and the (unconstructed)
    hybrid path are interchangeable at the boundary.
    """

    def retrieve(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        top_k: int,
        candidate_depth: int,
        requested_manifest_id: str | None = None,
        temporal_identity: TemporalIdentity | None = None,
        source_classes: tuple[str, ...] | None = None,
        temporal_centre_date: str | None = None,
    ) -> v1_result.RetrievalResultSet: ...

    def retrieve_with_observation(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        top_k: int,
        candidate_depth: int,
        requested_manifest_id: str | None = None,
        temporal_identity: TemporalIdentity | None = None,
        source_classes: tuple[str, ...] | None = None,
        temporal_centre_date: str | None = None,
    ) -> Any: ...


def _declares_rerank_stage(observation: Any) -> bool:
    """True when the retriever's own observation declares a rerank stage.

    The lexical retriever never does, so a ``None`` result from it is a lexical
    contract failure, never "the reranker did not serve".
    """
    arms = tuple(getattr(observation, "arm_names", ()) or ())
    if any(name in _RERANK_STAGE_NAMES for name in arms):
        return True
    for field in ("served_mode", "requested_mode"):
        if getattr(observation, field, None) in _RERANK_STAGE_NAMES:
            return True
    return False


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
    # WP-B B3: the executed query policy facts. Each is ``None`` when the
    # retriever did not expose it, never a fabricated default.
    match_mode: str | None = None
    retrieval_policy: str | None = None
    temporal_window_days: int | None = None
    temporal_centre_date: str | None = None
    served_build_id: str | None = None
    # sha256 of the exact query string the adapter sent to the retriever.
    query_sha256: str | None = None
    # WP-B B7a: the verified served projection the call ran on, or None.
    projection_build_id: str | None = None
    projection_sha256: str | None = None


class RetrievalDegradedError(RuntimeError):
    """A real retriever degradation with its observation preserved.

    ``reason`` is only supplied when the retriever's own degradation reasons do
    not already name one (for example a lexical retriever that returned no
    result, which the V1.1 lexical path never does).
    """

    def __init__(self, observation: Any, *, reason: str | None = None) -> None:
        self.observation = observation
        reasons = tuple(observation.degradation_reasons) or (
            (reason,) if reason else ("retrieval_degraded_not_v1_representable",)
        )
        super().__init__(
            "resolved retrieval degraded "
            f"(served_mode={observation.served_mode!r}): {', '.join(reasons)}"
        )


def _scores_by_stage(hit: RetrievalHit) -> dict[str, float | None]:
    return {entry.stage: entry.value for entry in hit.scores}


def _ranks_by_stage(hit: RetrievalHit) -> dict[str, int | None]:
    return {entry.stage: entry.value for entry in hit.ranks}


def _hit_modes(hit: RetrievalHit, observation: Any | None) -> tuple[str, str]:
    """Requested/served mode as observed by the retriever (V1.1: lexical/fts5).

    Without an observation, the hit's own most-processed stage is reported.
    """
    if observation is not None:
        return str(observation.requested_mode), str(observation.served_mode)
    stages = {entry.stage for entry in hit.ranks}
    for stage in ("reranked", "fusion", "lexical", "dense"):
        if stage in stages:
            return stage, stage
    return "lexical", "lexical"


def _to_retrieved_evidence(hit: RetrievalHit, observation: Any | None = None) -> RetrievedEvidence:
    """Map one V1.1 hit to the adapter evidence record (never fabricated)."""
    mode_requested, mode_served = _hit_modes(hit, observation)
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
        mode_requested=mode_requested,
        mode_served=mode_served,
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
        source_time_at=hit.source_time_at,
        provider_updated_at=hit.provider_updated_at,
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


def _query_sha256(query: str) -> str:
    """sha256 of the exact query string sent to the retriever (WP-B B3)."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


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
    query: str,
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
        query_sha256=_query_sha256(query),
    )


def _observe_call(
    *,
    observation: Any,
    result: v1_result.RetrievalResultSet,
    query: str,
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
        match_mode=getattr(observation, "match_mode", None),
        # WP-B B3 field ``retrieval_policy`` carries the executed query policy
        # (B2 ``LexicalDisplayObservation.policy``: ``temporal_window`` /
        # ``content``). The hybrid observation exposes neither, so it stays
        # ``None`` there rather than being given a fabricated value.
        retrieval_policy=getattr(observation, "policy", None),
        temporal_window_days=getattr(observation, "temporal_window_days", None),
        temporal_centre_date=getattr(observation, "temporal_centre_date", None),
        served_build_id=getattr(observation, "served_build_id", None),
        query_sha256=_query_sha256(query),
        projection_build_id=getattr(observation, "projection_build_id", None),
        projection_sha256=getattr(observation, "projection_sha256", None),
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

    def __init__(self, retriever: RetrieverProtocol) -> None:
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
        source_classes: tuple[str, ...] | None = None,
        temporal_centre_date: str | None = None,
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
                source_classes=source_classes,
                temporal_centre_date=temporal_centre_date,
            )
            result = observed.result_set
            observation = observed.observation
            if result is None:
                # A retriever whose observation declares a rerank stage observed
                # a reranker that did not serve; the V1.1 contract has no
                # representation for that, so the call fails closed with the
                # real degradation reason. The lexical V1.1 retriever declares
                # no rerank stage and never returns ``None``; if it ever does,
                # that is still a fail-closed degradation, never a fabricated
                # reranked result.
                raise RetrievalDegradedError(
                    observation,
                    reason=(
                        None
                        if _declares_rerank_stage(observation)
                        else "retrieval_retriever_returned_no_result"
                    ),
                )
        else:
            result = self._retriever.retrieve(
                query,
                ticker=ticker,
                cutoff=cutoff,
                requested_manifest_id=requested_manifest_id,
                temporal_identity=temporal_identity,
                top_k=top_k,
                candidate_depth=candidate_depth,
                source_classes=source_classes,
                temporal_centre_date=temporal_centre_date,
            )
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        if not isinstance(result, v1_result.RetrievalResultSet):
            raise TypeError(
                "production retriever must return the V1.1 RetrievalResultSet"
            )
        evidence = tuple(
            _to_retrieved_evidence(hit, observation) for hit in result.hits[:top_k]
        )
        return result, evidence, latency_ms, observation

    def lead_terms(self, *, ticker: str, temporal_identity: Any) -> tuple[str, ...]:
        inner = getattr(self._retriever, "lead_terms", None)
        if not callable(inner):
            return ()
        return tuple(inner(ticker=ticker, temporal_identity=temporal_identity))

    def retrieve_with_observations(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
        temporal_identity: TemporalIdentity | None = None,
        top_k: int,
        candidate_depth: int,
        source_classes: tuple[str, ...] | None = None,
        temporal_centre_date: str | None = None,
    ) -> tuple[tuple[RetrievedEvidence, ...], RetrievalCallObservation]:
        result, evidence, latency_ms, observed = self._call(
            query,
            ticker=ticker,
            cutoff=cutoff,
            requested_manifest_id=requested_manifest_id,
            temporal_identity=temporal_identity,
            top_k=top_k,
            candidate_depth=candidate_depth,
            source_classes=source_classes,
            temporal_centre_date=temporal_centre_date,
        )
        if observed is None:
            observation = _observe_from_v1_result_set(
                result=result,
                query=query,
                ticker=ticker,
                cutoff=cutoff,
                top_k=top_k,
                requested_mode=evidence[0].mode_requested if evidence else "lexical",
                latency_ms=latency_ms,
            )
        else:
            observation = _observe_call(
                observation=observed,
                result=result,
                query=query,
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
        top_k: int,
        candidate_depth: int,
        source_classes: tuple[str, ...] | None = None,
        temporal_centre_date: str | None = None,
    ) -> tuple[RetrievedEvidence, ...]:
        evidence, _observation = self.retrieve_with_observations(
            query,
            ticker=ticker,
            cutoff=cutoff,
            requested_manifest_id=requested_manifest_id,
            temporal_identity=temporal_identity,
            top_k=top_k,
            candidate_depth=candidate_depth,
            source_classes=source_classes,
            temporal_centre_date=temporal_centre_date,
        )
        return evidence


__all__ = [
    "AgentRetrieverAdapter",
    "RetrievalCallObservation",
    "RetrievalDegradedError",
    "RetrieverProtocol",
]
