"""Production hybrid retrieval facade with typed arm failure semantics.

Input/filter/manifest/query-vector validation completes before any arm runs.
Only a confirmed backend availability failure (``RetrievalArmUnavailableError``)
may degrade to the surviving arm; contract errors, identity drift, persisted
row corruption, and programmer errors always propagate.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Literal, overload

import numpy as np

from .dense import retrieve_dense
from .fts5 import _validate_inputs, retrieve_lexical
from .fusion import fuse
from .query_policy import TemporalCenterResolution, resolve_temporal_center
from .reranker import RerankerGate, rerank
from datetime import datetime, timezone

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import SourceClass, source_role_for
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval import v1_result
from .result import (
    RetrievalArmUnavailableError,
    RetrievalContractError,
    RetrievalResult,
    RetrievalResultSet,
)

V1_QUERY_POLICY_VERSION = "qp:v1"
_V1_POLICY_VERSION = V1_QUERY_POLICY_VERSION


def _utc(iso: str) -> datetime:
    text = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _temporal_identity_for_cutoff(cutoff: str) -> TemporalIdentity:
    """Non-production fallback used only when no validated TemporalIdentity is
    injected (fixture callers). The production path injects the exact validated
    TemporalIdentity and never reconstructs it from the cutoff (M4-0 §1.2)."""
    cutoff_at = _utc(cutoff)
    session_date = cutoff_at.date().isoformat()
    return TemporalIdentity(
        session_date=session_date,
        market_timezone="America/New_York",
        session_open_at=cutoff_at,
        session_close_at=cutoff_at,
        information_window_start_at=cutoff_at,
        cutoff_at=cutoff_at,
    )


_CANONICAL_LOOKUP_COLUMNS = (
    "chunk_id", "document_id", "chunk_profile_version", "section_key", "ordinal",
    "content_hash", "source_class", "dedup_cluster_id", "available_at",
    "section_parse_degraded", "canonical_asset_id", "content_version_id",
    "corpus_document_id", "content_state", "independence_group_id",
    "parse_quality", "provider", "ticker_associations", "status",
)

_CANONICAL_LOOKUP_SQL = f"""
    SELECT {", ".join("c." + name for name in _CANONICAL_LOOKUP_COLUMNS)}
    FROM corpus_build_chunks c
    JOIN corpus_publication_builds b ON b.build_id = c.build_id
    WHERE b.manifest_id = ? AND c.chunk_id = ?
