"""T5-T8 four-arm post-import runner (importable core).

Only ``packages/eval/scripts/run_post_import_four_arm.py`` may serve as the
production entry point; this module contains the testable orchestration and
fail-closed validation shared by that CLI.

Amendment P2: each case computes one query embedding and calls
``retrieve_hybrid(mode='reranked')`` exactly once. All four arms are derived
from that single shared result: fts5 <- lexical_results, dense <- dense_results,
hybrid <- fusion_results, reranked <- final_results. The reranker input set is
strictly the fused candidate set by construction.

Amendment P4: runs use a unique sibling staging directory, all artifacts are
reloaded and validated before the staging directory is atomically renamed to
the final run directory. No caller-supplied token exists; ``FOUR_ARM_E2E_OK``
is auto-written only when every success-gate condition holds.

Amendment P6: contract-only mode_served values (fts5/dense/hybrid/reranked),
real measured latency, and fail-closed degraded mode without a manager
authorization artifact.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable

import numpy as np

from catalyst_data.config import (
    BGE_M3_DIMENSION,
    BGE_M3_REVISION,
    BGE_RERANKER_REVISION,
)
from catalyst_data.corpus.streaming_publication import served_chunks_relation
from catalyst_data.retrieval.artifacts import load_arm_artifact, write_arm_artifact
from catalyst_data.retrieval.hybrid import HybridRetrievalResult, retrieve_hybrid as _retrieve_hybrid
from catalyst_data.retrieval.pool import (
    generate_union_pool,
    load_union_pool,
    write_union_pool,
)
from catalyst_data.retrieval.result import SEARCHABLE_STATUSES
from catalyst_data.retrieval.reranker import RerankerGate
from .case_pack import CasePackCase
from .index_identity import ResolvedRuntimeIdentity
from .t4_evidence import (
    APPROVED_T4_CONTRACT,
    ValidatedT4Evidence,
    validate_case_pack_against_contract,
    validate_t4_evidence,
)

ARM_ORDER = ("fts5", "dense", "hybrid", "reranked")
META_SCHEMA_VERSION = "post_import_run_meta_v1"
SUCCESS_TOKEN = "FOUR_ARM_E2E_OK"
_TEMPORAL_IDENTITY_KEYS = (
    "temporal_center_date", "query_date", "query_date_conflict", "query_date_decision",
)

# Sentinel distinguishing "attribute absent on a runtime object" from None.
_MISSING = object()

# Contract-only served modes for a production four-arm run.
_CONTRACT_MODE_SERVED = {"fts5", "dense", "hybrid", "reranked", "failed"}


def expected_case_temporal_identity(case: CasePackCase) -> dict[str, Any]:
    """Expected structured temporal identity for one approved case.

    Computes only the approved-case expectation (query never overrides).  It
    must never read runtime retrieval fields; the runtime side has its own
    reader (``extract_runtime_temporal_identity``).
    """
    from catalyst_data.retrieval.query_policy import resolve_temporal_center

    resolved = resolve_temporal_center(
        query=case.query,
        cutoff=case.cutoff,
        session_date=getattr(case, "session_date", None),
    )
    return {
        "temporal_center_date": resolved.center_date,
        "query_date": resolved.query_date,
        "query_date_conflict": resolved.conflict,
        "query_date_decision": resolved.decision,
    }


def case_temporal_identity(case: CasePackCase) -> dict[str, Any]:
    """Backward-compatible alias for the expected-side helper."""
    return expected_case_temporal_identity(case)


def extract_runtime_temporal_identity(hybrid_result: HybridRetrievalResult) -> dict[str, Any]:
    """Read the actual temporal identity stamped by the hybrid facade.

    This is the only sanctioned runtime-side reader: it reads
    ``HybridRetrievalResult`` outer fields and never recomputes the expected
    identity via ``resolve_temporal_center``.  A missing field (absent
    attribute) is reported as ``_MISSING`` so validation can distinguish
    "absent" from a ``None`` value.
    """
    return {
        key: getattr(hybrid_result, key, _MISSING)
        for key in _TEMPORAL_IDENTITY_KEYS
    }


def _temporal_inner_mismatches(
    hybrid_result: HybridRetrievalResult,
    outer: dict[str, Any],
) -> list[str]:
    """Served evidence (fusion/final) must carry the same temporal identity as
    the outer result; otherwise the run cannot claim the stamped identity."""
    problems: list[str] = []
    for label in ("fusion_results", "final_results"):
        results = getattr(hybrid_result, label, ()) or ()
        for position, item in enumerate(results, start=1):
            for key in _TEMPORAL_IDENTITY_KEYS:
                inner = getattr(item, key, _MISSING)
                if inner != outer[key]:
                    problems.append(
                        f"{label}[{position}].{key} {inner!r} != outer {outer[key]!r}"
                    )
    return problems


def validate_runtime_temporal_identity(
    case: CasePackCase,
    hybrid_result: HybridRetrievalResult,
) -> dict[str, Any]:
    """Fail-closed per-case binding of the actual runtime temporal identity.

    Compares the hybrid facade's actual identity field-by-field against the
    approved-case expectation and raises ``RunnerValidationError`` on any
    mismatch, missing/invalid value, inner/outer inconsistency, or an approved
    case whose query date conflicts with the structured session date (Wave 2
    preflight fail-closed; the ordinary online attribution path keeps the
    ``structured_ignore_query`` recording policy).
    """
    expected = expected_case_temporal_identity(case)
    actual = extract_runtime_temporal_identity(hybrid_result)
    problems: list[str] = []

    # Attribute absence is always a failure.  ``query_date`` is the one field
    # whose model contract allows ``None`` (queries without a date), so only
    # the other required fields must additionally be non-None.
    for key in _TEMPORAL_IDENTITY_KEYS:
        if actual[key] is _MISSING:
            problems.append(f"runtime temporal field {key} absent")
    for key in ("temporal_center_date", "query_date_conflict", "query_date_decision"):
        if actual[key] is None:
            problems.append(f"runtime temporal field {key} missing")

    center = actual.get("temporal_center_date")
    if center is not None and (
        not isinstance(center, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", center) is None
    ):
        problems.append(f"runtime temporal_center_date invalid: {center!r}")
    elif center is not None and center != expected["temporal_center_date"]:
        problems.append(
            f"runtime temporal_center_date {center!r} != approved {expected['temporal_center_date']!r}"
        )

    query_date = actual.get("query_date")
    if query_date is not _MISSING and query_date != expected["query_date"]:
        problems.append(
            f"runtime query_date {query_date!r} != expected {expected['query_date']!r}"
        )

    conflict = actual.get("query_date_conflict")
    if type(conflict) is not bool:
        problems.append(f"runtime query_date_conflict must be a strict bool")
    elif conflict != expected["query_date_conflict"]:
        problems.append(
            f"runtime query_date_conflict {conflict!r} != expected {expected['query_date_conflict']!r}"
        )

    decision = actual.get("query_date_decision")
    if decision is _MISSING or decision is None or decision not in {"structured", "structured_ignore_query", "none"}:
        problems.append(f"runtime query_date_decision invalid: {decision!r}")
    elif decision != expected["query_date_decision"]:
        problems.append(
            f"runtime query_date_decision {decision!r} != expected {expected['query_date_decision']!r}"
        )

    problems.extend(_temporal_inner_mismatches(hybrid_result, actual))

    if expected["query_date_conflict"] is True:
        problems.append(
            "approved case query date conflicts with structured session date "
            "(Wave 2 preflight fail-closed)"
        )

    if problems:
        raise RunnerValidationError(
            "temporal identity validation failed: " + "; ".join(dict.fromkeys(problems))
        )
    return actual


def build_temporal_identity_validation(cases: list[CasePackCase]) -> dict[str, dict[str, Any]]:
    """Expected-only fixture helper: build the *expected* identity map.

    The four-arm runner persists the *actual* runtime identity map
    (``actual_temporal_by_case``); this helper exists for tests and Wave 2
    evidence builders to construct the approved-case expectation fixture.
    """
    return {case.case_id: expected_case_temporal_identity(case) for case in cases}


def validate_temporal_identity_validation(
    raw: Any,
    cases: list[CasePackCase],
) -> list[str]:
    """Fail-closed checks for meta.temporal_identity_validation vs approved cases."""
    reasons: list[str] = []
    if not isinstance(raw, dict) or not raw:
        return ["temporal_identity_validation missing"]
    expected_ids = [case.case_id for case in cases]
    actual_ids = set(raw)
    expected_set = set(expected_ids)
    if actual_ids != expected_set:
        missing = sorted(expected_set - actual_ids)
        extra = sorted(actual_ids - expected_set)
        reasons.append(
            f"temporal_identity_validation case set mismatch "
            f"(missing={missing}, extra={extra})"
        )
    for case in cases:
        entry = raw.get(case.case_id)
        if not isinstance(entry, dict):
            reasons.append(f"temporal_identity_validation missing record for {case.case_id}")
            continue
        if set(entry) < set(_TEMPORAL_IDENTITY_KEYS):
            reasons.append(
                f"temporal_identity_validation {case.case_id} missing required fields"
            )
            continue
        expected = case_temporal_identity(case)
        if entry.get("temporal_center_date") != expected["temporal_center_date"]:
            reasons.append(
                f"temporal_identity_validation {case.case_id} temporal_center_date "
                f"does not match approved case structured center"
            )
        for key in _TEMPORAL_IDENTITY_KEYS:
            if entry.get(key) != expected[key]:
                reasons.append(
                    f"temporal_identity_validation {case.case_id} {key} "
                    f"does not match approved case identity"
                )
                break
    return reasons


class RunnerValidationError(ValueError):
    """Fail-closed runner validation failure before any success artifact."""


@dataclass(frozen=True)
class RunIdentities:
    code_revision: str
    git_head: str
    snapshot_id: str
    corpus_manifest_id: str
    source_bundle_id: str
    probe_report_id: str
    postbuild_readiness_id: str
    index_manifest_id: str
    lancedb_dir: str
    active_table_name: str
    model_name: str
    model_revision: str
    tokenizer_revision: str
    reranker_model: str
    reranker_revision: str
    dimension: int
    dtype: str
    normalization_mode: str
    vector_count: int
    lancedb_row_count: int
    db_path: str
    db_sha256: str
    db_user_version: int
    db_foreign_key_violations: int
    schema_version: str = META_SCHEMA_VERSION


@dataclass(frozen=True)
class EmbeddingBoundary:
    embedding_mode: str  # production_pinned | mock_unit_test | degraded
    dimension: int
    model_revision: str
    tokenizer_revision: str
    is_mock: bool
    cuda_available: bool


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    case_count: int
    arm_order: tuple[str, ...]
    embedding_mode: str
    completed_at: str
    meta_path: Path
    arms_written: int
    pools_written: int
    token_written: bool


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")


def _validate_identities(identities: RunIdentities) -> None:
    """Structural validation; value binding happens in index_identity.py."""
    for field_name in ("code_revision", "git_head"):
        value = getattr(identities, field_name)
        if _HEX40.fullmatch(value) is None:
            raise ValueError(f"{field_name} must be a 40-char SHA")
    for field_name in (
        "snapshot_id", "corpus_manifest_id", "source_bundle_id",
        "probe_report_id", "postbuild_readiness_id", "index_manifest_id",
    ):
        value = getattr(identities, field_name)
        if _HEX64.fullmatch(value) is None:
            raise ValueError(f"{field_name} must be a 64-char SHA")
    if _HEX64.fullmatch(identities.db_sha256) is None:
        raise ValueError("db_sha256 must be a 64-char SHA")
    for field_name in ("lancedb_dir", "active_table_name", "model_name", "db_path"):
        if not getattr(identities, field_name):
            raise ValueError(f"{field_name} is required")
    if identities.dimension <= 0:
        raise ValueError("dimension must be positive")
    if identities.vector_count <= 0 or identities.lancedb_row_count <= 0:
        raise ValueError("vector/lancedb row counts must be positive")
    if identities.db_user_version < 0 or identities.db_foreign_key_violations < 0:
        raise ValueError("DB facts must be non-negative")
    for field_name in ("dtype", "normalization_mode"):
        if not getattr(identities, field_name):
            raise ValueError(f"{field_name} is required")


def validate_query_vector(vector: Any, *, dimension: int) -> np.ndarray:
    """Validate an actual per-case query vector: 1-D, exact dimension, finite,
    L2-normalized. Returns the float32 vector. No probe-query embedding exists."""
    query = np.asarray(vector, dtype=np.float32)
    if query.ndim != 1 or query.size != dimension:
        raise ValueError(f"query vector must be one-dimensional {dimension}-d")
    norm = float(np.linalg.norm(query))
    if not math.isfinite(norm) or not math.isfinite(float(np.sum(query))):
        raise ValueError("query vector must be finite")
    if abs(norm - 1.0) > 1e-3:
        raise ValueError("query vector must be L2-normalized")
    return query


def validate_embedding_boundary(
    boundary: EmbeddingBoundary,
    *,
    manager_authorization_path: Path | None = None,
) -> None:
    if boundary.embedding_mode not in {"production_pinned", "mock_unit_test", "degraded"}:
        raise ValueError(f"invalid embedding_mode: {boundary.embedding_mode}")
    if boundary.embedding_mode == "production_pinned":
        if boundary.is_mock:
            raise ValueError("production_pinned rejects mock/hash embeddings")
        if boundary.model_revision != BGE_M3_REVISION:
            raise ValueError("production_pinned model revision mismatch")
        if boundary.tokenizer_revision != BGE_M3_REVISION:
            raise ValueError("production_pinned tokenizer revision mismatch")
        if boundary.dimension != BGE_M3_DIMENSION:
            raise ValueError(f"production_pinned dimension must be {BGE_M3_DIMENSION}")
        if not boundary.cuda_available:
            raise RuntimeError("CUDA is required for production_pinned; CPU fallback disabled")
    elif boundary.embedding_mode == "mock_unit_test":
        if not boundary.is_mock:
            raise ValueError("mock_unit_test requires an explicit mock embedder")
    elif boundary.embedding_mode == "degraded":
        if boundary.is_mock:
            raise ValueError("degraded runs must not use mock embeddings")
        if manager_authorization_path is None or not Path(manager_authorization_path).is_file():
            raise ValueError(
                "degraded mode requires a manager authorization artifact; fail-closed"
            )
        if not boundary.cuda_available:
            raise RuntimeError("degraded production embedding still requires CUDA")


def _result_to_artifact(result: Any, *, rank: int) -> dict[str, Any]:
    return {
        "chunk_id": result.chunk_id,
        "document_id": result.document_id,
        "available_at": result.available_at,
        "source_class": result.source_class,
        "rank": rank,
        "lexical_raw_score": result.lexical_raw_score,
        "lexical_rank": result.lexical_rank,
        "dense_score": result.dense_score,
        "dense_rank": result.dense_rank,
        "fusion_score": result.fusion_score,
        "fusion_rank": result.fusion_rank,
        "arm_ranks": [list(pair) for pair in result.arm_ranks],
        "arm_scores": [list(pair) for pair in result.arm_scores],
        "reranker_score": result.reranker_score,
        "reranker_rank": result.reranker_rank,
    }


def _set_to_arm(
    result_set: Any,
    *,
    mode_requested: str,
    latency_ms: float,
    degradation_reasons: tuple[str, ...] = (),
) -> dict[str, Any]:
    results = [
        _result_to_artifact(item, rank=position)
        for position, item in enumerate(result_set.results, start=1)
    ]
    return {
        "mode_requested": mode_requested,
        "mode_served": result_set.mode_served,
        "status": "ok",
        "latency_ms": latency_ms,
        "degradation_reasons": list(degradation_reasons),
        "results": results,
    }


def _hybrid_to_arm(
    hybrid: HybridRetrievalResult,
    *,
    mode_requested: str,
    latency_ms: float,
    use_fusion: bool,
) -> dict[str, Any]:
    if use_fusion and hybrid.mode_served in {"hybrid", "reranked"} and hybrid.fusion_results:
        served = tuple(hybrid.fusion_results)
    else:
        served = tuple(hybrid.final_results)
    results = [
        _result_to_artifact(item, rank=position)
        for position, item in enumerate(served, start=1)
    ]
    if use_fusion:
        # The hybrid arm's own served mode is "hybrid" when both retrieval
        # arms succeeded (regardless of whether the outer pipeline reranked).
        mode_served = "hybrid" if hybrid.mode_served in {"hybrid", "reranked"} else hybrid.mode_served
    else:
        mode_served = hybrid.mode_served
    return {
        "mode_requested": mode_requested,
        "mode_served": mode_served,
        "status": "ok",
        "latency_ms": latency_ms,
        "degradation_reasons": list(hybrid.degradation_reasons),
        "results": results,
    }


def _failed_arm(mode_requested: str, reason: str) -> dict[str, Any]:
    return {
        "mode_requested": mode_requested,
        "mode_served": "failed",
        "status": "failed",
        "latency_ms": 0.0,
        "degradation_reasons": [reason],
        "results": [],
    }


def _compute_effect_metrics(arms: dict[str, dict[str, Any]], hybrid_result: Any) -> dict[str, Any]:
    """Per-case effect metrics persisted into the arm artifact (AMEND-5)."""
    hybrid = arms["hybrid"]["results"]
    reranked = arms["reranked"]["results"]
    # Persisted hybrid arm is the reranker input contract (AMEND-5.1).
    reranker_input = len(hybrid)
    return {
        "lexical_count": len(arms["fts5"]["results"]),
        "dense_count": len(arms["dense"]["results"]),
        "hybrid_count": len(hybrid),
        "reranked_count": len(reranked),
        "hybrid_lexical_contribution": sum(
            1 for result in hybrid if result.get("lexical_rank") is not None
        ),
        "hybrid_dense_contribution": sum(
            1 for result in hybrid if result.get("dense_rank") is not None
        ),
        "reranker_input_count": reranker_input,
        "reranker_output_count": len(reranked),
        "reranker_provenance": [
            {
                "chunk_id": result["chunk_id"],
                "rank": position,
                "reranker_score": result.get("reranker_score"),
                "reranker_rank": result.get("reranker_rank"),
            }
            for position, result in enumerate(reranked, start=1)
        ],
    }


def _effect_validity_problems(arms: dict[str, dict[str, Any]], hybrid_result: Any) -> list[str]:
    """Effect-validity problems that must block FOUR_ARM_E2E_OK (AMEND-5)."""
    problems: list[str] = []
    for name in ARM_ORDER:
        arm = arms[name]
        if arm["status"] != "ok":
            problems.append(f"{name} arm failed")
        elif not arm["results"]:
            problems.append(f"{name} arm ok with zero results")
    hybrid = arms["hybrid"]["results"]
    if hybrid and not any(result.get("lexical_rank") is not None for result in hybrid):
        problems.append("hybrid has no lexical contribution")
    if hybrid and not any(result.get("dense_rank") is not None for result in hybrid):
        problems.append("hybrid has no dense contribution")
    if arms["reranked"]["mode_served"] == "reranked" and not arms["reranked"]["results"]:
        problems.append("reranker output is empty")
    return problems


def _chunk_served_for_case(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    manifest_id: str,
    ticker: str,
    cutoff: str,
) -> bool:
    relation = served_chunks_relation(conn)
    status_placeholders = ", ".join("?" for _ in SEARCHABLE_STATUSES)
    row = conn.execute(
        f"""SELECT 1 FROM {relation} c
            WHERE c.chunk_id = ?
              AND c.manifest_id = ?
              AND c.status IN ({status_placeholders})
              AND c.eligibility = 'eligible'
              AND c.available_at <= ?
              AND EXISTS (
                SELECT 1 FROM json_each(c.ticker_associations) je
                WHERE je.value = ?
              )""",
        (chunk_id, manifest_id, *SEARCHABLE_STATUSES, cutoff, ticker),
    ).fetchone()
    return row is not None


def _validate_case_arms(
    conn: sqlite3.Connection,
    *,
    case: CasePackCase,
    arms: dict[str, dict[str, Any]],
    manifest_id: str,
) -> None:
    for name in ARM_ORDER:
        arm = arms[name]
        if arm["mode_requested"] != name:
            raise RunnerValidationError(f"{name} arm mode_requested mismatch")
        if arm["mode_served"] not in _CONTRACT_MODE_SERVED:
            raise RunnerValidationError(f"{name} arm mode_served invalid: {arm['mode_served']}")
        seen: set[str] = set()
        for position, result in enumerate(arm["results"], start=1):
            chunk_id = result["chunk_id"]
            if not isinstance(chunk_id, str) or not chunk_id:
                raise RunnerValidationError(f"{name} arm has empty chunk_id")
            if chunk_id in seen:
                raise RunnerValidationError(f"{name} arm duplicate chunk_id {chunk_id}")
            seen.add(chunk_id)
            if result["rank"] != position:
                raise RunnerValidationError(f"{name} arm served-order rank mismatch")
            if result["available_at"] > case.cutoff:
                raise RunnerValidationError(
                    f"{name} arm look-ahead evidence {chunk_id} after cutoff"
                )
            if not _chunk_served_for_case(
                conn,
                chunk_id=chunk_id,
                manifest_id=manifest_id,
                ticker=case.ticker,
                cutoff=case.cutoff,
            ):
                raise RunnerValidationError(
                    f"{name} arm chunk {chunk_id} not served for case ticker/cutoff"
                )


def _validate_raw_ticker_associations(
    results: Any,
    *,
    case: CasePackCase,
    arm_name: str,
) -> None:
    for result in results:
        associations = tuple(getattr(result, "ticker_associations", ()) or ())
        if associations and case.ticker not in associations:
            raise RunnerValidationError(
                f"{arm_name} arm wrong-ticker evidence {result.chunk_id}"
            )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _pinned_retrieval_config(reranker_timeout_seconds: float = 2.0) -> dict[str, Any]:
    return {
        "lexical_top_k": 20,
        "dense_top_k": 20,
        "fusion_k": 60,
        "fused_top_k": 20,
        "display_top_k": 8,
        "embedding_revision": BGE_M3_REVISION,
        "reranker_revision": BGE_RERANKER_REVISION,
        "reranker_timeout_seconds": reranker_timeout_seconds,
    }


def _reload_validate_run(staging_dir: Path) -> list[dict[str, Any]]:
    """Reload every persisted artifact/pool and enforce schema/identity."""
    arms_dir = staging_dir / "arms"
    pool_dir = staging_dir / "pool"
    arm_paths = sorted(arms_dir.glob("*.json"))
    pool_paths = sorted(pool_dir.glob("*.json"))
    if not arm_paths or not pool_paths:
        raise RunnerValidationError("run missing arms or pools")
    if len(arm_paths) != len(pool_paths):
        raise RunnerValidationError("arm/pool count mismatch")
    validation: list[dict[str, Any]] = []
    for arm_path in arm_paths:
        arm = load_arm_artifact(arm_path)
        if list(arm.arms) != list(ARM_ORDER):
            raise RunnerValidationError("artifact arm order mismatch")
        pool_path = pool_dir / arm_path.name
        if not pool_path.is_file():
            raise RunnerValidationError(f"pool missing for {arm_path.name}")
        pool = load_union_pool(pool_path)
        if pool.source_artifact_id != arm.artifact_id:
            raise RunnerValidationError("pool source artifact id mismatch")
        validation.append({
            "artifact": arm_path.name,
            "arms": list(arm.arms),
            "pool_source_artifact_id": pool.source_artifact_id,
        })
    return validation


def _evidence_fields_match(
    left: ValidatedT4Evidence,
    right: ValidatedT4Evidence,
) -> bool:
    """Field-by-field canonical equality (no sentinels, no magic markers)."""
    for field in fields(ValidatedT4Evidence):
        a = getattr(left, field.name)
        b = getattr(right, field.name)
        if field.name == "evidence_dir":
            a = Path(a).resolve()
            b = Path(b).resolve()
        if a != b:
            return False
    return True


def _success_token_gate(
    *,
    embedding_mode: str,
    limit: int | None,
    executed_case_count: int,
    full_case_count: int,
    case_pack_id: str,
    validated_evidence: ValidatedT4Evidence | None,
    validated_runtime_identity: ResolvedRuntimeIdentity | None,
    run_identities: RunIdentities,
    arms_written: int,
    pools_written: int,
    reload_validation: list[dict[str, Any]],
    has_failed_arm: bool,
    has_degradation: bool,
    effect_problems: list[str],
    revalidated_evidence: ValidatedT4Evidence | None = None,
    evidence_revalidation_error: str | None = None,
    temporal_identity_validation: dict[str, Any] | None = None,
    cases: list[CasePackCase] | None = None,
) -> tuple[bool, list[str]]:
    """Return (may_write_token, reasons). Token is only writable when both
    validated objects are held, the evidence directory re-validates to an
    identical canonical object, and every identity matches the actual run."""
    reasons: list[str] = []
    if embedding_mode != "production_pinned":
        reasons.append("embedding_mode != production_pinned")
    if limit is not None:
        reasons.append("limit was used")
    if executed_case_count != full_case_count:
        reasons.append("case count < full case pack")
    if case_pack_id != APPROVED_T4_CONTRACT.approved_case_pack_id:
        reasons.append("case pack is not the manager-approved T4 pack")
    if executed_case_count != APPROVED_T4_CONTRACT.expected_case_count:
        reasons.append(
            f"case count != approved {APPROVED_T4_CONTRACT.expected_case_count}"
        )
    if validated_evidence is None:
        reasons.append("validated T4 evidence object missing")
    elif case_pack_id != validated_evidence.case_pack_id:
        reasons.append("case_pack_id does not match validated T4 evidence")
    if evidence_revalidation_error is not None:
        reasons.append(f"T4 evidence re-validation failed: {evidence_revalidation_error}")
    elif validated_evidence is not None and revalidated_evidence is None:
        reasons.append("validated T4 evidence could not be re-validated from its evidence directory")
    elif validated_evidence is not None and not _evidence_fields_match(
        revalidated_evidence, validated_evidence
    ):
        reasons.append("validated T4 evidence fields do not match the evidence directory")
    if validated_runtime_identity is None:
        reasons.append("validated runtime identity object missing")
    elif _identity_binding_mismatch(run_identities, validated_runtime_identity):
        reasons.append("runtime identity mismatch with validated object")
    if arms_written != executed_case_count or pools_written != executed_case_count:
        reasons.append("missing arm or pool")
    if has_failed_arm:
        reasons.append("failed base arm")
    if has_degradation:
        reasons.append("unauthorized degradation (reranker/arm fallback)")
    if effect_problems:
        reasons.append("effect-validity: " + "; ".join(dict.fromkeys(effect_problems)))
    if not reload_validation:
        reasons.append("artifact reload/validation failed")
    if cases is not None:
        reasons.extend(
            validate_temporal_identity_validation(temporal_identity_validation, cases)
        )
    elif not temporal_identity_validation:
        reasons.append("temporal_identity_validation missing")
    return (not reasons, reasons)


def _identity_binding_mismatch(
    run_identities: RunIdentities,
    runtime: ResolvedRuntimeIdentity,
) -> bool:
    """Full runtime identity parity between the run and the resolved object.

    Every field that a caller could observe or forge is compared here; the
    validated runtime object is itself re-derived from disk by the CLI, so a
    mismatch means the run cannot claim the resolved production identity.
    """
    return not (
        run_identities.code_revision == runtime.code_revision
        and run_identities.git_head == runtime.git_head
        and run_identities.snapshot_id == runtime.snapshot_id
        and run_identities.corpus_manifest_id == runtime.corpus_manifest_id
        and run_identities.source_bundle_id == runtime.source_bundle_id
        and run_identities.probe_report_id == runtime.probe_report_id
        and run_identities.postbuild_readiness_id == runtime.postbuild_readiness_id
        and run_identities.index_manifest_id == runtime.index_manifest_id
        and run_identities.active_table_name == runtime.active_table_name
        and Path(run_identities.lancedb_dir).resolve() == runtime.lancedb_dir.resolve()
        and run_identities.model_name == runtime.model_name
        and run_identities.model_revision == runtime.model_revision
        and run_identities.tokenizer_revision == runtime.tokenizer_revision
        and run_identities.dimension == runtime.dimension
        and run_identities.dtype == runtime.dtype
        and run_identities.normalization_mode == runtime.normalization_mode
        and run_identities.vector_count == runtime.vector_count
        and run_identities.lancedb_row_count == runtime.lancedb_row_count
        and Path(run_identities.db_path).resolve() == runtime.db_path.resolve()
        and run_identities.db_sha256 == runtime.db_sha256
        and run_identities.db_user_version == runtime.db_user_version
        and run_identities.db_foreign_key_violations == runtime.db_foreign_key_violations
    )


def run_four_arm_cases(
    *,
    db: sqlite3.Connection,
    lancedb_table: Any,
    cases: list[CasePackCase],
    run_id: str,
    output_root: Path,
    identities: RunIdentities,
    boundary: EmbeddingBoundary,
    query_embedding_fn: Callable[[str], np.ndarray],
    reranker: Any | None = None,
    reranker_timeout_seconds: float = 2.0,
    limit: int | None = None,
    started_at: str | None = None,
    case_pack_id: str = "",
    case_pack_path: str = "",
    full_case_count: int | None = None,
    validated_evidence: ValidatedT4Evidence | None = None,
    validated_runtime_identity: ResolvedRuntimeIdentity | None = None,
    manager_authorization_path: Path | None = None,
) -> RunSummary:
    """Run exactly four arms per case with a shared retrieval call and staging."""
    import datetime

    _validate_identities(identities)
    validate_embedding_boundary(
        boundary,
        manager_authorization_path=manager_authorization_path,
    )

    if boundary.embedding_mode == "production_pinned":
        # The CLI is the only production entry point, but the library boundary
        # must also fail closed against caller-forgeable case packs.
        validate_case_pack_against_contract(cases)

    if reranker_timeout_seconds <= 0:
        raise ValueError("reranker_timeout_seconds must be positive")

    if limit is not None:
        executed_cases = cases[:limit]
    else:
        executed_cases = cases
    if not executed_cases:
        raise ValueError("case list is empty")
    full_case_count = full_case_count if full_case_count is not None else len(cases)

    started_at = started_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
    output_root = Path(output_root)
    final_dir = output_root / run_id
    if final_dir.exists():
        raise ValueError(f"run_id already exists: {run_id}")

    staging_dir = output_root / f".{run_id}.staging-{uuid.uuid4().hex[:12]}"
    staging_dir.mkdir(parents=True, exist_ok=False)
    arms_dir = staging_dir / "arms"
    pool_dir = staging_dir / "pool"
    arms_dir.mkdir(parents=True, exist_ok=False)
    pool_dir.mkdir(parents=True, exist_ok=False)

    arms_written = 0
    pools_written = 0
    validation: list[dict[str, Any]] = []
    effect_problems: list[str] = []
    has_failed_arm = False
    has_degradation = False
    reranker_gate = RerankerGate()
    # AMEND-5.2C: meta.temporal_identity_validation must be the *actual*
    # runtime identity extracted from each hybrid result, never a re-computed
    # expected fixture.  Validation runs before any arm/pool artifact write.
    actual_temporal_by_case: dict[str, dict[str, Any]] = {}
    try:
        for case in executed_cases:
            query_vector = validate_query_vector(
                query_embedding_fn(case.query), dimension=boundary.dimension,
            )
            started_hybrid = time.perf_counter()
            hybrid_result = _retrieve_hybrid(
                db, query=case.query, ticker=case.ticker, cutoff=case.cutoff,
                mode="reranked", query_embedding=query_vector,
                requested_manifest_id=identities.corpus_manifest_id,
                index_manifest_id=identities.index_manifest_id,
                lancedb_table=lancedb_table, reranker=reranker,
                reranker_timeout_seconds=reranker_timeout_seconds,
                reranker_gate=reranker_gate,
                return_v1=False,
            )
            hybrid_latency_ms = (time.perf_counter() - started_hybrid) * 1000.0

            # Bind the actual temporal identity before any artifact write:
            # raises RunnerValidationError on missing/invalid/mismatched
            # runtime fields, inner/outer inconsistency, or approved-case
            # query/session date conflict (Wave 2 preflight fail-closed).
            actual_temporal_by_case[case.case_id] = validate_runtime_temporal_identity(
                case, hybrid_result,
            )

            arms: dict[str, dict[str, Any]] = {}
            if hybrid_result.lexical_results is None:
                arms["fts5"] = _failed_arm("fts5", "fts5_unavailable")
                has_failed_arm = True
            else:
                arms["fts5"] = _set_to_arm(
                    hybrid_result.lexical_results, mode_requested="fts5",
                    latency_ms=float(
                        getattr(hybrid_result.lexical_results.trace, "total_ms", 0) or 0.0
                    ),
                    degradation_reasons=(
                        (hybrid_result.lexical_results.fallback_reason,)
                        if hybrid_result.lexical_results.is_degraded
                        and hybrid_result.lexical_results.fallback_reason
                        else ()
                    ),
                )
            if hybrid_result.dense_results is None:
                arms["dense"] = _failed_arm("dense", "dense_unavailable")
                has_failed_arm = True
            else:
                arms["dense"] = _set_to_arm(
                    hybrid_result.dense_results, mode_requested="dense",
                    latency_ms=float(
                        getattr(hybrid_result.dense_results.trace, "total_ms", 0) or 0.0
                    ),
                )
            arms["hybrid"] = _hybrid_to_arm(
                hybrid_result, mode_requested="hybrid",
                latency_ms=hybrid_latency_ms, use_fusion=True,
            )
            arms["reranked"] = _hybrid_to_arm(
                hybrid_result, mode_requested="reranked",
                latency_ms=hybrid_latency_ms, use_fusion=False,
            )
            if hybrid_result.degradation_reasons:
                has_degradation = True

            if hybrid_result.lexical_results is not None:
                _validate_raw_ticker_associations(
                    hybrid_result.lexical_results.results, case=case, arm_name="fts5"
                )
            if hybrid_result.dense_results is not None:
                _validate_raw_ticker_associations(
                    hybrid_result.dense_results.results, case=case, arm_name="dense"
                )
            _validate_raw_ticker_associations(
                hybrid_result.fusion_results, case=case, arm_name="hybrid"
            )
            _validate_raw_ticker_associations(
                hybrid_result.final_results, case=case, arm_name="reranked"
            )
            _validate_case_arms(
                db, case=case, arms=arms,
                manifest_id=identities.corpus_manifest_id,
            )
            effect_problems.extend(
                _effect_validity_problems(arms, hybrid_result)
            )

            filters = {
                "ticker": case.ticker,
                "evidence_types": [],
                "source_classes": [],
                "corpus_manifest_id": identities.corpus_manifest_id,
                "index_manifest_id": identities.index_manifest_id,
            }
            written = write_arm_artifact(
                root=staging_dir,
                run_id=run_id,
                case_id=case.case_id,
                query=case.query,
                cutoff_ts=case.cutoff,
                filters=filters,
                retrieval_config=_pinned_retrieval_config(reranker_timeout_seconds),
                arms=arms,
                effect_metrics=_compute_effect_metrics(arms, hybrid_result),
                created_at=started_at,
            )
            final_arm_path = arms_dir / f"{case.case_id}.json"
            os.replace(written, final_arm_path)
            try:
                written.parent.rmdir()
            except OSError:
                pass
            arms_written += 1

            pool = generate_union_pool(final_arm_path)
            pool_path = pool_dir / f"{case.case_id}.json"
            write_union_pool(pool, pool_path)
            pools_written += 1

            validation.append({
                "case_id": case.case_id,
                "ticker": case.ticker,
                "cutoff": case.cutoff,
                "arms": list(ARM_ORDER),
                "look_ahead": 0,
                "wrong_ticker": 0,
                "passed": True,
            })

        reload_validation = _reload_validate_run(staging_dir)

        completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        meta: dict[str, Any] = {
            "schema_version": META_SCHEMA_VERSION,
            "run_id": run_id,
            "code_revision": identities.code_revision,
            "index_build_revision": identities.code_revision,
            "git_head": identities.git_head,
            "runtime_git_head": identities.git_head,
            "snapshot_id": identities.snapshot_id,
            "corpus_manifest_id": identities.corpus_manifest_id,
            "source_bundle_id": identities.source_bundle_id,
            "probe_report_id": identities.probe_report_id,
            "postbuild_readiness_id": identities.postbuild_readiness_id,
            "index_manifest_id": identities.index_manifest_id,
            "lancedb_dir": identities.lancedb_dir,
            "active_table_name": identities.active_table_name,
            "case_pack_id": case_pack_id,
            "case_pack_path": case_pack_path,
            "embedding_mode": boundary.embedding_mode,
            "model_name": identities.model_name,
            "model_revision": identities.model_revision,
            "tokenizer_revision": identities.tokenizer_revision,
            "reranker_model": identities.reranker_model,
            "reranker_revision": identities.reranker_revision,
            "reranker_timeout_seconds": reranker_timeout_seconds,
            "started_at": started_at,
            "completed_at": completed_at,
            "case_count": len(executed_cases),
            "full_case_count": full_case_count,
            "arm_order": list(ARM_ORDER),
            "cutoff_ticker_validation": validation,
            "revision_mismatch": {
                "code_revision": identities.code_revision,
                "git_head": identities.git_head,
                "explanation": (
                    "index_build_revision is the frozen embedding/import revision; "
                    "runtime_git_head is the current runner checkout HEAD"
                ),
            },
            "temporal_identity_validation": actual_temporal_by_case,
        }
        meta_path = staging_dir / "meta.json"
        _atomic_write_json(meta_path, meta)

        revalidated_evidence = None
        evidence_revalidation_error = None
        if validated_evidence is not None:
            try:
                revalidated_evidence = validate_t4_evidence(
                    evidence_dir=validated_evidence.evidence_dir,
                    current_case_pack=cases,
                    resolved=validated_runtime_identity,
                )
            except Exception as exc:
                evidence_revalidation_error = str(exc)

        may_write, gate_reasons = _success_token_gate(
            embedding_mode=boundary.embedding_mode,
            limit=limit,
            executed_case_count=len(executed_cases),
            full_case_count=full_case_count,
            case_pack_id=case_pack_id,
            validated_evidence=validated_evidence,
            validated_runtime_identity=validated_runtime_identity,
            run_identities=identities,
            arms_written=arms_written,
            pools_written=pools_written,
            reload_validation=reload_validation,
            has_failed_arm=has_failed_arm,
            has_degradation=has_degradation,
            effect_problems=effect_problems,
            revalidated_evidence=revalidated_evidence,
            evidence_revalidation_error=evidence_revalidation_error,
            temporal_identity_validation=meta.get("temporal_identity_validation"),
            cases=executed_cases,
        )
        token_written = False
        if not may_write:
            meta["token_gate"] = {"written": False, "reasons": gate_reasons}
            _atomic_write_json(meta_path, meta)
        else:
            token_written = True
            meta["token_gate"] = {"written": True, "reasons": []}
            _atomic_write_json(meta_path, meta)
            token_path = staging_dir / "WAVE_TOKEN.txt"
            temporary = token_path.with_name(token_path.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(SUCCESS_TOKEN + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, token_path)

        os.replace(staging_dir, final_dir)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    # token_written is only defined on the success path above.
    return RunSummary(
        run_id=run_id,
        case_count=len(executed_cases),
        arm_order=ARM_ORDER,
        embedding_mode=boundary.embedding_mode,
        completed_at=completed_at,
        meta_path=final_dir / "meta.json",
        arms_written=arms_written,
        pools_written=pools_written,
        token_written=token_written,
    )


__all__ = [
    "ARM_ORDER", "META_SCHEMA_VERSION", "SUCCESS_TOKEN", "EmbeddingBoundary",
    "RunIdentities", "RunSummary", "RunnerValidationError",
    "build_temporal_identity_validation", "case_temporal_identity",
    "expected_case_temporal_identity", "extract_runtime_temporal_identity",
    "run_four_arm_cases", "validate_embedding_boundary", "validate_query_vector",
    "validate_runtime_temporal_identity", "validate_temporal_identity_validation",
    "_success_token_gate",
]
