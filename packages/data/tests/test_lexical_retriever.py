"""B2 — ``LexicalDisplayRetriever``: the one V1.1 production retrieval path.

Frozen-A3 §2.1 / §2.2 and WP-B B2. The retriever:

* resolves the served build (``lexical_index_state`` generation for the current
  manifest whose ``corpus_publication_builds`` row is ``published`` **and**
  ``lexical_ready=1``; exactly one row, never a ``lexical_ready``-only build);
* asks ``retrieve_lexical`` for the full candidate window at the frozen depth
  (100) and treats **``result.candidates`` in lexical rank order** as the
  window — never ``results[:top_k]``;
* displays with the frozen ``select_within_document_slots(top_k=DISPLAY_TOP_K)``
  policy on the served ``build_id``;
* returns the V1.1 ``RetrievalResultSet`` with every required hit field read
  from build-scoped stored columns (the candidate adapter's metadata read ported
  onto the served build), the lexical stage scored, and the dense/fusion/rerank
  stages absent.

The fixture is a production-shaped SQLite derivative: canonical registry rows,
one published build with ``corpus_build_chunks`` + per-build FTS rows, a
``lexical_index_state`` row pointing at it, and the legacy served view.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import SourceClass, source_role_for
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.corpus.streaming_publication import (
    build_candidate_fts,
    ensure_streaming_publication_schema,
)
from catalyst_data.retrieval.fts5 import retrieve_lexical
from catalyst_data.retrieval.lexical_display import (
    CANDIDATE_DEPTH_CAP,
    DISPLAY_TOP_K,
    EXCLUSION_BELOW_DISPLAY_CAP,
    EXCLUSION_DISPLAYED,
    EXCLUSION_NON_POSITIVE_SCORE,
    EXCLUSION_PER_DOCUMENT_CAP,
    RETRIEVAL_PROFILE,
    displayed_chunk_ids,
    select_within_document_slots,
)
from catalyst_data.retrieval.lexical_retriever import (
    LexicalDisplayRetriever,
    ObservedLexicalDisplay,
    ServedBuildResolution,
    resolve_served_build,
)
from catalyst_data.retrieval.result import RetrievalContractError
from catalyst_data.retrieval.v1_result import RetrievalResultSet
from catalyst_data.storage.sqlite import init_db

MANIFEST = "a" * 64
BUILD = "b" * 64
OTHER_BUILD = "c" * 64
TICKER = "ZZZZ"
CUTOFF = "2026-08-02T00:00:00Z"
NOW = "2026-08-01T00:00:00Z"
SESSION_DATE = "2026-08-02"

# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------

_CHUNK_COLUMNS = (
    "build_id", "chunk_id", "document_id", "chunk_profile_version",
    "section_key", "ordinal", "content_text", "content_hash", "metadata_hash",
    "source_class", "dedup_cluster_id", "cluster_first_available_at",
    "representative_document_id", "available_at", "ticker_associations",
    "eligibility", "status", "boundary_kind", "body_token_start",
    "body_token_end", "body_overlap_tokens", "prefix_token_count",
    "prefix_truncated", "section_parse_degraded", "source_kind", "provider",
    "source_type", "canonical_asset_id", "content_version_id",
    "corpus_document_id", "content_state", "independence_group_id",
    "parse_quality", "created_at", "updated_at",
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunk(
    chunk_id: str,
    text: str,
    *,
    document_id: str,
    ordinal: str,
    content_state: str = "FULL_TEXT",
    available_at: str = "2026-08-01T13:00:00Z",
    source_class: str = "issuer_disclosure",
    asset_type: str = "FILING",
    ticker: str = TICKER,
    provider: str = "fixture-provider",
) -> dict[str, object]:
    return {
        "build_id": BUILD,
        "chunk_id": chunk_id,
        "document_id": document_id,
        "chunk_profile_version": "filing_v3",
        "section_key": "item_2_02",
        "ordinal": ordinal,
        "content_text": text,
        "content_hash": _sha256(text),
        "metadata_hash": _sha256("{}"),
        "source_class": source_class,
        "dedup_cluster_id": f"cluster-{chunk_id}",
        "cluster_first_available_at": available_at,
        "representative_document_id": document_id,
        "available_at": available_at,
        "ticker_associations": json.dumps([ticker]),
        "eligibility": "eligible",
        "status": "active",
        "boundary_kind": "section",
        "body_token_start": 0,
        "body_token_end": 10,
        "body_overlap_tokens": 0,
        "prefix_token_count": 0,
        "prefix_truncated": 0,
        "section_parse_degraded": 0,
        "source_kind": "filing",
        "provider": provider,
        "source_type": asset_type.lower(),
        "canonical_asset_id": f"asset-{chunk_id}",
        "content_version_id": f"version-{chunk_id}",
        "corpus_document_id": document_id,
        "content_state": content_state,
        "independence_group_id": f"group-{document_id}",
        "parse_quality": "full",
        "created_at": NOW,
        "updated_at": NOW,
    }


# d1 is the richest document: three positive-scoring chunks (one of which is
# pushed out by the per-document cap) plus two non-positive (XBRL/cover-page)
# chunks that only exist outside the window but are still expanded and scored.
_CHUNKS: tuple[dict[str, object], ...] = (
    _chunk(
        "d1-0001",
        "ZZZZ Item 2.02 Results of Operations and Financial Condition: total "
        "revenue and net income grew; diluted earnings per share rose.",
        document_id="doc-d1",
        ordinal="0001",
    ),
    _chunk(
        "d1-0002",
        "ZZZZ Item 8.01 Other Events. Exhibit 99.1 press release attached.",
        document_id="doc-d1",
        ordinal="0002",
    ),
    _chunk(
        "d1-0003",
        "ZZZZ XBRL taxonomy fasb.org us-gaap schema reference for the filing.",
        document_id="doc-d1",
        ordinal="0003",
    ),
    _chunk(
        "d1-0004",
        "ZZZZ Exchange Act of 1934 commission file number cover page.",
        document_id="doc-d1",
        ordinal="0004",
    ),
    _chunk(
        "d1-0005",
        "ZZZZ total revenue and net income grew.",
        document_id="doc-d1",
        ordinal="0005",
    ),
    _chunk(
        "d1-0006",
        "ZZZZ press release exhibit 99 earnings results.",
        document_id="doc-d1",
        ordinal="0006",
    ),
    _chunk(
        "d2-0001",
        "ZZZZ quarterly dividend declaration of seventy cents per share.",
        document_id="doc-d2",
        ordinal="0001",
        source_class="reported_news",
        asset_type="NEWS",
        provider="fixture-news",
    ),
    # TITLE_ONLY / METADATA_ONLY: identify their document, never take a slot.
    _chunk(
        "d3-0001",
        "ZZZZ annual outlook summary title",
        document_id="doc-d3",
        ordinal="0001",
        content_state="TITLE_ONLY",
        source_class="reported_news",
        asset_type="NEWS",
    ),
    _chunk(
        "d5-0001",
        "ZZZZ sector outlook metadata row",
        document_id="doc-d5",
        ordinal="0001",
        content_state="METADATA_ONLY",
        source_class="reported_news",
        asset_type="NEWS",
    ),
    # Post-cutoff material text: never eligible, never displayed.
    _chunk(
        "d6-0001",
        "ZZZZ post cutoff earnings beat total revenue results",
        document_id="doc-d6",
        ordinal="0001",
        available_at="2026-09-01T13:00:00Z",
    ),
    # Another issuer: never ticker-eligible for ZZZZ.
    _chunk(
        "d7-0001",
        "AAAA unrelated earnings beat total revenue results",
        document_id="doc-d7",
        ordinal="0001",
        ticker="AAAA",
    ),
    # A different source class, for the source_classes filter.
    _chunk(
        "d8-0001",
        "ZZZZ investor presentation dividend policy summary.",
        document_id="doc-d8",
        ordinal="0001",
        source_class="corporate_press_release",
        asset_type="OFFICIAL_RELEASE",
    ),
    _chunk(
        "d9-0001",
        "ZZZZ net income results press release earnings.",
        document_id="doc-d9",
        ordinal="0001",
    ),
    _chunk(
        "d10-0001",
        "ZZZZ total revenue results of operations earnings.",
        document_id="doc-d10",
        ordinal="0001",
    ),
    _chunk(
        "d11-0001",
        "ZZZZ earnings results of operations.",
        document_id="doc-d11",
        ordinal="0001",
    ),
    _chunk(
        "d12-0001",
        "ZZZZ diluted earnings results.",
        document_id="doc-d12",
        ordinal="0001",
    ),
    _chunk(
        "d13-0001",
        "ZZZZ earnings results of operations per share.",
        document_id="doc-d13",
        ordinal="0001",
    ),
    _chunk(
        "d14-0001",
        "ZZZZ earnings press release net income.",
        document_id="doc-d14",
        ordinal="0001",
    ),
    _chunk(
        "d15-0001",
        "ZZZZ earnings total revenue and results.",
        document_id="doc-d15",
        ordinal="0001",
    ),
    _chunk(
        "d16-0001",
        "ZZZZ earnings diluted per share results.",
        document_id="doc-d16",
        ordinal="0001",
    ),
)

_ASSET_COLUMNS = (
    "asset_id", "asset_type", "issuer_id", "tickers_json", "provider",
    "publisher", "canonical_url", "source_class", "source_published_at",
    "eligible_at", "eligible_at_reason", "temporal_precision",
    "accepted_time_recovered", "fail_closed", "ingested_at", "content_state",
    "serving_status", "title", "content_ref", "dedup_cluster_id",
    "independence_group_id", "parse_quality", "subtype_metadata", "created_at",
    "updated_at",
)


def _asset_row(record: dict[str, object]) -> dict[str, object]:
    chunk_id = str(record["chunk_id"])
    return {
        "asset_id": f"asset-{chunk_id}",
        "asset_type": record["source_type"].upper() == "FILING" and "FILING" or "NEWS",
        "issuer_id": "issuer-1",
        "tickers_json": record["ticker_associations"],
        "provider": record["provider"],
        "publisher": "Fixture Publisher",
        "canonical_url": f"https://example.test/{chunk_id}",
        "source_class": record["source_class"],
        "source_published_at": record["available_at"],
        "eligible_at": record["available_at"],
        "eligible_at_reason": "publication_time",
        "temporal_precision": "publication_time",
        "accepted_time_recovered": 0,
        "fail_closed": 0,
        "ingested_at": NOW,
        "content_state": record["content_state"],
        "serving_status": "body_candidate",
        "title": f"title {chunk_id}",
        "content_ref": None,
        "dedup_cluster_id": record["dedup_cluster_id"],
        "independence_group_id": record["independence_group_id"],
        "parse_quality": "full",
        "subtype_metadata": "{}",
        "created_at": NOW,
        "updated_at": NOW,
    }


def _asset_type_for(record: dict[str, object]) -> str:
    return {
        "FILING": "FILING",
        "NEWS": "NEWS",
        "OFFICIAL_RELEASE": "OFFICIAL_RELEASE",
        "STRUCTURED_CONTEXT": "STRUCTURED_CONTEXT",
    }[str(record["source_type"]).upper()]


def _insert_chunk(conn: sqlite3.Connection, record: dict[str, object]) -> None:
    conn.execute(
        f"INSERT INTO corpus_build_chunks ({', '.join(_CHUNK_COLUMNS)}) "
        f"VALUES ({', '.join(':' + name for name in _CHUNK_COLUMNS)})",
        record,
    )


def _insert_asset(conn: sqlite3.Connection, record: dict[str, object]) -> None:
    row = _asset_row(record)
    row["asset_type"] = _asset_type_for(record)
    conn.execute(
        f"INSERT INTO canonical_assets ({', '.join(_ASSET_COLUMNS)}) "
        f"VALUES ({', '.join(':' + name for name in _ASSET_COLUMNS)})",
        row,
    )
    conn.execute(
        """INSERT INTO canonical_content_versions (
             canonical_content_version_id, asset_id, content_hash,
             normalizer_version, materiality_version, version_ordinal, created_at
           ) VALUES (?,?,?,?,?,1,?)""",
        (
            record["content_version_id"],
            row["asset_id"],
            record["content_hash"],
            "1.0.0",
            "1.0.0",
            NOW,
        ),
    )


def _publication_db(
    path: Path,
    *,
    published: bool = True,
    extra_published: bool = False,
) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    init_db(conn)
    ensure_streaming_publication_schema(conn)
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, ?, 1, ?)""",
        (MANIFEST, json.dumps({"manifest_id": MANIFEST}), NOW),
    )
    if extra_published:
        other_manifest = "d" * 64
        conn.execute(
            """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
               VALUES (?, ?, 0, ?)""",
            (other_manifest, json.dumps({"manifest_id": other_manifest}), NOW),
        )
    conn.execute(
        """INSERT INTO corpus_publication_builds (
             build_id, certified_snapshot_identity, header_json, status,
             manifest_id, manifest_json, document_count, chunk_count,
             inventory_digest, created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            BUILD, "9" * 64, json.dumps({"manifest_id": MANIFEST}), "staged",
            MANIFEST, json.dumps({"manifest_id": MANIFEST}),
            len({record["document_id"] for record in _CHUNKS}),
            len(_CHUNKS), "8" * 64, NOW, NOW,
        ),
    )
    for record in _CHUNKS:
        _insert_chunk(conn, record)
        _insert_asset(conn, record)
    if extra_published:
        conn.execute(
            """INSERT INTO corpus_publication_builds (
                 build_id, certified_snapshot_identity, header_json, status,
                 manifest_id, manifest_json, document_count, chunk_count,
                 inventory_digest, created_at, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                OTHER_BUILD, "9" * 64, json.dumps({"manifest_id": MANIFEST}),
                "published", MANIFEST, json.dumps({"manifest_id": MANIFEST}),
                0, 0, "8" * 64, NOW, NOW,
            ),
        )
        conn.execute(
            "UPDATE corpus_publication_builds SET lexical_ready=1 WHERE build_id=?",
            (OTHER_BUILD,),
        )
    conn.commit()

    build_candidate_fts(conn, build_id=BUILD)
    conn.execute(
        "UPDATE corpus_publication_builds SET status=?, lexical_ready=1, "
        "published_at=? WHERE build_id=?",
        ("published" if published else "lexical_ready", None if published else NOW, BUILD),
    )
    row = conn.execute(
        "SELECT lexical_digest, lexical_row_count FROM corpus_publication_builds "
        "WHERE build_id=?",
        (BUILD,),
    ).fetchone()
    conn.execute("DELETE FROM lexical_index_state")
    conn.execute(
        """INSERT INTO lexical_index_state (
             singleton_id, schema_version, corpus_manifest_id, mode_served,
             row_count, built_at, lexical_generation_id, lexical_digest
           ) VALUES (1, '1.0.0', ?, 'fts5', ?, ?, ?, ?)""",
        (MANIFEST, int(row[1]), NOW, BUILD, row[0]),
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# fixture expectations
# ---------------------------------------------------------------------------


def _temporal_identity() -> TemporalIdentity:
    cutoff_at = utc_iso_z_parse(CUTOFF)
    return TemporalIdentity(
        session_date=SESSION_DATE,
        market_timezone="America/New_York",
        session_open_at=cutoff_at,
        session_close_at=cutoff_at,
        information_window_start_at=cutoff_at,
        cutoff_at=cutoff_at,
    )


def utc_iso_z_parse(value: str):
    from datetime import datetime, timezone

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def _runtime_identity() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="snapshot-1",
        corpus_manifest_id=MANIFEST,
        fts_index_version="fts-digest-1",
        dense_index_version=None,
        embedding_model_revision=None,
        reranker_revision=None,
        query_policy_version="qp:v1",
    )


