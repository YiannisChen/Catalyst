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

Fail-closed corrections (2026-09):

- Active-generation protection is mandatory for ``execute``: the active
  LanceDB directory and active-generation pointer must be named and their
  bounded streaming fingerprints must be unchanged after the run.
- The candidate directory must contain an existing ``candidate_generation.json``
  (status ``inactive``) plus the authoritative ``index_manifest.json`` copy;
  every identity is cross-checked against the repository IndexManifest and the
  candidate generation record. Manually supplied substitute identities are
  never accepted when the file is absent.
- Every successful pool run must contain genuine, non-degraded, non-empty
  fts5/dense/hybrid/reranked arms with a real reranker-served final arm.
- Each pool's ``source_artifact_id`` is the persisted canonical arm artifact ID
  (``compute_arm_artifact_id``), not an ad-hoc {case_id, union} digest.
- Timestamp eligibility uses typed timezone-aware UTC parsing, never raw
  lexical string comparison.
- Outputs are explicitly unsigned and non-authoritative. The runner never
  calls providers/Analyst/Writer, never promotes the candidate, never touches
  active pointers, and refuses identity mismatches or active-pointer aliasing.
"""
from __future__ import annotations

import hashlib
import json
import math
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
from catalyst_data.index.candidate_staging_cli import _bounded_directory_digest
from catalyst_data.retrieval.artifacts import (
    compute_arm_artifact_id,
    load_arm_artifact,
    write_arm_artifact,
)
from catalyst_data.retrieval.pool import generate_union_pool, write_union_pool

# Canonical arm-artifact serializers live in the existing four-arm runner.
# candidate_pool reuses them so there is exactly one retrieval-arm artifact
# serializer/identity formula in the repository.
from catalyst_eval.post_import import four_arm as _four_arm

HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
ARM_ORDER = ("fts5", "dense", "hybrid", "reranked")
POOL_SCHEMA = "q011_candidate_pool_manifest_v1"
PACKET_SCHEMA = "q011_candidate_evidence_packet_v1"
POOL_SCHEMA_VERSION = "1.0.0"
EXCERPT_LIMIT = 500
Q011_SLOT_COUNT = 12
Q011_EXPECTED_SLOTS = tuple(f"c{index:02d}" for index in range(1, 13))

_GENERATION_FIELDS = (
    "schema_version",
    "status",
    "index_manifest_id",
    "source_bundle_id",
    "table_name",
    "chunk_count",
    "corpus_manifest_id",
    "embedding_model",
    "embedding_revision",
    "embedding_dimension",
)
_CHUNK_PROFILE_VERSION = "candidate_chunk_profile_v1"
_MODE_SERVED_CONTRACT = {"fts5", "dense", "hybrid", "reranked", "sql_like", "failed"}


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


def _canonical_dumps(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _parse_utc_datetime(value: Any, *, label: str) -> datetime:
    """Parse a timestamp as a timezone-aware UTC datetime.

    Accepts ISO-8601 representations with 'Z' or a numeric UTC offset and
    optional fractional seconds.  Naive timestamps and malformed values are
    rejected so eligibility ordering is never a lexical comparison.
    """
    if not isinstance(value, str) or not value:
        raise CandidatePoolError(f"{label} must be a non-empty ISO-8601 timestamp")
    text = value
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CandidatePoolError(
            f"{label} is not a valid ISO-8601 timestamp: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CandidatePoolError(f"{label} must be timezone-aware UTC: {value!r}")
    if parsed.utcoffset().total_seconds() != 0:
        parsed = parsed.astimezone(timezone.utc)
    return parsed


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
        _parse_utc_datetime(case["cutoff_utc"], label=f"case {slot} cutoff_utc")
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


def _candidate_table_count(candidate_dir: Path, table_name: str) -> int | None:
    """Bounded vector count for the candidate LanceDB table.

    Uses the LanceDB row-count API when available; never loads table contents
    into RAM.  Returns None only when the table is absent (fail-closed caller
    decides) or lancedb is not importable (unit environments).
    """
    try:
        import lancedb  # type: ignore
    except Exception:
        return None
    try:
        db = lancedb.connect(str(candidate_dir))
        names = set(db.table_names())
    except Exception:
        return None
    if table_name not in names:
        return None
    try:
        table = db.open_table(table_name)
        return int(table.count_rows())
    except Exception:
        return None


def _load_candidate_identity_payload(candidate_dir: Path) -> dict[str, Any]:
    generation_path = candidate_dir / "candidate_generation.json"
    if not generation_path.is_file():
        raise CandidatePoolError(
            "candidate_generation.json is required for an inactive candidate "
            "evidence pool; refusing manually supplied substitute identities"
        )
    try:
        payload = json.loads(generation_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CandidatePoolError("candidate_generation.json is malformed") from exc
    if not isinstance(payload, dict):
        raise CandidatePoolError("candidate_generation.json must be an object")
    missing = [name for name in _GENERATION_FIELDS if payload.get(name) in (None, "")]
    if missing:
        raise CandidatePoolError(
            "candidate_generation.json is missing required identity fields: "
            + ",".join(missing)
        )
    if payload.get("schema_version") != "candidate_generation_v1":
        raise CandidatePoolError("candidate_generation.json schema mismatch")
    if payload.get("status") != "inactive":
        raise CandidatePoolError(
            "candidate dense generation is not inactive; refusing evidence pool"
        )
    return payload


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
    """Build and validate candidate identity from the staged candidate records.

    The candidate directory must already contain an inactive
    ``candidate_generation.json`` and its authoritative ``index_manifest.json``.
    The generation record, IndexManifest, and caller-supplied identities are
    cross-checked; no missing file is silently replaced by CLI identities.
    """
    for value, label in (
        (build_id, "build_id"),
        (corpus_manifest_id, "corpus_manifest_id"),
        (source_bundle_id, "source_bundle_id"),
        (snapshot_id, "snapshot_id"),
        (probe_report_id, "probe_report_id"),
        (postbuild_readiness_id, "postbuild_readiness_id"),
    ):
        _require_hex64(value, label=label)

    candidate_dir = Path(lancedb_dir).resolve()
    payload = _load_candidate_identity_payload(candidate_dir)

    # 1. IndexManifest file is the authoritative chain for model/revision and
    #    the snapshot/probe/postbuild/source identities.
    manifest_path = candidate_dir / "index_manifest.json"
    if not manifest_path.is_file():
        raise CandidatePoolError(
            "candidate directory is missing its authoritative index_manifest.json"
        )
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CandidatePoolError("candidate index_manifest.json is malformed") from exc
    if not isinstance(raw_manifest, dict):
        raise CandidatePoolError("candidate index_manifest.json must be an object")
    from catalyst_data.retrieval.index_manifest import IndexManifest

    try:
        manifest = IndexManifest.from_dict(raw_manifest)
    except Exception as exc:
        raise CandidatePoolError(
            "candidate index_manifest.json is not a valid IndexManifest"
        ) from exc

    # 2. Generation record vs IndexManifest.
    disagreements = []
    if payload.get("index_manifest_id") != manifest.index_manifest_id:
        disagreements.append("index_manifest_id")
    if payload.get("source_bundle_id") != manifest.source_bundle_id:
        disagreements.append("source_bundle_id")
    if payload.get("corpus_manifest_id") != manifest.corpus_manifest_id:
        disagreements.append("corpus_manifest_id")
    if payload.get("chunk_count") != manifest.vector_count:
        disagreements.append("chunk_count/vector_count")
    if payload.get("embedding_model") != manifest.model_name:
        disagreements.append("embedding_model")
    if payload.get("embedding_revision") != manifest.model_revision:
        disagreements.append("embedding_revision")
    if payload.get("embedding_dimension") != manifest.dimension:
        disagreements.append("embedding_dimension")
    expected_table = f"candidate_{manifest.index_manifest_id[:16]}"
    if payload.get("table_name") != expected_table:
        disagreements.append("table_name")
    if disagreements:
        raise CandidatePoolError(
            "candidate_generation/IndexManifest disagreement: "
            + ",".join(disagreements)
        )

    # 3. Caller-supplied identity chain must agree with the records.
    cli_checks = [
        ("index_manifest_id", index_manifest_id, manifest.index_manifest_id),
        ("table_name", table_name, expected_table),
        ("corpus_manifest_id", corpus_manifest_id, payload.get("corpus_manifest_id")),
        ("source_bundle_id", source_bundle_id, payload.get("source_bundle_id")),
        ("snapshot_id", snapshot_id, manifest.snapshot_id),
        ("probe_report_id", probe_report_id, manifest.probe_report_id),
        ("postbuild_readiness_id", postbuild_readiness_id, manifest.postbuild_readiness_id),
    ]
    mismatched = [
        label
        for label, supplied, expected in cli_checks
        if supplied is not None and expected is not None and supplied != expected
    ]
    if mismatched:
        raise CandidatePoolError(
            "supplied candidate identity disagrees with authoritative records: "
            + ",".join(mismatched)
        )
    if payload.get("chunk_count") != int(manifest.vector_count or 0):
        raise CandidatePoolError(
            "candidate chunk/vector count mismatch with IndexManifest"
        )

    # 4. Bounded table existence/count check when the table is present.
    table_count = _candidate_table_count(candidate_dir, expected_table)
    if table_count is not None and table_count != int(payload.get("chunk_count") or 0):
        raise CandidatePoolError(
            "candidate LanceDB table count mismatch: "
            f"table={table_count} generation={payload.get('chunk_count')}"
        )

    # 5. Derivative corpus manifest must remain inactive.
    derivative_path = Path(derivative).resolve()
    if not derivative_path.is_file():
        raise CandidatePoolError(f"candidate derivative DB not found: {derivative_path}")
    uri = f"{derivative_path.as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            "SELECT is_current FROM corpus_manifest WHERE manifest_id=?",
            (payload.get("corpus_manifest_id"),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise CandidatePoolError(
            f"corpus_manifest {payload.get('corpus_manifest_id')} not found on derivative"
        )
    if int(row[0] or 0) != 0:
        raise CandidatePoolError(
            f"corpus_manifest {payload.get('corpus_manifest_id')} is_current=1; "
            "refusing active-corpus-derived identity"
        )

    packet = Path(packet_path)
    return CandidatePoolIdentity(
        run_id=run_id,
        packet_sha256=sha256_bytes(packet.read_bytes()),
        packet_path=str(packet.resolve()),
        derivative=str(derivative_path),
        build_id=build_id,
        corpus_manifest_id=str(payload.get("corpus_manifest_id")),
        source_bundle_id=str(payload.get("source_bundle_id")),
        snapshot_id=snapshot_id,
        probe_report_id=probe_report_id,
        postbuild_readiness_id=postbuild_readiness_id,
        index_manifest_id=manifest.index_manifest_id,
        table_name=expected_table,
        lancedb_dir=str(candidate_dir),
        model_name=BGE_M3_MODEL,
        model_revision=BGE_M3_REVISION,
        dimension=BGE_M3_DIMENSION,
        reranker_model=BGE_RERANKER_MODEL,
        reranker_revision=BGE_RERANKER_REVISION,
    )


def _resolve_lexical(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:  # pragma: no cover - defensive
        return Path(str(path))


def _check_active_alias(
    *,
    lancedb_dir: Path,
    active_lancedb_dir: Path | None,
    active_generation_pointer: Path | None,
) -> None:
    candidate = _resolve_lexical(lancedb_dir)
    if active_lancedb_dir is not None:
        active = _resolve_lexical(active_lancedb_dir)
        if candidate == active or candidate.is_relative_to(active) or active.is_relative_to(candidate):
            raise CandidatePoolError(
                f"candidate LanceDB dir {candidate} aliases active dir {active}"
            )
    if active_generation_pointer is not None:
        pointer = _resolve_lexical(active_generation_pointer)
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


def _cutoff_decision(available_at: Any, cutoff: Any) -> str:
    available = _parse_utc_datetime(available_at, label="available_at")
    cutoff_at = _parse_utc_datetime(cutoff, label="cutoff")
    return "eligible" if available <= cutoff_at else "post_cutoff_excluded"


def _available_meta_fields(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    if meta is None:
        return {}
    return {
        key: meta.get(key)
        for key in (
            "canonical_asset_id",
            "content_version_id",
            "content_hash",
            "metadata_hash",
            "content_state",
            "independence_group_id",
            "parse_quality",
            "section_parse_degraded",
            "provider",
            "corpus_document_id",
            "source_class",
        )
        if key in meta and meta.get(key) is not None
    }


def _packet_row_for_result(
    result: Any,
    *,
    case: CaseQuery,
    arm: str,
    meta: Mapping[str, Any] | None = None,
    identity: CandidatePoolIdentity | None = None,
) -> dict[str, Any]:
    meta_fields = _available_meta_fields(meta)
    content_text = str(getattr(result, "content_text", "") or "")
    available_at = str(getattr(result, "available_at", "") or "")
    decision = _cutoff_decision(available_at, case.cutoff)
    ticker_associations = list(getattr(result, "ticker_associations", ()) or ())
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "evidence_id": result.chunk_id,
        "canonical_asset_id": meta_fields.get("canonical_asset_id"),
        "content_version_id": meta_fields.get("content_version_id"),
        "document_id": getattr(result, "document_id", None)
        or meta_fields.get("corpus_document_id"),
        "source_class": getattr(result, "source_class", None)
        or meta_fields.get("source_class"),
        "available_at": available_at,
        "case_cutoff_utc": case.cutoff,
        "temporal_status": decision,
        "excerpt": content_text[:EXCERPT_LIMIT],
        "excerpt_bounded": True,
        "ticker": ticker_associations[0] if ticker_associations else case.ticker,
        "ticker_associations": ticker_associations,
        "dedup_cluster_id": getattr(result, "dedup_cluster_id", None),
        "cluster_first_available_at": getattr(result, "cluster_first_available_at", None),
        "representative_document_id": getattr(result, "representative_document_id", None),
        "content_hash": meta_fields.get("content_hash"),
        "metadata_hash": meta_fields.get("metadata_hash"),
        "chunk_profile_version": meta_fields.get("chunk_profile_version")
        or _CHUNK_PROFILE_VERSION,
        "independence_group_id": meta_fields.get("independence_group_id"),
        "parse_quality": meta_fields.get("parse_quality"),
        "content_state": meta_fields.get("content_state"),
        "section_parse_degraded": meta_fields.get("section_parse_degraded"),
        "arm": arm,
        **_result_scores(result, arm=arm),
    }
    if identity is not None:
        row.update(
            {
                "corpus_manifest_id": identity.corpus_manifest_id,
                "index_manifest_id": identity.index_manifest_id,
                "table_name": identity.table_name,
                "build_id": identity.build_id,
                "source_bundle_id": identity.source_bundle_id,
                "snapshot_id": identity.snapshot_id,
                "probe_report_id": identity.probe_report_id,
                "postbuild_readiness_id": identity.postbuild_readiness_id,
            }
        )
    return {key: value for key, value in row.items()}


def _arm_rows(hybrid: Any) -> dict[str, Sequence[Any]]:
    fts5_set = getattr(hybrid, "lexical_results", None)
    dense_set = getattr(hybrid, "dense_results", None)
    return {
        "fts5": tuple(fts5_set.results) if fts5_set is not None else (),
        "dense": tuple(dense_set.results) if dense_set is not None else (),
        "hybrid": tuple(getattr(hybrid, "fusion_results", ()) or ()),
        "reranked": tuple(getattr(hybrid, "final_results", ()) or ()),
    }


def validate_four_arm_served(hybrid: Any, *, case_id: str) -> dict[str, Sequence[Any]]:
    """Require genuine, non-degraded, non-empty four arms.

    A timeout, unavailable model, degraded mode, empty arm, or fallback hybrid
    result is never labeled ``reranked`` and fails the pool run.
    """
    mode_served = str(getattr(hybrid, "mode_served", "") or "")
    if mode_served != "reranked":
        raise CandidatePoolError(
            f"case {case_id}: production pool run must serve reranked mode; "
            f"got mode_served={mode_served!r}"
        )
    degradation = tuple(getattr(hybrid, "degradation_reasons", ()) or ())
    if degradation:
        raise CandidatePoolError(
            f"case {case_id}: degraded retrieval cannot be published as a "
            f"success pool: {','.join(degradation)}"
        )
    arms = _arm_rows(hybrid)
    problems: list[str] = []
    for name in ARM_ORDER:
        items = arms[name]
        if not items:
            problems.append(f"{name} arm is empty")
        for item in items:
            served = str(getattr(item, "mode_served", "") or "")
            requested = str(getattr(item, "mode_requested", "") or "")
            if served not in _MODE_SERVED_CONTRACT:
                problems.append(f"{name} arm has invalid mode_served {served!r}")
            if bool(getattr(item, "is_degraded", False)):
                problems.append(f"{name} arm result is degraded")
            if served == "failed":
                problems.append(f"{name} arm result is failed")
            if requested and name == "fts5" and requested not in {"lexical", "fts5"}:
                problems.append(f"{name} arm mode_requested mismatch")
    # Reranked arm coherence: every final result must prove the reranker served
    # the row (finite reranker_score and a 1-based reranker_rank).
    final = arms["reranked"]
    for item in final:
        score = getattr(item, "reranker_score", None)
        rank = getattr(item, "reranker_rank", None)
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(float(score)):
            problems.append("reranked arm result lacks a finite reranker score")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            problems.append("reranked arm result lacks a valid reranker rank")
    if problems:
        raise CandidatePoolError(
            f"case {case_id}: four-arm retrieval is not a genuine success: "
            + "; ".join(problems)
        )
    return arms


def _candidate_metadata_lookup(
    derivative: Path,
    corpus_manifest_id: str,
    chunk_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Bounded per-case canonical metadata lookup from the candidate DB.

    Reads only the requested chunk rows (never whole tables/documents). Fields
    that do not exist on the authoritative schema stay unavailable in packets.
    """
    if not chunk_ids:
        return {}
    uri = f"{derivative.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = conn.execute(
            f"""
            SELECT c.chunk_id, c.document_id, c.content_hash, c.metadata_hash,
                   c.chunk_profile_version, c.source_class, c.dedup_cluster_id,
                   c.cluster_first_available_at, c.representative_document_id,
                   c.canonical_asset_id, c.content_version_id,
                   c.corpus_document_id, c.content_state,
                   c.independence_group_id, c.parse_quality,
                   c.section_parse_degraded, c.available_at
            FROM corpus_build_chunks c
            JOIN corpus_publication_builds b ON b.build_id = c.build_id
            WHERE b.manifest_id = ? AND c.chunk_id IN ({placeholders})
            """,
            (corpus_manifest_id, *chunk_ids),
        ).fetchall()
    except sqlite3.Error:
        # Table set may be a fixture subset; enrichment is best-effort and
        # bounded. Missing metadata fields are reported as unavailable.
        return {}
    finally:
        conn.close()
    columns = (
        "chunk_id", "document_id", "content_hash", "metadata_hash",
        "chunk_profile_version", "source_class", "dedup_cluster_id",
        "cluster_first_available_at", "representative_document_id",
        "canonical_asset_id", "content_version_id", "corpus_document_id",
        "content_state", "independence_group_id", "parse_quality",
        "section_parse_degraded", "available_at",
    )
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        record = dict(zip(columns, raw))
        result[str(record["chunk_id"])] = record
    return result


