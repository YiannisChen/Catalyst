"""AMEND-2 P1: full fail-closed T4 evidence validator.

``validate_t4_evidence`` returns a non-trivial ``ValidatedT4Evidence`` object
(never a caller-forgeable string) after cross-checking the evidence directory
token, files, recomputed case-pack ID, probe report hash/content, meta identity
contract, and the actual resolved runtime identity. Every rejection happens
before model factory, embedding, retrieval, staging, and artifact writes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from catalyst_eval.post_import.case_pack import (
    CasePackCase,
    compute_case_pack_id,
    load_case_pack,
)
from catalyst_eval.post_import.index_identity import ResolvedRuntimeIdentity
from catalyst_eval.post_import.probe import T4_PROBE_TOKEN, T4_PROBE_TOKEN_FILENAME

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")

T4_EXPECTED_CASE_COUNT = 10
T4_EXPECTED_PASSED_COUNT = 10
T4_EXPECTED_NN_RESULT = "10/10"
T4_PROBE_SCHEMA = "served_corpus_probe_v1"
T4_PROBE_META_SCHEMA = "t4_probe_meta_v1"

# Manager-approved T4 contract. Not overridable by any CLI flag.
APPROVED_T4_CASE_PACK_ID = (
    "579844605892bd2a99b4287fb469c8800d48a9ec552300504ebde9c548b9076d"
)
APPROVED_T4_CASE_COUNT = 10
APPROVED_T4_ORDERED_CASE_IDS = (
    "g006", "g013", "g017", "g024", "g041",
    "g007", "h001", "h004", "h005", "h007",
)


@dataclass(frozen=True)
class ApprovedT4Contract:
    approved_case_pack_id: str = APPROVED_T4_CASE_PACK_ID
    expected_case_count: int = APPROVED_T4_CASE_COUNT
    ordered_case_ids: tuple[str, ...] = APPROVED_T4_ORDERED_CASE_IDS


APPROVED_T4_CONTRACT = ApprovedT4Contract()


@dataclass(frozen=True)
class ValidatedT4Evidence:
    evidence_dir: Path
    case_pack_id: str
    case_count: int
    passed_count: int
    db_sha256: str
    db_user_version: int
    db_foreign_key_violations: int
    snapshot_id: str
    corpus_manifest_id: str
    source_bundle_id: str
    probe_report_id: str
    postbuild_readiness_id: str
    index_manifest_id: str
    active_table_name: str
    model_name: str
    model_revision: str
    tokenizer_revision: str
    dimension: int
    dtype: str
    normalization_mode: str
    runtime_git_head: str
    index_build_code_revision: str
    probe_report_sha256: str


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_hex64(value: str, label: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase hex64")
    return value


def validate_case_pack_against_contract(
    cases: list[CasePackCase],
    *,
    contract: ApprovedT4Contract = APPROVED_T4_CONTRACT,
) -> None:
    """Fail-closed check against the manager-approved T4 case pack contract."""
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != contract.expected_case_count:
        raise ValueError(
            f"case pack must contain exactly {contract.expected_case_count} cases"
        )
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("case pack contains duplicate case_id")
    if tuple(case_ids) != contract.ordered_case_ids:
        raise ValueError("case pack case IDs/order do not match the approved T4 contract")
    computed = compute_case_pack_id(cases)
    if computed != contract.approved_case_pack_id:
        raise ValueError("case pack ID does not match the manager-approved T4 case pack")


def validate_t4_evidence(
    *,
    evidence_dir: Path,
    current_case_pack: list[CasePackCase] | None = None,
    resolved: ResolvedRuntimeIdentity | None = None,
) -> ValidatedT4Evidence:
    """Validate the T4 evidence directory; raise ValueError on any failure."""
    evidence_dir = Path(evidence_dir).resolve()
    if not evidence_dir.is_dir():
        raise ValueError(f"evidence directory missing: {evidence_dir}")

    token_path = evidence_dir / T4_PROBE_TOKEN_FILENAME
    if not token_path.is_file():
        raise ValueError(f"{T4_PROBE_TOKEN_FILENAME} missing in evidence directory")
    if token_path.read_text(encoding="utf-8").strip() != T4_PROBE_TOKEN:
        raise ValueError(f"{T4_PROBE_TOKEN_FILENAME} content must be {T4_PROBE_TOKEN}")
    if (evidence_dir / "WAVE_TOKEN.txt").exists():
        raise ValueError("WAVE_TOKEN.txt must not exist in T4 evidence")

    for filename in ("case_pack.jsonl", "probe_report.json", "meta.json"):
        if not (evidence_dir / filename).is_file():
            raise ValueError(f"{filename} missing in evidence directory")

    evidence_cases = load_case_pack(evidence_dir / "case_pack.jsonl")
    validate_case_pack_against_contract(evidence_cases)
    recomputed_case_pack_id = compute_case_pack_id(evidence_cases)
    _require_hex64(recomputed_case_pack_id, "case_pack_id")
    if recomputed_case_pack_id != APPROVED_T4_CONTRACT.approved_case_pack_id:
        raise ValueError("evidence case pack is not the manager-approved T4 case pack")

    meta = json.loads((evidence_dir / "meta.json").read_text(encoding="utf-8"))
    meta_case_pack_id = _require_hex64(meta.get("case_pack_id", ""), "meta.case_pack_id")
    if meta_case_pack_id != recomputed_case_pack_id:
        raise ValueError("meta.case_pack_id does not match recomputed evidence case pack")

    if current_case_pack is not None:
        validate_case_pack_against_contract(current_case_pack)
        current_id = compute_case_pack_id(current_case_pack)
        if current_id != recomputed_case_pack_id:
            raise ValueError("current CLI case pack does not match evidence case pack")

    probe_report_path = evidence_dir / "probe_report.json"
    probe_report_sha256 = _sha256_bytes(probe_report_path.read_bytes())
    if meta.get("probe_report_sha256") != probe_report_sha256:
        raise ValueError("probe_report_sha256 mismatch with meta")

    probe = json.loads(probe_report_path.read_text(encoding="utf-8"))
    if probe.get("schema_version") != T4_PROBE_SCHEMA:
        raise ValueError("probe report schema_version mismatch")
    if probe.get("all_passed") is not True:
        raise ValueError("probe report all_passed must be true")
    if probe.get("case_count") != T4_EXPECTED_CASE_COUNT:
        raise ValueError(f"probe report case_count must be {T4_EXPECTED_CASE_COUNT}")
    if probe.get("passed_count") != T4_EXPECTED_PASSED_COUNT:
        raise ValueError(f"probe report passed_count must be {T4_EXPECTED_PASSED_COUNT}")

    per_case = probe.get("per_case")
    if not isinstance(per_case, list) or len(per_case) != T4_EXPECTED_CASE_COUNT:
        raise ValueError("probe report per_case must contain 10 entries")
    if len(per_case) != len(evidence_cases):
        raise ValueError("probe per_case count must equal evidence case count")
    evidence_by_id = {case.case_id: case for case in evidence_cases}
    per_case_ids = [entry.get("case_id") for entry in per_case]
    if len(set(per_case_ids)) != len(per_case_ids):
        raise ValueError("probe per_case case ids must be unique")
    if per_case_ids != [case.case_id for case in evidence_cases]:
        raise ValueError("probe per_case ordered ids must match the case pack order")
    for entry in per_case:
        case = evidence_by_id.get(entry.get("case_id"))
        if case is None:
            raise ValueError("probe per-case references unknown case id")
        if entry.get("ticker") != case.ticker or entry.get("cutoff") != case.cutoff:
            raise ValueError("probe per-case ticker/cutoff mismatch with case pack")
        if not isinstance(entry.get("count"), int) or entry.get("count", 0) < 1:
            raise ValueError("probe per-case count must be >= 1")
        if entry.get("ok") is not True:
            raise ValueError("probe per-case ok must be true")
    if probe.get("passed_count") != sum(1 for entry in per_case if entry.get("ok") is True):
        raise ValueError("probe report passed_count must equal the number of ok entries")
    if probe.get("all_passed") != (
        probe.get("passed_count") == probe.get("case_count") == T4_EXPECTED_CASE_COUNT
    ):
        raise ValueError("probe report all_passed must equal (passed == case == 10)")

    # ---- probe's own identity binding (probe -> meta -> resolved) ----
    for key in ("corpus_manifest_id", "db_sha256"):
        if probe.get(key) != meta.get(key):
            raise ValueError(f"probe.{key} does not match meta.{key}")
    if probe.get("case_count") != meta.get("case_count"):
        raise ValueError("probe.case_count does not match meta.case_count")
    if probe.get("passed_count") != meta.get("passed_count"):
        raise ValueError("probe.passed_count does not match meta.passed_count")
    if resolved is not None:
        if probe.get("corpus_manifest_id") != resolved.corpus_manifest_id:
            raise ValueError("probe.corpus_manifest_id does not match resolved runtime identity")
        if probe.get("db_sha256") != resolved.db_sha256:
            raise ValueError("probe.db_sha256 does not match resolved runtime identity")

    # ---- meta schema/path/timestamp contract ----
    if meta.get("schema_version") != T4_PROBE_META_SCHEMA:
        raise ValueError("meta.schema_version must be t4_probe_meta_v1")
    if meta.get("task") != "T4":
        raise ValueError("meta.task must be T4")
    if meta.get("phase") != "wave2_preparation":
        raise ValueError("meta.phase must be wave2_preparation")
    if meta.get("probe_report_path") != "probe_report.json":
        raise ValueError("meta.probe_report_path must be probe_report.json")
    if meta.get("case_pack_path") != "case_pack.jsonl":
        raise ValueError("meta.case_pack_path must be the evidence-relative case_pack.jsonl")
    if not (evidence_dir / meta.get("case_pack_path", "")).is_file():
        raise ValueError("meta.case_pack_path does not resolve to a case pack file")
    for key in ("started_at", "completed_at"):
        value = meta.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"meta.{key} is required")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"meta.{key} is not an ISO-8601 timestamp") from exc
    if datetime.fromisoformat(
        meta["completed_at"].replace("Z", "+00:00")
    ) < datetime.fromisoformat(meta["started_at"].replace("Z", "+00:00")):
        raise ValueError("meta.completed_at must not precede meta.started_at")
    if meta.get("case_count") != T4_EXPECTED_CASE_COUNT:
        raise ValueError("meta.case_count must be 10")
    if meta.get("passed_count") != T4_EXPECTED_PASSED_COUNT:
        raise ValueError("meta.passed_count must be 10")
    if meta.get("nn_result") != T4_EXPECTED_NN_RESULT:
        raise ValueError("meta.nn_result must be 10/10")

    if resolved is not None:
        if not isinstance(meta.get("db_path"), str) or not isinstance(meta.get("lancedb_dir"), str):
            raise ValueError("meta.db_path/lancedb_dir are required")
        if Path(meta["db_path"]).resolve() != resolved.db_path.resolve():
            raise ValueError("meta.db_path does not resolve to the actual frozen DB path")
        if Path(meta["lancedb_dir"]).resolve() != resolved.lancedb_dir.resolve():
            raise ValueError("meta.lancedb_dir does not resolve to the actual LanceDB dir")
        comparisons = (
            ("db_sha256", meta.get("db_sha256"), resolved.db_sha256),
            ("db_user_version", meta.get("db_user_version"), resolved.db_user_version),
            ("db_foreign_key_violations", meta.get("db_foreign_key_violations"), resolved.db_foreign_key_violations),
            ("snapshot_id", meta.get("snapshot_id"), resolved.snapshot_id),
            ("corpus_manifest_id", meta.get("corpus_manifest_id"), resolved.corpus_manifest_id),
            ("source_bundle_id", meta.get("source_bundle_id"), resolved.source_bundle_id),
            ("probe_report_id", meta.get("probe_report_id"), resolved.probe_report_id),
            ("postbuild_readiness_id", meta.get("postbuild_readiness_id"), resolved.postbuild_readiness_id),
            ("index_manifest_id", meta.get("index_manifest_id"), resolved.index_manifest_id),
            ("active_table_name", meta.get("active_table_name"), resolved.active_table_name),
            ("model_name", meta.get("model_name"), resolved.model_name),
            ("model_revision", meta.get("model_revision"), resolved.model_revision),
            ("tokenizer_revision", meta.get("tokenizer_revision"), resolved.tokenizer_revision),
            ("dimension", meta.get("dimension"), resolved.dimension),
            ("dtype", meta.get("dtype"), resolved.dtype),
            ("normalization_mode", meta.get("normalization_mode"), resolved.normalization_mode),
            ("runtime_git_head", meta.get("runtime_git_head"), resolved.git_head),
            ("index_build_code_revision", meta.get("index_build_code_revision"), resolved.code_revision),
        )
        for label, actual, expected in comparisons:
            if actual != expected:
                raise ValueError(f"meta.{label} does not match resolved runtime identity")

    return ValidatedT4Evidence(
        evidence_dir=evidence_dir,
        case_pack_id=meta_case_pack_id,
        case_count=T4_EXPECTED_CASE_COUNT,
        passed_count=T4_EXPECTED_PASSED_COUNT,
        db_sha256=meta["db_sha256"],
        db_user_version=meta["db_user_version"],
        db_foreign_key_violations=meta["db_foreign_key_violations"],
        snapshot_id=meta["snapshot_id"],
        corpus_manifest_id=meta["corpus_manifest_id"],
        source_bundle_id=meta["source_bundle_id"],
        probe_report_id=meta["probe_report_id"],
        postbuild_readiness_id=meta["postbuild_readiness_id"],
        index_manifest_id=meta["index_manifest_id"],
        active_table_name=meta["active_table_name"],
        model_name=meta["model_name"],
        model_revision=meta["model_revision"],
        tokenizer_revision=meta["tokenizer_revision"],
        dimension=meta["dimension"],
        dtype=meta["dtype"],
        normalization_mode=meta["normalization_mode"],
        runtime_git_head=meta["runtime_git_head"],
        index_build_code_revision=meta["index_build_code_revision"],
        probe_report_sha256=probe_report_sha256,
    )


__all__ = [
    "APPROVED_T4_CONTRACT", "APPROVED_T4_CASE_PACK_ID", "APPROVED_T4_CASE_COUNT",
    "APPROVED_T4_ORDERED_CASE_IDS", "ApprovedT4Contract",
    "ValidatedT4Evidence", "validate_t4_evidence", "validate_case_pack_against_contract",
]