def _retriever(conn: sqlite3.Connection, **kwargs: object) -> LexicalDisplayRetriever:
    options = {
        "db": conn,
        "data_runtime_identity": _runtime_identity(),
        "temporal_identity": _temporal_identity(),
    }
    options.update(kwargs)
    return LexicalDisplayRetriever(**options)


def _retrieve(conn, query: str, **kwargs):
    options = {
        "ticker": TICKER,
        "cutoff": CUTOFF,
        "top_k": DISPLAY_TOP_K,
        "candidate_depth": CANDIDATE_DEPTH_CAP,
    }
    options.update(kwargs)
    return _retriever(conn).retrieve_with_observation(query, **options)


def _stored(
    conn: sqlite3.Connection, table: str, key: str, value: str
) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
    assert row is not None, f"{table} row {value} missing"
    return row


# ---------------------------------------------------------------------------
# 1. display == policy on the same window
# ---------------------------------------------------------------------------


def test_display_is_the_policy_applied_to_the_full_candidate_window(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    observed = _retrieve(conn, "ZZZZ earnings")
    assert isinstance(observed, ObservedLexicalDisplay)
    result = observed.result_set
    assert isinstance(result, RetrievalResultSet)
    observation = observed.observation

    raw = retrieve_lexical(
        conn,
        "ZZZZ earnings",
        ticker=TICKER,
        cutoff=CUTOFF,
        requested_manifest_id=MANIFEST,
        top_k=DISPLAY_TOP_K,
        candidate_depth=CANDIDATE_DEPTH_CAP,
        include_trace=True,
    )
    window = tuple(candidate.chunk_id for candidate in raw.candidates)
    # The window is result.candidates in lexical rank order, not results[:top_k].
    assert observation.ordered_candidate_evidence_ids == window
    assert len(window) > DISPLAY_TOP_K

    slots = select_within_document_slots(
        conn, build_id=BUILD, candidate_chunk_ids=window, top_k=DISPLAY_TOP_K
    )
    expected_display = displayed_chunk_ids(slots)
    assert expected_display
    assert tuple(hit.chunk_id for hit in result.hits) == expected_display
    assert observation.ordered_final_ranked_evidence_ids == expected_display
    assert observation.slot_exclusions == tuple(
        (slot.chunk_id, slot.exclusion_reason) for slot in slots
    )
    # Every exclusion reason the frozen policy can assign is exercised here.
    reasons = {slot.exclusion_reason for slot in slots}
    assert reasons == {
        EXCLUSION_DISPLAYED,
        EXCLUSION_NON_POSITIVE_SCORE,
        EXCLUSION_PER_DOCUMENT_CAP,
        EXCLUSION_BELOW_DISPLAY_CAP,
    }
    conn.close()


def test_display_is_stable_at_the_frozen_display_cap(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    result = _retrieve(conn, "ZZZZ earnings").result_set
    assert len(result.hits) <= DISPLAY_TOP_K
    assert len(result.hits) == DISPLAY_TOP_K
    conn.close()


# ---------------------------------------------------------------------------
# 2. the V1.1 hit fields come from build-scoped stored columns
# ---------------------------------------------------------------------------


def test_every_required_hit_field_is_read_from_stored_columns(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    runtime = _runtime_identity()
    temporal = _temporal_identity()
    result = _retriever(conn).retrieve(
        "ZZZZ earnings",
        ticker=TICKER,
        cutoff=CUTOFF,
        top_k=DISPLAY_TOP_K,
        candidate_depth=CANDIDATE_DEPTH_CAP,
    )
    assert result.temporal_identity == temporal
    assert result.data_runtime_identity == runtime
    assert result.hits

    for hit in result.hits:
        chunk = _stored(conn, "corpus_build_chunks", "chunk_id", hit.chunk_id)
        assert chunk["build_id"] == BUILD
        asset = _stored(conn, "canonical_assets", "asset_id", chunk["canonical_asset_id"])
        assert hit.evidence_id == hit.chunk_id
        assert hit.canonical_asset_id == chunk["canonical_asset_id"]
        assert hit.content_version_id == chunk["content_version_id"]
        assert hit.corpus_document_id == chunk["corpus_document_id"]
        assert hit.excerpt == chunk["content_text"]
        assert hit.source_class == SourceClass(chunk["source_class"])
        assert hit.content_state == "FULL_TEXT"
        assert hit.eligible_at == utc_iso_z_parse(chunk["available_at"])
        assert hit.ticker_scope == (TICKER,)
        assert hit.provider == chunk["provider"]
        assert hit.publisher == asset["publisher"]
        assert hit.dedup_cluster_id == chunk["dedup_cluster_id"]
        assert hit.parse_quality == chunk["parse_quality"]
        assert hit.retrieval_policy_version == RETRIEVAL_PROFILE
        assert hit.section_key == chunk["section_key"]
        assert hit.chunk_ordinal == int(chunk["ordinal"])
        assert hit.asset_type == asset["asset_type"]
        assert hit.content_hash == chunk["content_hash"]
        assert hit.material_capability == "MATERIAL_CAPABLE"
        assert hit.serving_status == asset["serving_status"]
        assert hit.temporal_precision == asset["temporal_precision"]
        assert hit.independence_group_id == chunk["independence_group_id"]
        assert hit.canonical_url == asset["canonical_url"]
        assert hit.evidence_role == source_role_for(hit.source_class).value
        assert hit.temporal_identity == temporal
        assert hit.data_runtime_identity == runtime
    conn.close()


def test_only_the_lexical_stage_is_scored(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    result = _retrieve(conn, "ZZZZ earnings").result_set
    for hit in result.hits:
        assert [entry.stage for entry in hit.scores] == ["lexical"]
        assert [entry.stage for entry in hit.ranks] == ["lexical"]
    ranks = [hit.ranks[0].value for hit in result.hits]
    assert ranks == list(range(1, len(result.hits) + 1))
    conn.close()


# ---------------------------------------------------------------------------
# 3. observed match modes and observation fields
# ---------------------------------------------------------------------------


def test_match_mode_is_observed_for_and_or_and_temporal_queries(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    retriever = _retriever(conn)
    and_mode = retriever.retrieve_with_observation(
        "ZZZZ earnings",
        ticker=TICKER,
        cutoff=CUTOFF,
        top_k=DISPLAY_TOP_K,
        candidate_depth=CANDIDATE_DEPTH_CAP,
    ).observation
    or_mode = retriever.retrieve_with_observation(
        "ZZZZ earnings dividend",
        ticker=TICKER,
        cutoff=CUTOFF,
        top_k=DISPLAY_TOP_K,
        candidate_depth=CANDIDATE_DEPTH_CAP,
    ).observation
    temporal_mode = retriever.retrieve_with_observation(
        "ZZZZ",
        ticker=TICKER,
        cutoff=CUTOFF,
        top_k=DISPLAY_TOP_K,
        candidate_depth=CANDIDATE_DEPTH_CAP,
    ).observation
    assert and_mode.match_mode == "AND"
    assert or_mode.match_mode == "OR"
    assert temporal_mode.match_mode == "temporal"
    assert temporal_mode.policy == "temporal_window"
    assert and_mode.policy == "content"
    conn.close()


def test_observation_carries_the_served_facts(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    observed = _retrieve(conn, "ZZZZ", temporal_centre_date=SESSION_DATE)
    observation = observed.observation
    assert observation.served_mode == "fts5"
    assert observation.is_degraded is False
    assert observation.policy == "temporal_window"
    assert observation.temporal_window_days is not None
    assert observation.temporal_centre_date == SESSION_DATE
    assert observation.served_build_id == BUILD
    assert observation.served_manifest_id == MANIFEST
    assert observation.retrieval_profile == RETRIEVAL_PROFILE
    assert observation.latency_ms >= 0
    assert observation.ticker_violations == ()
    assert observation.cutoff_violations == ()
    assert observation.ordered_window_chunk_ids == (
        observation.ordered_candidate_evidence_ids
    )
    assert observation.ordered_displayed_chunk_ids == (
        observation.ordered_final_ranked_evidence_ids
    )
    assert set(observation.ordered_final_ranked_evidence_ids).issubset(
        set(hit.chunk_id for hit in observed.result_set.hits)
    )
    conn.close()


# ---------------------------------------------------------------------------
# 4. the display policy's invariants survive the seam
# ---------------------------------------------------------------------------


def test_title_or_metadata_only_hit_never_takes_a_slot(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    observed = _retrieve(conn, "ZZZZ outlook")
    window = set(observed.observation.ordered_candidate_evidence_ids)
    assert {"d3-0001", "d5-0001"} <= window
    assert observed.result_set.hits == ()
    assert observed.observation.ordered_final_ranked_evidence_ids == ()
    conn.close()


def test_post_cutoff_and_other_ticker_chunks_never_appear(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    observed = _retrieve(conn, "ZZZZ earnings results revenue")
    window = set(observed.observation.ordered_candidate_evidence_ids)
    assert "d6-0001" not in window  # available_at after the cutoff
    assert "d7-0001" not in window  # another issuer's ticker
    for hit in observed.result_set.hits:
        assert hit.eligible_at <= utc_iso_z_parse(CUTOFF)
        assert TICKER in hit.ticker_scope
    conn.close()


def test_source_classes_filter_is_passed_through(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    observed = _retrieve(
        conn,
        "ZZZZ dividend",
        source_classes=(SourceClass.CORPORATE_PRESS_RELEASE,),
    )
    window = set(observed.observation.ordered_candidate_evidence_ids)
    assert "d8-0001" in window
    assert "d2-0001" not in window
    for hit in observed.result_set.hits:
        assert hit.source_class is SourceClass.CORPORATE_PRESS_RELEASE
    unfiltered = _retrieve(conn, "ZZZZ dividend")
    assert "d2-0001" in set(unfiltered.observation.ordered_candidate_evidence_ids)
    conn.close()


# ---------------------------------------------------------------------------
# 5. served-build resolution
# ---------------------------------------------------------------------------


def test_resolver_returns_the_published_generation(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    resolution = resolve_served_build(conn)
    assert isinstance(resolution, ServedBuildResolution)
    assert resolution.build_id == BUILD
    assert resolution.corpus_manifest_id == MANIFEST
    assert resolution.matched_row_count == 1
    assert resolution.lexical_row_count == len(_CHUNKS)
    row = conn.execute(
        "SELECT lexical_digest FROM corpus_publication_builds WHERE build_id=?",
        (BUILD,),
    ).fetchone()
    assert resolution.lexical_digest == row[0]
    assert _retriever(conn).resolve_served_build().build_id == BUILD
    conn.close()


def test_resolver_refuses_a_lexical_ready_only_build(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db", published=False)
    status = conn.execute(
        "SELECT status, lexical_ready FROM corpus_publication_builds WHERE build_id=?",
        (BUILD,),
    ).fetchone()
    assert status == ("lexical_ready", 1)
    with pytest.raises(RetrievalContractError):
        resolve_served_build(conn)
    conn.close()


def test_resolver_refuses_zero_published_builds(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db", published=False)
    conn.execute(
        "UPDATE corpus_publication_builds SET status='staged', lexical_ready=0 "
        "WHERE build_id=?",
        (BUILD,),
    )
    conn.commit()
    with pytest.raises(RetrievalContractError):
        resolve_served_build(conn)
    conn.close()


def test_resolver_refuses_two_published_builds(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db", extra_published=True)
    with pytest.raises(RetrievalContractError):
        resolve_served_build(conn)
    conn.close()


def test_resolver_refuses_a_generation_from_another_manifest(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, ?, 0, ?)""",
        ("e" * 64, json.dumps({"manifest_id": "e" * 64}), NOW),
    )
    conn.execute(
        "UPDATE lexical_index_state SET corpus_manifest_id=?",
        ("e" * 64,),
    )
    conn.commit()
    with pytest.raises(RetrievalContractError):
        resolve_served_build(conn)
    conn.close()


def test_resolver_refuses_an_expected_build_that_is_not_served(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    with pytest.raises(RetrievalContractError):
        resolve_served_build(conn, expected_build_id="f" * 64)
    conn.close()


# ---------------------------------------------------------------------------
# 6. frozen limits and the required signature
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    (
        {"top_k": 5},
        {"top_k": 20},
        {"candidate_depth": 20},
        {"candidate_depth": 50},
        {"candidate_depth": 101},
    ),
)
def test_non_frozen_depth_or_top_k_raises(tmp_path, kwargs):
    conn = _publication_db(tmp_path / "derivative.db")
    retriever = _retriever(conn)
    options = {
        "ticker": TICKER,
        "cutoff": CUTOFF,
        "top_k": DISPLAY_TOP_K,
        "candidate_depth": CANDIDATE_DEPTH_CAP,
    }
    options.update(kwargs)
    with pytest.raises(ValueError):
        retriever.retrieve("ZZZZ", **options)
    with pytest.raises(ValueError):
        retriever.retrieve_with_observation("ZZZZ", **options)
    conn.close()


def test_top_k_and_candidate_depth_have_no_defaults():
    import inspect

    for name in ("retrieve", "retrieve_with_observation"):
        signature = inspect.signature(getattr(LexicalDisplayRetriever, name))
        for parameter in ("top_k", "candidate_depth"):
            entry = signature.parameters[parameter]
            assert entry.kind is inspect.Parameter.KEYWORD_ONLY, (name, parameter)
            assert entry.default is inspect.Parameter.empty, (name, parameter)
        for parameter in ("source_classes", "temporal_centre_date"):
            entry = signature.parameters[parameter]
            assert entry.kind is inspect.Parameter.KEYWORD_ONLY, (name, parameter)


def test_identity_is_required_and_must_match_the_requested_manifest(tmp_path):
    conn = _publication_db(tmp_path / "derivative.db")
    with pytest.raises(RetrievalContractError):
        LexicalDisplayRetriever(db=conn).retrieve(
            "ZZZZ",
            ticker=TICKER,
            cutoff=CUTOFF,
            top_k=DISPLAY_TOP_K,
            candidate_depth=CANDIDATE_DEPTH_CAP,
        )
    with pytest.raises(RetrievalContractError):
        _retriever(conn).retrieve(
            "ZZZZ",
            ticker=TICKER,
            cutoff=CUTOFF,
            top_k=DISPLAY_TOP_K,
            candidate_depth=CANDIDATE_DEPTH_CAP,
            requested_manifest_id="f" * 64,
        )
    conn.close()


# ---------------------------------------------------------------------------
# 7. isolation: the data-core retrieval package never imports the eval package
# ---------------------------------------------------------------------------


def test_data_core_retrieval_never_imports_catalyst_eval():
    """No module of ``catalyst_data.retrieval`` may import ``catalyst_eval``.

    The display policy is a *move* into data-core (Frozen-A3 §2.1): the eval
    module is the sealed reproduction path only, and the dependency direction is
    eval -> data-core, never the reverse.
    """
    import ast

    import catalyst_data.retrieval as retrieval_package

    root = Path(retrieval_package.__file__).parent
    offenders: list[str] = []
    for source_path in sorted(root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "catalyst_eval" or name.startswith("catalyst_eval.") for name in names):
                offenders.append(f"{source_path.name}:{node.lineno}")
    assert offenders == [], f"data-core retrieval imported catalyst_eval: {offenders}"
