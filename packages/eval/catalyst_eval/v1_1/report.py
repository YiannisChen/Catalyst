"""Stage-1 report write-once publication (M7-8).

Report JSON is serialized with the M7 canonical bytes and published
write-once: an absent target is created atomically, identical bytes are
idempotent, and differing bytes raise a conflict without overwrite. Markdown
is a deterministic rendering of the JSON payload and cannot supply new
facts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_eval.v1_1.loader import (
    PublicationConflictError as ReportConflictError,
    canonical_bytes,
    write_once_bytes,
)

REPORT_SCHEMA_VERSION = "v1_1_stage1_report_v1"

_SECRET_PATTERNS = (
    "sk-",
    "Bearer ",
    "api_key",
    "authorization",
)


def _sum_known_metrics(values: Sequence[Any]) -> int | float | None:
    """Sum a metric only when every contributing run recorded it."""
    if any(value is None for value in values):
        return None
    return sum(values)


def build_report_payload(
    *,
    eval_manifest: Any,
    gold_cases: Sequence[Any],
    stratification: Mapping[str, Any],
    ledger: Any,
    audits: Sequence[Any],
    execution_head_sha8: str,
    max_provider_calls: int | None = None,
    max_cost_usd: float | None = None,
) -> dict[str, Any]:
    """Compute the real Stage-1 report payload from sealed inputs only.

    Joins the sealed execution ledger run facts with the sealed human audit
    and computes real retrieval/attribution/trajectory gates. It never
    fabricates empty gates or counts: missing ledger/audit rows fail closed.
    Q-002 is MANDATORY NON-COMPARABLE while the promoted environment tuple is
    unrecovered.
    """
    from catalyst_eval.v1_1.attribution_metrics import (
        RunAttributionOutput,
        RunClaimOutput,
        compute_attribution_metrics,
    )
    from catalyst_eval.v1_1.retrieval_metrics import (
        RetrievalResult,
        compute_retrieval_metrics,
    )
    from catalyst_eval.v1_1.trajectory_metrics import (
        RunTrajectoryFacts,
        compute_trajectory_metrics,
    )

    eval_id = eval_manifest.evaluation_identity.eval_id
    ordered_ids = list(eval_manifest.evaluation_identity.ordered_case_ids)
    gold_by_case = {case.case_id: case for case in gold_cases}
    if set(ordered_ids) != set(gold_by_case):
        raise ValueError("gold cases must cover the eval manifest case ids")
    coverage_by_case = {
        case_id: bool((stratification.get("per_case") or {}).get(case_id, {}).get("coverage_limited"))
        for case_id in ordered_ids
    }

    completed_rows = [
        row
        for row in ledger.rows
        if row.eval_id == eval_id
        and row.terminal_status == "COMPLETED"
        and row.identity_valid
        and row.run_facts is not None
    ]
    completed_ids = [row.case_id for row in completed_rows]
    duplicate_ids = sorted(
        case_id
        for case_id in set(completed_ids)
        if completed_ids.count(case_id) > 1
    )
    if duplicate_ids:
        raise ValueError(
            "report requires exactly one authoritative COMPLETED row per case; "
            f"duplicates {duplicate_ids}"
        )
    rows = {row.case_id: row for row in completed_rows}
    if set(rows) != set(ordered_ids):
        missing = sorted(set(ordered_ids) - set(rows))
        raise ValueError(
            "report requires one identity-valid COMPLETED run per ordered case; "
            f"missing {missing}"
        )

    run_outputs: list[RunAttributionOutput] = []
    trajectory_facts: list[RunTrajectoryFacts] = []
    retrieval_results: list[RetrievalResult] = []
    provider_calls = 0
    cost_values: list[float] = []
    latency_values: list[int | None] = []
    token_values: list[int | None] = []
    for case_id in ordered_ids:
        row = rows[case_id]
        facts = row.run_facts
        from catalyst_eval.v1_1.run_facts import validate_run_facts

        validated = validate_run_facts(
            facts,
            expected_case_id=case_id,
            row_provider_calls=row.provider_calls,
        )
        claims = tuple(
            RunClaimOutput(
                claim_id=claim.claim_id,
                material=claim.material,
                citation_ids=tuple(claim.citation_ids),
                role=claim.role,
                statement=claim.statement,
            )
            for claim in validated.claims
        )
        run_outputs.append(
            RunAttributionOutput(
                case_id=case_id,
                output_status=validated.output_status,
                attribution_type=validated.attribution_type,
                refusal_reason=validated.refusal_reason,
                refusal_reason_available=validated.refusal_reason_available,
                claims=claims,
                sanity_tasks_completed=tuple(validated.sanity_tasks_completed),
                latency_ms=validated.latency_ms,
                tokens=validated.tokens,
                cost_usd=validated.cost_usd,
                coverage_limited=coverage_by_case[case_id],
                model_limited=validated.model_limited,
            )
        )
        trajectory = validated.trajectory
        trajectory_facts.append(
            RunTrajectoryFacts(
                case_id=case_id,
                corrective_triggered=trajectory.corrective_triggered,
                # Observed code-owned gap reason codes and action identities.
                gap_ids=tuple(trajectory.gap_reason_codes),
                corrective_actions=tuple(trajectory.corrective_actions),
                rounds_executed=trajectory.rounds_executed,
                produced_structure=trajectory.produced_structure,
            )
        )
        retrieval = validated.retrieval
        if not retrieval.observed:
            raise ValueError(
                f"run facts for {case_id!r} record no observed retrieval; the "
                "run cannot be scored for retrieval and must not be reported as "
                "a measured zero"
            )
        pool_data = retrieval.pool
        if not isinstance(pool_data, dict):
            raise ValueError(
                f"run facts for {case_id!r} are missing the observed pool manifest"
            )
        from catalyst_eval.benchmark.pool_manifest import PoolManifest

        retrieval_results.append(
            RetrievalResult(
                case_id=case_id,
                pool=PoolManifest.model_validate(pool_data),
                ranked_evidence_ids=tuple(retrieval.ranked_evidence_ids),
                reranker_contributed=retrieval.reranker_contributed,
                latency_ms=retrieval.latency_ms,
                degraded=retrieval.degraded,
                ticker_violations=tuple(retrieval.ticker_violations),
                cutoff_violations=tuple(retrieval.cutoff_violations),
            )
        )
        if row.cost_usd is None or validated.cost_usd is None:
            raise ValueError(
                f"report cannot publish with unknown provider cost for {case_id!r}"
            )
        if validated.cost_usd != row.cost_usd:
            raise ValueError(
                f"run facts and ledger cost disagree for {case_id!r}"
            )
        provider_calls += row.provider_calls
        cost_values.append(float(validated.cost_usd))
        latency_values.append(validated.latency_ms)
        token_values.append(validated.tokens)

    audits_by_case = {audit.case_id: audit for audit in audits}
    if set(audits_by_case) != set(ordered_ids):
        raise ValueError("audits must cover the ordered cases one-to-one")
    ordered_audits = [audits_by_case[case_id] for case_id in ordered_ids]

    attribution = compute_attribution_metrics(run_outputs, ordered_audits, gold_cases)
    trajectory = compute_trajectory_metrics(trajectory_facts, gold_cases)
    retrieval = compute_retrieval_metrics(retrieval_results, gold_cases, top_k=8)

    gates: dict[str, bool | None] = {}
    gates.update(attribution.gates)
    gates.update(retrieval.gates)
    # Trajectory useful/unnecessary rates are development signals, not hard
    # gates; they never decide the passing seal.
    hard_gates = dict(gates)
    passed = bool(hard_gates) and all(
        value is True for value in hard_gates.values()
    )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "eval_id": eval_id,
        "dataset_id": eval_manifest.evaluation_identity.dataset_id,
        "execution_head_sha8": execution_head_sha8,
        "case_count": len(ordered_ids),
        "comparability": "NON-COMPARABLE",
        "comparability_reason": "q_002_promoted_environment_tuple_unrecovered",
        "max_provider_calls": max_provider_calls,
        "max_cost_usd": max_cost_usd,
        "provider_calls": provider_calls,
        "cost_usd": (
            round(float(cost_total), 6)
            if (cost_total := _sum_known_metrics(cost_values)) is not None
            else None
        ),
        "latency_ms": _sum_known_metrics(latency_values),
        "tokens": _sum_known_metrics(token_values),
        "coverage_limited_count": attribution.coverage_limited_count,
        "model_limited_count": attribution.model_limited_count,
        "sealed_audit": True,
        "audit_rows": len(ordered_audits),
        "hard_gates": hard_gates,
        "gates_passed": passed,
        "attribution_metrics": attribution.as_dict(),
        "trajectory_metrics": trajectory.as_dict(),
        "retrieval_metrics": retrieval.as_dict(),
    }


def report_bytes(payload: Mapping[str, Any]) -> bytes:
    return canonical_bytes(payload)


def write_report_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Write-once canonical JSON publication with conflict detection."""
    write_once_bytes(Path(path), report_bytes(payload))


