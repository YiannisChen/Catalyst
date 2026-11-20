"""Aggregate per-call retrieval observations into run-level diagnostics.

The aggregation is deterministic and additive: it never invents a value for a
fact the runtime did not observe.

* ``ordered_candidate_evidence_ids`` is the union of the per-task candidate
  orders, first-seen order preserved across tasks in research-task order.
* ``ordered_final_ranked_evidence_ids`` is the union of the per-task final
  rankings in the same order (the run-level ranking actually served).
* ``measured_latency_ms`` is the total measured retrieval wall time for the run
  (the sum of the per-call measured latencies).
* ``degradation_reasons`` come from the persisted degradation records.
* ``ticker_violations``/``cutoff_violations`` are the union of the per-call
  computations over the served hits.
"""
from __future__ import annotations

from typing import Any, Sequence

from catalyst_agents.observability.diagnostics import (
    RetrievalArmObservation,
    RetrievalDiagnostics,
    RetrievalTaskObservation,
    data_runtime_identity_object_hash,
)


def _union_preserving_order(groups: Sequence[Sequence[str]]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for group in groups:
        for value in group:
            if value not in seen:
                seen.add(value)
                ordered.append(value)
    return tuple(ordered)


def build_retrieval_diagnostics(
    *,
    data_runtime_identity: Any,
    executions: Sequence[Any],
) -> RetrievalDiagnostics:
    """Build the run-level retrieval diagnostics from executed stages."""
    calls = [
        call
        for execution in executions
        for call in (getattr(execution, "observed_retrieval_calls", ()) or ())
    ]
    per_task = tuple(
        RetrievalTaskObservation(
            task_id=call.task_id,
            task_fingerprint=call.task_fingerprint,
            requested_mode=call.observation.requested_mode,
            served_mode=call.observation.served_mode,
            ordered_candidate_evidence_ids=(
                call.observation.ordered_candidate_evidence_ids
            ),
            ordered_final_ranked_evidence_ids=(
                call.observation.ordered_final_ranked_evidence_ids
            ),
            rank_changes=dict(call.observation.rank_changes),
            duplicate_drops=None,
        )
        for call in calls
    )

    arm_names: tuple[str, ...] = ()
    arm_top_k: int | None = None
    for call in calls:
        arm_names = tuple(sorted(set(arm_names) | set(call.observation.arm_names)))
        if arm_top_k is None and call.observation.arm_top_k is not None:
            arm_top_k = call.observation.arm_top_k

    arms: tuple[RetrievalArmObservation, ...] = ()
    if arm_names and arm_top_k is not None:
        versions = {
            "lexical": data_runtime_identity.fts_index_version,
            "dense": data_runtime_identity.dense_index_version,
            "fusion": data_runtime_identity.query_policy_version,
            "reranked": data_runtime_identity.reranker_revision,
        }
        arms = tuple(
            RetrievalArmObservation(
                arm=name, version=str(versions.get(name) or "unavailable"), top_k=arm_top_k
            )
            for name in arm_names
        )

    latency_values = [
        call.observation.measured_latency_ms
        for call in calls
        if call.observation.measured_latency_ms is not None
    ]
    degradation_reasons: tuple[str, ...] = tuple(
        sorted(
            {
                str(getattr(degradation, "reason_code", degradation))
                for execution in executions
                for degradation in (getattr(execution, "degradations", ()) or ())
            }
        )
    )
    ticker_violations = _union_preserving_order(
        [call.observation.ticker_violations for call in calls]
    )
    cutoff_violations = _union_preserving_order(
        [call.observation.cutoff_violations for call in calls]
    )
    return RetrievalDiagnostics(
        corpus_manifest_id=str(data_runtime_identity.corpus_manifest_id),
        index_manifest_id=(
            str(data_runtime_identity.dense_index_version)
            if getattr(data_runtime_identity, "dense_index_version", None)
            else None
        ),
        data_runtime_identity_hash=data_runtime_identity_object_hash(
            data_runtime_identity
        ),
        arms=arms,
        ordered_candidate_evidence_ids=_union_preserving_order(
            [call.observation.ordered_candidate_evidence_ids for call in calls]
        ),
        ordered_final_ranked_evidence_ids=_union_preserving_order(
            [call.observation.ordered_final_ranked_evidence_ids for call in calls]
        ),
        per_task=per_task,
        measured_latency_ms=sum(latency_values) if latency_values else None,
        degradation_reasons=degradation_reasons,
        ticker_violations=ticker_violations,
        cutoff_violations=cutoff_violations,
        observed=bool(calls),
    )


def unavailable_retrieval_diagnostics(data_runtime_identity: Any) -> RetrievalDiagnostics:
    """Retrieval diagnostics for a run that never reached retrieval.

    ``observed=False`` means "no retrieval observation exists"; it is never a
    measured "no results" state, so retrieval metrics stay non-scorable.
    """
    return RetrievalDiagnostics(
        corpus_manifest_id=str(data_runtime_identity.corpus_manifest_id),
        index_manifest_id=(
            str(data_runtime_identity.dense_index_version)
            if getattr(data_runtime_identity, "dense_index_version", None)
            else None
        ),
        data_runtime_identity_hash=data_runtime_identity_object_hash(
            data_runtime_identity
        ),
        observed=False,
    )


__all__ = ["build_retrieval_diagnostics", "unavailable_retrieval_diagnostics"]