def _pinned_retrieval_config(reranker_timeout_seconds: float) -> dict[str, Any]:
    return {
        "lexical_top_k": 20,
        "dense_top_k": 20,
        "fusion_k": 60,
        "fused_top_k": 20,
        "display_top_k": 8,
        "embedding_revision": BGE_M3_REVISION,
        "reranker_revision": BGE_RERANKER_REVISION,
        "reranker_timeout_seconds": float(reranker_timeout_seconds),
    }


def _write_case_arm_artifact(
    *,
    root: Path,
    identity: CandidatePoolIdentity,
    case: CaseQuery,
    hybrid: Any,
    arms: dict[str, Sequence[Any]],
    latency_ms: float,
    reranker_timeout_seconds: float,
    started_at: str,
) -> Path:
    """Persist one canonical retrieval-arm artifact per case (B5)."""
    lexical_set = getattr(hybrid, "lexical_results", None)
    dense_set = getattr(hybrid, "dense_results", None)
    artifact_arms: dict[str, dict[str, Any]] = {}
    if lexical_set is None:
        artifact_arms["fts5"] = _four_arm._failed_arm("fts5", "fts5_unavailable")
    else:
        artifact_arms["fts5"] = _four_arm._set_to_arm(
            lexical_set,
            mode_requested="fts5",
            latency_ms=float(getattr(lexical_set.trace, "total_ms", 0) or 0.0),
            degradation_reasons=(
                (lexical_set.fallback_reason,)
                if getattr(lexical_set, "is_degraded", False)
                and lexical_set.fallback_reason
                else ()
            ),
        )
    if dense_set is None:
        artifact_arms["dense"] = _four_arm._failed_arm("dense", "dense_unavailable")
    else:
        artifact_arms["dense"] = _four_arm._set_to_arm(
            dense_set,
            mode_requested="dense",
            latency_ms=float(getattr(dense_set.trace, "total_ms", 0) or 0.0),
        )
    artifact_arms["hybrid"] = _four_arm._hybrid_to_arm(
        hybrid, mode_requested="hybrid", latency_ms=latency_ms, use_fusion=True,
    )
    artifact_arms["reranked"] = _four_arm._hybrid_to_arm(
        hybrid, mode_requested="reranked", latency_ms=latency_ms, use_fusion=False,
    )
    path = write_arm_artifact(
        root=root,
        run_id=identity.run_id,
        case_id=case.case_id,
        query=case.question,
        cutoff_ts=case.cutoff,
        filters={
            "ticker": case.ticker,
            "evidence_types": [],
            "source_classes": [],
            "corpus_manifest_id": identity.corpus_manifest_id,
            "index_manifest_id": identity.index_manifest_id,
        },
        retrieval_config=_pinned_retrieval_config(reranker_timeout_seconds),
        arms=artifact_arms,
        effect_metrics=_four_arm._compute_effect_metrics(artifact_arms, hybrid),
        created_at=started_at,
    )
    return path