def write_report_markdown(path: str | Path, markdown: str) -> None:
    """Write deterministic Markdown through the same write-once primitive."""
    write_once_bytes(Path(path), markdown.encode("utf-8"))


def render_report_markdown(payload: Mapping[str, Any]) -> str:
    """Deterministic markdown rendering; never adds facts beyond JSON."""
    lines = [
        f"# {payload.get('schema_version', REPORT_SCHEMA_VERSION)}",
        "",
        f"- eval_id: `{payload.get('eval_id', '')}`",
        f"- dataset_id: `{payload.get('dataset_id', '')}`",
        f"- execution_head: `{payload.get('execution_head_sha8', '')}`",
        f"- comparability: `{payload.get('comparability', '')}`",
        f"- gates_passed: `{payload.get('gates_passed', '')}`",
        "",
        "## Hard gates",
    ]
    gates = payload.get("hard_gates") or {}
    for gate_id, passed in sorted(gates.items()):
        lines.append(f"- `{gate_id}`: {passed}")
    lines.append("")
    lines.append("## Counts")
    for key in ("coverage_limited_count", "model_limited_count", "case_count",
                "provider_calls", "cost_usd", "latency_ms", "tokens"):
        if key in payload:
            lines.append(f"- {key}: {payload[key]}")
    lines.append("")
    lines.append("_Deterministic rendering of the canonical report JSON._")
    return "\n".join(lines) + "\n"


def scan_report_for_secrets(
    payload: Mapping[str, Any],
    *,
    secret_values: Sequence[str] = (),
) -> list[str]:
    """Scan a report payload for secret-shaped text or configured values."""
    findings: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, str):
            lowered = value.lower()
            if any(pattern.lower() in lowered for pattern in _SECRET_PATTERNS):
                findings.append(f"{path}: secret-shaped text")
            if any(secret and secret in value for secret in secret_values):
                findings.append(f"{path}: configured secret value")

    walk(payload, "")
    return findings


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "ReportConflictError",
    "build_report_payload",
    "render_report_markdown",
    "report_bytes",
    "scan_report_for_secrets",
    "write_report_json",
    "write_report_markdown",
]