"""


def _lookup_canonical_chunk(
    db: Any, chunk_id: str, *, requested_manifest_id: str
) -> dict[str, Any]:
    """Resolve canonical metadata for one served chunk bound to the requested
    corpus manifest/build (M4-0 §1.2).

    The lookup is always generation-qualified: ``(requested manifest/build,
    chunk_id)``. Zero rows, duplicate rows, or a canonical chain that does not
    resolve to exactly one consistent record (asset/version join disagreement
    or document identity mismatch) fail closed with ``RetrievalContractError``
    and never fall back to a legacy shape.
    """
    from catalyst_data.retrieval.result import RetrievalContractError

    try:
        rows = db.execute(_CANONICAL_LOOKUP_SQL, (requested_manifest_id, chunk_id)).fetchall()
    except Exception as exc:
        raise RetrievalContractError(
            "canonical_chunk_lookup_failed", str(exc)
        ) from exc
    if len(rows) == 0:
        raise RetrievalContractError(
            "canonical_chunk_not_found",
            f"chunk {chunk_id!r} is not served under manifest {requested_manifest_id!r}",
        )
    if len(rows) > 1:
        raise RetrievalContractError(
            "canonical_chunk_ambiguous",
            f"chunk {chunk_id!r} resolves to multiple builds under manifest "
            f"{requested_manifest_id!r}",
        )
    row = rows[0]
    meta = dict(zip(_CANONICAL_LOOKUP_COLUMNS, row))
    asset_id = meta["canonical_asset_id"]
    version_id = meta["content_version_id"]
    if not asset_id or not version_id:
        raise RetrievalContractError(
            "canonical_chunk_incomplete",
            f"chunk {chunk_id!r} lacks canonical asset/version identity",
        )
    if meta["document_id"] != meta["corpus_document_id"]:
        raise RetrievalContractError(
            "canonical_chunk_identity_disagreement",
            f"chunk {chunk_id!r} document identity disagrees with its canonical "
            "corpus document",
        )
    asset_rows = db.execute(
        """SELECT asset_type, provider, publisher, canonical_url, eligible_at,
                  temporal_precision, serving_status, independence_group_id,
                  content_state
           FROM canonical_assets WHERE asset_id = ?""",
        (asset_id,),
    ).fetchall()
    if len(asset_rows) != 1:
        raise RetrievalContractError(
            "canonical_asset_join_ambiguous",
            f"chunk {chunk_id!r} canonical asset {asset_id!r} does not resolve "
            f"to exactly one row",
        )
    asset = asset_rows[0]
    version_rows = db.execute(
        """SELECT content_hash
           FROM canonical_content_versions
           WHERE canonical_content_version_id = ? AND asset_id = ?""",
        (version_id, asset_id),
    ).fetchall()
    if len(version_rows) != 1:
        raise RetrievalContractError(
            "canonical_version_join_ambiguous",
            f"chunk {chunk_id!r} content version {version_id!r} does not resolve "
            f"to exactly one consistent chain for asset {asset_id!r}",
        )
    served_eligible_at = str(meta["available_at"])
    asset_eligible_at = asset[4]
    if asset_eligible_at is not None and str(asset_eligible_at) != served_eligible_at:
        raise RetrievalContractError(
            "canonical_eligible_at_disagreement",
            f"chunk {chunk_id!r} served eligible timestamp disagrees with its "
            "canonical asset",
        )
    provider = meta["provider"] or asset[1]
    if not provider:
        raise RetrievalContractError(
            "canonical_provider_missing",
            f"chunk {chunk_id!r} has no provider on build or canonical asset",
        )
    tickers: tuple[str, ...] = ()
    try:
        ticker_payload = meta["ticker_associations"]
        if isinstance(ticker_payload, str) and ticker_payload:
            ticker_values = json.loads(ticker_payload)
            tickers = tuple(sorted({str(value) for value in ticker_values}))
    except Exception as exc:
        raise RetrievalContractError(
            "canonical_ticker_associations_invalid",
            f"chunk {chunk_id!r} ticker associations are not valid JSON: {exc}",
        ) from exc
    return {
        "canonical_asset_id": asset_id,
        "content_version_id": version_id,
        "corpus_document_id": meta["corpus_document_id"],
        "document_id": meta["document_id"],
        "section_key": meta["section_key"],
        "chunk_ordinal": int(meta["ordinal"]),
        "content_hash": str(version_rows[0][0]),
        "source_class": meta["source_class"],
        "content_state": meta["content_state"],
        "eligible_at": served_eligible_at,
        "dedup_cluster_id": meta["dedup_cluster_id"],
        "independence_group_id": meta["independence_group_id"],
        "parse_quality": meta["parse_quality"] or "not_applicable",
        "provider": provider,
        "publisher": asset[2],
        "canonical_url": asset[3],
        "asset_type": asset[0],
        "temporal_precision": asset[5],
        "serving_status": asset[6],
        "ticker_scope": tickers,
    }


_MATERIAL_CAPABILITY_BY_STATE = {
    "FULL_TEXT": "MATERIAL_CAPABLE",
    "TITLE_ONLY": "LEAD_ONLY",
    "METADATA_ONLY": "NOT_CAPABLE",
}


def _to_v1_result_set(
    db: Any,
    *,
    hybrid: "HybridRetrievalResult",
    requested_manifest_id: str,
    index_manifest_id: str,
    cutoff: str,
    temporal_identity: TemporalIdentity | None = None,
    data_runtime_identity: DataRuntimeIdentity | None = None,
) -> v1_result.RetrievalResultSet:
    """Convert a successful reranked hybrid result into the V1.1 result set.

    Production binding (M4-0 §1.2): the exact validated ``TemporalIdentity``
    and the runtime-authority ``DataRuntimeIdentity`` are injected and pass
    through unchanged as the set-level identities. The DataRuntimeIdentity
    corpus manifest must equal the requested manifest. Canonical metadata
    lookup is manifest/build-qualified and fails closed; a missing chain never
    produces a silent legacy fallback.
    """
    from catalyst_data.retrieval.result import RetrievalContractError

    if hybrid.mode_served != "reranked":
        raise RetrievalContractError(
            "retrieval_v1_contract_not_served",
            f"mode served {hybrid.mode_served!r} cannot produce a V1.1 result set",
        )
    if temporal_identity is not None:
        temporal = temporal_identity
        if temporal.cutoff_at != _utc(cutoff):
            raise RetrievalContractError(
                "temporal_identity_cutoff_mismatch",
                "injected TemporalIdentity cutoff does not match the requested cutoff",
            )
    else:
        temporal = _temporal_identity_for_cutoff(cutoff)
    if data_runtime_identity is not None:
        if data_runtime_identity.corpus_manifest_id != requested_manifest_id:
            raise RetrievalContractError(
                "runtime_identity_manifest_mismatch",
                "injected DataRuntimeIdentity corpus manifest does not match "
                "the requested manifest",
            )
        runtime = data_runtime_identity
    else:
        runtime = DataRuntimeIdentity(
            data_snapshot_id=requested_manifest_id,
            corpus_manifest_id=requested_manifest_id,
            fts_index_version=hybrid.mode_served,
            dense_index_version=index_manifest_id,
            embedding_model_revision=None,
            reranker_revision=None,
            query_policy_version=_V1_POLICY_VERSION,
        )

    selected: list[tuple[Any, dict[str, Any]]] = []
    for item in hybrid.final_results:
        meta = _lookup_canonical_chunk(
            db, item.chunk_id, requested_manifest_id=requested_manifest_id
        )
        content_state = meta["content_state"] or "FULL_TEXT"
        if content_state in {"EMPTY", "FAILED"}:
            continue
        eligible_at = _utc(str(meta["eligible_at"]))
        if eligible_at > temporal.cutoff_at:
            continue
        selected.append((item, meta))
    hits: list[v1_result.RetrievalHit] = []
    for rank, (item, meta) in enumerate(selected, start=1):
        content_state = meta["content_state"] or "FULL_TEXT"
        eligible_at = _utc(str(meta["eligible_at"]))
        excerpt = item.content_text or ""
        source_class = SourceClass(meta["source_class"])
        hit = v1_result.RetrievalHit(
            evidence_id=item.chunk_id,
            canonical_asset_id=str(meta["canonical_asset_id"]),
            content_version_id=str(meta["content_version_id"]),
            corpus_document_id=str(meta["corpus_document_id"]),
            chunk_id=item.chunk_id,
            excerpt=excerpt,
            scores={
                "lexical": None,
                "dense": None,
                "fusion": None,
                "reranked": item.reranker_score,
            },
            ranks={
                "lexical": None,
                "dense": None,
                "fusion": None,
                "reranked": rank,
            },
            source_class=source_class,
            content_state=content_state,
            eligible_at=eligible_at,
            ticker_scope=meta["ticker_scope"],
            provider=str(meta["provider"]),
            publisher=meta["publisher"],
            dedup_cluster_id=meta["dedup_cluster_id"],
            parse_quality=str(meta["parse_quality"]),
            retrieval_policy_version=_V1_POLICY_VERSION,
            temporal_identity=temporal,
            data_runtime_identity=runtime,
            section_key=meta["section_key"],
            chunk_ordinal=meta["chunk_ordinal"],
            asset_type=meta["asset_type"],
            content_hash=meta["content_hash"],
            material_capability=_MATERIAL_CAPABILITY_BY_STATE[content_state],
            serving_status=str(meta["serving_status"]),
            temporal_precision=str(meta["temporal_precision"]),
            independence_group_id=meta["independence_group_id"],
            canonical_url=meta["canonical_url"],
            evidence_role=source_role_for(source_class).value,
        )
        hits.append(hit)
    return v1_result.RetrievalResultSet(
        hits=tuple(hits),
        temporal_identity=temporal,
        data_runtime_identity=runtime,
    )


def _validate_hybrid_inputs(
    db: Any,
    *,
    query: str,
    ticker: str,
    cutoff: str,
    mode: str,
    query_embedding: Any,
    requested_manifest_id: str,
    index_manifest_id: str,
    lancedb_table: Any,
    source_classes: tuple[str, ...] | None,
    evidence_types: tuple[str, ...] | None,
) -> None:
    """Validate everything that can fail before entering arm fallback.

    Raises RetrievalContractError for filter/manifest violations and ValueError
    for programmer errors (bad embedding shape, missing dense backend, non-hex
    index identity). SQLite unavailability is deliberately not decided here:
    the lexical arm boundary converts confirmed OperationalError to
    RetrievalArmUnavailableError so a dense-only fallback remains possible.
    """
    if mode not in {"hybrid", "reranked"}:
        raise ValueError("hybrid facade supports hybrid and reranked modes")
    if re.fullmatch(r"[0-9a-f]{64}", index_manifest_id) is None:
        raise RetrievalContractError("invalid_index_manifest_id")
    if lancedb_table is None:
        raise ValueError("identity-bound LanceDB table is required for dense retrieval")
    if not isinstance(query, str) or not query:
        raise RetrievalContractError("invalid_query")
    _validate_inputs(
        requested_manifest_id, ticker, cutoff, 20, 20,
        source_classes, evidence_types,
    )
    try:
        row = db.execute(
            "SELECT 1 FROM corpus_manifest WHERE manifest_id = ?",
            (requested_manifest_id,),
        ).fetchone()
        db_reachable = True
    except sqlite3.OperationalError:
        # An unreachable SQLite backend is not a contract failure; the lexical
        # arm boundary converts confirmed OperationalError to
        # RetrievalArmUnavailableError so a dense-only fallback can serve.
        row = None
        db_reachable = False
    if db_reachable and row is None:
        raise RetrievalContractError("manifest_not_found")
    query_vector = np.asarray(query_embedding, dtype=np.float32)
    if query_vector.ndim != 1 or query_vector.size != 1024:
        raise ValueError("query_embedding must be a one-dimensional 1024-d vector")
    norm = float(np.linalg.norm(query_vector))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("query_embedding must be non-zero")


def _bind_index_manifest(result_set: RetrievalResultSet, index_manifest_id: str) -> RetrievalResultSet:
    values = tuple(item.model_copy(update={"index_manifest_id": index_manifest_id}) for item in result_set.candidates)
    return result_set.model_copy(update={"candidates": values, "results": values[:len(result_set.results)]})


def _stamp_temporal(
    results: tuple[RetrievalResult, ...] | list[RetrievalResult],
    center: TemporalCenterResolution,
) -> tuple[RetrievalResult, ...]:
    """Copy structured temporal identity onto every production evidence result."""
    update = {
        "temporal_center_date": center.center_date,
        "query_date": center.query_date,
        "query_date_conflict": center.conflict,
        "query_date_decision": center.decision,
    }
    return tuple(item.model_copy(update=update) for item in results)


@dataclass(frozen=True)
class HybridRetrievalResult:
    mode_requested: str
    mode_served: str
    lexical_results: RetrievalResultSet | None = None
    dense_results: RetrievalResultSet | None = None
    fusion_results: tuple[RetrievalResult, ...] = ()
    reranker_results: RetrievalResultSet | None = None
    final_results: tuple[RetrievalResult, ...] = ()
    degradation_reasons: tuple[str, ...] = ()
    # AMEND-5.2A: production evidence temporal identity (not include_trace-only).
    temporal_center_date: str | None = None
    query_date: str | None = None
    query_date_conflict: bool = False
    query_date_decision: str | None = None


class ProductionHybridRetriever:
    """Production retriever binding (M4-0 §1.2).

    It binds the approved corpus/index identities once and sends every query
    through the same lexical+dense+RRF+reranker facade. It never recreates
    ``DataRuntimeIdentity``: the actual identity is injected by the runtime
    dependency authority and must match the requested manifest. The exact
    request-scoped ``TemporalIdentity`` is passed per call and is never
    reconstructed from the cutoff. The adapter does not create an embedding
    lambda or expose the legacy LanceDB search path.
    """

    def __init__(
        self,
        *,
        db: Any = None,
        db_conn_factory: Callable[[], Any] | None = None,
        lancedb_table: Any,
        embedding_fn: Callable[[str], Any],
        reranker: object | None,
        index_manifest_id: str,
        data_runtime_identity: DataRuntimeIdentity | None = None,
        reranker_timeout_seconds: float = 2.0,
    ) -> None:
        # ``db`` is the narrow injected fixture seam for tests that already
        # hold a connection; production uses ``db_conn_factory`` so every
        # retrieval operation obtains and closes its own read-only connection
        # (Final TSD §16 connection-per-operation; FIX 3A).
        self._db = db
        self._db_conn_factory = db_conn_factory
        self.lancedb_table = lancedb_table
        self.embedding_fn = embedding_fn
        self.reranker = reranker
        self.index_manifest_id = index_manifest_id
        self.data_runtime_identity = data_runtime_identity
        self.reranker_timeout_seconds = reranker_timeout_seconds
        self._reranker_gate = RerankerGate()

    def _operation_connection(self) -> tuple[Any, bool]:
        """Return (connection, owned). Owned connections must be closed."""
        if self._db is not None:
            return self._db, False
        if self._db_conn_factory is None:
            raise RetrievalContractError(
                "db_conn_factory_unavailable",
                "production retrieval requires a read-only connection factory",
            )
        return self._db_conn_factory(), True

    def retrieve(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
        temporal_identity: TemporalIdentity | None = None,
        top_k: int = 8,
        candidate_depth: int = 20,
    ) -> v1_result.RetrievalResultSet:
        if candidate_depth != 20:
            raise ValueError("production hybrid candidate_depth must be 20")
        if self.data_runtime_identity is None:
            raise RetrievalContractError(
                "data_runtime_identity_unavailable",
                "production retrieval requires the runtime-authority "
                "DataRuntimeIdentity",
            )
        if temporal_identity is None:
            raise RetrievalContractError(
                "temporal_identity_unavailable",
                "production retrieval requires the validated request "
                "TemporalIdentity",
            )
        conn, owned = self._operation_connection()
        try:
            result = retrieve_hybrid(
                conn,
                query=query,
                ticker=ticker,
                cutoff=cutoff,
                mode="reranked",
                reranker=self.reranker,
                query_embedding=self.embedding_fn(query),
                requested_manifest_id=requested_manifest_id,
                index_manifest_id=self.index_manifest_id,
                lancedb_table=self.lancedb_table,
                reranker_timeout_seconds=self.reranker_timeout_seconds,
                reranker_gate=self._reranker_gate,
                temporal_identity=temporal_identity,
                data_runtime_identity=self.data_runtime_identity,
            )
            if not isinstance(result, v1_result.RetrievalResultSet):
                raise RetrievalContractError(
                    "retrieval_v1_contract_not_served",
                    "production hybrid retrieval did not serve the V1.1 result set",
                )
            return result
        finally:
            if owned:
                conn.close()


@overload
def retrieve_hybrid(
    db: Any,
    *,
    query: str,
    ticker: str,
    cutoff: str,
    mode: str = "hybrid",
    reranker: Any = None,
    query_embedding: Any = None,
    requested_manifest_id: str,
    index_manifest_id: str,
    lancedb_table: Any,
    source_classes: tuple[str, ...] | None = None,
    evidence_types: tuple[str, ...] | None = None,
    reranker_timeout_seconds: float = 2.0,
    reranker_gate: RerankerGate | None = None,
    temporal_identity: TemporalIdentity | None = None,
    data_runtime_identity: DataRuntimeIdentity | None = None,
    inactive_build_id: str | None = None,
    return_v1: Literal[False],
) -> HybridRetrievalResult:
    """Overload: explicit ``return_v1=False`` always yields the legacy shape."""


@overload
def retrieve_hybrid(
    db: Any,
    *,
    query: str,
    ticker: str,
    cutoff: str,
    mode: str = "hybrid",
    reranker: Any = None,
    query_embedding: Any = None,
    requested_manifest_id: str,
    index_manifest_id: str,
    lancedb_table: Any,
    source_classes: tuple[str, ...] | None = None,
    evidence_types: tuple[str, ...] | None = None,
    reranker_timeout_seconds: float = 2.0,
    reranker_gate: RerankerGate | None = None,
    temporal_identity: TemporalIdentity | None = None,
    data_runtime_identity: DataRuntimeIdentity | None = None,
    inactive_build_id: str | None = None,
    return_v1: Literal[True] = True,
) -> v1_result.RetrievalResultSet | HybridRetrievalResult:
    """Overload: the default path may emit the V1.1 set or a legacy result."""


def retrieve_hybrid(
    db: Any,
    *,
    query: str,
    ticker: str,
    cutoff: str,
    mode: str = "hybrid",
    reranker: Any = None,
    query_embedding: Any = None,
    requested_manifest_id: str,
    index_manifest_id: str,
    lancedb_table: Any,
    source_classes: tuple[str, ...] | None = None,
    evidence_types: tuple[str, ...] | None = None,
    reranker_timeout_seconds: float = 2.0,
    reranker_gate: RerankerGate | None = None,
    temporal_identity: TemporalIdentity | None = None,
    data_runtime_identity: DataRuntimeIdentity | None = None,
    inactive_build_id: str | None = None,
    return_v1: bool = True,
) -> v1_result.RetrievalResultSet | HybridRetrievalResult:
    """Run lexical and dense arms directly with identical scope arguments.

    The declared return type reflects both runtime shapes: a successful
    default ``return_v1=True`` ``mode="reranked"`` run emits the M3-11 V1.1
    ``v1_result.RetrievalResultSet``; ``return_v1=False``, ``mode="hybrid"``,
    and fail/degraded paths emit the legacy ``HybridRetrievalResult`` (outer
    arm result sets + flat temporal identity). The M1/M3 four-arm exit library
    passes ``return_v1=False`` to opt out of the V1 conversion; all other
    callers are unchanged. ``inactive_build_id`` binds the lexical arm to one
    exact inactive candidate build's per-build FTS (pointer-free); when
    omitted the unchanged active served-manifest lexical path is used.
    """
    _validate_hybrid_inputs(
        db,
        query=query,
        ticker=ticker,
        cutoff=cutoff,
        mode=mode,
        query_embedding=query_embedding,
        requested_manifest_id=requested_manifest_id,
        index_manifest_id=index_manifest_id,
        lancedb_table=lancedb_table,
        source_classes=source_classes,
        evidence_types=evidence_types,
    )
    # Always resolve structured temporal center on the production hybrid path
    # (independent of lexical include_trace).
    temporal = resolve_temporal_center(query=query, cutoff=cutoff)
    temporal_kwargs = {
        "temporal_center_date": temporal.center_date,
        "query_date": temporal.query_date,
        "query_date_conflict": temporal.conflict,
        "query_date_decision": temporal.decision,
    }
    shared_scope = {
        "ticker": ticker,
        "cutoff": cutoff,
        "requested_manifest_id": requested_manifest_id,
        "source_classes": source_classes,
        "evidence_types": evidence_types,
    }
    lexical = dense = None
    reasons: list[str] = []
    try:
        lexical_kwargs = dict(shared_scope)
        if inactive_build_id is not None:
            lexical_kwargs["inactive_build_id"] = inactive_build_id
        lexical = retrieve_lexical(
            db, query, top_k=20, candidate_depth=20, **lexical_kwargs,
        )
    except RetrievalArmUnavailableError as exc:
        reasons.append(exc.code)
    try:
        dense = retrieve_dense(
            db, query_embedding, top_k=20, index_manifest_id=index_manifest_id,
            lancedb_table=lancedb_table, **shared_scope,
        )
    except RetrievalArmUnavailableError as exc:
        reasons.append(exc.code)
    if lexical is None and dense is None:
        return HybridRetrievalResult(
            mode, "failed", degradation_reasons=tuple(reasons), **temporal_kwargs,
        )
    if lexical is None or dense is None:
        survivor = dense if lexical is None else lexical
        survivor = _bind_index_manifest(survivor, index_manifest_id)
        served = "dense" if lexical is None else survivor.mode_served
        finals = _stamp_temporal(tuple(survivor.results), temporal)
        return HybridRetrievalResult(
            mode, served, lexical_results=lexical, dense_results=dense,
            final_results=finals, degradation_reasons=tuple(reasons),
            **temporal_kwargs,
        )
    lexical = _bind_index_manifest(lexical, index_manifest_id)
    dense = _bind_index_manifest(dense, index_manifest_id)
    fused = _stamp_temporal(
        tuple(fuse(lexical.results, dense.results, k=60, output_k=20)),
        temporal,
    )
    if mode == "hybrid":
        return HybridRetrievalResult(
            mode, "hybrid", lexical_results=lexical, dense_results=dense,
            fusion_results=fused, final_results=fused,
            degradation_reasons=tuple(reasons), **temporal_kwargs,
        )
    reranked = rerank(
        query=query, candidates=fused, reranker=reranker,
        timeout_seconds=reranker_timeout_seconds, gate=reranker_gate,
    )
    if reranked.is_degraded:
        reasons.extend(reranked.degradation_reasons)
        # Serve hybrid fusion order with temporal identity still stamped.
        return HybridRetrievalResult(
            mode, "hybrid", lexical_results=lexical, dense_results=dense,
            fusion_results=fused, reranker_results=reranked,
            final_results=fused, degradation_reasons=tuple(reasons),
            **temporal_kwargs,
        )
    finals = _stamp_temporal(tuple(reranked.results), temporal)
    hybrid_result = HybridRetrievalResult(
        mode, "reranked", lexical_results=lexical, dense_results=dense,
        fusion_results=fused, reranker_results=reranked,
        final_results=finals, degradation_reasons=tuple(reasons),
        **temporal_kwargs,
    )
    if not return_v1:
        return hybrid_result
    return _to_v1_result_set(
        db,
        hybrid=hybrid_result,
        requested_manifest_id=requested_manifest_id,
        index_manifest_id=index_manifest_id,
        cutoff=cutoff,
        temporal_identity=temporal_identity,
        data_runtime_identity=data_runtime_identity,
    )


__all__ = [
    "HybridRetrievalResult", "ProductionHybridRetriever", "V1_QUERY_POLICY_VERSION",
    "retrieve_hybrid",
]