def _require_execution_guards(
    *,
    active_lancedb_dir: Path | None,
    active_generation_pointer: Path | None,
) -> None:
    missing = []
    if active_lancedb_dir is None:
        missing.append("--active-lancedb-dir")
    if active_generation_pointer is None:
        missing.append("--active-generation-pointer")
    if missing:
        raise CandidatePoolError(
            "execute requires explicit active-generation protection; missing: "
            + ", ".join(missing)
        )


def build_production_retriever(
    *,
    derivative: str | Path,
    lancedb_dir: str | Path,
    table_name: str,
    index_manifest_id: str,
    corpus_manifest_id: str,
    reranker_timeout: float = 2.0,
    cuda_available: Callable[[], bool] | None = None,
    embedder_factory: Any | None = None,
    reranker_loader: Callable[[], Any] | None = None,
):
    """Build the cloud-only production retrieve_case over the inactive candidate.

    Fails closed before any 12-case run when CUDA or the pinned offline
    BGE-M3/reranker contract cannot be satisfied.  The returned callable is
    bound to a read-only candidate SQLite URI and the candidate LanceDB table.
    """
    import lancedb
    import torch

    from catalyst_agents.runtime.query_embedding import (
        ProductionBgeM3QueryEmbeddingFactory,
    )
    from catalyst_data.retrieval.hybrid import retrieve_hybrid as _retrieve_hybrid
    from catalyst_data.retrieval.reranker import rerank as _rerank

    if cuda_available is None:
        cuda_available = lambda: bool(torch.cuda.is_available())  # noqa: E731
    if not cuda_available():
        raise CandidatePoolError("production_pinned requires CUDA (cloud-only)")

    factory = embedder_factory or ProductionBgeM3QueryEmbeddingFactory()
    embedder = factory.create(model_name=BGE_M3_MODEL)
    # Reranker contract: pinned offline model.  load_reranker returns None on
    # failure, so a None reranker must abort the pool before the first case.
    reranker = None
    if reranker_loader is None:
        from catalyst_data.storage.lancedb_store import load_reranker

        reranker = load_reranker(model_name=BGE_RERANKER_MODEL)
    else:
        reranker = reranker_loader()
    if reranker is None:
        raise CandidatePoolError("production reranker could not be loaded")

    db_path = Path(derivative)
    if not db_path.is_file():
        raise CandidatePoolError(f"candidate derivative DB not found: {db_path}")
    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    lancedb_conn = lancedb.connect(str(lancedb_dir))
    table = lancedb_conn.open_table(table_name)
    closed = {"value": False}

    def _retrieve_case(case):
        if closed["value"]:
            raise CandidatePoolError("retriever is closed")
        try:
            return _retrieve_hybrid(
                conn,
                query=case.question,
                ticker=case.ticker,
                cutoff=case.cutoff,
                mode="reranked",
                query_embedding=embedder.embed_query(case.question),
                requested_manifest_id=corpus_manifest_id,
                index_manifest_id=index_manifest_id,
                lancedb_table=table,
                reranker=reranker,
                reranker_timeout_seconds=reranker_timeout,
                return_v1=False,
            )
        except CandidatePoolError:
            raise
        except Exception as exc:
            raise CandidatePoolError(
                f"candidate retrieval failed for {case.case_id}"
            ) from exc

    def _close():
        if closed["value"]:
            return
        closed["value"] = True
        try:
            conn.close()
        except Exception:
            pass
        try:
            table.close()
        except Exception:
            pass

    return _retrieve_case, _close


