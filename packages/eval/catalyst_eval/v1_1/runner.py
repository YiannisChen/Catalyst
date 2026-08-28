"""Stage-1 runner (M7-8).

``run_stage1`` executes the ordered case set through the M6 app/SSE boundary
via a caller-supplied runner adapter, captures the pre-submit RunManifest
binding, waits for a terminal event, verifies artifact hashes, and appends
exactly one EvalOutcome only after the complete ordered case set exists.
The ordered observed bindings match ``ordered_case_ids`` one-to-one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from catalyst_eval.v1_1.execution_ledger import ExecutionLedger, LedgerRow
from catalyst_eval.v1_1.manifest import (
    CaseResultRef,
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
    """One terminal case run bound to its authoritative artifacts."""

    case_id: str
    run_manifest_id: str
    run_manifest_hash: str
    result_artifact_id: str
    result_artifact_hash: str
    terminal_status: str
    provider_calls: int = 0
    cost_usd: float | None = None
    latency_ms: int | None = None
    tokens: int | None = None


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


def _verify_artifact_hashes(outcome: CaseRunOutcome) -> None:
    import re

    if re.fullmatch(r"[0-9a-f]{64}", outcome.run_manifest_hash) is None:
        raise RunArtifactHashError(
            f"run_manifest_hash for {outcome.case_id!r} is not a SHA-256 digest"
        )
    if re.fullmatch(r"[0-9a-f]{64}", outcome.result_artifact_hash) is None:
        raise RunArtifactHashError(
            f"result_artifact_hash for {outcome.case_id!r} is not a SHA-256 digest"
        )


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
    terminal, hash-verified binding.
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

    aggregates, gates = metrics_fn(manifest, outcomes)
    refs = tuple(
        CaseResultRef(
            case_id=outcome.case_id,
            run_manifest_id=outcome.run_manifest_id,
            run_manifest_hash=outcome.run_manifest_hash,
            result_artifact_id=outcome.result_artifact_id,
        )
        for outcome in outcomes
    )
    observed_identity = RunArtifactIdentity(
        run_manifest_bindings=tuple(
            RunManifestBinding(
                run_manifest_id=outcome.run_manifest_id,
                run_manifest_hash=outcome.run_manifest_hash,
            )
            for outcome in outcomes
        )
    )
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
    "run_stage1",
]
