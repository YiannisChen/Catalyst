"""The V1.1 production lexical retrieval path (WP-B B2).

Frozen-A3 §2.1 gives V1.1 exactly one production path:

    QueryPolicy
    -> eligibility / ticker / cutoff filters
    -> FTS5 candidate window, candidate_depth = 100 (fail closed below 50)
    -> document identification; TITLE_ONLY / METADATA_ONLY hits contribute a
       document_id only, never a slot or a body
    -> expansion to every FULL_TEXT chunk of those documents for the served build
    -> within-document scoring by public earnings/results markers, FTS order as
       tie-break
    -> at most 3 chunks per document; an empty slot beats an unscored sibling
    -> display_top_k = 8
    -> RetrievalResultSet

:class:`LexicalDisplayRetriever` is that path. It owns three things and nothing
else:

1. **Served-build resolution** (A3 §2.2). The served build is the
   ``lexical_index_state.lexical_generation_id`` generation of the current
   ``corpus_manifest`` whose ``corpus_publication_builds`` row is
   ``status='published'`` **and** ``lexical_ready=1``. Zero or several matches
   fail closed. A ``status='lexical_ready'`` (staged, unpublished) build is
   never selectable as a served build.
2. **The candidate window.** ``retrieve_lexical`` is called at the frozen depth
   (``CANDIDATE_DEPTH_CAP``) with ``include_trace=True``; the window is
   ``result.candidates`` in lexical rank order, never ``results[:top_k]``.
3. **The display and the V1.1 hit set.** ``lexical_display``
   (``select_within_document_slots``, the frozen ``lexical_display_v1`` policy)
   decides the slots on the served ``build_id``; every required
   ``RetrievalHit`` field is read from build-scoped stored columns.

Column→field provenance (nothing is synthesized):

* ``corpus_build_chunks`` (build-scoped, ``build_id = <served build>``):
  ``chunk_id``, ``ordinal``, ``content_text``, ``ticker_associations``,
  ``document_id``, ``corpus_document_id``, ``canonical_asset_id``,
  ``content_version_id``, ``content_state``, ``source_class``, ``content_hash``,
  ``dedup_cluster_id``, ``independence_group_id``, ``parse_quality``,
  ``section_key``, ``available_at``, ``provider`` — the eval candidate
  adapter's metadata read (``v1_1/candidate_lexical_adapter.py:54-74`` and
  ``:117-136``) ported onto the served ``build_id``, plus ``provider`` because a
  served V1.1 hit must name its provider. Data-core never imports the eval
  package (the direction is eval -> data-core, never the reverse).
* ``canonical_assets`` (by the chunk's ``canonical_asset_id``, the same
  canonical registry join the production hybrid hit builder uses —
  ``hybrid.py`` ``_lookup_canonical_chunk``): ``asset_type``, ``publisher``,
  ``canonical_url``, ``serving_status``, ``temporal_precision``, and the
  fallback ``provider``. Exactly one row is required; zero or several fail
  closed.
* Derived, never substituted: ``material_capability`` from ``content_state``
  (``v1_result.material_capability_for_content_state``), ``evidence_role`` from
  ``source_class`` (``canonical.model.source_role_for``), ``eligible_at`` from
  the stored ``available_at`` timestamp, and ``ticker_scope`` from the stored
  ``ticker_associations`` JSON.
* Supplied by the caller, not by a row: the ``TemporalIdentity`` and the
  ``DataRuntimeIdentity`` (A3 §2.2), and the frozen policy identity
  ``retrieval_policy_version`` = ``lexical_display_v1`` (the same constant-not-
  column pattern the production hybrid uses for ``_V1_POLICY_VERSION``). The
  lexical stage rank is the hit's position in the served (display) order, as
  the frozen set contract requires contiguous per-stage ranks.

Stage semantics (A3 §2.1): the lexical stage is scored and ranked; the dense,
fusion and rerank stages are absent, because V1.1 has no dense arm.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import SourceClass, source_role_for
from catalyst_data.canonical.temporal import TemporalIdentity, utc_iso_z

from . import v1_result
from .fts5 import retrieve_lexical
from .lexical_display import (
    CANDIDATE_DEPTH_CAP,
    CANDIDATE_DEPTH_FLOOR,
    DISPLAY_TOP_K,
    RETRIEVAL_PROFILE,
    WithinDocumentSlot,
    select_within_document_slots,
)
from .result import RetrievalContractError

FULL_TEXT_STATE = "FULL_TEXT"
SERVED_MODE_FTS5 = "fts5"
REQUESTED_MODE = "lexical"

# --------------------------------------------------------------------------- #
# Served-build resolution (A3 §2.2)
# --------------------------------------------------------------------------- #
#
# ``corpus_manifest`` holds exactly one current manifest; ``lexical_index_state``
# is the singleton FTS pointer whose ``lexical_generation_id`` names the served
# build; ``corpus_publication_builds`` carries that build's admission state. The
# resolver accepts only ``status='published'`` (a ``lexical_ready`` row is a
# staged, unpublished build and is never a served build) and requires the
# generation to belong to the current manifest. The three inner counts make the
# "zero or several" refusals explicit: a second published build for the current
# manifest, a missing/duplicated FTS state row, or a state row naming another
# generation all fail closed.
_SERVED_BUILD_SQL = """
WITH current_manifest AS (
    SELECT manifest_id FROM corpus_manifest WHERE is_current = 1
),
served_state AS (
    SELECT corpus_manifest_id, lexical_generation_id
    FROM lexical_index_state
    WHERE singleton_id = 1
),
published_build AS (
    SELECT b.build_id, b.manifest_id, b.lexical_digest, b.lexical_row_count
    FROM corpus_publication_builds b
    JOIN current_manifest cm ON cm.manifest_id = b.manifest_id
    WHERE b.status = 'published' AND b.lexical_ready = 1
)
SELECT p.build_id,
       p.manifest_id,
       p.lexical_digest,
       p.lexical_row_count,
       (SELECT COUNT(*) FROM published_build) AS published_build_count,
       (SELECT COUNT(*) FROM served_state) AS served_state_row_count,
       (SELECT COUNT(*) FROM served_state s
         WHERE s.corpus_manifest_id = p.manifest_id
           AND s.lexical_generation_id = p.build_id) AS generation_match_count
