"""Eval-only candidate-FTS retrieval seam for the M8-C attribution probe.

This adapter is the M8-C counterpart of the production
``catalyst_agents.runtime.retrieval_adapter.AgentRetrieverAdapter``: it returns
the same evidence/observation contract, but it reads one exact INACTIVE
candidate generation through ``retrieve_lexical(..., inactive_build_id=...)``
instead of the served hybrid pipeline.

Guarantees
----------
* The database is opened ``mode=ro&immutable=1`` with ``PRAGMA query_only=ON``;
  the adapter never changes ``corpus_manifest.is_current``,
  ``corpus_served_chunks``, ``lexical_index_state``, or any dense pointer.
* Only ``FULL_TEXT`` candidate chunks are citable; ``METADATA_ONLY`` /
  ``TITLE_ONLY`` bodies are carried but never reported as citable evidence.
* The observation contract (served mode, ordered ranking, violations, latency)
  matches the production adapter, so the agents' ContextPack/Analyst path is
  unchanged.
* Construction fails closed unless the inactive build passes the frozen
  identity check (build/manifest/digest parity).
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_agents.attribution.provider import RetrievedEvidence
from catalyst_agents.runtime.retrieval_adapter import RetrievalCallObservation
from catalyst_data.retrieval.v1_result import DataRuntimeIdentity, TemporalIdentity

SCHEMA_VERSION = "v1_1_candidate_lexical_adapter_v1"
REQUESTED_MODE = "lexical"
SERVED_MODE = "fts5"
FULL_TEXT = "FULL_TEXT"

_METADATA_COLUMNS = (
    "chunk_id",
    "document_id",
    "corpus_document_id",
    "canonical_asset_id",
    "content_version_id",
    "content_state",
    "source_class",
    "content_hash",
    "dedup_cluster_id",
    "cluster_first_available_at",
    "representative_document_id",
    "independence_group_id",
    "parse_quality",
    "section_key",
    "available_at",
)


class CandidateLexicalAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateLexicalIdentity:
    corpus_manifest_id: str
    build_id: str
    fts_digest: str | None = None


def _read_only_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro&immutable=1"


def _verify_inactive_build(
    conn: sqlite3.Connection, *, corpus_manifest_id: str, build_id: str
) -> str:
    """Fail-closed identity check; returns the verified lexical digest."""
    from catalyst_data.retrieval.fts5 import (
        RetrievalContractError,
        verify_inactive_lexical_build,
    )

    try:
        verify_inactive_lexical_build(
            conn,
            requested_manifest_id=corpus_manifest_id,
            build_id=build_id,
        )
    except RetrievalContractError as exc:
        raise CandidateLexicalAdapterError(
            f"inactive candidate build verification failed: {exc}"
        ) from exc
    row = conn.execute(
        "SELECT lexical_digest FROM corpus_publication_builds WHERE build_id=?",
        (build_id,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] else ""


def _metadata_for_chunks(
    conn: sqlite3.Connection, *, build_id: str, chunk_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    unique = tuple(dict.fromkeys(chunk_ids))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    rows = conn.execute(
        "SELECT " + ", ".join(_METADATA_COLUMNS) + " FROM corpus_build_chunks "
        f"WHERE build_id=? AND chunk_id IN ({placeholders})",
        (build_id, *unique),
    ).fetchall()
    metadata = {str(row[0]): dict(zip(_METADATA_COLUMNS, row)) for row in rows}
    missing = [chunk_id for chunk_id in unique if chunk_id not in metadata]
    if missing:
        raise CandidateLexicalAdapterError(
            "candidate chunk metadata missing for: " + ",".join(missing[:5])
        )
    return metadata


class CandidateLexicalAdapter:
    """Pointer-free candidate lexical retriever for the eval Analyst path."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        corpus_manifest_id: str,
        build_id: str,
        expected_fts_digest: str | None = None,
        top_k: int = 8,
        candidate_depth: int = 20,
        temporal_identity: TemporalIdentity | None = None,
        data_runtime_identity: DataRuntimeIdentity | None = None,
    ) -> None:
        path = Path(db_path)
        if not path.is_file():
            raise CandidateLexicalAdapterError(f"candidate derivative not found: {path}")
        self._db_path = path
        self.identity = CandidateLexicalIdentity(
            corpus_manifest_id=corpus_manifest_id,
            build_id=build_id,
            fts_digest=expected_fts_digest,
        )
        self._top_k = top_k
        self._candidate_depth = candidate_depth
        self._temporal_identity = temporal_identity
        self._data_runtime_identity = data_runtime_identity
        self._conn = sqlite3.connect(_read_only_uri(path), uri=True)
        self._conn.execute("PRAGMA query_only=ON")
        digest = _verify_inactive_build(
            self._conn, corpus_manifest_id=corpus_manifest_id, build_id=build_id
        )
        if expected_fts_digest is not None and digest != expected_fts_digest:
            raise CandidateLexicalAdapterError(
                "candidate FTS digest does not match the sealed recovery report"
            )
        self.verified_fts_digest = digest

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> "CandidateLexicalAdapter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- retrieval protocol ------------------------------------------------
    def retrieve_with_observations(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str | None = None,
        temporal_identity: Any = None,
        top_k: int | None = None,
        candidate_depth: int | None = None,
    ) -> tuple[tuple[RetrievedEvidence, ...], RetrievalCallObservation]:
        from catalyst_data.retrieval.fts5 import retrieve_lexical

        effective_top_k = top_k or self._top_k
        started = time.monotonic()
        session_date = None
        resolved_temporal = temporal_identity or self._temporal_identity
        if resolved_temporal is not None:
            session_date = getattr(resolved_temporal, "session_date", None)
        result = retrieve_lexical(
            self._conn,
            query,
            ticker=ticker,
            cutoff=cutoff,
            requested_manifest_id=(
                requested_manifest_id or self.identity.corpus_manifest_id
            ),
            top_k=effective_top_k,
            candidate_depth=candidate_depth or self._candidate_depth,
            session_date=session_date,
            inactive_build_id=self.identity.build_id,
        )
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        hits = tuple(result.results[:effective_top_k])
        chunk_ids = tuple(hit.chunk_id for hit in hits)
        metadata = _metadata_for_chunks(
            self._conn, build_id=self.identity.build_id, chunk_ids=chunk_ids
        )
        evidence = tuple(
            self._to_evidence(hit, metadata[hit.chunk_id]) for hit in hits
        )
        ticker_violations = tuple(
            hit.chunk_id for hit in hits if ticker not in tuple(hit.ticker_associations)
        )
        cutoff_violations = tuple(
            hit.chunk_id for hit in hits if hit.available_at > cutoff
        )
        observation = RetrievalCallObservation(
            requested_mode=REQUESTED_MODE,
            served_mode=result.mode_served,
            ordered_candidate_evidence_ids=tuple(
                item.chunk_id for item in result.candidates
            ),
            ordered_final_ranked_evidence_ids=chunk_ids,
            rank_changes={},
            duplicate_drops=None,
            measured_latency_ms=latency_ms,
            ticker_violations=ticker_violations,
            cutoff_violations=cutoff_violations,
            degradation_reasons=tuple(
                reason for reason in (result.fallback_reason,) if reason
            ),
            arm_top_k=effective_top_k,
            arm_names=(REQUESTED_MODE,),
        )
        return evidence, observation

    def retrieve(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str | None = None,
        temporal_identity: Any = None,
        top_k: int | None = None,
        candidate_depth: int | None = None,
    ) -> tuple[RetrievedEvidence, ...]:
        evidence, _ = self.retrieve_with_observations(
            query,
            ticker=ticker,
            cutoff=cutoff,
            requested_manifest_id=requested_manifest_id,
            temporal_identity=temporal_identity,
            top_k=top_k,
            candidate_depth=candidate_depth,
        )
        return evidence

    # -- mapping -----------------------------------------------------------
    def _to_evidence(
        self, hit: Any, metadata: Mapping[str, Any]
    ) -> RetrievedEvidence:
        content_state = str(metadata.get("content_state") or "")
        corpus_manifest_id = (
            self.identity.corpus_manifest_id
            if self._data_runtime_identity is None
            else self._data_runtime_identity.corpus_manifest_id
        )
        return RetrievedEvidence(
            chunk_id=hit.chunk_id,
            document_id=str(metadata.get("corpus_document_id") or hit.document_id),
            content_text=hit.content_text or "",
            available_at=str(metadata.get("available_at") or hit.available_at),
            source_class=str(metadata.get("source_class") or hit.source_class),
            ticker_associations=tuple(hit.ticker_associations),
            dedup_cluster_id=metadata.get("dedup_cluster_id"),
            cluster_first_available_at=str(
                metadata.get("cluster_first_available_at")
                or metadata.get("available_at")
                or hit.available_at
            ),
            representative_document_id=str(
                metadata.get("representative_document_id")
                or metadata.get("corpus_document_id")
                or hit.document_id
            ),
            is_novel=False,
            lexical_raw_score=hit.lexical_raw_score,
            lexical_rank=hit.lexical_rank,
            corpus_manifest_id=corpus_manifest_id,
            index_manifest_id=None,
            mode_requested=REQUESTED_MODE,
            mode_served=hit.mode_served,
            is_degraded=bool(hit.is_degraded),
            fallback_reason=hit.fallback_reason,
            temporal_center_date=hit.temporal_center_date,
            query_date=hit.query_date,
            query_date_conflict=bool(hit.query_date_conflict),
            query_date_decision=hit.query_date_decision,
            canonical_asset_id=metadata.get("canonical_asset_id"),
            canonical_content_version_id=metadata.get("content_version_id"),
            corpus_document_id=metadata.get("corpus_document_id"),
            section_key=metadata.get("section_key"),
            content_hash=metadata.get("content_hash"),
            material_capability=content_state,
            independence_group_id=metadata.get("independence_group_id"),
            parse_quality=metadata.get("parse_quality"),
            content_state=content_state,
            temporal_identity=self._temporal_identity,
            data_runtime_identity=self._data_runtime_identity,
        )

    def citable_evidence_ids(
        self, evidence: Sequence[RetrievedEvidence]
    ) -> tuple[str, ...]:
        """Evidence ids whose candidate body is citable FULL_TEXT."""
        return tuple(
            item.chunk_id
            for item in evidence
            if item.chunk_id and item.content_state == FULL_TEXT
        )


__all__ = [
    "SCHEMA_VERSION",
    "CandidateLexicalAdapter",
    "CandidateLexicalAdapterError",
    "CandidateLexicalIdentity",
]
