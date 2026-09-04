"""Stage-1 report write-once publication (M7-8).

Report JSON is serialized with the M7 canonical bytes and published
write-once: an absent target is created atomically, identical bytes are
idempotent, and differing bytes raise a conflict without overwrite. Markdown
is a deterministic rendering of the JSON payload and cannot supply new
facts.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_eval.v1_1.loader import canonical_bytes

REPORT_SCHEMA_VERSION = "v1_1_stage1_report_v1"

_SECRET_PATTERNS = (
    "sk-",
    "Bearer ",
    "api_key",
    "authorization",
)


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
    cost_total = 0.0
    latency_total = 0
    tokens_total = 0
    for case_id in ordered_ids:
        row = rows[case_id]
        facts = row.run_facts
        if facts.get("schema_version") != "v1_1_stage1_run_facts_v1":
            raise ValueError(
                f"run facts for {case_id!r} have an unknown schema "
                f"{facts.get('schema_version')!r}"
            )
        claims = tuple(
            RunClaimOutput(
                claim_id=str(claim["claim_id"]),
                material=bool(claim.get("material", True)),
                citation_ids=tuple(claim.get("citation_ids") or ()),
                role=str(claim.get("role") or "PRIMARY"),
                statement=claim.get("statement"),
            )
            for claim in facts.get("claims") or ()
        )
        run_outputs.append(
            RunAttributionOutput(
                case_id=case_id,
                output_status=str(facts.get("output_status") or "UNKNOWN"),
                attribution_type=facts.get("attribution_type"),
                refusal_reason=facts.get("refusal_reason"),
                claims=claims,
                sanity_tasks_completed=tuple(facts.get("sanity_tasks_completed") or ()),
                latency_ms=facts.get("latency_ms"),
                tokens=facts.get("tokens"),
                cost_usd=facts.get("cost_usd"),
                coverage_limited=coverage_by_case[case_id],
                model_limited=bool(facts.get("model_limited")),
            )
        )
        trajectory = facts.get("trajectory") or {}
        trajectory_facts.append(
            RunTrajectoryFacts(
                case_id=case_id,
                corrective_triggered=bool(trajectory.get("corrective_triggered")),
                gap_ids=tuple(trajectory.get("gap_ids") or ()),
                corrective_actions=tuple(trajectory.get("corrective_actions") or ()),
                stop_correct=bool(trajectory.get("stop_correct", True)),
                new_structure_created=bool(trajectory.get("new_structure_created")),
                corrected=bool(trajectory.get("corrected")),
            )
        )
        retrieval = facts.get("retrieval") or {}
        pool_data = retrieval.get("pool")
        if not isinstance(pool_data, dict):
            raise ValueError(
                f"run facts for {case_id!r} are missing the serialized pool manifest"
            )
        from catalyst_eval.benchmark.pool_manifest import PoolManifest

        retrieval_results.append(
            RetrievalResult(
                case_id=case_id,
                pool=PoolManifest.model_validate(pool_data),
                ranked_evidence_ids=tuple(retrieval.get("ranked_evidence_ids") or ()),
                reranker_contributed=bool(retrieval.get("reranker_contributed")),
                latency_ms=retrieval.get("latency_ms"),
                degraded=bool(retrieval.get("degraded")),
                ticker_violations=tuple(retrieval.get("ticker_violations") or ()),
                cutoff_violations=tuple(retrieval.get("cutoff_violations") or ()),
            )
        )
        provider_calls += row.provider_calls
        cost_total += row.cost_usd or 0.0
        latency_total += facts.get("latency_ms") or 0
        tokens_total += facts.get("tokens") or 0

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
        "cost_usd": round(cost_total, 6),
        "latency_ms": latency_total,
        "tokens": tokens_total,
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

class ReportConflictError(RuntimeError):
    pass


def report_bytes(payload: Mapping[str, Any]) -> bytes:
    return canonical_bytes(payload)


def write_report_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Write-once canonical JSON publication with conflict detection."""
    _write_once_bytes(Path(path), report_bytes(payload))


def _write_once_bytes(path: Path, data: bytes) -> None:
    """Publish bytes without ever replacing a concurrently-created target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        if existing == data:
            return  # idempotent
        raise ReportConflictError(
            f"refusing to overwrite existing report {path} with differing bytes"
        )
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            if path.read_bytes() == data:
                return
            raise ReportConflictError(
                f"refusing to overwrite existing report {path} with differing bytes"
            ) from None
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_report_markdown(path: str | Path, markdown: str) -> None:
    """Write deterministic Markdown through the same write-once primitive."""
    _write_once_bytes(Path(path), markdown.encode("utf-8"))


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
