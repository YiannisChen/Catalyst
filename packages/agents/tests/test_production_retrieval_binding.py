"""M4-0: production retrieval binding end-to-end (amendment §1).

A real staged fixture database drives the production hybrid path with injected
DataRuntimeIdentity/TemporalIdentity; the adapter output builds EvidenceState
items with independence/content-hash/section/materiality/source-role intact
and identities pass unchanged.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity

CUTOFF = "2026-08-02T00:00:00Z"
SNAPSHOT = "b" * 64
MANIFEST_HEX = "a" * 64


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-08-01",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-08-01T13:30:00Z"),
        session_close_at=_utc("2026-08-01T20:00:00Z"),
        information_window_start_at=_utc("2026-07-31T20:00:00Z"),
        cutoff_at=_utc(CUTOFF),
    )


def _runtime(corpus_manifest_id: str) -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id=SNAPSHOT,
        corpus_manifest_id=corpus_manifest_id,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _canonical_db() -> sqlite3.Connection:
    """Minimal served-generation DB: corpus_manifest, publication build,
    corpus_build_chunks, canonical_assets, canonical_content_versions."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE corpus_manifest (
            manifest_id TEXT PRIMARY KEY, manifest_json TEXT,
            is_current INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE corpus_publication_builds (
            build_id TEXT PRIMARY KEY, certified_snapshot_identity TEXT,
            header_json TEXT, manifest_id TEXT, status TEXT,
            document_count INTEGER, chunk_count INTEGER, source_utf8_bytes INTEGER,
            inventory_digest TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE corpus_build_chunks (
            build_id TEXT NOT NULL, chunk_id TEXT NOT NULL, document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL, section_key TEXT NOT NULL,
            ordinal TEXT NOT NULL, content_text TEXT NOT NULL, content_hash TEXT NOT NULL,
            metadata_hash TEXT NOT NULL, source_class TEXT NOT NULL, dedup_cluster_id TEXT,
            cluster_first_available_at TEXT, representative_document_id TEXT,
            available_at TEXT NOT NULL, ticker_associations TEXT NOT NULL,
            eligibility TEXT NOT NULL, status TEXT NOT NULL, boundary_kind TEXT NOT NULL,
            body_token_start INTEGER NOT NULL, body_token_end INTEGER NOT NULL,
            body_overlap_tokens INTEGER NOT NULL, prefix_token_count INTEGER NOT NULL,
            prefix_truncated INTEGER NOT NULL, section_parse_degraded INTEGER NOT NULL,
            source_kind TEXT NOT NULL, provider TEXT, source_type TEXT,
            canonical_asset_id TEXT, content_version_id TEXT, corpus_document_id TEXT,
            content_state TEXT, independence_group_id TEXT, parse_quality TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (build_id, chunk_id)
        );
        CREATE TABLE canonical_assets (
            asset_id TEXT PRIMARY KEY, asset_type TEXT NOT NULL, issuer_id TEXT NOT NULL,
            tickers_json TEXT NOT NULL, provider TEXT NOT NULL, publisher TEXT,
            canonical_url TEXT, source_class TEXT NOT NULL, source_published_at TEXT,
            eligible_at TEXT, eligible_at_reason TEXT NOT NULL, temporal_precision TEXT NOT NULL,
            accepted_time_recovered INTEGER NOT NULL, fail_closed INTEGER NOT NULL,
            ingested_at TEXT NOT NULL, content_state TEXT NOT NULL,
            serving_status TEXT NOT NULL, title TEXT, content_ref TEXT,
            dedup_cluster_id TEXT, independence_group_id TEXT, parse_quality TEXT NOT NULL,
            subtype_metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE canonical_content_versions (
            canonical_content_version_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL,
            content_hash TEXT NOT NULL, normalizer_version TEXT NOT NULL,
            materiality_version TEXT NOT NULL, version_ordinal INTEGER NOT NULL,
            created_at TEXT NOT NULL, payload_ref TEXT
        );
        """
    )
    now = "2026-08-01T00:00:00Z"
    conn.execute(
        "INSERT INTO corpus_manifest VALUES (?,?,1,?)",
        (MANIFEST_HEX, "{}", now),
    )
    build_id = "b" * 64
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, manifest_id, status,
            document_count, chunk_count, source_utf8_bytes, inventory_digest,
            created_at, updated_at)
           VALUES (?,?,?,?,?,1,1,100,'digest',?,?)""",
        (build_id, SNAPSHOT, "{}", MANIFEST_HEX, "staging", now, now),
    )
    chunk_id = "chunk:0001"
    asset_id = "v1:asset:news-1"
    version_id = "v1:content:news-1"
    conn.execute(
        """INSERT INTO corpus_build_chunks (
            build_id, chunk_id, document_id, chunk_profile_version, section_key,
            ordinal, content_text, content_hash, metadata_hash, source_class,
            dedup_cluster_id, cluster_first_available_at,
            representative_document_id, available_at, ticker_associations,
            eligibility, status, boundary_kind, body_token_start, body_token_end,
            body_overlap_tokens, prefix_token_count, prefix_truncated,
            section_parse_degraded, source_kind, provider, source_type,
            canonical_asset_id, content_version_id, corpus_document_id,
            content_state, independence_group_id, parse_quality, created_at,
            updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            build_id, chunk_id, "doc:1", "news_v2", "body", "0001",
            "NVDA guided higher in its earnings call.", "c" * 64, "m" * 64,
            "reported_news", "v1:dedup:1", now, "doc:1", now,
            '["NVDA"]', "eligible", "pending_embedding", "document_end", 0, 1, 0, 0, 0, 0,
            "article", "polygon", "polygon", asset_id, version_id, "doc:1",
            "FULL_TEXT", "v1:ind:1", "full", now, now,
        ),
    )
    conn.execute(
        """INSERT INTO canonical_assets (
            asset_id, asset_type, issuer_id, tickers_json, provider, publisher,
            canonical_url, source_class, source_published_at, eligible_at,
            eligible_at_reason, temporal_precision, accepted_time_recovered,
            fail_closed, ingested_at, content_state, serving_status, title,
            content_ref, dedup_cluster_id, independence_group_id, parse_quality,
            subtype_metadata, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            asset_id, "NEWS", "issuer:NVDA", '["NVDA"]', "polygon", "Wire Co",
            "https://example.test/nvda", "reported_news", now, now,
            "publication_time_provider", "publication_time", 0, 0, now,
            "FULL_TEXT", "body_candidate", "NVDA guidance", "raw:1",
            "v1:dedup:1", "v1:ind:1", "full",
            json.dumps({"description": "lead"}), now, now,
        ),
    )
    conn.execute(
        """INSERT INTO canonical_content_versions VALUES (?,?,?,?,?,1,?,?)""",
        (version_id, asset_id, "c" * 64, "news_body_v1", "materiality_v1", now, "raw:1"),
    )
    conn.commit()
    return conn


def _stage_arms(conn, monkeypatch):
    """Monkeypatch the lexical/dense arms with one served chunk from the db."""
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalFilters, RetrievalResult, RetrievalResultSet

    row = conn.execute(
        "SELECT chunk_id, document_id, content_text, available_at FROM corpus_build_chunks LIMIT 1"
    ).fetchone()
    value = RetrievalResult(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        available_at=row["available_at"],
        cutoff=CUTOFF,
        content_text=row["content_text"],
        filters_applied=RetrievalFilters(
            ticker="NVDA", requested_manifest_id=MANIFEST_HEX, cutoff=CUTOFF,
        ),
        source_class="reported_news",
        lexical_raw_score=-1.0,
        lexical_rank=1,
        corpus_manifest_id=MANIFEST_HEX,
        index_manifest_id="d" * 64,
        mode_requested="lexical",
        mode_served="fts5",
        is_degraded=False,
        timing_ms=1.0,
    )
    arm = RetrievalResultSet(
        candidates=(value,), results=(value,), candidate_count=1,
        mode_requested="lexical", mode_served="fts5", is_degraded=False,
    )
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *a, **k: arm)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", lambda *a, **k: arm)
    return row


