"""M4-4: cumulative EvidenceState builder (amendment §4).

The locked construction context carries run/round identities, the exact
TemporalIdentity and DataRuntimeIdentity, the research policy version,
ordered ResearchTaskResults, and an optional prior EvidenceState. The builder
performs an immutable cumulative union by evidence/fact ID: round two equals
union(round one, new), first_seen_round is preserved, same evidence IDs merge
contributions, distinct chunks remain distinct, syndicated assets stay
distinct assets in one group, structured facts and text evidence retain
separate identity partitions, and conflicting immutable metadata fails
integrity validation. The canonical state hash excludes only ``state_hash``.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from catalyst_agents.attribution.evidence_state import (
    EvidenceState,
    EvidenceStateItem,
    ResearchTaskResult,
    compute_state_hash,
)
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity

EVIDENCE_STATE_SCHEMA_VERSION = "evidence_state_v1"


class EvidenceIntegrityError(RuntimeError):
    """Identity or immutable-metadata conflict: the run fails, never degrades."""


class EvidenceStateBuildContext(BaseModel):
    """Locked construction context (M4-4 amendment §4).

    ``prior`` may be None for round one. All result-set identities must equal
    the construction context and the prior state; any mismatch is an integrity
    failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    round: int
    temporal_identity: TemporalIdentity
    data_runtime_identity: DataRuntimeIdentity
    research_policy_version: str
    task_results: tuple[ResearchTaskResult, ...] = ()
    prior: EvidenceState | None = None

    @model_validator(mode="after")
    def _round_positive(self) -> "EvidenceStateBuildContext":
        if self.round < 1:
            raise ValueError("round must be positive")
        return self


def _integrity(message: str) -> EvidenceIntegrityError:
    return EvidenceIntegrityError(message)


def _validate_identity(
    context: EvidenceStateBuildContext,
) -> None:
    for result in context.task_results:
        if result.data_runtime_identity != context.data_runtime_identity:
            raise _integrity(
                f"task {result.task_id!r} runtime identity does not match the "
                "construction context"
            )
    prior = context.prior
    if prior is None:
        return
    if prior.run_id != context.run_id:
        raise _integrity("prior state run_id does not match the construction context")
    if prior.temporal_identity != context.temporal_identity:
        raise _integrity(
            "prior state TemporalIdentity does not match the construction context"
        )
    if prior.data_runtime_identity != context.data_runtime_identity:
        raise _integrity(
            "prior state DataRuntimeIdentity does not match the construction context"
        )
    if prior.round >= context.round:
        raise _integrity(
            "prior state round must precede the construction round"
        )
    if prior.research_policy_version != context.research_policy_version:
        raise _integrity(
            "prior state research policy version does not match the construction context"
        )


def _normalize_item(item: EvidenceStateItem) -> EvidenceStateItem:
    """Deterministic normalization: sorted task/contribution provenance so task
    completion order never changes the final bytes/hash."""
    payload = item.model_dump(mode="python")
    payload["contributing_task_ids"] = tuple(sorted(set(item.contributing_task_ids)))
    payload["retrieval_contributions"] = tuple(
        sorted(
            item.retrieval_contributions,
            key=lambda c: (c.task_id, c.original_rank if c.original_rank is not None else 0),
        )
    )
    return EvidenceStateItem.model_validate(payload)


def _union_task_results(
    prior_results: tuple[ResearchTaskResult, ...],
    new_results: tuple[ResearchTaskResult, ...],
) -> tuple[ResearchTaskResult, ...]:
    by_id: dict[str, ResearchTaskResult] = {}
    for result in (*prior_results, *new_results):
        by_id[result.task_id] = result
    return tuple(sorted(by_id.values(), key=lambda r: (r.priority, r.task_id)))


def build_evidence_state(context: EvidenceStateBuildContext) -> EvidenceState:
    """Build the immutable cumulative EvidenceState for one round."""
    _validate_identity(context)
    prior = context.prior
    if prior is None:
        state = EvidenceState(
            schema_version=EVIDENCE_STATE_SCHEMA_VERSION,
            run_id=context.run_id,
            round=context.round,
            temporal_identity=context.temporal_identity,
            data_runtime_identity=context.data_runtime_identity,
            research_policy_version=context.research_policy_version,
            task_results=(),
            evidence_items=(),
            structured_facts=(),
            degradations=(),
            capability_gaps=(),
            state_hash="0" * 64,
        )
    else:
        state = EvidenceState.model_validate(
            {
                **prior.model_dump(mode="python"),
                "round": context.round,
                "task_results": (),
                "state_hash": "0" * 64,
            }
        )
    for result in sorted(
        context.task_results, key=lambda item: (item.priority, item.task_id)
    ):
        for item in result.evidence_items:
            state = state.upsert(_normalize_item(item))
        for item in result.structured_facts:
            state = state.upsert(_normalize_item(item))
    normalized_results = _union_task_results(state.task_results, context.task_results)
    payload = {
        **state.model_dump(mode="python"),
        "task_results": normalized_results,
        "round": context.round,
        "state_hash": "0" * 64,
    }
    rebuilt = EvidenceState.model_validate(payload)
    return rebuilt.model_validate(
        {**rebuilt.model_dump(mode="python"), "state_hash": compute_state_hash(rebuilt)}
    )


__all__ = [
    "EvidenceIntegrityError",
    "EvidenceStateBuildContext",
    "build_evidence_state",
]
