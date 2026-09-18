"""Versioned, production-neutral run diagnostics (M7 Phase A observability).

The V1.1 runtime already observes the facts below while executing one run
(retrieval execution, corrective rounds, bounded provider retries, terminal
validation). This module is the single typed contract that carries them out of
``run_v1_graph`` so the M6 app runtime can persist them as a versioned
artifact bound to the run's terminal sequence.

Design rules (M7 Phase A corrective):

* Only objectively observed facts are represented. A field that the runtime
  cannot observe is a typed "unavailable" marker, never a defaulted value.
* Nothing here depends on harness or benchmark concepts; the evaluation
  harness maps these neutral facts onto its own metric inputs.
* ``build_run_diagnostics`` derives every value from the already-computed
  ``V1RunResult`` (itself derived from the graph's real observations). It never
  invents a value for something the run did not observe.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DIAGNOSTICS_SCHEMA_VERSION = "v1.1_run_diagnostics_v1"

# How the recorded provider cost was obtained:
#   reported              - the provider reported actual token usage/cost
#   upper_bound_charged   - actual usage was unavailable; the conservative
#                           pre-call upper bound was charged (never under-charged)
#   unavailable           - no cost bound was computable (no price registered)
CostMethod = Literal["reported", "upper_bound_charged", "unavailable"]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


class RetrievalArmObservation(BaseModel):
    """One retrieval arm as the runtime actually served it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: str
    version: str
    top_k: int = Field(ge=1)


class RetrievalTaskObservation(BaseModel):
    """Per-task retrieval observation captured at the retrieval boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    task_fingerprint: str
    requested_mode: str
    served_mode: str | None = None
    ordered_candidate_evidence_ids: tuple[str, ...] = ()
    ordered_final_ranked_evidence_ids: tuple[str, ...] = ()
    rank_changes: dict[str, int] = Field(default_factory=dict)
    degradation_reasons: tuple[str, ...] = ()
    # The V1.1 retrieval contract exposes no post-dedup drop list. The field is
    # typed ``None`` so it can never be read as "observed: no duplicates were
    # dropped"; a future contract that exposes drops must change this type.
    duplicate_drops: None = None

    @model_validator(mode="after")
    def _rank_changes_are_displacements(self) -> "RetrievalTaskObservation":
        for evidence_id, delta in self.rank_changes.items():
            if not evidence_id:
                raise ValueError("rank_changes keys must be non-empty evidence ids")
            if delta == 0:
                raise ValueError("rank_changes must only record actual displacement")
        return self


class RetrievalDiagnostics(BaseModel):
    """Run-level retrieval facts observed by the runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_manifest_id: str
    index_manifest_id: str | None = None
    data_runtime_identity_hash: str
    arms: tuple[RetrievalArmObservation, ...] = ()
    ordered_candidate_evidence_ids: tuple[str, ...] = ()
    ordered_final_ranked_evidence_ids: tuple[str, ...] = ()
    per_task: tuple[RetrievalTaskObservation, ...] = ()
    measured_latency_ms: int | None = Field(default=None, ge=0)
    degradation_reasons: tuple[str, ...] = ()
    ticker_violations: tuple[str, ...] = ()
    cutoff_violations: tuple[str, ...] = ()
    # False when the run performed no retrieval (e.g. an immediate failure);
    # consumers must then treat retrieval metrics as non-scorable rather than
    # reading a zero as a measured result.
    observed: bool = True


class EvidenceDeltaObservation(BaseModel):
    """Evidence added/removed by one executed corrective round."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    round: int = Field(ge=1)
    added_evidence_ids: tuple[str, ...] = ()
    removed_evidence_ids: tuple[str, ...] = ()


class CorrectiveRoundObservation(BaseModel):
    """One corrective round the runtime actually executed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    round: int = Field(ge=1)
    gap_ids: tuple[str, ...] = ()
    # Code-owned deterministic gap reason codes for the gaps this round
    # actually addressed; these are the comparable surface, the opaque
    # ``gap_id`` identity is not.
    gap_reason_codes: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    evidence_needs: tuple[str, ...] = ()
    time_scopes: tuple[str, ...] = ()
    research_fingerprints: tuple[str, ...] = ()
    evidence_delta: EvidenceDeltaObservation | None = None


class TrajectoryDiagnostics(BaseModel):
    """Observed trajectory facts: what the runtime did, not what it should do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    corrective_rounds_executed: int = Field(ge=0)
    initial_task_ids: tuple[str, ...] = ()
    initial_task_fingerprints: tuple[str, ...] = ()
    rounds: tuple[CorrectiveRoundObservation, ...] = ()

    @model_validator(mode="after")
    def _rounds_match_executed(self) -> "TrajectoryDiagnostics":
        if len(self.rounds) != self.corrective_rounds_executed:
            raise ValueError(
                "corrective_rounds_executed must equal the number of recorded "
                "rounds"
            )
        return self


class ProviderAccountingDiagnostics(BaseModel):
    """Observed provider accounting for one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    analyst_logical_calls: int = Field(ge=0)
    analyst_provider_attempts: int = Field(ge=0)
    writer_logical_calls: int = Field(ge=0)
    writer_provider_attempts: int = Field(ge=0)
    total_tokens_in: int | None = Field(default=None, ge=0)
    total_tokens_out: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    cost_method: CostMethod = "unavailable"

    @property
    def provider_calls(self) -> int:
        """Provider calls consumed = analyst + writer provider attempts."""
        return self.analyst_provider_attempts + self.writer_provider_attempts

    @model_validator(mode="after")
    def _cost_method_is_consistent(self) -> "ProviderAccountingDiagnostics":
        if self.cost_usd is not None and self.cost_method == "unavailable":
            raise ValueError("a recorded cost requires a real cost method")
        if self.cost_usd is None and self.cost_method != "unavailable":
            raise ValueError("cost_method must be 'unavailable' without a cost")
        return self