def test_production_binding_identities_pass_and_evidence_state_item_is_complete(monkeypatch):
    from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
    from catalyst_data.retrieval.hybrid import ProductionHybridRetriever

    class Reranker:
        def score(self, query, candidates):
            return [1.0 for _ in candidates]

    conn = _canonical_db()
    _stage_arms(conn, monkeypatch)
    retriever = ProductionHybridRetriever(
        db=conn,
        lancedb_table=object(),
        embedding_fn=lambda q: np.ones(1024, dtype=np.float32),
        reranker=Reranker(),
        index_manifest_id="d" * 64,
        data_runtime_identity=_runtime(MANIFEST_HEX),
    )
    adapter = AgentRetrieverAdapter(retriever)
    evidence = adapter.retrieve(
        "why did NVDA move",
        ticker="NVDA",
        cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_HEX,
        temporal_identity=_temporal(),
        top_k=8,
        candidate_depth=20,
    )
    assert len(evidence) == 1
    item = evidence[0]
    assert item.provider == "polygon"
    assert item.ticker_associations == ("NVDA",)
    assert item.independence_status == "KNOWN_GROUP"
    assert item.evidence_role == "INDEPENDENT_REPORT"
    assert item.material_capability == "MATERIAL_CAPABLE"
    assert item.content_hash == "c" * 64
    assert item.section_key == "body"
    assert item.chunk_ordinal == 1
    assert item.asset_type == "NEWS"
    assert item.publisher == "Wire Co"
    assert item.canonical_url == "https://example.test/nvda"
    assert item.temporal_precision == "publication_time"
    assert item.serving_status == "body_candidate"
    assert item.temporal_identity == _temporal()
    assert item.data_runtime_identity == _runtime(MANIFEST_HEX)

    state_item = item.to_evidence_state_item(
        first_seen_round=1, contributing_task_ids=("research:1:0:abc",)
    )
    assert state_item.evidence_id == item.chunk_id
    assert state_item.independence_group_id == "v1:ind:1"
    assert state_item.independence_status == "KNOWN_GROUP"
    assert state_item.evidence_role == "INDEPENDENT_REPORT"
    assert state_item.material_capability == "MATERIAL_CAPABLE"
    assert state_item.content_hash == "c" * 64
    assert state_item.section_key == "body"
    assert state_item.chunk_ordinal == 1
    assert state_item.first_seen_round == 1
    assert state_item.contributing_task_ids == ("research:1:0:abc",)


def test_production_retriever_fails_closed_without_injected_identity():
    """The production retriever cannot serve the V1.1 contract without the
    runtime-authority DataRuntimeIdentity or the request TemporalIdentity."""
    from catalyst_data.retrieval.hybrid import ProductionHybridRetriever
    from catalyst_data.retrieval.result import RetrievalContractError

    retriever = ProductionHybridRetriever(
        db=object(),
        lancedb_table=object(),
        embedding_fn=lambda q: np.ones(1024, dtype=np.float32),
        reranker=None,
        index_manifest_id="d" * 64,
    )
    with pytest.raises(RetrievalContractError):
        retriever.retrieve(
            "q", ticker="NVDA", cutoff=CUTOFF,
            requested_manifest_id=MANIFEST_HEX,
        )
