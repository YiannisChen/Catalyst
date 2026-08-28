"""Stage-1 runner (M7-8, Batch-B corrective).

``run_stage1`` executes the ordered case set through the M6 app/SSE boundary
via a caller-supplied runner adapter, captures the pre-submit RunManifest
binding, waits for a terminal event, recomputes the authoritative
RunManifest/result hashes from the serialized artifacts, verifies the
ContextPack/ClaimPlan/Assurance refs, and appends exactly one EvalOutcome
only after the complete ordered case set exists as identity-valid COMPLETED
bindings. Ordered observed bindings match ``ordered_case_ids`` one-to-one;
FAILED/CANCELLED/nonterminal/identity-mismatched bindings never count as
completed success.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from catalyst_eval.v1_1.execution_ledger import ExecutionLedger, LedgerRow
from catalyst_eval.v1_1.loader import canonical_bytes
from catalyst_eval.v1_1.manifest import (
    CaseResultRef,
    EvalArtifactRef,
    EvalManifest,
    EvalOutcome,
    GateResult,
    LatencyTokensCost,
    MetricAggregate,
    RunArtifactIdentity,
    RunManifestBinding,
)

RunnerAdapter = Callable[[str], "CaseRunOutcome"]


class RunArtifactHashError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaseRunOutcome:
    """One terminal case run bound to its authoritative artifacts.

    ``run_manifest_payload`` and ``result_artifact_payload`` are the exact
    serialized artifacts; the authoritative hashes are recomputed from them
    and every mismatch fails closed.
    """

    case_id: str
    run_manifest_id: str
    run_manifest_hash: str
    result_artifact_id: str
    result_artifact_hash: str
    terminal_status: str
    run_manifest_payload: Mapping[str, Any] | None = None
    result_artifact_payload: Mapping[str, Any] | None = None
    context_pack_ref: EvalArtifactRef | Mapping[str, Any] | None = None
    claim_plan_ref: EvalArtifactRef | Mapping[str, Any] | None = None
    assurance_ref: EvalArtifactRef | Mapping[str, Any] | None = None
    provider_calls: int = 0
    cost_usd: float | None = None
    latency_ms: int | None = None
    tokens: int | None = None
    run_facts: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Stage1Report:
    manifest: EvalManifest
    case_outcomes: tuple[CaseRunOutcome, ...]
    aggregate_metrics: tuple[MetricAggregate, ...] = ()
    gate_results: tuple[GateResult, ...] = ()
    ledger: ExecutionLedger | None = None
    report_payload: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        if self.report_payload is not None:
            return self.report_payload
        return {
            "schema_version": "v1_1_stage1_report_v1",
            "eval_id": self.manifest.evaluation_identity.eval_id,
            "case_count": len(self.case_outcomes),
        }


def _coerce_artifact_ref(ref: EvalArtifactRef | Mapping[str, Any] | None) -> EvalArtifactRef | None:
    if ref is None:
        return None
    if isinstance(ref, EvalArtifactRef):
        return ref
    return EvalArtifactRef.model_validate(dict(ref))


def _verify_artifact_hashes(outcome: CaseRunOutcome) -> None:
    """Recompute authoritative hashes from the serialized artifacts."""
    if outcome.run_manifest_payload is None:
        raise RunArtifactHashError(
            f"run_manifest_payload for {outcome.case_id!r} is missing; "
            "authoritative hash cannot be recomputed"
        )
    if outcome.result_artifact_payload is None:
        raise RunArtifactHashError(
            f"result_artifact_payload for {outcome.case_id!r} is missing; "
            "authoritative hash cannot be recomputed"
        )
    actual_manifest = hashlib.sha256(
        canonical_bytes(outcome.run_manifest_payload)
    ).hexdigest()
    if actual_manifest != outcome.run_manifest_hash:
        raise RunArtifactHashError(
            f"run_manifest_hash for {outcome.case_id!r} does not match the "
            f"serialized RunManifest artifact: declared "
            f"{outcome.run_manifest_hash} recomputed {actual_manifest}"
        )
    actual_result = hashlib.sha256(
        canonical_bytes(outcome.result_artifact_payload)
    ).hexdigest()
    if actual_result != outcome.result_artifact_hash:
        raise RunArtifactHashError(
            f"result_artifact_hash for {outcome.case_id!r} does not match the "
            f"serialized result artifact: declared {outcome.result_artifact_hash} "
            f"recomputed {actual_result}"
        )


def validate_terminal_success(outcomes: Sequence[CaseRunOutcome]) -> None:
    """Every outcome must be an identity-valid COMPLETED success binding.

    FAILED, CANCELLED, corrupt, nonterminal, or identity-mismatched rows are
    never counted as completed success (Batch-B corrective).
    """
    for outcome in outcomes:
        if outcome.terminal_status != "COMPLETED":
            raise ValueError(
                f"case {outcome.case_id!r} terminal_status must be COMPLETED to "
                f"count as success, got {outcome.terminal_status!r}"
            )
        if not outcome.run_manifest_id or not outcome.run_manifest_hash:
            raise ValueError(
                f"case {outcome.case_id!r} run manifest binding is missing"
            )


def build_outcome_identity(
    outcomes: Sequence[CaseRunOutcome],
) -> tuple[RunArtifactIdentity, tuple[CaseResultRef, ...]]:
    """Build the observed RunArtifactIdentity and ordered per-case refs.

    The bindings are ordered exactly like the outcomes; refs and bindings
    must agree one-to-one on run_manifest_id+hash (fail closed otherwise).
    """
    bindings: list[RunManifestBinding] = []
    refs: list[CaseResultRef] = []
    context_packs: list[EvalArtifactRef] = []
    claim_plans: list[EvalArtifactRef] = []
    assurances: list[EvalArtifactRef] = []
    seen_manifest_ids: set[str] = set()
    for outcome in outcomes:
        if outcome.run_manifest_id in seen_manifest_ids:
            raise ValueError(
                f"duplicate run_manifest_id {outcome.run_manifest_id!r} across "
                "ordered case bindings; bindings must be unique one-to-one"
            )
        seen_manifest_ids.add(outcome.run_manifest_id)
        pack = _coerce_artifact_ref(outcome.context_pack_ref)
        plan = _coerce_artifact_ref(outcome.claim_plan_ref)
        assurance = _coerce_artifact_ref(outcome.assurance_ref)
        if pack is None or plan is None or assurance is None:
            raise ValueError(
                f"case {outcome.case_id!r} is missing a required "
                "ContextPack/ClaimPlan/Assurance artifact ref"
            )
        context_packs.append(pack)
        claim_plans.append(plan)
        assurances.append(assurance)
        bindings.append(
            RunManifestBinding(
                run_manifest_id=outcome.run_manifest_id,
                run_manifest_hash=outcome.run_manifest_hash,
            )
        )
        refs.append(
            CaseResultRef(
                case_id=outcome.case_id,
                run_manifest_id=outcome.run_manifest_id,
                run_manifest_hash=outcome.run_manifest_hash,
                result_artifact_id=outcome.result_artifact_id,
            )
        )
    return (
        RunArtifactIdentity(
            run_manifest_bindings=tuple(bindings),
            context_pack_refs=tuple(context_packs),
            claim_plan_refs=tuple(claim_plans),
            assurance_refs=tuple(assurances),
        ),
        tuple(refs),
    )


def case_outcome_to_run_facts(outcome: CaseRunOutcome) -> dict[str, Any]:
    """Serialize the sealed run facts for the execution ledger/report.

    These are the observed, immutable run facts joined by audit/report; they
    are never derived from gold.
    """
    return {
        "schema_version": "v1_1_stage1_run_facts_v1",
        "case_id": outcome.case_id,
        "output_status": outcome.terminal_status,
        "provider_calls": outcome.provider_calls,
        "cost_usd": outcome.cost_usd,
        "latency_ms": outcome.latency_ms,
        "tokens": outcome.tokens,
    }


def run_stage1(
    manifest: EvalManifest,
    cases: Mapping[str, Any],
    *,
    runner_adapter: RunnerAdapter,
    metrics_fn: Callable[
        [EvalManifest, Sequence[CaseRunOutcome]],
        tuple[Sequence[MetricAggregate], Sequence[GateResult]],
    ],
    completed_at: datetime | None = None,
) -> Stage1Report:
    """Run the ordered case set and append exactly one outcome.

    ``metrics_fn`` computes aggregate metrics and gates from the sealed
    outcomes; the outcome is appended only after every ordered case has a
    terminal, hash-verified COMPLETED binding whose ContextPack/ClaimPlan/
    Assurance refs are present.
    """
    ordered_ids = list(manifest.evaluation_identity.ordered_case_ids)
    if set(ordered_ids) != set(cases):
        raise ValueError(
            "cases must cover the manifest ordered_case_ids one-to-one"
        )

    outcomes: list[CaseRunOutcome] = []
    for case_id in ordered_ids:
        outcome = runner_adapter(case_id)
        if outcome.case_id != case_id:
            raise ValueError(
                f"runner adapter returned outcome for {outcome.case_id!r} "
                f"while executing {case_id!r}"
            )
        _verify_artifact_hashes(outcome)
        outcomes.append(outcome)
    validate_terminal_success(outcomes)

    aggregates, gates = metrics_fn(manifest, outcomes)
    observed_identity, refs = build_outcome_identity(outcomes)
    total_cost = sum(outcome.cost_usd or 0.0 for outcome in outcomes)
    outcome = EvalOutcome(
        completed_at=completed_at or datetime.now(timezone.utc),
        observed_run_artifact_identity=observed_identity,
        per_case_result_refs=refs,
        aggregate_metrics=tuple(aggregates),
        latency_tokens_cost=LatencyTokensCost(
            total_latency_ms=sum(outcome.latency_ms or 0 for outcome in outcomes),
            total_tokens=sum(outcome.tokens or 0 for outcome in outcomes),
            total_cost=total_cost,
        ),
        gate_results=tuple(gates),
    )
    completed = manifest.append_outcome(outcome)
    ledger = ExecutionLedger(
        rows=tuple(
            LedgerRow(
                eval_id=manifest.evaluation_identity.eval_id,
                case_id=outcome.case_id,
                run_manifest_id=outcome.run_manifest_id,
                run_manifest_hash=outcome.run_manifest_hash,
                result_artifact_id=outcome.result_artifact_id,
                result_artifact_hash=outcome.result_artifact_hash,
                terminal_status=outcome.terminal_status,
                attempts=1,
                provider_calls=outcome.provider_calls,
                cost_usd=outcome.cost_usd,
                checksum="",
                run_facts=case_outcome_to_run_facts(outcome),
            )
            for outcome in outcomes
        )
    )
    return Stage1Report(
        manifest=completed,
        case_outcomes=tuple(outcomes),
        aggregate_metrics=tuple(aggregates),
        gate_results=tuple(gates),
        ledger=ledger,
    )


__all__ = [
    "CaseRunOutcome",
    "RunArtifactHashError",
    "RunnerAdapter",
    "Stage1Report",
    "build_outcome_identity",
    "case_outcome_to_run_facts",
    "run_stage1",
    "validate_terminal_success",
]
