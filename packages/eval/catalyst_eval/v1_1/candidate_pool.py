"""Q-011 candidate evidence-pool core (pointer-free, pre-approval).

Generates identity-bound, inactive-candidate evidence pools for the unsigned
12-case Q-011 packet using the existing fts5/dense/hybrid/reranked retrieval
path (``catalyst_data.retrieval.hybrid.retrieve_hybrid``), existing
``UnionJudgmentPool`` contract, and canonical arm order. It is eval-owned and
never publishes official GoldenCase files.

The CLI is production-path capable only on a cloud host: production_pinned
requires CUDA + the pinned BGE models and is never run locally. The core
accepts an injectable ``retrieve_case`` so fixture tests exercise the full
pool/artifact/identity contract offline without corpus-scale retrieval.

Outputs are explicitly unsigned and non-authoritative. The runner never calls
providers/Analyst/Writer, never promotes the candidate, never touches active
pointers, and refuses identity mismatches or active-pointer aliasing.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from catalyst_data.config import (
    BGE_M3_DIMENSION,
    BGE_M3_MODEL,
    BGE_M3_REVISION,
    BGE_RERANKER_MODEL,
    BGE_RERANKER_REVISION,
)
from catalyst_data.retrieval.pool import UnionJudgmentPool, write_union_pool

HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
ARM_ORDER = ("fts5", "dense", "hybrid", "reranked")
POOL_SCHEMA = "q011_candidate_pool_manifest_v1"
PACKET_SCHEMA = "q011_candidate_evidence_packet_v1"
POOL_SCHEMA_VERSION = "1.0.0"
EXCERPT_LIMIT = 500
Q011_SLOT_COUNT = 12
Q011_EXPECTED_SLOTS = tuple(f"c{index:02d}" for index in range(1, 13))


class CandidatePoolError(RuntimeError):
    """Fail-closed candidate evidence-pool operator error."""


@dataclass(frozen=True)
class CaseQuery:
    case_id: str
    ticker: str
    question: str
    cutoff: str
    session_date: str | None = None


@dataclass(frozen=True)
class CandidatePoolIdentity:
    run_id: str
    packet_sha256: str
    packet_path: str
    derivative: str
    build_id: str
    corpus_manifest_id: str
    source_bundle_id: str
    snapshot_id: str
    probe_report_id: str
    postbuild_readiness_id: str
    index_manifest_id: str
    table_name: str
    lancedb_dir: str
    model_name: str
    model_revision: str
    dimension: int
    reranker_model: str
    reranker_revision: str


def _require_hex64(value: str, *, label: str) -> None:
    if not isinstance(value, str) or HEX64_RE.fullmatch(value) is None:
        raise CandidatePoolError(f"{label} must be a lowercase SHA-256")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_q011_cases(packet_path: str | Path) -> list[CaseQuery]:
    """Load the unsigned Q-011 12-case packet as case queries."""
    path = Path(packet_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise CandidatePoolError("Q-011 packet must contain a cases array")
    if raw.get("unsigned_q011") is not True:
        raise CandidatePoolError("Q-011 packet must be unsigned (unsigned_q011=true)")
    cases = raw["cases"]
    if len(cases) != Q011_SLOT_COUNT:
        raise CandidatePoolError(
            f"Q-011 packet must contain {Q011_SLOT_COUNT} cases, got {len(cases)}"
        )
    queries: list[CaseQuery] = []
    for slot, case in zip(Q011_EXPECTED_SLOTS, cases):
        if not isinstance(case, dict) or case.get("slot") != slot:
            raise CandidatePoolError(
                f"Q-011 case order mismatch: expected {slot}, got {case.get('slot')!r}"
            )
        for field in ("ticker", "question", "cutoff_utc"):
            if not isinstance(case.get(field), str) or not case[field]:
                raise CandidatePoolError(f"Q-011 case {slot} missing {field}")
        queries.append(
            CaseQuery(
                case_id=slot,
                ticker=case["ticker"],
                question=case["question"],
                cutoff=case["cutoff_utc"],
                session_date=case.get("session_date"),
            )
        )
    return queries


def load_candidate_identity(
    *,
    run_id: str,
    packet_path: str | Path,
    derivative: str | Path,
    build_id: str,
    corpus_manifest_id: str,
    source_bundle_id: str,
    snapshot_id: str,
    probe_report_id: str,
    postbuild_readiness_id: str,
    lancedb_dir: str | Path,
    index_manifest_id: str | None = None,
    table_name: str | None = None,
) -> CandidatePoolIdentity:
    """Build and validate candidate identity, including inactive status."""
    for value, label in (
        (build_id, "build_id"),
        (corpus_manifest_id, "corpus_manifest_id"),
        (source_bundle_id, "source_bundle_id"),
        (snapshot_id, "snapshot_id"),
        (probe_report_id, "probe_report_id"),
        (postbuild_readiness_id, "postbuild_readiness_id"),
    ):
        _require_hex64(value, label=label)
    candidate_dir = Path(lancedb_dir)
    generation_path = candidate_dir / "candidate_generation.json"
    if generation_path.is_file():
        payload = json.loads(generation_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "candidate_generation_v1":
            raise CandidatePoolError("candidate_generation.json schema mismatch")
        if payload.get("status") != "inactive":
            raise CandidatePoolError(
                "candidate dense generation is not inactive; refusing evidence pool"
            )
        if payload.get("corpus_manifest_id") != corpus_manifest_id:
            raise CandidatePoolError("candidate_generation corpus_manifest_id mismatch")
        if index_manifest_id is not None and payload.get("index_manifest_id") != index_manifest_id:
            raise CandidatePoolError("candidate_generation index_manifest_id mismatch")
        if table_name is not None and payload.get("table_name") != table_name:
            raise CandidatePoolError("candidate_generation table_name mismatch")
        index_manifest_id = index_manifest_id or payload.get("index_manifest_id")
        table_name = table_name or payload.get("table_name")
    if index_manifest_id is None or table_name is None:
        raise CandidatePoolError(
            "index_manifest_id/table_name must be supplied or present in "
            "candidate_generation.json"
        )
    _require_hex64(index_manifest_id, label="index_manifest_id")
    if not isinstance(table_name, str) or not table_name:
        raise CandidatePoolError("table_name must be non-empty")
    packet = Path(packet_path)
    return CandidatePoolIdentity(
        run_id=run_id,
        packet_sha256=sha256_bytes(packet.read_bytes()),
        packet_path=str(packet.resolve()),
        derivative=str(Path(derivative).resolve()),
        build_id=build_id,
        corpus_manifest_id=corpus_manifest_id,
        source_bundle_id=source_bundle_id,
        snapshot_id=snapshot_id,
        probe_report_id=probe_report_id,
        postbuild_readiness_id=postbuild_readiness_id,
        index_manifest_id=index_manifest_id,
        table_name=table_name,
        lancedb_dir=str(Path(lancedb_dir).resolve()),
        model_name=BGE_M3_MODEL,
        model_revision=BGE_M3_REVISION,
        dimension=BGE_M3_DIMENSION,
        reranker_model=BGE_RERANKER_MODEL,
        reranker_revision=BGE_RERANKER_REVISION,
    )


def _check_active_alias(*, lancedb_dir: Path, active_lancedb_dir: Path | None,
                        active_generation_pointer: Path | None) -> None:
    candidate = lancedb_dir.resolve()
    if active_lancedb_dir is not None:
        active = active_lancedb_dir.resolve()
        if candidate == active or candidate.is_relative_to(active) or active.is_relative_to(candidate):
            raise CandidatePoolError(
                f"candidate LanceDB dir {candidate} aliases active dir {active}"
            )
    if active_generation_pointer is not None:
        pointer = active_generation_pointer.resolve()
        if candidate == pointer or pointer.is_relative_to(candidate):
            raise CandidatePoolError(
                f"candidate LanceDB dir {candidate} aliases active pointer {pointer}"
            )


def _verify_derivative_inactive(derivative: Path, corpus_manifest_id: str) -> None:
    if not derivative.is_file():
        raise CandidatePoolError(f"candidate derivative DB not found: {derivative}")
    uri = f"{derivative.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            "SELECT is_current FROM corpus_manifest WHERE manifest_id=?",
            (corpus_manifest_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise CandidatePoolError(
            f"corpus_manifest {corpus_manifest_id} not found on derivative"
        )
    if int(row[0] or 0) != 0:
        raise CandidatePoolError(
            f"corpus_manifest {corpus_manifest_id} is_current=1; "
            "refusing active-pointer-derived identity"
        )


def preflight_candidate_pool(
    *,
    identity: CandidatePoolIdentity,
    packet_path: str | Path,
    active_lancedb_dir: Path | None = None,
    active_generation_pointer: Path | None = None,
) -> dict[str, Any]:
    """Validate identities and packet without writing any output."""
    cases = load_q011_cases(packet_path)
    _check_active_alias(
        lancedb_dir=Path(identity.lancedb_dir),
        active_lancedb_dir=active_lancedb_dir,
        active_generation_pointer=active_generation_pointer,
    )
    _verify_derivative_inactive(Path(identity.derivative), identity.corpus_manifest_id)
    return {
        "schema_version": POOL_SCHEMA,
        "mode": "preflight",
        "run_id": identity.run_id,
        "packet_sha256": identity.packet_sha256,
        "case_count": len(cases),
        "corpus_manifest_id": identity.corpus_manifest_id,
        "index_manifest_id": identity.index_manifest_id,
        "table_name": identity.table_name,
    }


def _pointer_bytes(pointer: Path | None) -> bytes | None:
    if pointer is None:
        return None
    return pointer.read_bytes() if pointer.is_file() else b""


def _canonical_dumps(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _result_scores(result: Any, *, arm: str) -> dict[str, Any]:
    mapping = {
        "fts5": ("lexical_rank", "lexical_raw_score"),
        "dense": ("dense_rank", "dense_score"),
        "hybrid": ("fusion_rank", "fusion_score"),
        "reranked": ("reranker_rank", "reranker_score"),
    }
    rank_field, score_field = mapping[arm]
    return {
        "rank": getattr(result, rank_field, None),
        "score": getattr(result, score_field, None),
        "arm_ranks": [list(pair) for pair in (getattr(result, "arm_ranks", None) or ())],
        "arm_scores": [list(pair) for pair in (getattr(result, "arm_scores", None) or ())],
    }


def _packet_row_for_result(result: Any, *, case: CaseQuery, arm: str) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "evidence_id": result.chunk_id,
        "canonical_asset_id": getattr(result, "canonical_asset_id", None),
        "canonical_content_version_id": getattr(result, "canonical_content_version_id", None),
        "document_id": result.document_id,
        "source_class": getattr(result, "source_class", None),
        "available_at": result.available_at,
        "case_cutoff_utc": case.cutoff,
        "cutoff_decision": "eligible" if str(result.available_at) <= case.cutoff else "post_cutoff_excluded",
        "excerpt": str(getattr(result, "content_text", ""))[:EXCERPT_LIMIT],
        "excerpt_bounded": True,
        "arm": arm,
        "ticker_associations": list(getattr(result, "ticker_associations", ()) or ()),
        "dedup_cluster_id": getattr(result, "dedup_cluster_id", None),
        "cluster_first_available_at": getattr(result, "cluster_first_available_at", None),
        "representative_document_id": getattr(result, "representative_document_id", None),
        **_result_scores(result, arm=arm),
    }


def _arm_rows(
    hybrid: Any, *, case: CaseQuery
) -> dict[str, Sequence[Any]]:
    fts5_set = getattr(hybrid, "lexical_results", None)
    dense_set = getattr(hybrid, "dense_results", None)
    return {
        "fts5": tuple(fts5_set.results) if fts5_set is not None else (),
        "dense": tuple(dense_set.results) if dense_set is not None else (),
        "hybrid": tuple(getattr(hybrid, "fusion_results", ()) or ()),
        "reranked": tuple(getattr(hybrid, "final_results", ()) or ()),
    }


def run_candidate_pools(
    *,
    identity: CandidatePoolIdentity,
    packet_path: str | Path,
    output_dir: str | Path,
    retrieve_case: Callable[[CaseQuery], Any],
    active_lancedb_dir: Path | None = None,
    active_generation_pointer: Path | None = None,
    embedding_mode: str = "mock_unit_test",
) -> dict[str, Any]:
    """Generate 12 identity-bound candidate pools + bounded annotation packet.

    Production callers pass a ``retrieve_case`` built on the existing
    retrieve_hybrid implementation; fixture tests inject deterministic hybrid
    results. Never writes official GoldenCase files and never mutates active
    pointers.
    """
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise CandidatePoolError(f"output dir must be empty or absent: {out}")
    preflight = preflight_candidate_pool(
        identity=identity,
        packet_path=packet_path,
        active_lancedb_dir=active_lancedb_dir,
        active_generation_pointer=active_generation_pointer,
    )
    cases = load_q011_cases(packet_path)
    pointer_before = _pointer_bytes(active_generation_pointer)
    out.mkdir(parents=True, exist_ok=False)
    pools_dir = out / "pools"
    packets_dir = out / "packets"
    pools_dir.mkdir()
    packets_dir.mkdir()

    pool_hashes: dict[str, str] = {}
    packet_hashes: dict[str, str] = {}
    row_count = 0
    try:
        for case in cases:
            hybrid = retrieve_case(case)
            arms = _arm_rows(hybrid, case=case)
            per_arm_chunk_ids = {
                name: tuple(dict.fromkeys(item.chunk_id for item in items))
                for name, items in arms.items()
            }
            union: list[str] = []
            seen: set[str] = set()
            for name in ARM_ORDER:
                for chunk_id in per_arm_chunk_ids[name]:
                    if chunk_id not in seen:
                        seen.add(chunk_id)
                        union.append(chunk_id)
            source_artifact_id = sha256_bytes(
                _canonical_dumps({"case_id": case.case_id, "union": union})
            )
            pool = UnionJudgmentPool(
                schema_version=POOL_SCHEMA_VERSION,
                case_id=case.case_id,
                chunk_ids=tuple(union),
                per_arm_chunk_ids=per_arm_chunk_ids,
                corpus_manifest_id=identity.corpus_manifest_id,
                index_manifest_id=identity.index_manifest_id,
                source_artifact_id=source_artifact_id,
            )
            pool_path = pools_dir / f"{case.case_id}.json"
            write_union_pool(pool, pool_path)
            pool_hashes[case.case_id] = sha256_bytes(pool_path.read_bytes())

            rows = [
                _packet_row_for_result(item, case=case, arm=name)
                for name in ARM_ORDER
                for item in arms[name]
            ]
            row_count += len(rows)
            packet_payload = {
                "schema_version": PACKET_SCHEMA,
                "case_id": case.case_id,
                "ticker": case.ticker,
                "question": case.question,
                "cutoff_utc": case.cutoff,
                "arm_order": list(ARM_ORDER),
                "corpus_manifest_id": identity.corpus_manifest_id,
                "index_manifest_id": identity.index_manifest_id,
                "table_name": identity.table_name,
                "non_authoritative": True,
                "unsigned_q011": True,
                "rows": rows,
            }
            packet_path_out = packets_dir / f"{case.case_id}.json"
            packet_path_out.write_text(
                json.dumps(packet_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            packet_hashes[case.case_id] = sha256_bytes(packet_path_out.read_bytes())
    except BaseException:
        import shutil

        shutil.rmtree(out, ignore_errors=True)
        raise

    if active_generation_pointer is not None and _pointer_bytes(active_generation_pointer) != pointer_before:
        raise CandidatePoolError("active-generation pointer changed during candidate pool run")

    manifest_payload = {
        "schema_version": POOL_SCHEMA,
        "run_id": identity.run_id,
        "mode": "execute",
        "status": "inactive_candidate_evidence_pool",
        "generated_at_utc": _utc_now(),
        "embedding_mode": embedding_mode,
        "non_authoritative": True,
        "unsigned_q011": True,
        "packet": {
            "path": identity.packet_path,
            "sha256": identity.packet_sha256,
        },
        "identity": {
            "derivative": identity.derivative,
            "build_id": identity.build_id,
            "corpus_manifest_id": identity.corpus_manifest_id,
            "source_bundle_id": identity.source_bundle_id,
            "snapshot_id": identity.snapshot_id,
            "probe_report_id": identity.probe_report_id,
            "postbuild_readiness_id": identity.postbuild_readiness_id,
            "index_manifest_id": identity.index_manifest_id,
            "table_name": identity.table_name,
            "lancedb_dir": identity.lancedb_dir,
            "model_name": identity.model_name,
            "model_revision": identity.model_revision,
            "reranker_model": identity.reranker_model,
            "reranker_revision": identity.reranker_revision,
        },
        "case_count": len(cases),
        "row_count": row_count,
        "pool_hashes": pool_hashes,
        "packet_hashes": packet_hashes,
        "pointer_unchanged": True,
    }
    manifest_path = out / "candidate_pool_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_payload


__all__ = [
    "ARM_ORDER",
    "CandidatePoolError",
    "CandidatePoolIdentity",
    "CaseQuery",
    "load_candidate_identity",
    "load_q011_cases",
    "preflight_candidate_pool",
    "run_candidate_pools",
]
