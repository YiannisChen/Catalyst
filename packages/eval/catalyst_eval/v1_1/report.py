"""Stage-1 report write-once publication (M7-8).

Report JSON is serialized with the M7 canonical bytes and published
write-once: an absent target is created atomically, identical bytes are
idempotent, and differing bytes raise a conflict without overwrite. Markdown
is a deterministic rendering of the JSON payload and cannot supply new
facts.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_eval.v1_1.loader import (
    PublicationConflictError as ReportConflictError,
    canonical_bytes,
    write_once_bytes,
)

REPORT_SCHEMA_VERSION = "v1_1_stage1_report_v1"
DERIVED_LEAKAGE_SCHEMA = "m7_derived_leakage_scan_v2"
DIAGNOSTICS_SCHEMA = "v1.1_run_diagnostics_v1"

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


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{label} evidence is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} evidence is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} evidence must be a JSON object")
    return value


def _strict_nonnegative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _strict_string_list(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string list")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _payload_sha256(payload_json: str) -> str:
    payload = json.loads(payload_json)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _strict_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 hex string")
    return value


def _validate_handoff_manifest(
    *,
    handoff_manifest_path: Path,
    handoff_manifest_sha256: str,
    expected_execution_head: str,
    expected_eval_id: str,
    supplied_files: Mapping[str, Path],
) -> dict[str, Any]:
    if not handoff_manifest_path.is_file():
        raise ValueError(f"handoff manifest is missing: {handoff_manifest_path}")
    expected_sha = _strict_sha256(
        handoff_manifest_sha256, label="handoff manifest SHA-256"
    )
    actual_sha = _sha256_file(handoff_manifest_path)
    if actual_sha != expected_sha:
        raise ValueError("handoff manifest SHA-256 does not match explicit expectation")
    manifest = _read_json_object(handoff_manifest_path, label="handoff manifest")
    if manifest.get("schema_version") != "m7_stage1_final_handoff_manifest_v1":
        raise ValueError("handoff manifest schema mismatch")
    if manifest.get("head") != expected_execution_head:
        raise ValueError("handoff manifest head does not match prepare execution identity")
    if manifest.get("eval_id") != expected_eval_id:
        raise ValueError("handoff manifest eval_id does not match prepare/eval manifest")
    if manifest.get("missing_required") != []:
        raise ValueError("handoff manifest missing_required must be empty")
    if manifest.get("secret_scan_hits") != []:
        raise ValueError("handoff manifest secret_scan_hits must be empty")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("handoff manifest files must be a list")
    file_count = _strict_nonnegative_int(
        manifest.get("file_count"), label="handoff manifest file_count"
    )
    if file_count != len(entries):
        raise ValueError("handoff manifest file_count does not match unique files")
    by_path: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("handoff manifest file entry must be an object")
        path_value = entry.get("path")
        if not isinstance(path_value, str) or not path_value or Path(path_value).is_absolute():
            raise ValueError("handoff manifest file path is invalid")
        if path_value in by_path:
            raise ValueError(f"duplicate handoff manifest file path: {path_value}")
        if Path(path_value).as_posix() != path_value or ".." in Path(path_value).parts:
            raise ValueError(f"handoff manifest file path is not normalized: {path_value}")
        by_path[path_value] = {
            "bytes": _strict_nonnegative_int(
                entry.get("bytes"), label=f"handoff file bytes {path_value}"
            ),
            "sha256": _strict_sha256(
                entry.get("sha256"), label=f"handoff file SHA-256 {path_value}"
            ),
        }
    for relative, supplied_path in supplied_files.items():
        entry = by_path.get(relative)
        if entry is None:
            raise ValueError(f"handoff manifest lacks required file: {relative}")
        if not supplied_path.is_file():
            raise ValueError(f"handoff file is missing: {relative}")
        actual_bytes = supplied_path.stat().st_size
        actual_file_sha = _sha256_file(supplied_path)
        if actual_bytes != entry["bytes"] or actual_file_sha != entry["sha256"]:
            raise ValueError(f"handoff file does not match manifest: {relative}")
    return {
        "sha256": actual_sha,
        "head": manifest["head"],
        "eval_id": manifest["eval_id"],
        "file_hashes": {
            relative: by_path[relative]["sha256"] for relative in supplied_files
        },
        "file_bytes": {
            relative: by_path[relative]["bytes"] for relative in supplied_files
        },
    }


def validate_report_gate_evidence(
    *,
    leakage_scan_path: str | Path,
    derived_leakage_scan_path: str | Path,
    secret_scan_path: str | Path,
    runtime_db_path: str | Path,
    handoff_manifest_path: str | Path,
    handoff_manifest_sha256: str,
    expected_execution_head: str,
    expected_eval_id: str,
) -> dict[str, Any]:
    """Validate identity-bound leakage and secret evidence before report build."""
    original_path = Path(leakage_scan_path).resolve()
    derived_path = Path(derived_leakage_scan_path).resolve()
    secret_path = Path(secret_scan_path).resolve()
    runtime_path = Path(runtime_db_path).resolve()
    handoff_path = Path(handoff_manifest_path).resolve()
    handoff = _validate_handoff_manifest(
        handoff_manifest_path=handoff_path,
        handoff_manifest_sha256=handoff_manifest_sha256,
        expected_execution_head=expected_execution_head,
        expected_eval_id=expected_eval_id,
        supplied_files={
            "report_inputs/leakage_scan.json": original_path,
            "report_inputs/secret_scan.json": secret_path,
            "runtime.sqlite3": runtime_path,
        },
    )
    original = _read_json_object(original_path, label="original leakage scan")
    derived = _read_json_object(derived_path, label="derived leakage scan")
    secret = _read_json_object(secret_path, label="secret scan")

    original_n = _strict_nonnegative_int(original.get("n"), label="original n")
    original_findings = _strict_string_list(
        original.get("findings"), label="original findings"
    )
    if original_n != len(original_findings):
        raise ValueError("original leakage scan n does not match findings")
    original_sha = _sha256_file(original_path)
    if derived.get("schema_version") != DERIVED_LEAKAGE_SCHEMA:
        raise ValueError("derived leakage scan schema mismatch")
    if derived.get("derived") is not True:
        raise ValueError("derived leakage scan must be marked derived")
    declared_original = Path(str(derived.get("original_scan_path", "")))
    if not declared_original.is_absolute():
        declared_original = Path.cwd() / declared_original
    if declared_original.resolve() != original_path:
        raise ValueError("derived leakage scan does not point to supplied original scan")
    if derived.get("original_scan_sha256") != original_sha:
        raise ValueError("original leakage scan SHA-256 mismatch")
    if _strict_nonnegative_int(derived.get("original_scan_n"), label="derived original_scan_n") != original_n:
        raise ValueError("derived/original leakage counts disagree")
    if _strict_nonnegative_int(derived.get("n"), label="derived n") != 0:
        raise ValueError("derived leakage findings are non-zero")
    if _strict_string_list(derived.get("findings"), label="derived findings"):
        raise ValueError("derived leakage findings are non-empty")
    if _strict_string_list(derived.get("c04_rendered_messages_findings"), label="rendered_messages findings"):
        raise ValueError("rendered_messages leakage findings are non-empty")
    if _strict_string_list(derived.get("hidden_gold_boundary"), label="hidden_gold_boundary"):
        raise ValueError("hidden_gold_boundary findings are non-empty")
    if derived.get("model_visible_prompt") is not False:
        raise ValueError("model-visible leakage status is not verified false")
    if derived.get("classifier") != "verified_post_writer_run_diagnostics_terminal_status_exemption_v2":
        raise ValueError("leakage classifier mismatch")
    contract = derived.get("exemption_contract")
    if not isinstance(contract, dict):
        raise ValueError("leakage exemption contract is missing")
    expected_contract = {
        "artifact_type": "run_diagnostics",
        "post_writer_persisted": True,
        "schema_version": DIAGNOSTICS_SCHEMA,
        "terminal_event_type": "run.completed",
        "path_only_exemption": False,
        "generic_or_model_visible_trace_same_fields": "reported",
    }
    for key, expected in expected_contract.items():
        if contract.get(key) != expected:
            raise ValueError(f"leakage exemption contract mismatch: {key}")
    if contract.get("exempted_paths") != [
        "terminal.result_status",
        "terminal.status_ceiling",
    ]:
        raise ValueError("leakage exemption paths mismatch")
    source = derived.get("source_artifact")
    if not isinstance(source, dict):
        raise ValueError("source run_diagnostics artifact proof is missing")
    for key, expected in {
        "artifact_type": "run_diagnostics",
        "post_writer_persisted": True,
        "schema_version": DIAGNOSTICS_SCHEMA,
        "terminal_event_type": "run.completed",
    }.items():
        if source.get(key) != expected:
            raise ValueError(f"source artifact proof mismatch: {key}")
    artifact_id, run_id, payload_hash = (
        source.get("artifact_id"), source.get("run_id"), source.get("payload_hash")
    )
    if not all(isinstance(value, str) and value for value in (artifact_id, run_id, payload_hash)):
        raise ValueError("source artifact identity is incomplete")
    event_seq = _strict_nonnegative_int(source.get("event_seq"), label="source event_seq")
    if not runtime_path.is_file():
        raise ValueError(f"runtime DB evidence is missing: {runtime_path}")
    try:
        uri = f"file:{runtime_path}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            artifact = conn.execute(
                "SELECT artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json "
                "FROM run_artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            event = conn.execute(
                "SELECT seq, event_type FROM run_events WHERE run_id = ? AND seq = ?",
                (run_id, event_seq),
            ).fetchone()
            run = conn.execute(
                "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("runtime DB evidence could not be read immutably") from exc
    if artifact is None or event is None or run is None:
        raise ValueError("source artifact, lifecycle event, or run is missing")
    for key in ("artifact_id", "run_id", "event_seq", "artifact_type", "payload_hash"):
        if artifact[key] != source[key]:
            raise ValueError(f"source artifact does not match runtime DB: {key}")
    if event["event_type"] != "run.completed" or run["lifecycle_status"] != "COMPLETED":
        raise ValueError("source artifact is not bound to completed lifecycle")
    if _payload_sha256(artifact["payload_json"]) != payload_hash:
        raise ValueError("run_diagnostics payload hash mismatch")
    payload = json.loads(artifact["payload_json"])
    if not isinstance(payload, dict) or payload.get("schema_version") != DIAGNOSTICS_SCHEMA:
        raise ValueError("run_diagnostics payload schema mismatch")
    terminal = payload.get("terminal")
    if not isinstance(terminal, dict) or terminal.get("terminal_event_type") != "run.completed":
        raise ValueError("run_diagnostics payload lifecycle mismatch")
    if _strict_string_list(secret.get("findings"), label="secret findings"):
        raise ValueError("secret scan findings are non-empty")
    return {
        "leakage_scan": {
            "status": "PASS",
            "original_count": original_n,
            "derived_count": 0,
            "model_visible_count": 0,
            "original_sha256": original_sha,
            "derived_sha256": _sha256_file(derived_path),
            "original_false_positive_disposition": (
                "original terminal fields retained as false positives and "
                "exempted only by verified post-Writer run_diagnostics proof"
            ),
        },
        "secret_scan": {
            "status": "PASS",
            "count": 0,
            "sha256": _sha256_file(secret_path),
        },
        "handoff_manifest": handoff,
    }


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
    gate_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute the real Stage-1 report payload from sealed inputs only.

    Joins the sealed execution ledger run facts with the sealed human audit
    and computes real retrieval/attribution/trajectory gates. It never
    fabricates empty gates or counts: missing ledger/audit rows fail closed.
    Q-002 is MANDATORY NON-COMPARABLE while the promoted environment tuple is
    unrecovered.
    """
    if gate_evidence is None:
        raise ValueError("report requires validated leakage/secret gate evidence")
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
        "handoff_manifest": dict(gate_evidence["handoff_manifest"]),
        "attribution_metrics": attribution.as_dict(),
        "trajectory_metrics": trajectory.as_dict(),
        "retrieval_metrics": retrieval.as_dict(),
        "gate_evidence": dict(gate_evidence),
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
        f"- handoff_manifest_sha256: `{(payload.get('handoff_manifest') or {}).get('sha256', '')}`",
        f"- handoff_head: `{(payload.get('handoff_manifest') or {}).get('head', '')}`",
        f"- handoff_eval_id: `{(payload.get('handoff_manifest') or {}).get('eval_id', '')}`",
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
    evidence = payload.get("gate_evidence") or {}
    leakage = evidence.get("leakage_scan") or {}
    secret = evidence.get("secret_scan") or {}
    handoff = evidence.get("handoff_manifest") or {}
    handoff_files = handoff.get("file_hashes") or {}
    lines.extend(
        [
            "",
            "## Integrity evidence",
            (
                "- leakage_scan: "
                f"{leakage.get('status')} (original={leakage.get('original_count')}, "
                f"derived={leakage.get('derived_count')}, "
                f"model_visible={leakage.get('model_visible_count')}, "
                f"original_sha256={leakage.get('original_sha256')}, "
                f"derived_sha256={leakage.get('derived_sha256')})"
            ),
            (
                "- secret_scan: "
                f"{secret.get('status')} (count={secret.get('count')}, "
                f"sha256={secret.get('sha256')})"
            ),
            "- original_false_positive_disposition: "
            f"{leakage.get('original_false_positive_disposition')}",
            "- handoff_verified_files: "
            + "; ".join(
                f"{name}={file_sha}" for name, file_sha in sorted(handoff_files.items())
            ),
        ]
    )
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
    "validate_report_gate_evidence",
    "write_report_json",
    "write_report_markdown",
]