class TerminalDiagnostics(BaseModel):
    """Deterministic terminal status observed by the runtime.

    ``refusal_reason`` is the runtime's own typed status reason. The Stage-1
    benchmark's human refusal reasons are free prose and are therefore NOT
    comparable to any runtime string; ``refusal_reason_available`` records
    whether a machine-comparable reason exists at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_status: str
    attribution_type: str | None = None
    terminal_event_type: str
    status_ceiling: str | None = None
    refusal_reason: str | None = None
    refusal_reason_available: bool = False

    @model_validator(mode="after")
    def _refusal_availability_is_consistent(self) -> "TerminalDiagnostics":
        if self.refusal_reason is not None:
            if not self.refusal_reason_available:
                raise ValueError(
                    "a recorded refusal_reason requires refusal_reason_available"
                )
            if self.result_status != "ABSTAIN":
                raise ValueError(
                    "a refusal_reason is only defined for an ABSTAIN result"
                )
        return self


class RunDiagnostics(BaseModel):
    """The complete, versioned diagnostics artifact for one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = DIAGNOSTICS_SCHEMA_VERSION
    run_id: str
    data_runtime_identity_ref: str
    data_runtime_identity_hash: str
    recorded_at: datetime
    retrieval: RetrievalDiagnostics
    trajectory: TrajectoryDiagnostics
    provider: ProviderAccountingDiagnostics
    terminal: TerminalDiagnostics

    @model_validator(mode="after")
    def _schema_version(self) -> "RunDiagnostics":
        if self.schema_version != DIAGNOSTICS_SCHEMA_VERSION:
            raise ValueError(
                f"diagnostics schema_version must be {DIAGNOSTICS_SCHEMA_VERSION}"
            )
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware")
        return self

    def content_sha256(self) -> str:
        """Canonical content hash used to bind the persisted artifact."""
        return hashlib.sha256(_canonical_bytes(self.model_dump(mode="json"))).hexdigest()


# ---------------------------------------------------------------------------
# Derivation from the already-computed run result
# ---------------------------------------------------------------------------

def data_runtime_identity_object_hash(identity: Any) -> str:
    """Canonical object hash of a DataRuntimeIdentity.

    This is deliberately distinct from the SHA-256 of the Q-001 SQLite file.
    """
    return hashlib.sha256(
        _canonical_bytes(identity.model_dump(mode="json"))
    ).hexdigest()


def build_run_diagnostics(
    *,
    result: Any,
    run_id: str,
    retrieval: RetrievalDiagnostics,
    provider: ProviderAccountingDiagnostics,
    data_runtime_identity_ref: str,
    data_runtime_identity: Any,
    recorded_at: datetime | None = None,
) -> RunDiagnostics:
    """Assemble the diagnostics artifact from real observations only.

    ``retrieval`` and ``provider`` are captured at their own boundaries (the
    retrieval adapter and the provider budget guard) and are passed in
    unchanged. The trajectory/terminal facts come from the computed
    ``V1RunResult``, which is itself derived from the graph's real
    observations; nothing is invented here.
    """
    validated = getattr(result, "validated_claim_plan", None)
    assessment = getattr(result, "assessment", None)
    result_status = str(
        getattr(getattr(validated, "status", None), "value", "") or ""
    )
    attribution_type = getattr(
        getattr(validated, "attribution_type", None), "value", None
    )
    status_ceiling = getattr(getattr(assessment, "status_ceiling", None), "value", None)

    refusal_reason = getattr(result, "refusal_reason", None)
    terminal = TerminalDiagnostics(
        result_status=result_status or "UNKNOWN",
        attribution_type=attribution_type,
        terminal_event_type="run.completed",
        status_ceiling=status_ceiling,
        refusal_reason=refusal_reason,
        refusal_reason_available=bool(
            getattr(result, "refusal_reason_available", False)
        ),
    )
    trajectory = TrajectoryDiagnostics(
        corrective_rounds_executed=int(getattr(result, "corrective_rounds", 0)),
        initial_task_ids=tuple(getattr(result, "initial_task_ids", ()) or ()),
        initial_task_fingerprints=tuple(
            getattr(result, "initial_task_fingerprints", ()) or ()
        ),
        rounds=tuple(getattr(result, "corrective_round_observations", ()) or ()),
    )
    return RunDiagnostics(
        run_id=run_id,
        data_runtime_identity_ref=data_runtime_identity_ref,
        data_runtime_identity_hash=data_runtime_identity_object_hash(
            data_runtime_identity
        ),
        recorded_at=recorded_at or datetime.now(timezone.utc),
        retrieval=retrieval,
        trajectory=trajectory,
        provider=provider,
        terminal=terminal,
    )


__all__ = [
    "DIAGNOSTICS_SCHEMA_VERSION",
    "CorrectiveRoundObservation",
    "CostMethod",
    "EvidenceDeltaObservation",
    "ProviderAccountingDiagnostics",
    "RetrievalArmObservation",
    "RetrievalDiagnostics",
    "RetrievalTaskObservation",
    "RunDiagnostics",
    "TerminalDiagnostics",
    "TrajectoryDiagnostics",
    "build_run_diagnostics",
    "data_runtime_identity_object_hash",
]
