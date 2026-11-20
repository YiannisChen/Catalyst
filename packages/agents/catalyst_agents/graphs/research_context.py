"""Shared foundation retrieval and trace recording for V1 graph stages."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity

from catalyst_agents.foundation import FoundationRunResult
from catalyst_agents.observability.decision_trace import DecisionTrace
from catalyst_agents.runtime.control import raise_if_control_expired


@dataclass(frozen=True)
class FoundationGraphInputs:
    """Explicit inputs accepted by the existing foundation graph builder."""

    run_id: str
    round: int
    temporal_identity: TemporalIdentity
    data_runtime_identity: DataRuntimeIdentity
    ticker: str
    cutoff: str
    requested_manifest_id: str
    observation_provider: Any
    retriever: Any
    policy_config: Any
    research_concurrency: int
    research_stage_timeout_seconds: float
    persistence: Any
    packing_policy_version: str
    template_bytes: bytes
    template_version: str
    structured_provider: Any | None
    run_deadline_epoch_ms: int | None
    observation_policy_version: str | None
    move_profile_override: Any | None

    def builder_kwargs(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "round": self.round,
            "temporal_identity": self.temporal_identity,
            "data_runtime_identity": self.data_runtime_identity,
            "ticker": self.ticker,
            "cutoff": self.cutoff,
            "requested_manifest_id": self.requested_manifest_id,
            "observation_provider": self.observation_provider,
            "retriever": self.retriever,
            "policy_config": self.policy_config,
            "research_concurrency": self.research_concurrency,
            "research_stage_timeout_seconds": self.research_stage_timeout_seconds,
            "persistence": self.persistence,
            "packing_policy_version": self.packing_policy_version,
            "template_bytes": self.template_bytes,
            "template_version": self.template_version,
            "structured_provider": self.structured_provider,
            "run_deadline_epoch_ms": self.run_deadline_epoch_ms,
            "observation_policy_version": self.observation_policy_version,
            "move_profile_override": self.move_profile_override,
            "analyst_boundary": None,
            "on_facts_ready": None,
        }


@dataclass(frozen=True)
class FoundationRetrievalStageResult:
    foundation: FoundationRunResult
    duration_ms: int
    inventory_count: int
    included_in_pack: int
    lead_only: int


@dataclass(frozen=True)
class FoundationRetrievalStageInput:
    foundation_inputs: FoundationGraphInputs
    foundation_builder: Callable[..., FoundationRunResult]
    trace: DecisionTrace
    emit: Callable[[str, dict[str, Any]], None]
    stage_name: str
    round: int
    temporal_identity: TemporalIdentity
    control: Any | None
    trace_cascade_stage: int | None = None
    cascade_retrieval_stage: int | None = None


def run_foundation_retrieval_stage(
    stage_input: FoundationRetrievalStageInput,
) -> FoundationRetrievalStageResult:
    """Build a foundation pack, record retrieval trace/events, and check control."""
    foundation_inputs = stage_input.foundation_inputs
    trace = stage_input.trace
    stage = trace.begin(
        stage_input.stage_name,
        round=stage_input.round,
        **({"cascade_stage": stage_input.trace_cascade_stage}
           if stage_input.trace_cascade_stage is not None else {}),
    )
    foundation = stage_input.foundation_builder(**foundation_inputs.builder_kwargs())
    trace.end(stage)
    trace.retrieval(
        evidence_state=foundation.evidence_state,
        context_pack=foundation.context_pack,
        research_tasks=getattr(getattr(foundation, "research_execution", None), "tasks", ()),
    )
    if stage_input.cascade_retrieval_stage == 1:
        trace.payload["cascade_retrievals"] = [{"cascade_stage": 1, **trace.payload["retrieval"]}]
    elif stage_input.cascade_retrieval_stage is not None:
        trace.payload.setdefault("cascade_retrievals", []).append(
            {"cascade_stage": stage_input.cascade_retrieval_stage, **trace.payload["retrieval"]}
        )

    inventory = tuple(foundation.context_pack.evidence_inventory)
    included_ids = set(foundation.context_pack.included_evidence_ids)
    lead_ids = {item.evidence_id for item in foundation.context_pack.lead_only_evidence}
    duration_ms = max(0, int(stage.get("duration_ms", 0)))
    stage_input.emit("retrieval.done", {
        "inventory_count": len(inventory),
        "included_in_pack": len(included_ids),
        "lead_only": len(lead_ids),
        "items": [
            {
                "evidence_id": item.evidence_id,
                "title": None,
                "publisher": item.publisher or str(getattr(item.source_class, "value", item.source_class)),
                "published_at": item.eligible_at.isoformat() if item.eligible_at else None,
                "url": item.canonical_url,
                "phase": "PRE_OPEN" if item.eligible_at <= stage_input.temporal_identity.session_open_at else "SESSION",
                "content_state": str(getattr(item.content_state, "value", item.content_state)),
            }
            for item in inventory[:40]
        ],
        "duration_ms": duration_ms,
    })
    raise_if_control_expired(stage_input.control)
    return FoundationRetrievalStageResult(
        foundation=foundation,
        duration_ms=duration_ms,
        inventory_count=len(inventory),
        included_in_pack=len(included_ids),
        lead_only=len(lead_ids),
    )