def run_candidate_pools(
    *,
    identity: CandidatePoolIdentity,
    packet_path: str | Path,
    output_dir: str | Path,
    retrieve_case: Callable[[CaseQuery], Any],
    active_lancedb_dir: Path | None = None,
    active_generation_pointer: Path | None = None,
    embedding_mode: str = "mock_unit_test",
    retriever_close: Callable[[], None] | None = None,
    reranker_timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    """Generate 12 identity-bound candidate pools + bounded annotation packet.

    Production callers pass a ``retrieve_case`` built on the existing
    retrieve_hybrid implementation; fixture tests inject deterministic hybrid
    results. Never writes official GoldenCase files and never mutates active
    pointers.
    """
    _require_execution_guards(
        active_lancedb_dir=active_lancedb_dir,
        active_generation_pointer=active_generation_pointer,
    )
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
    active_digest_before = _bounded_directory_digest(active_lancedb_dir)
    out.mkdir(parents=True, exist_ok=False)
    pools_dir = out / "pools"
    packets_dir = out / "packets"
    arms_dir = out / "arms"
    pools_dir.mkdir()
    packets_dir.mkdir()
    arms_dir.mkdir()

    pool_hashes: dict[str, str] = {}
    packet_hashes: dict[str, str] = {}
    arm_hashes: dict[str, str] = {}
    row_count = 0
    try:
        for case in cases:
            hybrid = retrieve_case(case)
            arms = validate_four_arm_served(hybrid, case_id=case.case_id)
            per_arm_chunk_ids = {
                name: tuple(dict.fromkeys(item.chunk_id for item in items))
                for name, items in arms.items()
            }
            # Canonical arm artifact: one identity-bound artifact per case whose
            # artifact_id becomes the union-pool source_artifact_id.
            started_at = _utc_now()
            written = _write_case_arm_artifact(
                root=arms_dir,
                identity=identity,
                case=case,
                hybrid=hybrid,
                arms=arms,
                latency_ms=float(getattr(hybrid, "latency_ms", 0) or 0.0),
                reranker_timeout_seconds=reranker_timeout_seconds,
                started_at=started_at,
            )
            # Flatten the run-scoped artifact layout to arms/<case>.json.
            arm_path = arms_dir / f"{case.case_id}.json"
            written.replace(arm_path)
            arm = load_arm_artifact(arm_path)
            if arm.artifact_id != compute_arm_artifact_id(arm.payload):
                raise CandidatePoolError(
                    f"case {case.case_id}: persisted arm artifact identity mismatch"
                )
            pool = generate_union_pool(arm_path)
            if pool.per_arm_chunk_ids != per_arm_chunk_ids:
                raise CandidatePoolError(
                    f"case {case.case_id}: persisted arm/pool chunk identity drift"
                )
            pool_path = pools_dir / f"{case.case_id}.json"
            write_union_pool(pool, pool_path)
            pool_hashes[case.case_id] = sha256_bytes(pool_path.read_bytes())
            arm_hashes[case.case_id] = sha256_bytes(arm_path.read_bytes())

            # Evidence metadata enrichment is bounded: only the chunk rows that
            # appear in this case's arms are read from the candidate DB.
            lookup_chunks = list(dict.fromkeys(
                chunk_id
                for name in ARM_ORDER
                for chunk_id in per_arm_chunk_ids[name]
            ))
            meta = _candidate_metadata_lookup(
                Path(identity.derivative),
                identity.corpus_manifest_id,
                lookup_chunks,
            )
            rows = [
                _packet_row_for_result(
                    item,
                    case=case,
                    arm=name,
                    meta=meta.get(item.chunk_id),
                    identity=identity,
                )
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
                "model_name": identity.model_name,
                "model_revision": identity.model_revision,
                "embedding_dimension": identity.dimension,
                "reranker_model": identity.reranker_model,
                "reranker_revision": identity.reranker_revision,
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
        if _pointer_bytes(active_generation_pointer) != pointer_before:
            raise CandidatePoolError(
                "active-generation pointer changed during candidate pool run"
            )
        if _bounded_directory_digest(active_lancedb_dir) != active_digest_before:
            raise CandidatePoolError(
                "active LanceDB directory changed during candidate pool run"
            )
    except BaseException:
        import shutil

        shutil.rmtree(out, ignore_errors=True)
        raise
    finally:
        if retriever_close is not None:
            retriever_close()

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
        "arm_hashes": arm_hashes,
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
    "build_production_retriever",
    "load_candidate_identity",
    "load_q011_cases",
    "preflight_candidate_pool",
    "run_candidate_pools",
    "validate_four_arm_served",
]