FROM published_build p
"""


@dataclass(frozen=True)
class ServedBuildResolution:
    """The one served build, plus the counts the resolver refused on.

    ``build_id``/``corpus_manifest_id`` are the resolved identities, ``digest``
    is the persisted lexical (FTS) digest and ``lexical_row_count`` the FTS row
    count. ``matched_row_count`` is the resolver row count (always 1 on
    success); the remaining counts are the fail-closed guards.
    """

    build_id: str
    corpus_manifest_id: str
    lexical_digest: str
    lexical_row_count: int
    matched_row_count: int
    published_build_count: int
    served_state_row_count: int
    generation_match_count: int

    @property
    def manifest_id(self) -> str:
        return self.corpus_manifest_id


def _require_publication_state(conn: sqlite3.Connection) -> None:
    """Fail closed when the served-build state columns are not present."""
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    if not {"corpus_manifest", "corpus_publication_builds", "lexical_index_state"} <= tables:
        raise RetrievalContractError(
            "served_build_state_unavailable",
            "the served-build resolver requires corpus_manifest, "
            "corpus_publication_builds and lexical_index_state",
        )
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(lexical_index_state)")
    }
    if "lexical_generation_id" not in columns:
        raise RetrievalContractError(
            "served_build_state_unavailable",
            "lexical_index_state has no lexical_generation_id column",
        )


def resolve_served_build(
    conn: sqlite3.Connection,
    *,
    expected_manifest_id: str | None = None,
    expected_build_id: str | None = None,
) -> ServedBuildResolution:
    """Resolve the one served build (A3 §2.2); fail closed on zero or several."""
    _require_publication_state(conn)
    rows = conn.execute(_SERVED_BUILD_SQL).fetchall()
    if len(rows) != 1:
        raise RetrievalContractError(
            "served_build_not_singular",
            "the served build must resolve to exactly one published, "
            f"lexical_ready generation of the current manifest; matched {len(rows)}",
        )
    (
        build_id,
        manifest_id,
        digest,
        row_count,
        published_build_count,
        served_state_row_count,
        generation_match_count,
    ) = rows[0]
    if int(published_build_count or 0) != 1:
        raise RetrievalContractError(
            "served_build_not_singular",
            "the current manifest must have exactly one published lexical_ready "
            f"build; found {int(published_build_count or 0)}",
        )
    if int(served_state_row_count or 0) != 1:
        raise RetrievalContractError(
            "served_build_state_not_singular",
            "lexical_index_state must hold exactly one FTS pointer row; "
            f"found {int(served_state_row_count or 0)}",
        )
    if int(generation_match_count or 0) != 1:
        raise RetrievalContractError(
            "served_build_generation_mismatch",
            "the FTS generation must be the published build of the current "
            "manifest",
        )
    digest = str(digest or "")
    if not digest:
        raise RetrievalContractError(
            "served_build_fts_digest_missing",
            f"served build {build_id!r} has no persisted lexical digest",
        )
    lexical_row_count = int(row_count or 0)
    if lexical_row_count < 1:
        raise RetrievalContractError(
            "served_build_fts_empty",
            f"served build {build_id!r} has no served FTS rows",
        )
    build_id = str(build_id)
    manifest_id = str(manifest_id)
    if expected_manifest_id is not None and manifest_id != expected_manifest_id:
        raise RetrievalContractError(
            "served_build_manifest_mismatch",
            f"served build belongs to manifest {manifest_id!r}, not the "
            f"requested manifest {expected_manifest_id!r}",
        )
    if expected_build_id is not None and build_id != expected_build_id:
        raise RetrievalContractError(
            "served_build_id_mismatch",
            f"the served build is {build_id!r}, not the expected build "
            f"{expected_build_id!r}",
        )
    return ServedBuildResolution(
        build_id=build_id,
        corpus_manifest_id=manifest_id,
        lexical_digest=digest,
        lexical_row_count=lexical_row_count,
        matched_row_count=1,
        published_build_count=int(published_build_count or 0),
        served_state_row_count=int(served_state_row_count or 0),
        generation_match_count=int(generation_match_count or 0),
    )


# --------------------------------------------------------------------------- #
# Observation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LexicalDisplayObservation:
    """Observed facts for one lexical display retrieval call.

    Every field is taken from the retriever's own call: the served mode and the
    executed query policy / match mode come from the ``RetrievalTrace`` of the
    ``retrieve_lexical`` call, the window is ``result.candidates`` in rank
    order, the display is the ``lexical_display_v1`` slot decision, and the
    violations are recomputed from the served hits.
    """

    requested_mode: str
    served_mode: str
    match_mode: str
    policy: str | None
    temporal_window_days: int | None
    temporal_centre_date: str | None
    ordered_candidate_evidence_ids: tuple[str, ...]
    ordered_final_ranked_evidence_ids: tuple[str, ...]
    slot_exclusions: tuple[tuple[str, str], ...]
    ticker_violations: tuple[str, ...]
    cutoff_violations: tuple[str, ...]
    latency_ms: int
    served_build_id: str
    served_manifest_id: str
    retrieval_profile: str = RETRIEVAL_PROFILE
    is_degraded: bool = False
    rank_changes: dict[str, int] = field(default_factory=dict)
    duplicate_drops: None = None
    degradation_reasons: tuple[str, ...] = ()
    arm_names: tuple[str, ...] = (REQUESTED_MODE,)
    #: The verified served projection the call ran on (WP-B B7a), or None.
    projection_build_id: str | None = None
    projection_sha256: str | None = None

    @property
    def ordered_window_chunk_ids(self) -> tuple[str, ...]:
        """The FTS candidate window in rank order (alias of the candidate ids)."""
        return self.ordered_candidate_evidence_ids

    @property
    def ordered_displayed_chunk_ids(self) -> tuple[str, ...]:
        """The displayed chunk ids in slot order (alias of the final ranking)."""
        return self.ordered_final_ranked_evidence_ids


@dataclass(frozen=True)
class ObservedLexicalDisplay:
    """The served V1.1 result set plus the observation for the same call.

    The lexical path always serves a representable set, so ``result_set`` is
    never ``None`` (unlike the hybrid path, where a degraded reranker has no
    V1.1 representation).
    """

    observation: LexicalDisplayObservation
    result_set: v1_result.RetrievalResultSet


# --------------------------------------------------------------------------- #
# Build-scoped metadata reads
# --------------------------------------------------------------------------- #
# The eval candidate adapter's metadata read (``_METADATA_COLUMNS``,
# ``v1_1/candidate_lexical_adapter.py:54-74``, applied at ``:117-136``), ported
# onto the served ``build_id``. ``provider`` is added because a served V1.1 hit
# must name its provider and that value lives on this build-scoped row
# (``hybrid.py`` reads the same column and only falls back to the canonical
# asset).
_CHUNK_METADATA_COLUMNS = (
    "chunk_id",
    "ordinal",
    "content_text",
    "ticker_associations",
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
    "provider_updated_at",
    "provider",
)

_ASSET_METADATA_COLUMNS = (
    "asset_type",
    "provider",
    "publisher",
    "canonical_url",
    "serving_status",
    "temporal_precision",
    "source_published_at",
    "subtype_metadata",
)


def _authoritative_source_time(asset: dict[str, Any], source_class: SourceClass, canonical_url: str | None):
    """Return a source-owned timestamp only for store-backed source classes.

    SEC filings persist ``accepted_time`` in subtype metadata. Issuer
    disclosures and company press releases persist canonical
    ``source_published_at``. News, analysis, government sources outside SEC,
    and records without a precise stored time have no source-time path.
    """
    raw = None
    if source_class == SourceClass.OFFICIAL_GOVERNMENT and canonical_url and "sec.gov/" in canonical_url:
        metadata = asset.get("subtype_metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, json.JSONDecodeError):
                metadata = None
        if isinstance(metadata, dict):
            raw = metadata.get("accepted_time")
    elif source_class in {SourceClass.ISSUER_DISCLOSURE, SourceClass.CORPORATE_PRESS_RELEASE}:
        raw = asset.get("source_published_at")
    if not raw:
        return None
    return _utc_timestamp(str(raw), field_name="source_time_at", chunk_id="canonical_asset")


def _chunk_metadata(
    conn: sqlite3.Connection, *, build_id: str, chunk_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    unique = tuple(dict.fromkeys(chunk_ids))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    rows = conn.execute(
        "SELECT " + ", ".join(_CHUNK_METADATA_COLUMNS) + " FROM corpus_build_chunks "
        f"WHERE build_id=? AND chunk_id IN ({placeholders})",
        (build_id, *unique),
    ).fetchall()
    metadata = {str(row[0]): dict(zip(_CHUNK_METADATA_COLUMNS, row)) for row in rows}
    missing = [chunk_id for chunk_id in unique if chunk_id not in metadata]
    if missing:
        raise RetrievalContractError(
            "served_chunk_metadata_missing",
            "served build chunk metadata missing for: " + ",".join(missing[:5]),
        )
    return metadata


def _asset_metadata(
    conn: sqlite3.Connection, *, build_id: str, asset_id: str
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT " + ", ".join(_ASSET_METADATA_COLUMNS) + " FROM canonical_assets "
        "WHERE asset_id = ?",
        (asset_id,),
    ).fetchall()
    if len(rows) != 1:
        raise RetrievalContractError(
            "served_chunk_canonical_asset_unresolved",
            f"served build {build_id!r} chunk canonical asset {asset_id!r} does not "
            f"resolve to exactly one row (matched {len(rows)})",
        )
    return dict(zip(_ASSET_METADATA_COLUMNS, rows[0]))


_SEC_DOCUMENT_HEADER = re.compile(
    r"^\s*(?:EX-[\w.\-]+|8-K|10-Q|10-K|20-F|6-K|S-\d+|DEF 14A)\s*\n\s*\d+\s*\n\s*"
    r"([\w.\-]+\.(?:htm|html|txt|pdf))"
)


def _sec_document_url(
    conn: sqlite3.Connection, *, build_id: str, document_id: str, filing_url: str
) -> str | None:
    """URL of the EDGAR sub-document a served document was built from.

    Each served EDGAR document begins with the SEC submission header
    (``TYPE / SEQUENCE / FILENAME``); the sub-document lives next to the
    filing's primary document. None when the header is absent.
    """
    row = conn.execute(
        "SELECT content_text FROM corpus_build_chunks WHERE build_id=? "
        "AND document_id=? ORDER BY ordinal LIMIT 1",
        (build_id, document_id),
    ).fetchone()
    match = _SEC_DOCUMENT_HEADER.match(str(row[0] or "")) if row else None
    if match is None:
        return None
    return filing_url.rsplit("/", 1)[0] + "/" + match.group(1)


# ``hybrid.py`` ``_utc`` (lines 41-49): parse a persisted UTC timestamp, adding
# the offset SQLite's ``strftime``-free ISO rendering may omit. Naive values are
# never reinterpreted.
def _utc_timestamp(value: object, *, field_name: str, chunk_id: str) -> datetime:
    text = str(value or "")
    if not text:
        raise RetrievalContractError(
            "served_hit_timestamp_missing",
            f"served chunk {chunk_id!r} has no {field_name}",
        )
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise RetrievalContractError(
            "served_hit_timestamp_invalid",
            f"served chunk {chunk_id!r} {field_name} {text!r} is not ISO-8601",
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RetrievalContractError(
            "served_hit_timestamp_naive",
            f"served chunk {chunk_id!r} {field_name} {text!r} is not timezone-aware",
        )
    return parsed.astimezone(timezone.utc)


def _cutoff_at(cutoff: str) -> datetime:
    try:
        return datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        raise RetrievalContractError("invalid_cutoff", f"cutoff {cutoff!r}") from None


def _ticker_scope(raw: object, *, chunk_id: str) -> tuple[str, ...]:
    if isinstance(raw, str) and raw:
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise RetrievalContractError(
                "served_hit_ticker_associations_invalid",
                f"served chunk {chunk_id!r} ticker_associations is not valid JSON",
            ) from None
        if isinstance(values, list):
            return tuple(sorted({str(value) for value in values}))
    raise RetrievalContractError(
        "served_hit_ticker_associations_invalid",
        f"served chunk {chunk_id!r} ticker_associations is not a JSON array",
    )


# --------------------------------------------------------------------------- #
# The retriever
# --------------------------------------------------------------------------- #
class LexicalDisplayRetriever:
    """The single V1.1 production retrieval path (A3 §2.1).

    The retriever binds the served-build state per operation (the connection-
    per-operation pattern of :class:`~catalyst_data.retrieval.hybrid.
    ProductionHybridRetriever`), so a pointer flip is picked up by the next
    call and every call observes one consistent SQLite snapshot.
    """

    def __init__(
        self,
        *,
        db: Any = None,
        db_conn_factory: Callable[[], Any] | None = None,
        data_runtime_identity: DataRuntimeIdentity | None = None,
        temporal_identity: TemporalIdentity | None = None,
        retrieval_policy_version: str = RETRIEVAL_PROFILE,
        served_projection: Any = None,
    ) -> None:
        # ``db`` is the injected fixture seam for callers that already hold a
        # connection; production uses ``db_conn_factory`` so every operation
        # obtains and closes its own connection (TSD §16 connection-per-
        # operation), mirroring ProductionHybridRetriever.
        self._db = db
        self._db_conn_factory = db_conn_factory
        self.data_runtime_identity = data_runtime_identity
        self.temporal_identity = temporal_identity
        self.retrieval_policy_version = retrieval_policy_version
        # A ``ServedProjectionHandle`` (path + factory + verified identity; no
        # connection). Each operation opens and closes its own sidecar
        # connection next to the served one (WP-B B7a).
        self.served_projection = served_projection

    # -- lifecycle ---------------------------------------------------------
    def _operation_connection(self) -> tuple[Any, bool]:
        """Return (connection, owned). Owned connections must be closed."""
        if self._db is not None:
            return self._db, False
        if self._db_conn_factory is None:
            raise RetrievalContractError(
                "db_conn_factory_unavailable",
                "lexical display retrieval requires a connection or a factory",
            )
        return self._db_conn_factory(), True

    def lead_terms(self, *, ticker: str, temporal_identity: Any) -> tuple[str, ...]:
        """Session headline terms for a lead follow-up query (leads, not evidence)."""
        from catalyst_data.retrieval.lead_terms import session_lead_terms

        conn, owned = self._operation_connection()
        try:
            return session_lead_terms(
                conn,
                ticker=ticker,
                window_start_at=utc_iso_z(temporal_identity.information_window_start_at),
                cutoff_at=utc_iso_z(temporal_identity.cutoff_at),
            )
        finally:
            if owned:
                conn.close()

    def resolve_served_build(
        self,
        *,
        expected_manifest_id: str | None = None,
        expected_build_id: str | None = None,
    ) -> ServedBuildResolution:
        """Resolve the served build on a fresh connection (A3 §2.2)."""
        conn, owned = self._operation_connection()
        try:
            return resolve_served_build(
                conn,
                expected_manifest_id=expected_manifest_id,
                expected_build_id=expected_build_id,
            )
        finally:
            if owned:
                conn.close()

    # -- retrieval protocol ------------------------------------------------
    def retrieve(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        top_k: int,
        candidate_depth: int,
        requested_manifest_id: str | None = None,
        temporal_identity: TemporalIdentity | None = None,
        source_classes: tuple[str, ...] | None = None,
        temporal_centre_date: str | None = None,
    ) -> v1_result.RetrievalResultSet:
        """Serve the V1.1 result set for one query (no observation)."""
        observed = self.retrieve_with_observation(
            query,
            ticker=ticker,
            cutoff=cutoff,
            top_k=top_k,
            candidate_depth=candidate_depth,
            requested_manifest_id=requested_manifest_id,
            temporal_identity=temporal_identity,
            source_classes=source_classes,
            temporal_centre_date=temporal_centre_date,
        )
        return observed.result_set

    def retrieve_with_observation(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        top_k: int,
        candidate_depth: int,
        requested_manifest_id: str | None = None,
        temporal_identity: TemporalIdentity | None = None,
        source_classes: tuple[str, ...] | None = None,
        temporal_centre_date: str | None = None,
    ) -> ObservedLexicalDisplay:
        """Serve the V1.1 result set plus the observed retrieval facts.

        ``top_k`` and ``candidate_depth`` are required and must equal the frozen
        limits (``DISPLAY_TOP_K`` / ``CANDIDATE_DEPTH_CAP``): the retrieval
        policy is frozen (A3 C6), so any other value raises instead of silently
        changing the measured path.

        ``temporal_centre_date`` is the structured temporal centre the agent
        layer derived from ``time_scope`` (WP-B B3: the asked session date for
        ``SESSION_INFORMATION_WINDOW``, the previous closed trading day for
        ``PRIOR_SESSION``). It is passed to the lexical arm as the structured
        ``session_date``, the highest-priority structured field of
        ``resolve_temporal_center``; the free-text query date never overrides it
        (AMEND-5.2). When it is omitted the validated ``TemporalIdentity``
        session date is used. The cutoff remains the hard bound.
        """
        if top_k != DISPLAY_TOP_K:
            raise ValueError(
                f"lexical display top_k must be {DISPLAY_TOP_K}; the display "
                "policy is frozen (Frozen-A3 C6)"
            )
        if candidate_depth != CANDIDATE_DEPTH_CAP:
            raise ValueError(
                f"lexical display candidate_depth must be {CANDIDATE_DEPTH_CAP} "
                f"(floor {CANDIDATE_DEPTH_FLOOR}); the retrieval depth is frozen "
                "(Frozen-A3 C6)"
            )
        runtime = self.data_runtime_identity
        if runtime is None:
            raise RetrievalContractError(
                "data_runtime_identity_unavailable",
                "the lexical path requires the runtime-authority DataRuntimeIdentity",
            )
        identity = temporal_identity or self.temporal_identity
        if identity is None:
            raise RetrievalContractError(
                "temporal_identity_unavailable",
                "the lexical path requires the validated request TemporalIdentity",
            )
        requested_manifest_id = (
            runtime.corpus_manifest_id
            if requested_manifest_id is None
            else requested_manifest_id
        )
        if requested_manifest_id != runtime.corpus_manifest_id:
            raise RetrievalContractError(
                "runtime_identity_manifest_mismatch",
                "the injected DataRuntimeIdentity corpus manifest does not match "
                "the requested manifest",
            )
        cutoff_at = _cutoff_at(cutoff)
        if utc_iso_z(identity.cutoff_at) != cutoff:
            raise RetrievalContractError(
                "temporal_identity_cutoff_mismatch",
                "the injected TemporalIdentity cutoff does not match the "
                "requested cutoff",
            )
        window_start, window_end = identity.publication_window()
        retrieval_cutoff = utc_iso_z(window_end)
        retrieval_start = (
            utc_iso_z(window_start)
            if identity.evidence_window_start_at is not None else None
        )

        conn, owned = self._operation_connection()
        projection = self.served_projection
        sidecar = None
        try:
            resolution = resolve_served_build(
                conn, expected_manifest_id=requested_manifest_id
            )
            if projection is not None:
                sidecar = projection.connect()
            # Structured session centre: the B3 temporal_centre_date when the
            # agent layer supplies one, else the validated identity session.
            session_date = temporal_centre_date or identity.session_date
            started = time.monotonic()
            lexical = retrieve_lexical(
                conn,
                query,
                ticker=ticker,
                cutoff=retrieval_cutoff,
                requested_manifest_id=requested_manifest_id,
                top_k=top_k,
                candidate_depth=candidate_depth,
                source_classes=_normalize_source_classes(source_classes),
                include_trace=True,
                session_date=session_date,
                window_start_at=retrieval_start,
                allow_late_updates=identity.evidence_window_start_at is not None,
                served_projection=(
                    None if projection is None else (projection, sidecar)
                ),
            )
            latency_ms = max(0, int((time.monotonic() - started) * 1000))

            # The window is the full candidate set in lexical rank order.
            window = tuple(item.chunk_id for item in lexical.candidates)
            window_rank = {chunk_id: rank for rank, chunk_id in enumerate(window, 1)}
            raw_score = {
                item.chunk_id: item.lexical_raw_score for item in lexical.candidates
            }

            slots = select_within_document_slots(
                conn,
                build_id=resolution.build_id,
                candidate_chunk_ids=window,
                top_k=DISPLAY_TOP_K,
            )
            displayed = tuple(
                sorted(
                    (slot for slot in slots if slot.display_rank is not None),
                    key=lambda slot: slot.display_rank,
                )
            )
            displayed_ids = tuple(slot.chunk_id for slot in displayed)

            metadata = _chunk_metadata(
                conn, build_id=resolution.build_id, chunk_ids=displayed_ids
            )
            assets = {
                chunk_id: _asset_metadata(
                    conn,
                    build_id=resolution.build_id,
                    asset_id=str(metadata[chunk_id]["canonical_asset_id"] or ""),
                )
                for chunk_id in displayed_ids
            }
            # EDGAR: link the exact sub-document (e.g. Exhibit 99.1) that holds
            # the chunk, not the filing's primary document.
            document_urls: dict[str, str | None] = {}
            for chunk_id in displayed_ids:
                asset = assets[chunk_id]
                url = str(asset.get("canonical_url") or "")
                if "sec.gov/Archives/" not in url:
                    continue
                document_id = str(metadata[chunk_id]["document_id"])
                if document_id not in document_urls:
                    document_urls[document_id] = _sec_document_url(
                        conn, build_id=resolution.build_id,
                        document_id=document_id, filing_url=url,
                    )
                if document_urls[document_id]:
                    assets[chunk_id] = {**asset, "canonical_url": document_urls[document_id]}
            hits = tuple(
                self._to_v1_hit(
                    position,
                    slot,
                    metadata=metadata[slot.chunk_id],
                    asset=assets[slot.chunk_id],
                    lexical_raw_score=raw_score.get(slot.chunk_id),
                    temporal_identity=identity,
                    data_runtime_identity=runtime,
                )
                for position, slot in enumerate(displayed, start=1)
            )
            # M9-1: headline hits whose asset has an attached public body fill
            # the slots the display left empty (the headline is the lead).
            web_hits = self._web_body_hits(
                conn,
                build_id=resolution.build_id,
                window=window,
                ticker=ticker,
                free_slots=DISPLAY_TOP_K - len(hits),
                first_position=len(hits) + 1,
                temporal_identity=identity,
                data_runtime_identity=runtime,
            )
            hits = hits + web_hits
            displayed_ids = displayed_ids + tuple(hit.chunk_id for hit in web_hits)
        finally:
            if sidecar is not None:
                sidecar.close()
            if owned:
                conn.close()

        ticker_violations = tuple(
            hit.chunk_id for hit in hits if ticker not in tuple(hit.ticker_scope)
        )
        cutoff_violations = tuple(
            hit.chunk_id for hit in hits if hit.eligible_at > datetime.fromisoformat(retrieval_cutoff.replace("Z", "+00:00"))
        )
        trace = lexical.trace
        match_mode = str(getattr(trace, "match_mode", None) or "none")
        observation = LexicalDisplayObservation(
            requested_mode=REQUESTED_MODE,
            served_mode=str(lexical.mode_served),
            match_mode=match_mode,
            policy=getattr(trace, "policy", None),
            temporal_window_days=getattr(trace, "temporal_window_days", None),
            temporal_centre_date=getattr(trace, "temporal_center_date", None),
            ordered_candidate_evidence_ids=window,
            ordered_final_ranked_evidence_ids=displayed_ids,
            slot_exclusions=tuple(
                (slot.chunk_id, slot.exclusion_reason) for slot in slots
            ),
            ticker_violations=ticker_violations,
            cutoff_violations=cutoff_violations,
            latency_ms=latency_ms,
            served_build_id=resolution.build_id,
            served_manifest_id=resolution.corpus_manifest_id,
            retrieval_profile=self.retrieval_policy_version,
            is_degraded=bool(lexical.is_degraded),
            rank_changes={
                chunk_id: window_rank[chunk_id] - rank
                for rank, chunk_id in enumerate(displayed_ids, start=1)
                if chunk_id in window_rank and window_rank[chunk_id] - rank != 0
            },
            degradation_reasons=tuple(
                reason for reason in (lexical.fallback_reason,) if reason
            ),
            projection_build_id=(
                None if projection is None else str(projection.build_id)
            ),
            projection_sha256=(
                None if projection is None else str(projection.projection_sha256)
            ),
        )
        result_set = v1_result.RetrievalResultSet(
            hits=hits,
            temporal_identity=identity,
            data_runtime_identity=runtime,
        )
        return ObservedLexicalDisplay(observation=observation, result_set=result_set)

    # -- mapping -----------------------------------------------------------
    def _web_body_hits(
        self,
        conn: Any,
        *,
        build_id: str,
        window: Sequence[str],
        ticker: str,
        free_slots: int,
        first_position: int,
        temporal_identity: TemporalIdentity,
        data_runtime_identity: DataRuntimeIdentity,
    ) -> tuple[v1_result.RetrievalHit, ...]:
        """Attached public bodies for the session's headlines about ``ticker``.

        M9 v2 block 2: looked up by (ticker, session window) in the attached
        store, so headlines ingested after the snapshot serve the same way.
        Only headlines that name the company are used (subject rule).
        """
        from catalyst_data.retrieval import legacy_read
        from catalyst_data.retrieval.web_bodies import (
            MAX_SCAN_ORDINAL,
            names_subject,
            prior_session_recap,
            session_bodies,
            store_path,
            subject_matcher,
        )

        del build_id, window
        if free_slots <= 0 or (store_path() is None and legacy_read.configured_reader() is None):
            return ()
        about_target = subject_matcher(ticker)
        publication_start, publication_end = temporal_identity.publication_window()
        scanned = session_bodies(
            [ticker],
            utc_iso_z(publication_start),
            utc_iso_z(publication_end),
            max_ordinal=MAX_SCAN_ORDINAL,
            include_window_start=temporal_identity.evidence_window_start_at is not None,
        )
        # Admission reads the first two chunks as before; the later ones are
        # only scanned for the paragraph that names the subject.
        bodies = {
            asset_id: chunks[:2]
            for asset_id, chunks in scanned.items()
            if chunks and names_subject(
                ticker, str(chunks[0]["title"]), str(chunks[0]["text"]), about_target
            )
            and (temporal_identity.evidence_window_start_at is not None or not prior_session_recap(
                chunks[:2],
                utc_iso_z(temporal_identity.session_open_at),
                temporal_identity.session_open_at.strftime("%A"),
                temporal_identity.session_date,
            ))
        }
        # Primary sources first: the issuer's own filings (SEC 8-K exhibits)
        # are usually filed before the day's reporting, so newest-first order
        # pushed them out of the slots. Within each group order is unchanged.
        bodies = dict(
            sorted(
                bodies.items(),
                key=lambda item: item[1][0]["source_class"] != "issuer_disclosure",
            )
        )
        # A third slot, after every article's lead and second chunk: the first
        # later paragraph that names the subject (a roundup's "RBC downgraded
        # Starbucks" paragraph), only when neither shown chunk names it, so
        # articles already about the subject do not spend slots.
        for asset_id in bodies:
            if any(about_target(str(c["text"])) for c in bodies[asset_id]):
                continue
            named = next(
                (c for c in scanned[asset_id]
                 if c["ordinal"] > 2 and about_target(str(c["text"]))),
                None,
            )
            if named is not None:
                bodies[asset_id] = bodies[asset_id] + [named]
        picked: list[tuple[str, dict]] = []
        seen_bodies: set[str] = set()
        for slot in (0, 1, 2):  # leads first, then seconds, then named paragraphs
            for asset_id, chunks in bodies.items():
                if slot >= len(chunks) or len(picked) >= free_slots:
                    continue
                chunk = chunks[slot]
                if slot == 0 and chunk["ordinal"] != 1:
                    continue
                if slot == 0 and chunk["body_sha256"] in seen_bodies:
                    continue  # syndicated duplicate of a body already shown
                seen_bodies.add(chunk["body_sha256"])
                picked.append((asset_id, chunk))
        hits = []
        for offset, (asset_id, chunk) in enumerate(picked):
            rows = conn.execute(
                "SELECT " + ", ".join(_ASSET_METADATA_COLUMNS) + " FROM canonical_assets "
                "WHERE asset_id = ?",
                (asset_id,),
            ).fetchall()
            # Headlines ingested after the snapshot have no canonical row.
            asset = (
                dict(zip(_ASSET_METADATA_COLUMNS, rows[0]))
                if len(rows) == 1
                else {
                    "publisher": chunk["publisher"], "asset_type": "NEWS",
                    "serving_status": "lead_candidate",
                    "temporal_precision": "publication_time",
                }
            )
            source_class = SourceClass(chunk["source_class"])
            hits.append(
                v1_result.RetrievalHit(
                    evidence_id=chunk["chunk_id"],
                    canonical_asset_id=asset_id,
                    content_version_id=f"web:{chunk['body_sha256']}",
                    corpus_document_id=f"web:{asset_id}",
                    chunk_id=chunk["chunk_id"],
                    excerpt=chunk["text"],
                    scores=(v1_result.StageScore(stage="lexical", value=None),),
                    ranks=(
                        v1_result.StageRank(stage="lexical", value=first_position + offset),
                    ),
                    source_class=source_class,
                    content_state=FULL_TEXT_STATE,
                    eligible_at=_utc_timestamp(
                        chunk["eligible_at"], field_name="eligible_at",
                        chunk_id=chunk["chunk_id"],
                    ),
                    source_time_at=(_authoritative_source_time(asset, source_class, chunk["canonical_url"])
                                    if temporal_identity.evidence_window_start_at is not None else None),
                    ticker_scope=(ticker,),
                    provider="public_web",
                    publisher=chunk["publisher"] or asset["publisher"],
                    dedup_cluster_id=None,
                    parse_quality="not_applicable",
                    retrieval_policy_version=self.retrieval_policy_version,
                    temporal_identity=temporal_identity,
                    data_runtime_identity=data_runtime_identity,
                    section_key="body",
                    chunk_ordinal=int(chunk["ordinal"]),
                    asset_type=str(asset["asset_type"]),
                    content_hash=chunk["content_hash"],
                    material_capability=v1_result.material_capability_for_content_state(
                        FULL_TEXT_STATE
                    ),
                    serving_status=str(asset["serving_status"]),
                    temporal_precision=str(asset["temporal_precision"]),
                    independence_group_id=None,
                    canonical_url=chunk["canonical_url"],
                    evidence_role=source_role_for(source_class).value,
                )
            )
        return tuple(hits)

    def _to_v1_hit(
        self,
        position: int,
        slot: WithinDocumentSlot,
        *,
        metadata: Mapping[str, Any],
        asset: Mapping[str, Any],
        lexical_raw_score: float | None,
        temporal_identity: TemporalIdentity,
        data_runtime_identity: DataRuntimeIdentity,
    ) -> v1_result.RetrievalHit:
        """One displayed slot as a V1.1 hit, read from stored columns."""
        chunk_id = str(metadata["chunk_id"])
        content_state = str(metadata["content_state"] or "")
        if content_state != FULL_TEXT_STATE:
            # The policy only expands FULL_TEXT bodies; a metadata-only or
            # title-only chunk must never occupy a slot (A3 §2.1).
            raise RetrievalContractError(
                "displayed_chunk_not_full_text",
                f"served chunk {chunk_id!r} holds a slot with content_state "
                f"{content_state!r}",
            )
        source_class = SourceClass(str(metadata["source_class"]))
        provider = str(metadata["provider"] or "") or str(asset["provider"] or "")
        if not provider:
            raise RetrievalContractError(
                "served_hit_provider_missing",
                f"served chunk {chunk_id!r} has no provider on its build row or "
                "its canonical asset",
            )
        return v1_result.RetrievalHit(
            evidence_id=chunk_id,
            canonical_asset_id=str(metadata["canonical_asset_id"]),
            content_version_id=str(metadata["content_version_id"]),
            corpus_document_id=str(metadata["corpus_document_id"]),
            chunk_id=chunk_id,
            excerpt=str(metadata["content_text"] or ""),
            scores=(
                v1_result.StageScore(stage="lexical", value=lexical_raw_score),
            ),
            ranks=(v1_result.StageRank(stage="lexical", value=position),),
            source_class=source_class,
            content_state=content_state,
            eligible_at=_utc_timestamp(
                metadata["available_at"],
                field_name="available_at",
                chunk_id=chunk_id,
            ),
            provider_updated_at=(
                _utc_timestamp(
                    metadata["provider_updated_at"],
                    field_name="provider_updated_at",
                    chunk_id=chunk_id,
                )
                if metadata["provider_updated_at"] else None
            ),
            source_time_at=(_authoritative_source_time(asset, source_class, asset["canonical_url"])
                            if temporal_identity.evidence_window_start_at is not None else None),
            ticker_scope=_ticker_scope(
                metadata["ticker_associations"], chunk_id=chunk_id
            ),
            provider=provider,
            publisher=asset["publisher"],
            dedup_cluster_id=metadata["dedup_cluster_id"],
            parse_quality=str(metadata["parse_quality"] or "not_applicable"),
            retrieval_policy_version=self.retrieval_policy_version,
            temporal_identity=temporal_identity,
            data_runtime_identity=data_runtime_identity,
            section_key=metadata["section_key"],
            chunk_ordinal=int(metadata["ordinal"]),
            asset_type=str(asset["asset_type"]),
            content_hash=metadata["content_hash"],
            material_capability=v1_result.material_capability_for_content_state(
                content_state
            ),
            serving_status=str(asset["serving_status"]),
            temporal_precision=str(asset["temporal_precision"]),
            independence_group_id=metadata["independence_group_id"],
            canonical_url=asset["canonical_url"],
            evidence_role=source_role_for(source_class).value,
        )


def _normalize_source_classes(
    source_classes: tuple[Any, ...] | None,
) -> tuple[str, ...] | None:
    """Source classes as the lexical arm's own ``SOURCE_CLASSES`` values (R11).

    ``None`` means no filter (unchanged). The agents' ``SourceClass`` enum is the
    same taxonomy as the data-core enum, so an enum member is unwrapped to its
    value; an empty tuple stays empty and is rejected by ``retrieve_lexical``
    exactly as it is today.
    """
    if source_classes is None:
        return None
    return tuple(
        value.value if isinstance(value, SourceClass) else str(value)
        for value in source_classes
    )


__all__ = [
    "LexicalDisplayObservation",
    "LexicalDisplayRetriever",
    "ObservedLexicalDisplay",
    "ServedBuildResolution",
    "resolve_served_build",
]
