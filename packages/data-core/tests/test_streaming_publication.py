from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from catalyst_data.articles import upsert_article, upsert_article_ticker
from catalyst_data.corpus.manifest import build_manifest, compute_manifest_id
from catalyst_data.corpus.filing_v3 import FilingV3Profile
from catalyst_data.corpus.news_v2 import NewsV2Profile
from catalyst_data.corpus.streaming_publication import (
    PublicationLimits,
    ResumableResourceStop,
    _validated_fts_checkpoint,
    build_streaming_corpus_and_lexical_index,
    compute_streaming_manifest_id,
    estimate_streaming_publication_resources,
    export_legacy_manifest,
    ensure_streaming_publication_schema,
    iter_chunk_pages,
)
from catalyst_data.migrations import CURRENT_SCHEMA_VERSION
from catalyst_data.storage.sqlite import init_db


SNAPSHOT_ID = "b" * 64
OLD_MANIFEST_ID = "a" * 64
NOW = "2026-08-01T00:00:00Z"


def _inventory_item(chunk_id: str, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "chunk_id": chunk_id,
        "document_id": chunk_id.split(":news_v2", 1)[0],
        "chunk_profile_version": "news_v2",
        "section_key": "body",
        "ordinal": "0001",
        "content_hash": hashlib.sha256(chunk_id.encode()).hexdigest(),
        "metadata_hash": hashlib.sha256((chunk_id + "meta").encode()).hexdigest(),
        "available_at": NOW,
        "source_class": "reported_news",
        "dedup_cluster_id": None,
        "cluster_first_available_at": None,
        "representative_document_id": None,
        "eligibility": "eligible",
    }
    item.update(overrides)
    return item


def _manifest_header() -> dict[str, object]:
    return {
        "normalization_version": "1.0.0",
        "chunk_profile_versions": {"news": "news_v2", "filing": "filing_v3"},
        "source_classifier_version": "1.0.0",
        "certified_snapshot_identity": SNAPSHOT_ID,
        "tokenizer_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "embedding_revision": None,
    }


def _make_db(path: Path, *, article_count: int = 3, body_words: int = 80) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    init_db(conn)
    for index in range(article_count):
        article_id = f"poly:{index:06d}"
        raw_id = f"raw:{index:06d}"
        ticker = "AAPL" if index % 2 == 0 else "MSFT"
        conn.execute(
            """INSERT INTO raw_assets
               (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
               VALUES (?, ?, 'polygon_news', '2026-07-31', ?, ?)""",
            (raw_id, ticker, NOW, b"{}"),
        )
        upsert_article(
            conn,
            article={
                "article_id": article_id,
                "raw_asset_id": raw_id,
                "provider": "polygon",
                "source_type": "polygon_news",
                "ticker": ticker,
                "reference_date": "2026-07-31",
                "published_utc": NOW,
                "title": f"Unicode caf\N{LATIN SMALL LETTER E WITH ACUTE} {index}",
                "description": " ".join([f"word{index}"] * body_words),
                "publisher_name": "Fixture News",
                "dedup_group_id": f"group:{index}",
                "is_canonical": 1,
                "is_rag_eligible": 1,
            },
        )
        upsert_article_ticker(
            conn,
            article_id=article_id,
            ticker=ticker,
            raw_asset_id=raw_id,
            reference_date="2026-07-31",
        )
        conn.execute(
            """UPDATE articles SET is_canonical=1, is_rag_eligible=1,
                      source_class='reported_news', dedup_cluster_id=?
               WHERE article_id=?""",
            (f"cluster:{index}", article_id),
        )

    document_id = hashlib.sha256(b"filing-document").hexdigest()
    conn.execute(
        """INSERT INTO filings
           (filing_id, cik, ticker, form_type, filed_at, accession_number, url,
            dedup_group_id, is_canonical, is_rag_eligible)
           VALUES ('filing:1', '0000320193', 'AAPL', '8-K', ?, 'acc-1',
                   'https://example.test/8k', 'filing-group', 1, 1)""",
        (NOW,),
    )
    conn.execute(
        """INSERT INTO filing_documents
           (filing_id, document_url, document_type, text, char_len, content_type,
            byte_size, extraction_status, extracted_at, document_id)
           VALUES ('filing:1', 'https://example.test/8k.html', 'primary_doc', ?,
                   ?, 'text/html', ?, 'success', ?, ?)""",
        (
            "Item 1.01 Agreement\n" + " ".join(["filing"] * body_words),
            body_words * 7,
            body_words * 7,
            NOW,
            document_id,
        ),
    )
    _seed_old_selected_pair(conn)
    conn.commit()
    return conn


def _seed_old_selected_pair(conn: sqlite3.Connection) -> None:
    content_hash = hashlib.sha256(b"old text").hexdigest()
    metadata_hash = hashlib.sha256(b"old metadata").hexdigest()
    conn.execute(
        """INSERT INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, ?, 1, ?)""",
        (
            OLD_MANIFEST_ID,
            json.dumps({"certified_snapshot_identity": "0" * 64}),
            NOW,
        ),
    )
    conn.execute(
        """INSERT INTO corpus_chunks
           (chunk_id, document_id, chunk_profile_version, section_key, ordinal,
            content_text, content_hash, metadata_hash, source_class, available_at,
            ticker_associations, eligibility, manifest_id, status, boundary_kind,
            body_token_start, body_token_end, body_overlap_tokens,
            prefix_token_count, prefix_truncated, section_parse_degraded,
            created_at, updated_at)
           VALUES ('old:news_v2:body:0001', 'old', 'news_v2', 'body', '0001',
                   'old text', ?, ?, 'reported_news', ?, '[\"AAPL\"]', 'eligible',
                   ?, 'active', 'document_end', 0, 2, 0, 0, 0, 0, ?, ?)""",
        (content_hash, metadata_hash, NOW, OLD_MANIFEST_ID, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO corpus_chunks_fts (manifest_id, chunk_id, content_text) VALUES (?, ?, ?)",
        (OLD_MANIFEST_ID, "old:news_v2:body:0001", "old text"),
    )
    conn.execute(
        """INSERT INTO lexical_index_state
           (singleton_id, schema_version, corpus_manifest_id, mode_served,
            row_count, built_at) VALUES (1, '1.0.0', ?, 'fts5', 1, ?)""",
        (OLD_MANIFEST_ID, NOW),
    )


def _selected_pair(conn: sqlite3.Connection) -> tuple[str, str]:
    corpus = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current=1"
    ).fetchone()[0]
    lexical = conn.execute(
        "SELECT corpus_manifest_id FROM lexical_index_state WHERE singleton_id=1"
    ).fetchone()[0]
    return corpus, lexical


def _source_digest(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table, order in (
        ("articles", "article_id"),
        ("article_tickers", "article_id, ticker"),
        ("filings", "filing_id"),
        ("filing_documents", "filing_id, document_url"),
    ):
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            digest.update(json.dumps(dict(zip(columns, row)), sort_keys=True, default=str).encode())
    return digest.hexdigest()


def _fts_contents(conn: sqlite3.Connection, manifest_id: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        conn.execute(
            """SELECT chunk_id, content_text FROM corpus_chunks_fts
               WHERE manifest_id=? ORDER BY chunk_id COLLATE BINARY""",
            (manifest_id,),
        )
    )


def _lexical_rows_digest(rows: tuple[tuple[str, str], ...]) -> str:
    digest = hashlib.sha256()
    for chunk_id, content_text in rows:
        digest.update(
            json.dumps(
                [chunk_id, content_text],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


class _BoundedReadCursor:
    def __init__(self, cursor: object, read_sizes: list[int]):
        self._cursor = cursor
        self._read_sizes = read_sizes

    def fetchmany(self, size: int | None = None):
        if size is None or size > 500:
            raise AssertionError(f"unbounded cursor read requested: {size}")
        rows = self._cursor.fetchmany(size)
        self._read_sizes.append(len(rows))
        return rows

    def fetchone(self):
        return self._cursor.fetchone()

    def __iter__(self):
        raise AssertionError("full cursor iteration is forbidden")


class _BoundedReadConnection:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.read_sizes: list[int] = []

    def execute(self, sql: str, parameters: object = ()):
        return _BoundedReadCursor(
            self._conn.execute(sql, parameters), self.read_sizes
        )


def _seed_large_fts_checkpoint(
    conn: sqlite3.Connection, build_id: str, *, row_count: int = 1200
) -> None:
    ensure_streaming_publication_schema(conn)
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status,
            created_at, updated_at)
           VALUES (?, ?, '{}', 'staging', ?, ?)""",
        (build_id, SNAPSHOT_ID, NOW, NOW),
    )
    for index in range(row_count):
        chunk_id = f"synthetic:{index:04d}"
        content_text = f"synthetic text {index}"
        conn.execute(
            """INSERT INTO corpus_build_chunks
               (build_id, chunk_id, document_id, chunk_profile_version,
                section_key, ordinal, content_text, content_hash, metadata_hash,
                source_class, available_at, ticker_associations, eligibility,
                status, boundary_kind, body_token_start, body_token_end,
                body_overlap_tokens, prefix_token_count, prefix_truncated,
                section_parse_degraded, source_kind, created_at, updated_at)
               VALUES (?, ?, ?, 'news_v2', 'body', '0001', ?, ?, ?,
                       'reported_news', ?, '[\"AAPL\"]', 'eligible', 'active',
                       'document_end', 0, 3, 0, 0, 0, 0, 'article', ?, ?)""",
            (
                build_id,
                chunk_id,
                f"document:{index}",
                content_text,
                hashlib.sha256(content_text.encode()).hexdigest(),
                hashlib.sha256(f"metadata:{index}".encode()).hexdigest(),
                NOW,
                NOW,
                NOW,
            ),
        )
        conn.execute(
            """INSERT INTO corpus_build_chunks_fts
               (build_id, chunk_id, content_text) VALUES (?, ?, ?)""",
            (build_id, chunk_id, content_text),
        )
    for batch_no, start in enumerate(range(0, row_count, 400), start=1):
        batch_rows = tuple(
            (f"synthetic:{index:04d}", f"synthetic text {index}")
            for index in range(start, min(start + 400, row_count))
        )
        conn.execute(
            """INSERT INTO corpus_build_fts_batches
               (build_id, batch_no, row_count, text_utf8_bytes, first_chunk_id,
                last_chunk_id, batch_digest, checkpoint_chunk_id, committed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                build_id,
                batch_no,
                len(batch_rows),
                sum(len(text.encode("utf-8")) for _, text in batch_rows),
                batch_rows[0][0],
                batch_rows[-1][0],
                _lexical_rows_digest(batch_rows),
                batch_rows[-1][0],
                NOW,
            ),
        )
    conn.commit()


def _seed_same_manifest_legacy_generation(
    conn: sqlite3.Connection, manifest_id: str
) -> None:
    conn.execute("DELETE FROM lexical_index_state")
    conn.execute("DELETE FROM corpus_chunks_fts")
    conn.execute("DELETE FROM corpus_chunks")
    conn.execute("DELETE FROM corpus_manifest")
    conn.execute(
        """INSERT INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, ?, 1, ?)""",
        (manifest_id, json.dumps({"generation": "legacy"}), NOW),
    )
    for index in range(4):
        chunk_id = f"legacy:{index}:news_v2:body:0001"
        content_text = f"legacyterm old content {index}"
        conn.execute(
            """INSERT INTO corpus_chunks
               (chunk_id, document_id, chunk_profile_version, section_key, ordinal,
                content_text, content_hash, metadata_hash, source_class, available_at,
                ticker_associations, eligibility, manifest_id, status, boundary_kind,
                body_token_start, body_token_end, body_overlap_tokens,
                prefix_token_count, prefix_truncated, section_parse_degraded,
                created_at, updated_at)
               VALUES (?, ?, 'news_v2', 'body', '0001', ?, ?, ?,
                       'reported_news', ?, '["AAPL"]', 'eligible', ?, 'active',
                       'document_end', 0, 4, 0, 0, 0, 0, ?, ?)""",
            (
                chunk_id,
                f"legacy:{index}",
                content_text,
                hashlib.sha256(content_text.encode()).hexdigest(),
                hashlib.sha256(f"metadata:{index}".encode()).hexdigest(),
                NOW,
                manifest_id,
                NOW,
                NOW,
            ),
        )
        conn.execute(
            """INSERT INTO corpus_chunks_fts
               (manifest_id, chunk_id, content_text) VALUES (?, ?, ?)""",
            (manifest_id, chunk_id, content_text),
        )
    conn.execute(
        """INSERT INTO lexical_index_state
           (singleton_id, schema_version, corpus_manifest_id, mode_served,
            row_count, built_at) VALUES (1, '1.0.0', ?, 'fts5', 4, ?)""",
        (manifest_id, NOW),
    )
    conn.commit()


def test_streaming_manifest_identity_matches_legacy_for_shuffled_unicode_and_nulls(tmp_path: Path):
    inventory = [
        _inventory_item("poly:z:news_v2:body:0001", dedup_cluster_id="caf\N{LATIN SMALL LETTER E WITH ACUTE}"),
        _inventory_item(
            "sec:a:filing_v3:item_1.01:0001",
            document_id=hashlib.sha256(b"sec-a").hexdigest(),
            chunk_profile_version="filing_v3",
            section_key="item_1.01",
            source_class="official_government",
            cluster_first_available_at=None,
        ),
        _inventory_item("poly:a:news_v2:body:0001", representative_document_id=None),
    ]
    header = _manifest_header()
    legacy = build_manifest(active_chunk_inventory=list(reversed(inventory)), **header)
    legacy_id = compute_manifest_id(legacy)

    conn = sqlite3.connect(tmp_path / "identity.db")
    conn.execute("CREATE TABLE inventory (payload TEXT, chunk_id TEXT)")
    conn.executemany(
        "INSERT INTO inventory(payload, chunk_id) VALUES (?, ?)",
        [(json.dumps(item, ensure_ascii=False), item["chunk_id"]) for item in inventory],
    )
    rows = (
        json.loads(row[0])
        for row in conn.execute("SELECT payload FROM inventory ORDER BY chunk_id COLLATE BINARY")
    )
    assert compute_streaming_manifest_id(header, rows) == legacy_id

    output = tmp_path / "legacy.json"
    export_legacy_manifest(output, header=header, inventory_rows=(iter(inventory)), created_at=legacy["created_at"])
    assert compute_manifest_id(json.loads(output.read_text())) == legacy_id


def test_streaming_schema_upgrades_legacy_checkpoints_without_version_change(
    tmp_path: Path,
):
    from catalyst_data.corpus.streaming_publication import (
        ensure_streaming_publication_schema,
    )

    conn = sqlite3.connect(tmp_path / "streaming-schema-upgrade.db")
    init_db(conn)
    version_before = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.execute(
        """CREATE TABLE corpus_build_fts_batches (
               build_id TEXT NOT NULL,
               batch_no INTEGER NOT NULL,
               row_count INTEGER NOT NULL,
               text_utf8_bytes INTEGER NOT NULL,
               first_chunk_id TEXT NOT NULL,
               last_chunk_id TEXT NOT NULL,
               committed_at TEXT NOT NULL,
               PRIMARY KEY (build_id, batch_no)
           )"""
    )

    ensure_streaming_publication_schema(conn)

    batch_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(corpus_build_fts_batches)")
    }
    lexical_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(lexical_index_state)")
    }
    assert {"batch_digest", "checkpoint_chunk_id"} <= batch_columns
    assert {"lexical_generation_id", "lexical_digest"} <= lexical_columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version_before == CURRENT_SCHEMA_VERSION


@pytest.mark.parametrize(
    ("profile", "document"),
    [
        (
            NewsV2Profile(),
            {
                "document_id": "poly:iterator",
                "title": "Iterator",
                "description": " ".join(["news"] * 900),
                "available_at": NOW,
            },
        ),
        (
            FilingV3Profile(),
            {
                "document_id": hashlib.sha256(b"iterator-filing").hexdigest(),
                "filing_type": "8-K",
                "raw_text": "Item 1.01 Agreement\n" + " ".join(["filing"] * 900),
                "available_at": NOW,
            },
        ),
    ],
)
def test_profiles_offer_lazy_iter_chunks_with_legacy_equivalent_output(profile, document):
    lazy = profile.iter_chunks(document)
    assert iter(lazy) is lazy
    assert list(lazy) == profile.chunk(document)


def test_batch_size_does_not_change_chunks_hashes_or_reconciliation(tmp_path: Path):
    outputs = []
    for limits in (
        PublicationLimits(max_documents=1, source_utf8_bytes=32_000, max_chunks=1, chunk_text_utf8_bytes=32_000),
        PublicationLimits(max_documents=100, source_utf8_bytes=128_000, max_chunks=500, chunk_text_utf8_bytes=128_000),
    ):
        conn = _make_db(tmp_path / f"batch-{limits.max_documents}.db", article_count=5)
        result = build_streaming_corpus_and_lexical_index(
            conn, certified_snapshot_identity=SNAPSHOT_ID, clock=lambda: NOW, limits=limits
        )
        rows = tuple(
            row
            for page in iter_chunk_pages(conn, result.build_id, page_size=2)
            for row in page
        )
        assert rows
        assert result.corpus.chunk_count == len(rows)
        assert result.reconciliation.to_embed_count == len(rows)
        assert result.reconciliation.tombstone_count == 1
        outputs.append((result.corpus.manifest_id, rows))
    assert outputs[1] == outputs[0]


def test_enforced_buffer_limits_and_fixed_retention_for_10x_corpus(tmp_path: Path):
    limits = PublicationLimits(
        max_documents=3,
        source_utf8_bytes=12_000,
        max_chunks=4,
        chunk_text_utf8_bytes=12_000,
    )
    stats = []
    for count in (10, 100):
        conn = _make_db(tmp_path / f"bounded-{count}.db", article_count=count, body_words=30)
        result = build_streaming_corpus_and_lexical_index(
            conn, certified_snapshot_identity=SNAPSHOT_ID, clock=lambda: NOW, limits=limits
        )
        stats.append(result.buffer_stats)
    for stat in stats:
        assert stat.peak_documents <= 3
        assert stat.peak_source_utf8_bytes <= 12_000
        assert stat.peak_chunks <= 4
        assert stat.peak_chunk_text_utf8_bytes <= 12_000
        assert stat.retained_result_items == 0
    assert stats[1].peak_documents <= stats[0].peak_documents
    assert stats[1].peak_chunks <= stats[0].peak_chunks


def test_synthetic_medium_corpus_publishes_matching_identities_with_bounded_rss(tmp_path: Path):
    conn = _make_db(tmp_path / "medium.db", article_count=250, body_words=60)
    events: list[dict[str, object]] = []
    limits = PublicationLimits(
        max_documents=25,
        source_utf8_bytes=16_000,
        max_chunks=25,
        chunk_text_utf8_bytes=64_000,
    )
    result = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=limits,
        progress_callback=events.append,
        progress_interval=3600.0,
        monotonic=lambda: 1.0,
    )
    assert result.corpus.document_count == 251
    assert result.corpus.manifest_id == result.lexical.manifest_id
    assert result.corpus.chunk_count == result.lexical.row_count
    assert result.buffer_stats.peak_documents <= limits.max_documents
    assert result.buffer_stats.peak_chunks <= limits.max_chunks
    assert result.buffer_stats.peak_source_utf8_bytes <= limits.source_utf8_bytes
    assert result.buffer_stats.peak_chunk_text_utf8_bytes <= limits.chunk_text_utf8_bytes
    assert result.buffer_stats.retained_result_items == 0
    assert [event["documents"] for event in events] == [100, 200, 251]
    rss_samples = [int(event["rss_bytes"]) for event in events]
    assert max(rss_samples) - min(rss_samples) < 64 * 1024**2


@pytest.mark.parametrize(
    "phase",
    [
        "after_staging",
        "after_manifest",
        "after_reconciliation",
        "after_fts_batch",
        "before_cutover",
        "during_cutover",
    ],
)
def test_failure_before_cutover_preserves_selected_pair_and_resume_is_idempotent(tmp_path: Path, phase: str):
    conn = _make_db(tmp_path / f"failure-{phase}.db", article_count=4)
    source_before = _source_digest(conn)

    def fail(point: str) -> None:
        if point == phase:
            raise RuntimeError(f"injected:{phase}")

    with pytest.raises(RuntimeError, match=f"injected:{phase}"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            limits=PublicationLimits(max_documents=2, max_chunks=2),
            failure_injector=fail,
        )
    assert _selected_pair(conn) == (OLD_MANIFEST_ID, OLD_MANIFEST_ID)

    resumed = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=PublicationLimits(max_documents=5, max_chunks=5),
    )
    again = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=PublicationLimits(max_documents=1, max_chunks=1),
    )
    assert again.build_id == resumed.build_id
    assert again.corpus.manifest_id == resumed.corpus.manifest_id
    assert _selected_pair(conn) == (resumed.corpus.manifest_id,) * 2
    assert conn.execute(
        """SELECT COUNT(*) FROM corpus_build_chunks WHERE build_id=?""",
        (resumed.build_id,),
    ).fetchone()[0] == resumed.corpus.chunk_count
    assert _source_digest(conn) == source_before


def test_same_manifest_failure_after_first_fts_batch_preserves_selected_generation(
    tmp_path: Path,
):
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    expected_conn = _make_db(tmp_path / "same-id-expected.db", article_count=3)
    expected = build_streaming_corpus_and_lexical_index(
        expected_conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=PublicationLimits(max_chunks=1),
    )
    manifest_id = expected.corpus.manifest_id
    expected_conn.close()

    conn = _make_db(tmp_path / "same-id-failure.db", article_count=3)
    _seed_same_manifest_legacy_generation(conn, manifest_id)
    manifest_json_before = conn.execute(
        "SELECT manifest_json FROM corpus_manifest WHERE manifest_id=?",
        (manifest_id,),
    ).fetchone()[0]
    fts_before = _fts_contents(conn, manifest_id)
    fts_digest_before = _lexical_rows_digest(fts_before)

    with pytest.raises(RuntimeError, match="injected:first-fts-batch"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            limits=PublicationLimits(max_chunks=1),
            failure_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("injected:first-fts-batch"))
                if point == "after_fts_batch"
                else None
            ),
        )

    assert _selected_pair(conn) == (manifest_id, manifest_id)
    assert conn.execute(
        "SELECT manifest_json FROM corpus_manifest WHERE manifest_id=?",
        (manifest_id,),
    ).fetchone()[0] == manifest_json_before
    assert _fts_contents(conn, manifest_id) == fts_before
    assert _lexical_rows_digest(_fts_contents(conn, manifest_id)) == fts_digest_before
    found = retrieve_lexical(
        conn,
        "legacyterm",
        ticker="AAPL",
        cutoff=NOW,
        requested_manifest_id=manifest_id,
        top_k=4,
        candidate_depth=4,
    )
    assert found.mode_served == "fts5"
    assert len(found.results) == 4


def test_successful_same_manifest_migration_atomically_selects_new_generation(
    tmp_path: Path,
):
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    expected_conn = _make_db(tmp_path / "same-id-success-expected.db", article_count=3)
    expected = build_streaming_corpus_and_lexical_index(
        expected_conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=PublicationLimits(max_chunks=1),
    )
    manifest_id = expected.corpus.manifest_id
    expected_conn.close()

    conn = _make_db(tmp_path / "same-id-success.db", article_count=3)
    _seed_same_manifest_legacy_generation(conn, manifest_id)
    result = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=PublicationLimits(max_chunks=1),
    )

    assert result.corpus.manifest_id == manifest_id
    assert _selected_pair(conn) == (manifest_id, manifest_id)
    state = conn.execute(
        """SELECT lexical_generation_id, lexical_digest
           FROM lexical_index_state WHERE singleton_id=1"""
    ).fetchone()
    assert state == (result.build_id, result.lexical.digest)
    new = retrieve_lexical(
        conn,
        "word0",
        ticker="AAPL",
        cutoff=NOW,
        requested_manifest_id=manifest_id,
        top_k=4,
        candidate_depth=4,
    )
    old = retrieve_lexical(
        conn,
        "legacyterm",
        ticker="AAPL",
        cutoff=NOW,
        requested_manifest_id=manifest_id,
        top_k=4,
        candidate_depth=4,
    )
    assert new.mode_served == "fts5"
    assert new.results
    assert old.results == ()


def test_cutover_rejects_corrupt_persisted_fts_text_and_preserves_selection(
    tmp_path: Path,
):
    conn = _make_db(tmp_path / "persisted-fts-corruption.db", article_count=3)

    def corrupt(point: str) -> None:
        if point != "before_cutover":
            return
        build_id = conn.execute(
            "SELECT build_id FROM corpus_publication_builds"
        ).fetchone()[0]
        conn.execute(
            """UPDATE corpus_build_chunks_fts SET content_text='corrupted persisted text'
               WHERE rowid=(
                   SELECT MIN(rowid) FROM corpus_build_chunks_fts WHERE build_id=?
               )""",
            (build_id,),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="persisted lexical"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            limits=PublicationLimits(max_chunks=2),
            failure_injector=corrupt,
        )

    assert _selected_pair(conn) == (OLD_MANIFEST_ID, OLD_MANIFEST_ID)


def test_resume_regenerates_incomplete_document_after_committed_chunk_batch(tmp_path: Path):
    conn = _make_db(tmp_path / "incomplete.db", article_count=1, body_words=1400)
    calls = 0

    def fail(point: str) -> None:
        nonlocal calls
        if point == "after_staging_batch":
            calls += 1
            if calls == 1:
                raise RuntimeError("injected:partial-document")

    with pytest.raises(RuntimeError, match="partial-document"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            limits=PublicationLimits(max_chunks=1),
            failure_injector=fail,
        )
    incomplete = conn.execute(
        """SELECT build_id, document_id FROM corpus_build_documents
           WHERE status='in_progress'"""
    ).fetchone()
    assert incomplete is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_build_chunks WHERE build_id=? AND document_id=?",
        incomplete,
    ).fetchone()[0] == 1

    result = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=PublicationLimits(max_chunks=1),
    )
    duplicates = conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT chunk_id FROM corpus_build_chunks WHERE build_id=?
               GROUP BY chunk_id HAVING COUNT(*) > 1
           )""",
        (result.build_id,),
    ).fetchone()[0]
    assert duplicates == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_build_documents WHERE build_id=? AND status!='complete'",
        (result.build_id,),
    ).fetchone()[0] == 0


def test_final_cutover_revalidates_certified_source_identity(tmp_path: Path):
    conn = _make_db(tmp_path / "source-identity.db", article_count=2)

    def tamper(point: str) -> None:
        if point == "before_cutover":
            conn.execute(
                "UPDATE corpus_publication_builds SET certified_snapshot_identity=?",
                ("e" * 64,),
            )
            conn.commit()

    with pytest.raises(RuntimeError, match="source identity"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=tamper,
        )
    assert _selected_pair(conn) == (OLD_MANIFEST_ID, OLD_MANIFEST_ID)


def test_reconciled_chunk_generation_is_frozen_before_lexical_and_cutover(tmp_path: Path):
    conn = _make_db(tmp_path / "freeze.db", article_count=2)

    def fail(point: str) -> None:
        if point == "after_reconciliation":
            raise RuntimeError("stop-after-reconciliation")

    with pytest.raises(RuntimeError, match="stop-after-reconciliation"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=fail,
        )
    build_id = conn.execute(
        "SELECT build_id FROM corpus_publication_builds"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="corpus_build_chunks_frozen"):
        conn.execute(
            """UPDATE corpus_build_chunks SET content_text='tampered'
               WHERE build_id=? AND chunk_id=(
                   SELECT MIN(chunk_id) FROM corpus_build_chunks WHERE build_id=?
               )""",
            (build_id, build_id),
        )


def test_sql_reconciliation_uses_binary_minimum_profile_replacement(tmp_path: Path):
    conn = _make_db(tmp_path / "replacement.db", article_count=1, body_words=800)
    document_id = conn.execute(
        "SELECT document_id FROM filing_documents WHERE filing_id='filing:1'"
    ).fetchone()[0]
    conn.execute("DELETE FROM corpus_chunks_fts WHERE manifest_id=?", (OLD_MANIFEST_ID,))
    conn.execute(
        """UPDATE corpus_chunks
           SET chunk_id=?, document_id=?, chunk_profile_version='filing_v2',
               section_key='whole', ordinal='0001'
           WHERE manifest_id=?""",
        (f"{document_id}:filing_v2:whole:0001", document_id, OLD_MANIFEST_ID),
    )
    conn.commit()
    result = build_streaming_corpus_and_lexical_index(
        conn, certified_snapshot_identity=SNAPSHOT_ID, clock=lambda: NOW
    )
    delta = conn.execute(
        """SELECT reason, replacement_chunk_id FROM corpus_build_deltas
           WHERE build_id=? AND delta_kind='tombstone' AND document_id=?""",
        (result.build_id, document_id),
    ).fetchone()
    expected = conn.execute(
        """SELECT MIN(chunk_id COLLATE BINARY) FROM corpus_build_chunks
           WHERE build_id=? AND document_id=? AND chunk_profile_version='filing_v3'""",
        (result.build_id, document_id),
    ).fetchone()[0]
    assert delta == ("profile_version_replaced", expected)


def test_streaming_reconciliation_distinguishes_eligibility_loss_from_removal(
    tmp_path: Path,
):
    conn = _make_db(tmp_path / "eligibility-loss.db", article_count=2)
    first = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
    )
    target = conn.execute(
        """SELECT chunk_id, document_id FROM corpus_build_chunks
           WHERE build_id=? AND source_kind='article'
           ORDER BY chunk_id COLLATE BINARY LIMIT 1""",
        (first.build_id,),
    ).fetchone()
    conn.execute(
        "UPDATE articles SET is_rag_eligible=0 WHERE article_id=?",
        (target[1],),
    )
    conn.commit()

    second = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity="c" * 64,
        clock=lambda: NOW,
    )

    reason = conn.execute(
        """SELECT reason FROM corpus_build_deltas
           WHERE build_id=? AND delta_kind='tombstone' AND chunk_id=?""",
        (second.build_id, target[0]),
    ).fetchone()[0]
    assert reason == "eligibility_lost"


@pytest.mark.parametrize(
    ("transition", "expected_reason"),
    [
        ("document_removed", "document_removed"),
        ("eligibility_lost", "eligibility_lost"),
        ("disappeared_child", "disappeared_child"),
        ("dedup_cluster_reassigned", "dedup_cluster_reassigned"),
    ],
)
def test_streaming_reconciliation_reasons_match_legacy(
    tmp_path: Path, transition: str, expected_reason: str
):
    conn = _make_db(
        tmp_path / f"reconciliation-{transition}.db",
        article_count=1,
        body_words=1400,
    )
    first = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
    )
    article_chunks = list(
        conn.execute(
            """SELECT chunk_id, document_id FROM corpus_build_chunks
               WHERE build_id=? AND source_kind='article'
               ORDER BY chunk_id COLLATE BINARY""",
            (first.build_id,),
        )
    )
    assert len(article_chunks) > 1
    target_chunk, document_id = (
        article_chunks[-1]
        if transition == "disappeared_child"
        else article_chunks[0]
    )
    if transition == "document_removed":
        conn.execute("DELETE FROM article_tickers WHERE article_id=?", (document_id,))
        conn.execute("DELETE FROM articles WHERE article_id=?", (document_id,))
    elif transition == "eligibility_lost":
        conn.execute(
            "UPDATE articles SET is_rag_eligible=0 WHERE article_id=?",
            (document_id,),
        )
    elif transition == "disappeared_child":
        conn.execute(
            "UPDATE articles SET description='short body' WHERE article_id=?",
            (document_id,),
        )
    else:
        conn.execute(
            "UPDATE articles SET dedup_cluster_id='replacement-cluster' WHERE article_id=?",
            (document_id,),
        )
    conn.commit()

    second = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity="d" * 64,
        clock=lambda: NOW,
    )
    reason = conn.execute(
        """SELECT reason FROM corpus_build_deltas
           WHERE build_id=? AND delta_kind='tombstone' AND chunk_id=?""",
        (second.build_id, target_chunk),
    ).fetchone()
    assert reason == (expected_reason,)


def test_fts_digest_parity_and_transactions_are_at_most_500_rows(tmp_path: Path):
    conn = _make_db(tmp_path / "fts.db", article_count=19, body_words=500)
    result = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=PublicationLimits(max_documents=7, max_chunks=9),
    )
    batches = list(
        conn.execute(
            """SELECT row_count, text_utf8_bytes
               FROM corpus_build_fts_batches WHERE build_id=? ORDER BY batch_no""",
            (result.build_id,),
        )
    )
    assert batches and max(row[0] for row in batches) <= 500
    assert max(row[1] for row in batches) <= 16 * 1024**2
    assert sum(row[0] for row in batches) == result.lexical.row_count
    assert result.lexical.row_count == result.corpus.chunk_count
    assert result.lexical.digest == conn.execute(
        "SELECT lexical_digest FROM corpus_publication_builds WHERE build_id=?",
        (result.build_id,),
    ).fetchone()[0]


def test_fts_resume_keeps_valid_committed_batches(tmp_path: Path):
    conn = _make_db(tmp_path / "fts-resume-valid.db", article_count=4, body_words=900)
    calls = 0

    def stop_after_first(point: str) -> None:
        nonlocal calls
        if point == "after_fts_batch":
            calls += 1
            if calls == 1:
                raise RuntimeError("injected:first-committed-fts-batch")

    with pytest.raises(RuntimeError, match="first-committed-fts-batch"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            limits=PublicationLimits(max_chunks=2),
            failure_injector=stop_after_first,
        )
    build_id = conn.execute(
        "SELECT build_id FROM corpus_publication_builds"
    ).fetchone()[0]
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_build_fts_batches WHERE build_id=?",
        (build_id,),
    ).fetchone()[0] == 1
    conn.execute(
        f"""CREATE TRIGGER protect_valid_fts_batch
            BEFORE DELETE ON corpus_build_fts_batches
            WHEN OLD.build_id='{build_id}' AND OLD.batch_no=1
            BEGIN SELECT RAISE(ABORT, 'valid_fts_batch_deleted'); END"""
    )
    conn.commit()

    resumed = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=PublicationLimits(max_chunks=2),
    )

    assert resumed.build_id == build_id
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_build_fts_batches WHERE build_id=? AND batch_no=1",
        (build_id,),
    ).fetchone()[0] == 1


def test_fts_resume_repairs_corrupt_batch_suffix_without_touching_selection(
    tmp_path: Path,
):
    conn = _make_db(tmp_path / "fts-resume-repair.db", article_count=4, body_words=900)
    selected_before = _selected_pair(conn)
    selected_fts_before = _fts_contents(conn, OLD_MANIFEST_ID)
    calls = 0

    def stop_after_third(point: str) -> None:
        nonlocal calls
        if point == "after_fts_batch":
            calls += 1
            if calls == 3:
                raise RuntimeError("injected:three-fts-batches")

    with pytest.raises(RuntimeError, match="three-fts-batches"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            limits=PublicationLimits(max_chunks=2),
            failure_injector=stop_after_third,
        )
    build_id = conn.execute(
        "SELECT build_id FROM corpus_publication_builds"
    ).fetchone()[0]
    batch_one_before = conn.execute(
        """SELECT * FROM corpus_build_fts_batches
           WHERE build_id=? AND batch_no=1""",
        (build_id,),
    ).fetchone()
    batch_one_fts_before = tuple(
        conn.execute(
            """SELECT rowid, chunk_id, content_text FROM corpus_build_chunks_fts
               WHERE build_id=? AND chunk_id<=? COLLATE BINARY
               ORDER BY chunk_id COLLATE BINARY""",
            (build_id, batch_one_before[7]),
        )
    )
    conn.execute(
        """UPDATE corpus_build_fts_batches SET checkpoint_chunk_id='corrupt-checkpoint'
           WHERE build_id=? AND batch_no=2""",
        (build_id,),
    )
    conn.commit()

    resumed = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=PublicationLimits(max_chunks=2),
    )

    assert resumed.build_id == build_id
    assert batch_one_before == conn.execute(
        """SELECT * FROM corpus_build_fts_batches
           WHERE build_id=? AND batch_no=1""",
        (build_id,),
    ).fetchone()
    assert batch_one_fts_before == tuple(
        conn.execute(
            """SELECT rowid, chunk_id, content_text FROM corpus_build_chunks_fts
               WHERE build_id=? AND chunk_id<=? COLLATE BINARY
               ORDER BY chunk_id COLLATE BINARY""",
            (build_id, batch_one_before[7]),
        )
    )
    assert _selected_pair(conn) == (resumed.corpus.manifest_id,) * 2
    assert selected_before == (OLD_MANIFEST_ID, OLD_MANIFEST_ID)
    assert _fts_contents(conn, OLD_MANIFEST_ID) == selected_fts_before


def test_corrupt_fts_suffix_cleanup_is_bounded_persisted_and_resumable(
    tmp_path: Path,
):
    from catalyst_data.corpus.streaming_publication import (
        _discard_invalid_fts_suffix,
        ensure_streaming_publication_schema,
    )

    conn = _make_db(tmp_path / "bounded-fts-cleanup.db", article_count=0)
    ensure_streaming_publication_schema(conn)
    build_id = "e" * 64
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status,
            created_at, updated_at)
           VALUES (?, ?, '{}', 'reconciliation_ready', ?, ?)""",
        (build_id, SNAPSHOT_ID, NOW, NOW),
    )
    rows = [
        (build_id, f"repair:{index:04d}", f"text {index}")
        for index in range(2000)
    ]
    conn.executemany(
        """INSERT INTO corpus_build_chunks_fts
           (build_id, chunk_id, content_text) VALUES (?, ?, ?)""",
        rows,
    )
    conn.execute(
        """INSERT INTO corpus_build_fts_batches
           (build_id, batch_no, row_count, text_utf8_bytes, first_chunk_id,
            last_chunk_id, batch_digest, checkpoint_chunk_id, committed_at)
           VALUES (?, 1, 500, 0, 'repair:0000', 'repair:0499', ?,
                   'repair:0499', ?)""",
        (build_id, "d" * 64, NOW),
    )
    conn.commit()
    selected_before = _selected_pair(conn)
    selected_fts_before = _fts_contents(conn, OLD_MANIFEST_ID)
    remaining = 1500
    deleted_per_transaction: list[int] = []

    def interrupt_after_two(point: str) -> None:
        nonlocal remaining
        if point != "after_fts_cleanup_batch":
            return
        current = conn.execute(
            """SELECT COUNT(*) FROM corpus_build_chunks_fts
               WHERE build_id=? AND chunk_id>'repair:0499' COLLATE BINARY""",
            (build_id,),
        ).fetchone()[0]
        deleted_per_transaction.append(remaining - current)
        remaining = current
        if len(deleted_per_transaction) == 2:
            raise RuntimeError("injected:cleanup-interruption")

    with pytest.raises(RuntimeError, match="cleanup-interruption"):
        _discard_invalid_fts_suffix(
            conn,
            build_id=build_id,
            valid_batches=1,
            checkpoint="repair:0499",
            failure_injector=interrupt_after_two,
        )

    assert deleted_per_transaction == [500, 500]
    repair_state = conn.execute(
        """SELECT lexical_repair_checkpoint, lexical_repair_cursor
           FROM corpus_publication_builds WHERE build_id=?""",
        (build_id,),
    ).fetchone()
    assert repair_state == ("repair:0499", "repair:1000")
    assert _selected_pair(conn) == selected_before
    assert _fts_contents(conn, OLD_MANIFEST_ID) == selected_fts_before

    _discard_invalid_fts_suffix(
        conn,
        build_id=build_id,
        valid_batches=1,
        checkpoint="repair:0499",
        failure_injector=None,
    )

    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_build_chunks_fts WHERE build_id=?",
        (build_id,),
    ).fetchone()[0] == 500
    assert conn.execute(
        """SELECT lexical_repair_checkpoint, lexical_repair_cursor
           FROM corpus_publication_builds WHERE build_id=?""",
        (build_id,),
    ).fetchone() == (None, None)
    assert max(deleted_per_transaction) <= 500
    assert _selected_pair(conn) == selected_before
    assert _fts_contents(conn, OLD_MANIFEST_ID) == selected_fts_before


def test_fts_checkpoint_validation_reads_large_ranges_in_pages_of_at_most_500(
    tmp_path: Path,
):
    conn = sqlite3.connect(tmp_path / "bounded-checkpoint-validation.db")
    build_id = "f" * 64
    init_db(conn)
    _seed_large_fts_checkpoint(conn, build_id)

    bounded_conn = _BoundedReadConnection(conn)
    valid_batches, checkpoint = _validated_fts_checkpoint(bounded_conn, build_id)

    assert valid_batches == 3
    assert checkpoint == "synthetic:1199"
    assert bounded_conn.read_sizes
    assert max(bounded_conn.read_sizes) <= 500


@pytest.mark.parametrize(
    ("body_words", "max_chunks"),
    [(80, 500), (1400, 1)],
)
def test_after_staging_batch_fires_after_final_batch_and_resume_is_idempotent(
    tmp_path: Path, body_words: int, max_chunks: int
):
    conn = _make_db(
        tmp_path / f"final-staging-hook-{body_words}.db",
        article_count=1,
        body_words=body_words,
    )
    conn.execute("DELETE FROM filing_documents")
    conn.execute("DELETE FROM filings")
    conn.commit()
    callback_states: list[str] = []

    def stop_after_final_batch(point: str) -> None:
        if point != "after_staging_batch":
            return
        state = conn.execute(
            """SELECT status FROM corpus_build_documents
               WHERE source_kind='article'"""
        ).fetchone()[0]
        callback_states.append(state)
        if state == "complete":
            raise RuntimeError("injected:final-staging-batch")

    with pytest.raises(RuntimeError, match="final-staging-batch"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            limits=PublicationLimits(max_chunks=max_chunks),
            failure_injector=stop_after_final_batch,
        )

    build_id = conn.execute(
        "SELECT build_id FROM corpus_publication_builds"
    ).fetchone()[0]
    article_chunks_before = tuple(
        conn.execute(
            """SELECT chunk_id, content_hash FROM corpus_build_chunks
               WHERE build_id=? AND source_kind='article'
               ORDER BY chunk_id COLLATE BINARY""",
            (build_id,),
        )
    )
    assert callback_states[-1] == "complete"
    assert conn.execute(
        """SELECT status FROM corpus_build_documents
           WHERE build_id=? AND source_kind='article'""",
        (build_id,),
    ).fetchone()[0] == "complete"
    if max_chunks == 1:
        assert "in_progress" in callback_states

    conn.execute(
        f"""CREATE TRIGGER protect_completed_article_chunks
            BEFORE DELETE ON corpus_build_chunks
            WHEN OLD.build_id='{build_id}' AND OLD.source_kind='article'
            BEGIN SELECT RAISE(ABORT, 'completed_article_was_restaged'); END"""
    )
    conn.commit()

    result = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
        limits=PublicationLimits(max_chunks=max_chunks),
    )

    assert result.build_id == build_id
    assert article_chunks_before == tuple(
        conn.execute(
            """SELECT chunk_id, content_hash FROM corpus_build_chunks
               WHERE build_id=? AND source_kind='article'
               ORDER BY chunk_id COLLATE BINARY""",
            (build_id,),
        )
    )
    assert conn.execute(
        """SELECT COUNT(*) FROM corpus_build_chunks
           WHERE build_id=? GROUP BY chunk_id HAVING COUNT(*) > 1""",
        (build_id,),
    ).fetchone() is None


def test_lexical_retrieval_serves_only_the_selected_streaming_generation(tmp_path: Path):
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    conn = _make_db(tmp_path / "served.db", article_count=3)
    result = build_streaming_corpus_and_lexical_index(
        conn, certified_snapshot_identity=SNAPSHOT_ID, clock=lambda: NOW
    )
    found = retrieve_lexical(
        conn,
        "word0",
        ticker="AAPL",
        cutoff=NOW,
        requested_manifest_id=result.corpus.manifest_id,
        top_k=3,
        candidate_depth=3,
    )
    assert found.results
    assert all(item.corpus_manifest_id == result.corpus.manifest_id for item in found.results)
    old = retrieve_lexical(
        conn,
        "old",
        ticker="AAPL",
        cutoff=NOW,
        requested_manifest_id=OLD_MANIFEST_ID,
        top_k=3,
        candidate_depth=3,
    )
    assert old.results == ()


def test_estimator_uses_exact_eligibility_utf8_units_and_phase_max(tmp_path: Path):
    conn = _make_db(tmp_path / "estimate.db", article_count=1, body_words=1)
    conn.execute(
        "UPDATE articles SET title='\N{GRINNING FACE}', description=NULL WHERE article_id='poly:000000'"
    )
    ineligible_raw = "raw:ineligible"
    conn.execute(
        """INSERT INTO raw_assets
           (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES (?, 'AAPL', 'polygon_news', '2026-07-31', ?, ?)""",
        (ineligible_raw, NOW, b"{}"),
    )
    upsert_article(
        conn,
        article={
            "article_id": "poly:ineligible",
            "raw_asset_id": ineligible_raw,
            "ticker": "AAPL",
            "reference_date": "2026-07-31",
            "published_utc": NOW,
            "title": "must-not-count",
            "description": "must-not-count",
            "is_canonical": 0,
            "is_rag_eligible": 0,
        },
    )
    conn.execute("UPDATE filing_documents SET extraction_status='empty'")
    conn.commit()

    limits = PublicationLimits(source_utf8_bytes=10_000, chunk_text_utf8_bytes=20_000)
    estimate = estimate_streaming_publication_resources(conn, limits=limits)
    assert estimate.eligible_document_count == 1
    assert estimate.source_utf8_bytes == len("\N{GRINNING FACE}".encode("utf-8"))
    assert estimate.largest_source_document_utf8_bytes == len("\N{GRINNING FACE}".encode("utf-8"))
    assert estimate.required_headroom == max(estimate.phase_headroom_bytes.values())


def test_oversized_document_is_typed_resumable_stop(tmp_path: Path):
    conn = _make_db(tmp_path / "oversized.db", article_count=1, body_words=100)
    with pytest.raises(ResumableResourceStop) as exc_info:
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            limits=PublicationLimits(source_utf8_bytes=64, chunk_text_utf8_bytes=1_000),
        )
    assert exc_info.value.code == "source_document_too_large"
    assert exc_info.value.resumable is True
    assert _selected_pair(conn) == (OLD_MANIFEST_ID, OLD_MANIFEST_ID)


def test_publish_path_has_no_fetchall_or_corpus_sized_result_containers():
    module_path = Path(__file__).parents[1] / "catalyst_data/corpus/streaming_publication.py"
    tree = ast.parse(module_path.read_text())
    forbidden_names = {"produced_chunks", "active_chunks", "inventory", "reconciliation"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "fetchall"
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in forbidden_names:
                    assert not isinstance(node.value, (ast.List, ast.Dict, ast.ListComp, ast.DictComp))


def test_production_corpus_consumers_do_not_hardcode_legacy_chunk_table():
    package = Path(__file__).parents[1] / "catalyst_data"
    for relative in (
        "pre_b6_probes.py",
        "sec/readiness.py",
        "retrieval/source_bundle.py",
        "retrieval/embedder.py",
    ):
        source = (package / relative).read_text()
        assert "served_chunks_relation" in source
        assert "FROM corpus_chunks\n" not in source


def test_publication_rss_readers_reject_high_water_ru_maxrss():
    package = Path(__file__).parents[1] / "catalyst_data"
    for relative in (
        "index_builder.py",
        "manifests/operations.py",
        "corpus/streaming_publication.py",
    ):
        assert "ru_maxrss" not in (package / relative).read_text()


def test_limits_reject_contract_violations():
    base = PublicationLimits()
    with pytest.raises(ValueError, match="max_documents"):
        replace(base, max_documents=101)
    with pytest.raises(ValueError, match="max_chunks"):
        replace(base, max_chunks=501)


def test_cli_payload_returns_ids_counts_and_page_access_without_chunk_collection():
    from catalyst_data.b2o import _corpus_publication_payload

    result = type("Result", (), {
        "build_id": "c" * 64,
        "corpus": type("Corpus", (), {
            "manifest_id": "d" * 64,
            "document_count": 12,
            "chunk_count": 34,
        })(),
        "lexical": type("Lexical", (), {
            "manifest_id": "d" * 64,
            "row_count": 34,
        })(),
    })()
    payload = _corpus_publication_payload(result)
    assert payload == {
        "build_id": "c" * 64,
        "corpus_manifest_id": "d" * 64,
        "lexical_manifest_id": "d" * 64,
        "document_count": 12,
        "chunk_count": 34,
        "lexical_row_count": 34,
        "chunk_access": "iter_chunk_pages",
    }


def test_reconciliation_checkpoint_columns_added_by_schema_ensure(tmp_path):
    conn = _make_db(tmp_path / "schema-ensure.db")
    user_version_before = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version_before == CURRENT_SCHEMA_VERSION
    ensure_streaming_publication_schema(conn)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(corpus_publication_builds)")
    }
    for name in (
        "reconciliation_checkpoint",
        "reconciliation_base_manifest_id",
        "reconciliation_base_chunk_count",
        "reconciliation_base_inventory_digest",
    ):
        assert name in columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == user_version_before
    # Idempotent: a second ensure must not duplicate columns.
    ensure_streaming_publication_schema(conn)
    columns_again = {
        row[1]
        for row in conn.execute("PRAGMA table_info(corpus_publication_builds)")
    }
    assert columns_again == columns


def _stop_after_reconciliation(tmp_path: Path, name: str):
    """Run a build stopped after reconciliation (status=reconciliation_ready)."""
    conn = _make_db(tmp_path / name)

    def fail(point: str) -> None:
        if point == "after_reconciliation":
            raise RuntimeError("stop-after-reconciliation")

    with pytest.raises(RuntimeError, match="stop-after-reconciliation"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=fail,
        )
    return conn


def test_reconciliation_ready_null_checkpoint_validates_and_backfills(tmp_path):
    from catalyst_data.corpus.streaming_publication import resume_candidate_reconciliation

    conn = _stop_after_reconciliation(tmp_path, "old-ready.db")
    build_id = conn.execute("SELECT build_id FROM corpus_publication_builds").fetchone()[0]
    conn.execute(
        "UPDATE corpus_publication_builds SET reconciliation_checkpoint=NULL "
        "WHERE build_id=?",
        (build_id,),
    )
    conn.commit()
    deltas_before = conn.execute(
        "SELECT COUNT(*) FROM corpus_build_deltas WHERE build_id=?", (build_id,)
    ).fetchone()[0]
    assert deltas_before > 0

    summary = resume_candidate_reconciliation(conn, build_id=build_id)
    assert summary.to_embed_count >= 0

    row = conn.execute(
        "SELECT status, reconciliation_checkpoint, reconciliation_ready "
        "FROM corpus_publication_builds WHERE build_id=?",
        (build_id,),
    ).fetchone()
    assert row == ("reconciliation_ready", "reconciliation_ready", 1)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM corpus_build_deltas WHERE build_id=?", (build_id,)
        ).fetchone()[0]
        == deltas_before
    )

    # Validation failure must NOT backfill the checkpoint.
    conn.execute(
        "UPDATE corpus_publication_builds SET reconciliation_checkpoint=NULL "
        "WHERE build_id=?",
        (build_id,),
    )
    conn.execute(
        """INSERT INTO corpus_build_deltas
           (build_id, delta_kind, chunk_id, document_id)
           VALUES (?, 'to_embed', 'missing:news_v2:body:0001', 'missing-doc')""",
        (build_id,),
    )
    conn.commit()
    with pytest.raises(ValueError, match="finalize validation failed"):
        resume_candidate_reconciliation(conn, build_id=build_id)
    checkpoint = conn.execute(
        "SELECT reconciliation_checkpoint FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()[0]
    assert checkpoint is None


def test_reconciliation_manifest_ready_with_ready_checkpoint_is_inconsistent(tmp_path):
    from catalyst_data.corpus.streaming_publication import (
        reconciliation_resume_state,
        resume_candidate_reconciliation,
    )

    conn = _stop_after_reconciliation(tmp_path, "inconsistent-status.db")
    build_id = conn.execute("SELECT build_id FROM corpus_publication_builds").fetchone()[0]
    conn.execute(
        "UPDATE corpus_publication_builds SET status='manifest_ready' WHERE build_id=?",
        (build_id,),
    )
    conn.commit()
    with pytest.raises(ValueError, match="inconsistent reconciliation state"):
        reconciliation_resume_state(conn, build_id=build_id)
    with pytest.raises(ValueError, match="inconsistent reconciliation state"):
        resume_candidate_reconciliation(conn, build_id=build_id)


def test_reconciliation_ready_with_partial_checkpoint_is_inconsistent(tmp_path):
    from catalyst_data.corpus.streaming_publication import (
        reconciliation_resume_state,
        resume_candidate_reconciliation,
    )

    conn = _make_db(tmp_path / "partial-ready.db")

    def fail(point: str) -> None:
        if point == "after_recon_to_embed_done":
            raise RuntimeError("stop-after-to-embed")

    with pytest.raises(RuntimeError, match="stop-after-to-embed"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=fail,
        )
    build_id = conn.execute("SELECT build_id FROM corpus_publication_builds").fetchone()[0]
    checkpoint = conn.execute(
        "SELECT reconciliation_checkpoint FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()[0]
    assert checkpoint == "to_embed_done"
    conn.execute(
        "UPDATE corpus_publication_builds SET status='reconciliation_ready' "
        "WHERE build_id=?",
        (build_id,),
    )
    conn.commit()
    with pytest.raises(ValueError, match="inconsistent reconciliation state"):
        reconciliation_resume_state(conn, build_id=build_id)
    with pytest.raises(ValueError, match="inconsistent reconciliation state"):
        resume_candidate_reconciliation(conn, build_id=build_id)


def test_publication_entrypoint_does_not_destage_reconciliation_ready(tmp_path):
    conn = _make_db(tmp_path / "no-destage.db")

    def fail(point: str) -> None:
        if point == "after_reconciliation":
            raise RuntimeError("stop-after-reconciliation")

    with pytest.raises(RuntimeError, match="stop-after-reconciliation"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=fail,
        )
    build_id = conn.execute("SELECT build_id FROM corpus_publication_builds").fetchone()[0]
    state = conn.execute(
        "SELECT status, reconciliation_checkpoint FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()
    assert state == ("reconciliation_ready", "reconciliation_ready")

    # Chunks are frozen as soon as the build is reconciliation_ready.
    with pytest.raises(sqlite3.IntegrityError, match="corpus_build_chunks_frozen"):
        conn.execute(
            "UPDATE corpus_build_chunks SET content_text='tampered' "
            "WHERE build_id=? AND chunk_id=(SELECT MIN(chunk_id) FROM "
            "corpus_build_chunks WHERE build_id=?)",
            (build_id, build_id),
        )

    # Any attempt to destage a reconciliation_ready build must abort.
    conn.execute(
        """CREATE TRIGGER forbid_destage_reconciliation_ready
           BEFORE UPDATE ON corpus_publication_builds
           WHEN OLD.status IN ('reconciliation_ready','lexical_ready','published')
                AND NEW.status='staging_ready'
           BEGIN SELECT RAISE(ABORT, 'destaged_reconciliation_ready'); END"""
    )
    conn.commit()

    result = build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=SNAPSHOT_ID,
        clock=lambda: NOW,
    )
    assert result.build_id == build_id
    assert (
        conn.execute(
            "SELECT status FROM corpus_publication_builds WHERE build_id=?", (build_id,)
        ).fetchone()[0]
        == "published"
    )
    # Resume must never have written staging_ready after reconciliation_ready.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name='forbid_destage_reconciliation_ready'"
        ).fetchone()[0]
        == 1
    )


def test_reconciliation_dispatch_to_embed_done_does_not_rerun_to_embed(tmp_path):
    from catalyst_data.corpus.streaming_publication import resume_candidate_reconciliation

    conn = _make_db(tmp_path / "dispatch.db")

    def fail(point: str) -> None:
        if point == "after_recon_to_embed_done":
            raise RuntimeError("stop-after-to-embed")

    with pytest.raises(RuntimeError, match="stop-after-to-embed"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=fail,
        )
    build_id = conn.execute("SELECT build_id FROM corpus_publication_builds").fetchone()[0]
    checkpoint = conn.execute(
        "SELECT reconciliation_checkpoint FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()[0]
    assert checkpoint == "to_embed_done"
    to_embed_before = conn.execute(
        "SELECT COUNT(*) FROM corpus_build_deltas WHERE build_id=? AND delta_kind='to_embed'",
        (build_id,),
    ).fetchone()[0]
    assert to_embed_before > 0

    summary = resume_candidate_reconciliation(conn, build_id=build_id)
    to_embed_after = conn.execute(
        "SELECT COUNT(*) FROM corpus_build_deltas WHERE build_id=? AND delta_kind='to_embed'",
        (build_id,),
    ).fetchone()[0]
    assert to_embed_after == to_embed_before
    assert summary.tombstone_count >= 0
    assert (
        conn.execute(
            "SELECT status, reconciliation_checkpoint FROM corpus_publication_builds "
            "WHERE build_id=?",
            (build_id,),
        ).fetchone()
        == ("reconciliation_ready", "reconciliation_ready")
    )


def test_reconciliation_delta_sql_uses_temp_indexes_no_served_view(tmp_path):
    from catalyst_data.corpus import streaming_publication as sp

    for sql in (
        sp._RECON_TO_EMBED_SQL,
        sp._RECON_CLUSTER_TOMBSTONE_SQL,
        sp._RECON_METADATA_UPDATE_SQL,
    ):
        assert "temp_previous_served" in sql
        assert "corpus_served_chunks" not in sql
    removed = sp._RECON_REMOVED_TOMBSTONE_SQL
    assert "temp_tombstone_survivors" in removed
    assert "temp_previous_served" not in removed
    assert "temp_profile_replacements" in removed
    assert "GROUP BY" not in removed
    assert "EXISTS (" not in removed
    assert "MIN(replacement.chunk_id" not in removed
    replacements = sp._RECON_PROFILE_REPLACEMENTS_SQL
    assert "GROUP BY" in replacements
    assert "temp_new_doc_profiles" in replacements
    assert "temp_tombstone_survivors" in replacements

    conn = _make_db(tmp_path / "explain.db")
    ensure_streaming_publication_schema(conn)
    build_id = "b" * 64
    plans = sp.explain_reconciliation_dml(conn, build_id=build_id)
    removed_plan = " ".join(str(row) for row in plans["removed_tombstones"])
    assert "idx_temp_profile_replacements_lookup" in removed_plan
    assert "SCAN p" in removed_plan  # survivor TEMP is the bounded scan source
    assert "corpus_served_chunks" not in removed_plan
    assert "SCALAR SUBQUERY" not in removed_plan
    for phase in ("to_embed", "cluster_tombstones", "metadata_updates"):
        phase_plan = " ".join(str(row) for row in plans[phase])
        assert "idx_temp_previous_served_chunk" in phase_plan
        assert "corpus_served_chunks" not in phase_plan


def test_reconciliation_deadline_stops_phase_and_rolls_back(tmp_path):
    from catalyst_data.corpus.streaming_publication import (
        ReconciliationDeadlineStop,
        ResumableResourceStop,
        resume_candidate_reconciliation,
    )

    conn = _make_db(
        tmp_path / "deadline.db", article_count=250, body_words=60
    )
    with pytest.raises(RuntimeError, match="stop-after-manifest"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("stop-after-manifest"))
                if point == "after_manifest"
                else None
            ),
        )
    build_id = conn.execute("SELECT build_id FROM corpus_publication_builds").fetchone()[0]
    checkpoint_before = conn.execute(
        "SELECT reconciliation_checkpoint FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()[0]
    assert checkpoint_before is None

    with pytest.raises(ReconciliationDeadlineStop):
        resume_candidate_reconciliation(conn, build_id=build_id, deadline=0.0)
    checkpoint_after = conn.execute(
        "SELECT reconciliation_checkpoint FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()[0]
    assert checkpoint_after is None
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM corpus_build_deltas WHERE build_id=?", (build_id,)
        ).fetchone()[0]
        == 0
    )
    # No open transaction and the progress handler was cleared.
    assert conn.in_transaction is False
    assert conn.execute("SELECT 1").fetchone() == (1,)

    # Operator interrupt uses the typed ResumableResourceStop path.
    with pytest.raises(ResumableResourceStop, match="operator_interrupt"):
        resume_candidate_reconciliation(
            conn,
            build_id=build_id,
            deadline=900.0,
            operator_interrupt={"operator_interrupt": True},
        )
    assert conn.in_transaction is False
    assert conn.execute("SELECT 1").fetchone() == (1,)

    # A normal resume completes the remaining phases.
    summary = resume_candidate_reconciliation(conn, build_id=build_id)
    assert summary.to_embed_count >= 0
    assert (
        conn.execute(
            "SELECT status, reconciliation_checkpoint FROM corpus_publication_builds "
            "WHERE build_id=?",
            (build_id,),
        ).fetchone()
        == ("reconciliation_ready", "reconciliation_ready")
    )


def test_reconciliation_resume_rejects_changed_base_digest(tmp_path):
    from catalyst_data.corpus.streaming_publication import resume_candidate_reconciliation

    conn = _make_db(tmp_path / "changed-base.db")

    def fail(point: str) -> None:
        if point == "after_recon_initialized":
            raise RuntimeError("stop-after-initialized")

    with pytest.raises(RuntimeError, match="stop-after-initialized"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=fail,
        )
    build_id = conn.execute("SELECT build_id FROM corpus_publication_builds").fetchone()[0]
    bound = conn.execute(
        """SELECT reconciliation_base_manifest_id, reconciliation_base_chunk_count,
                  reconciliation_base_inventory_digest
           FROM corpus_publication_builds WHERE build_id=?""",
        (build_id,),
    ).fetchone()
    assert bound[0] == OLD_MANIFEST_ID
    assert int(bound[1]) >= 1
    assert bound[2]

    # Change a previous-served content_hash without changing the row count.
    conn.execute(
        """UPDATE corpus_chunks
           SET content_hash=?
           WHERE manifest_id=? AND chunk_id=(SELECT MIN(chunk_id) FROM corpus_chunks)""",
        (hashlib.sha256(b"changed-base").hexdigest(), OLD_MANIFEST_ID),
    )
    conn.commit()
    with pytest.raises(ValueError, match="base inventory digest changed"):
        resume_candidate_reconciliation(conn, build_id=build_id)

    # Restore the digest; a moved current manifest must also fail closed.
    conn.execute(
        "UPDATE corpus_chunks SET content_hash=? WHERE manifest_id=?",
        (
            hashlib.sha256(b"old text").hexdigest(),
            OLD_MANIFEST_ID,
        ),
    )
    conn.execute("UPDATE corpus_manifest SET is_current=0 WHERE manifest_id=?", (OLD_MANIFEST_ID,))
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 1, ?)""",
        ("c" * 64, NOW),
    )
    conn.commit()
    with pytest.raises(ValueError, match="base manifest changed"):
        resume_candidate_reconciliation(conn, build_id=build_id)


def _h64(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_legacy_matrix(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM corpus_chunks WHERE manifest_id=?", (OLD_MANIFEST_ID,))
    rows = (
        # chunk_id, document_id, content_hash, metadata_hash, dedup_cluster_id
        ("u:news_v2:body:0001", "doc-u", _h64("c1"), _h64("m1"), "d1"),
        ("c:news_v2:body:0001", "doc-c", _h64("old"), _h64("m1"), "d1"),
        ("m:news_v2:body:0001", "doc-m", _h64("c1"), _h64("old-meta"), "d1"),
        ("d:news_v2:body:0001", "doc-d", _h64("c1"), _h64("m1"), "d-old"),
        ("r:news_v2:body:0001", "doc-r", _h64("c1"), _h64("m1"), "d1"),
        ("nullsame:news_v2:body:0001", "doc-null-same", _h64("c1"), _h64("m1"), None),
        ("nullto:news_v2:body:0001", "doc-null-to", _h64("c1"), _h64("m1"), None),
    )
    for chunk_id, document_id, content_hash, metadata_hash, dedup_cluster_id in rows:
        conn.execute(
            """INSERT INTO corpus_chunks
               (chunk_id, document_id, chunk_profile_version, section_key, ordinal,
                content_text, content_hash, metadata_hash, source_class,
                dedup_cluster_id, available_at, ticker_associations, eligibility,
                manifest_id, status, boundary_kind, body_token_start,
                body_token_end, body_overlap_tokens, prefix_token_count,
                prefix_truncated, section_parse_degraded, created_at, updated_at)
               VALUES (?, ?, 'news_v2', 'body', '0001', 'text', ?, ?,
                       'reported_news', ?, ?, '["AAPL"]', 'eligible', ?,
                       'active', 'document_end', 0, 1, 0, 0, 0, 0, ?, ?)""",
            (
                chunk_id,
                document_id,
                content_hash,
                metadata_hash,
                dedup_cluster_id,
                NOW,
                OLD_MANIFEST_ID,
                NOW,
                NOW,
            ),
        )
    conn.commit()


def _seed_matrix_build(conn: sqlite3.Connection, build_id: str) -> None:
    ensure_streaming_publication_schema(conn)
    conn.execute(
        """INSERT INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 0, ?)""",
        ("1" * 64, NOW),
    )
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status,
            manifest_id, document_count, chunk_count, inventory_digest,
            reconciliation_ready, created_at, updated_at)
           VALUES (?, ?, '{}', 'manifest_ready', ?, 7, 7, ?, 0, ?, ?)""",
        (build_id, SNAPSHOT_ID, "1" * 64, "9" * 64, NOW, NOW),
    )
    rows = (
        # chunk_id, document_id, content_hash, metadata_hash, dedup_cluster_id
        ("u:news_v2:body:0001", "doc-u", _h64("c1"), _h64("m1"), "d1"),
        ("c:news_v2:body:0001", "doc-c", _h64("new"), _h64("m1"), "d1"),
        ("m:news_v2:body:0001", "doc-m", _h64("c1"), _h64("new-meta"), "d1"),
        ("d:news_v2:body:0001", "doc-d", _h64("c1"), _h64("m1"), "d-new"),
        ("n:news_v2:body:0001", "doc-n", _h64("c1"), _h64("m1"), "d1"),
        ("nullsame:news_v2:body:0001", "doc-null-same", _h64("c1"), _h64("m1"), None),
        ("nullto:news_v2:body:0001", "doc-null-to", _h64("c1"), _h64("m1"), "X"),
    )
    for chunk_id, document_id, content_hash, metadata_hash, dedup_cluster_id in rows:
        conn.execute(
            """INSERT INTO corpus_build_chunks
               (build_id, chunk_id, document_id, chunk_profile_version,
                section_key, ordinal, content_text, content_hash, metadata_hash,
                source_class, dedup_cluster_id, available_at,
                ticker_associations, eligibility, status, boundary_kind,
                body_token_start, body_token_end, body_overlap_tokens,
                prefix_token_count, prefix_truncated, section_parse_degraded,
                source_kind, created_at, updated_at)
               VALUES (?, ?, ?, 'news_v2', 'body', '0001', 'text', ?, ?,
                       'reported_news', ?, ?, '["AAPL"]', 'eligible', 'active',
                       'document_end', 0, 1, 0, 0, 0, 0, 'article', ?, ?)""",
            (
                build_id,
                chunk_id,
                document_id,
                content_hash,
                metadata_hash,
                dedup_cluster_id,
                NOW,
                NOW,
                NOW,
            ),
        )
    conn.commit()


def _delta_set(conn: sqlite3.Connection, build_id: str, kind: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT chunk_id FROM corpus_build_deltas "
            "WHERE build_id=? AND delta_kind=?",
            (build_id, kind),
        )
    }


def test_reconciliation_delta_matrix_unchanged_new_content_metadata_dedup(tmp_path):
    from catalyst_data.corpus.streaming_publication import resume_candidate_reconciliation

    conn = _make_db(tmp_path / "matrix.db", article_count=1)
    _seed_legacy_matrix(conn)
    build_id = "a" * 64
    _seed_matrix_build(conn, build_id)

    summary = resume_candidate_reconciliation(conn, build_id=build_id)
    assert summary.to_embed_count == 4
    assert summary.metadata_update_count == 1
    assert summary.tombstone_count == 3

    assert _delta_set(conn, build_id, "to_embed") == {
        "c:news_v2:body:0001",
        "d:news_v2:body:0001",
        "n:news_v2:body:0001",
        "nullto:news_v2:body:0001",
    }
    assert _delta_set(conn, build_id, "metadata_update") == {"m:news_v2:body:0001"}
    cluster = conn.execute(
        """SELECT chunk_id FROM corpus_build_deltas
           WHERE build_id=? AND delta_kind='tombstone'
             AND reason='dedup_cluster_reassigned'""",
        (build_id,),
    ).fetchall()
    assert {row[0] for row in cluster} == {
        "d:news_v2:body:0001",
        "nullto:news_v2:body:0001",
    }
    removed = conn.execute(
        """SELECT chunk_id, reason FROM corpus_build_deltas
           WHERE build_id=? AND delta_kind='tombstone'
             AND reason IN ('eligibility_lost','profile_version_replaced',
                            'disappeared_child','document_removed')""",
        (build_id,),
    ).fetchall()
    assert {row[0]: row[1] for row in removed} == {
        "r:news_v2:body:0001": "document_removed"
    }
    # NULL IS semantics: nullsame (NULL->NULL) has no delta; nullto (NULL->X)
    # is both to_embed and dedup_cluster_reassigned.
    assert "nullsame:news_v2:body:0001" not in _delta_set(conn, build_id, "to_embed")
    assert (
        conn.execute(
            "SELECT status, reconciliation_checkpoint FROM corpus_publication_builds "
            "WHERE build_id=?",
            (build_id,),
        ).fetchone()
        == ("reconciliation_ready", "reconciliation_ready")
    )
    statuses = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT chunk_id, status FROM corpus_build_chunks WHERE build_id=?",
            (build_id,),
        )
    }
    assert statuses["c:news_v2:body:0001"] == "pending_embedding"
    assert statuses["n:news_v2:body:0001"] == "pending_embedding"
    assert statuses["m:news_v2:body:0001"] == "metadata_only"
    assert statuses["u:news_v2:body:0001"] == "active"


def test_reconciliation_removed_tombstone_absent_from_target_passes_finalize(tmp_path):
    from catalyst_data.corpus.streaming_publication import resume_candidate_reconciliation

    conn = _make_db(tmp_path / "removed-target.db", article_count=1)
    _seed_legacy_matrix(conn)
    build_id = "a" * 64
    _seed_matrix_build(conn, build_id)
    summary = resume_candidate_reconciliation(conn, build_id=build_id)
    assert summary.tombstone_count == 3
    removed = conn.execute(
        """SELECT chunk_id FROM corpus_build_deltas
           WHERE build_id=? AND delta_kind='tombstone'
             AND reason='document_removed'""",
        (build_id,),
    ).fetchall()
    for (chunk_id,) in removed:
        assert (
            conn.execute(
                "SELECT 1 FROM corpus_build_chunks WHERE build_id=? AND chunk_id=?",
                (build_id, chunk_id),
            ).fetchone()
            is None
        )


def test_reconciliation_cluster_tombstone_present_in_target(tmp_path):
    from catalyst_data.corpus.streaming_publication import resume_candidate_reconciliation

    conn = _make_db(tmp_path / "cluster-target.db", article_count=1)
    _seed_legacy_matrix(conn)
    build_id = "a" * 64
    _seed_matrix_build(conn, build_id)
    resume_candidate_reconciliation(conn, build_id=build_id)
    cluster = conn.execute(
        """SELECT chunk_id FROM corpus_build_deltas
           WHERE build_id=? AND delta_kind='tombstone'
             AND reason='dedup_cluster_reassigned'""",
        (build_id,),
    ).fetchall()
    assert cluster
    for (chunk_id,) in cluster:
        assert (
            conn.execute(
                "SELECT 1 FROM corpus_build_chunks WHERE build_id=? AND chunk_id=?",
                (build_id, chunk_id),
            ).fetchone()
            is not None
        )


def _ordered_deltas(
    conn: sqlite3.Connection, build_id: str
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        conn.execute(
            """SELECT delta_kind, chunk_id, document_id, reason,
                      previous_content_hash, previous_metadata_hash,
                      replacement_chunk_id
               FROM corpus_build_deltas WHERE build_id=?
               ORDER BY delta_kind COLLATE BINARY, chunk_id COLLATE BINARY""",
            (build_id,),
        ).fetchall()
    )


def _frozen_legacy_reconciliation(
    conn: sqlite3.Connection, build_id: str
) -> None:
    """Frozen pre-TDD reconciliation SQL (previous = served view), verbatim."""
    previous = "corpus_served_chunks"
    conn.execute("DELETE FROM corpus_build_deltas WHERE build_id=?", (build_id,))
    conn.execute(
        f"""INSERT INTO corpus_build_deltas
            (build_id, delta_kind, chunk_id, document_id,
             previous_content_hash, previous_metadata_hash)
            SELECT ?, 'to_embed', n.chunk_id, n.document_id,
                   p.content_hash, p.metadata_hash
            FROM corpus_build_chunks n
            LEFT JOIN {previous} p ON p.chunk_id=n.chunk_id
            WHERE n.build_id=? AND n.eligibility='eligible'
              AND (p.chunk_id IS NULL OR p.content_hash != n.content_hash
                   OR p.dedup_cluster_id IS NOT n.dedup_cluster_id)""",
        (build_id, build_id),
    )
    conn.execute(
        f"""INSERT INTO corpus_build_deltas
            (build_id, delta_kind, chunk_id, document_id, reason,
             previous_content_hash, previous_metadata_hash)
            SELECT ?, 'tombstone', n.chunk_id, n.document_id,
                   'dedup_cluster_reassigned', p.content_hash, p.metadata_hash
            FROM corpus_build_chunks n
            JOIN {previous} p ON p.chunk_id=n.chunk_id
            WHERE n.build_id=? AND n.eligibility='eligible'
              AND p.dedup_cluster_id IS NOT n.dedup_cluster_id""",
        (build_id, build_id),
    )
    conn.execute(
        f"""INSERT INTO corpus_build_deltas
            (build_id, delta_kind, chunk_id, document_id,
             previous_content_hash, previous_metadata_hash)
            SELECT ?, 'metadata_update', n.chunk_id, n.document_id,
                   p.content_hash, p.metadata_hash
            FROM corpus_build_chunks n
            JOIN {previous} p ON p.chunk_id=n.chunk_id
            WHERE n.build_id=? AND n.eligibility='eligible'
              AND p.content_hash=n.content_hash
              AND p.metadata_hash != n.metadata_hash
              AND p.dedup_cluster_id IS n.dedup_cluster_id""",
        (build_id, build_id),
    )
    conn.execute(
        f"""INSERT INTO corpus_build_deltas
            (build_id, delta_kind, chunk_id, document_id, reason,
             previous_content_hash, previous_metadata_hash,
             replacement_chunk_id)
            SELECT ?, 'tombstone', p.chunk_id, p.document_id,
                   CASE
                     WHEN EXISTS (
                       SELECT 1 FROM corpus_build_source_documents source_doc
                       WHERE source_doc.build_id=?
                         AND source_doc.document_id=p.document_id
                         AND source_doc.eligibility='ineligible'
                     ) THEN 'eligibility_lost'
                     WHEN EXISTS (
                       SELECT 1 FROM corpus_build_chunks same_doc
                       WHERE same_doc.build_id=?
                         AND same_doc.document_id=p.document_id
                         AND same_doc.chunk_profile_version != p.chunk_profile_version
                     ) THEN 'profile_version_replaced'
                     WHEN EXISTS (
                       SELECT 1 FROM corpus_build_chunks same_doc
                       WHERE same_doc.build_id=?
                         AND same_doc.document_id=p.document_id
                     ) THEN 'disappeared_child'
                     ELSE 'document_removed'
                   END,
                   p.content_hash, p.metadata_hash,
                   (SELECT MIN(replacement.chunk_id COLLATE BINARY)
                    FROM corpus_build_chunks replacement
                    WHERE replacement.build_id=?
                      AND replacement.document_id=p.document_id
                      AND replacement.chunk_profile_version != p.chunk_profile_version)
            FROM {previous} p
            LEFT JOIN corpus_build_chunks n
              ON n.build_id=? AND n.chunk_id=p.chunk_id
            WHERE n.chunk_id IS NULL""",
        (build_id, build_id, build_id, build_id, build_id, build_id),
    )
    conn.execute(
        """UPDATE corpus_build_chunks SET status='pending_embedding'
           WHERE build_id=? AND chunk_id IN (
               SELECT chunk_id FROM corpus_build_deltas
               WHERE build_id=? AND delta_kind='to_embed'
           )""",
        (build_id, build_id),
    )
    conn.execute(
        """UPDATE corpus_build_chunks SET status='metadata_only'
           WHERE build_id=? AND chunk_id IN (
               SELECT chunk_id FROM corpus_build_deltas
               WHERE build_id=? AND delta_kind='metadata_update'
           )""",
        (build_id, build_id),
    )
    conn.execute(
        """UPDATE corpus_publication_builds
           SET status='reconciliation_ready', reconciliation_ready=1, updated_at=?
           WHERE build_id=?""",
        (NOW, build_id),
    )


def test_reconciliation_delta_sql_byte_equivalent_to_frozen_legacy(tmp_path):
    phased_conn = _make_db(tmp_path / "phased.db", article_count=2)
    with pytest.raises(RuntimeError, match="stop-after-reconciliation"):
        build_streaming_corpus_and_lexical_index(
            phased_conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("stop-after-reconciliation"))
                if point == "after_reconciliation"
                else None
            ),
        )
    build_id = phased_conn.execute(
        "SELECT build_id FROM corpus_publication_builds"
    ).fetchone()[0]
    phased_deltas = _ordered_deltas(phased_conn, build_id)

    legacy_conn = _make_db(tmp_path / "legacy.db", article_count=2)
    with pytest.raises(RuntimeError, match="stop-after-manifest"):
        build_streaming_corpus_and_lexical_index(
            legacy_conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("stop-after-manifest"))
                if point == "after_manifest"
                else None
            ),
        )
    legacy_build_id = legacy_conn.execute(
        "SELECT build_id FROM corpus_publication_builds"
    ).fetchone()[0]
    assert legacy_build_id == build_id
    legacy_conn.execute("BEGIN")
    _frozen_legacy_reconciliation(legacy_conn, build_id)
    legacy_deltas = _ordered_deltas(legacy_conn, build_id)
    legacy_conn.rollback()
    assert legacy_deltas == phased_deltas


@pytest.mark.parametrize(
    ("stop_point", "expected_checkpoint"),
    [
        ("after_recon_initialized", "initialized"),
        ("after_recon_to_embed_done", "to_embed_done"),
        ("after_recon_cluster_tombstones_done", "cluster_tombstones_done"),
        ("after_recon_metadata_updates_done", "metadata_updates_done"),
        ("after_recon_removed_tombstones_done", "removed_tombstones_done"),
        ("after_recon_statuses_done", "statuses_done"),
        ("after_recon_finalize", "reconciliation_ready"),
    ],
)
def test_reconciliation_resume_after_every_checkpoint_no_duplicate_deltas(
    tmp_path, stop_point: str, expected_checkpoint: str
):
    from catalyst_data.corpus.streaming_publication import resume_candidate_reconciliation

    conn = _make_db(tmp_path / f"resume-{stop_point}.db", article_count=2)
    with pytest.raises(RuntimeError, match=f"injected:{stop_point}"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError(f"injected:{stop_point}"))
                if point == stop_point
                else None
            ),
        )
    build_id = conn.execute("SELECT build_id FROM corpus_publication_builds").fetchone()[0]
    checkpoint = conn.execute(
        "SELECT reconciliation_checkpoint FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()[0]
    assert checkpoint == expected_checkpoint

    def delta_counts() -> dict[str, int]:
        return {
            kind: int(count)
            for kind, count in conn.execute(
                """SELECT delta_kind, COUNT(*) FROM corpus_build_deltas
                   WHERE build_id=? GROUP BY delta_kind""",
                (build_id,),
            ).fetchall()
        }

    before = delta_counts()
    summary = resume_candidate_reconciliation(conn, build_id=build_id)
    after = delta_counts()
    # Already-completed delta kinds must never be duplicated by remaining phases.
    for kind, count in before.items():
        assert after[kind] == count
    assert (
        conn.execute(
            "SELECT status, reconciliation_checkpoint FROM corpus_publication_builds "
            "WHERE build_id=?",
            (build_id,),
        ).fetchone()
        == ("reconciliation_ready", "reconciliation_ready")
    )
    again = resume_candidate_reconciliation(conn, build_id=build_id)
    assert again.to_embed_count == summary.to_embed_count
    assert delta_counts() == after


def test_reconciliation_poisoned_served_view_not_touched(tmp_path):
    from catalyst_data.corpus.streaming_publication import (
        _materialize_reconciliation_temp,
        _recon_flags,
        _recon_phase_to_embed,
    )

    conn = _make_db(tmp_path / "poisoned-view.db", article_count=2)
    with pytest.raises(RuntimeError, match="stop-after-manifest"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("stop-after-manifest"))
                if point == "after_manifest"
                else None
            ),
        )
    build_id = conn.execute("SELECT build_id FROM corpus_publication_builds").fetchone()[0]
    flags = _recon_flags(None)
    _materialize_reconciliation_temp(conn, build_id=build_id, deadline=900.0, flags=flags)

    # Any SELECT from the served view now fails.
    conn.execute("DROP VIEW corpus_served_chunks")
    conn.execute(
        "CREATE VIEW corpus_served_chunks AS SELECT * FROM missing_table"
    )
    conn.commit()

    _recon_phase_to_embed(
        conn,
        build_id=build_id,
        now=NOW,
        deadline=900.0,
        flags=flags,
        failure_injector=None,
        state={},
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM corpus_build_deltas "
            "WHERE build_id=? AND delta_kind='to_embed'",
            (build_id,),
        ).fetchone()[0]
        > 0
    )


def test_reconciliation_pathological_fixture_bounded(tmp_path):
    from catalyst_data.corpus.streaming_publication import resume_candidate_reconciliation

    conn = _make_db(tmp_path / "pathological.db", article_count=1)
    ensure_streaming_publication_schema(conn)
    conn.execute("DELETE FROM corpus_chunks WHERE manifest_id=?", (OLD_MANIFEST_ID,))
    for index in range(1000):
        conn.execute(
            """INSERT INTO corpus_chunks
               (chunk_id, document_id, chunk_profile_version, section_key, ordinal,
                content_text, content_hash, metadata_hash, source_class,
                dedup_cluster_id, available_at, ticker_associations, eligibility,
                manifest_id, status, boundary_kind, body_token_start,
                body_token_end, body_overlap_tokens, prefix_token_count,
                prefix_truncated, section_parse_degraded, created_at, updated_at)
               VALUES (?, 'doc-p', 'filing_v2', 'item', ?, 'text', ?, ?,
                       'official_government', 'p-cluster', ?, '[]', 'eligible', ?,
                       'active', 'document_end', 0, 1, 0, 0, 0, 0, ?, ?)""",
            (
                f"doc-p:filing_v2:item:{index:04d}",
                f"{index:04d}",
                _h64(f"p{index}"),
                _h64(f"pm{index}"),
                NOW,
                OLD_MANIFEST_ID,
                NOW,
                NOW,
            ),
        )
        conn.execute(
            """INSERT INTO corpus_chunks
               (chunk_id, document_id, chunk_profile_version, section_key, ordinal,
                content_text, content_hash, metadata_hash, source_class,
                dedup_cluster_id, available_at, ticker_associations, eligibility,
                manifest_id, status, boundary_kind, body_token_start,
                body_token_end, body_overlap_tokens, prefix_token_count,
                prefix_truncated, section_parse_degraded, created_at, updated_at)
               VALUES (?, 'doc-q', 'news_v2', 'body', ?, 'text', ?, ?,
                       'reported_news', 'q-cluster', ?, '[]', 'eligible', ?,
                       'active', 'document_end', 0, 1, 0, 0, 0, 0, ?, ?)""",
            (
                f"doc-q:news_v2:body:{index:04d}",
                f"{index:04d}",
                _h64(f"q{index}"),
                _h64(f"qm{index}"),
                NOW,
                OLD_MANIFEST_ID,
                NOW,
                NOW,
            ),
        )
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 0, ?)""",
        ("1" * 64, NOW),
    )
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status,
            manifest_id, document_count, chunk_count, inventory_digest,
            reconciliation_ready, created_at, updated_at)
           VALUES (?, ?, '{}', 'manifest_ready', ?, 1, 500, ?, 0, ?, ?)""",
        ("a" * 64, SNAPSHOT_ID, "1" * 64, "9" * 64, NOW, NOW),
    )
    for index in range(500):
        conn.execute(
            """INSERT INTO corpus_build_chunks
               (build_id, chunk_id, document_id, chunk_profile_version,
                section_key, ordinal, content_text, content_hash, metadata_hash,
                source_class, dedup_cluster_id, available_at,
                ticker_associations, eligibility, status, boundary_kind,
                body_token_start, body_token_end, body_overlap_tokens,
                prefix_token_count, prefix_truncated, section_parse_degraded,
                source_kind, created_at, updated_at)
               VALUES (?, ?, 'doc-p', 'filing_v3', 'item', ?, 'text', ?, ?,
                       'official_government', 'p-cluster', ?, '[]', 'eligible',
                       'active', 'document_end', 0, 1, 0, 0, 0, 0, 'filing', ?, ?)""",
            (
                "a" * 64,
                f"doc-p:filing_v3:item:{index:04d}",
                f"{index:04d}",
                _h64(f"p{index}"),
                _h64(f"pm{index}"),
                NOW,
                NOW,
                NOW,
            ),
        )
    conn.commit()

    summary = resume_candidate_reconciliation(conn, build_id="a" * 64)
    assert summary.tombstone_count == 2000
    replaced = conn.execute(
        """SELECT COUNT(*) FROM corpus_build_deltas
           WHERE build_id=? AND delta_kind='tombstone'
             AND reason='profile_version_replaced'""",
        ("a" * 64,),
    ).fetchone()[0]
    removed = conn.execute(
        """SELECT COUNT(*) FROM corpus_build_deltas
           WHERE build_id=? AND delta_kind='tombstone'
             AND reason='document_removed'""",
        ("a" * 64,),
    ).fetchone()[0]
    assert replaced == 1000
    assert removed == 1000
    expected_replacement = "doc-p:filing_v3:item:0000"
    bad_replacement = conn.execute(
        """SELECT COUNT(*) FROM corpus_build_deltas
           WHERE build_id=? AND delta_kind='tombstone'
             AND reason='profile_version_replaced'
             AND replacement_chunk_id != ?""",
        ("a" * 64, expected_replacement),
    ).fetchone()[0]
    assert bad_replacement == 0
    assert (
        conn.execute(
            "SELECT status, reconciliation_checkpoint FROM corpus_publication_builds "
            "WHERE build_id=?",
            ("a" * 64,),
        ).fetchone()
        == ("reconciliation_ready", "reconciliation_ready")
    )


def test_reconciliation_base_rejects_zero_or_multiple_current_manifests(tmp_path):
    from catalyst_data.corpus.streaming_publication import _reconciliation_base

    conn = _make_db(tmp_path / "zero-current.db")
    # Zero current manifests must fail closed.
    conn.execute("UPDATE corpus_manifest SET is_current=0 WHERE is_current=1")
    conn.commit()
    with pytest.raises(ValueError, match="exactly one current"):
        _reconciliation_base(conn)

    # Multiple current manifests must fail closed even without the partial
    # unique index and guard triggers (defense in depth).
    conn.execute("DROP INDEX idx_corpus_manifest_current")
    conn.execute("DROP TRIGGER IF EXISTS trg_corpus_manifest_current_guard")
    conn.execute("DROP TRIGGER IF EXISTS trg_corpus_manifest_current_guard_update")
    conn.execute(
        "UPDATE corpus_manifest SET is_current=1 WHERE manifest_id=?",
        (OLD_MANIFEST_ID,),
    )
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 1, ?)""",
        ("b" * 64, NOW),
    )
    conn.commit()
    with pytest.raises(ValueError, match="exactly one current"):
        _reconciliation_base(conn)


def test_reconciliation_base_rejects_multiple_published_for_current(tmp_path):
    from catalyst_data.corpus.streaming_publication import _reconciliation_base

    conn = _make_db(tmp_path / "multi-published.db")
    ensure_streaming_publication_schema(conn)
    for suffix in ("a", "b"):
        conn.execute(
            """INSERT INTO corpus_publication_builds
               (build_id, certified_snapshot_identity, header_json, status,
                manifest_id, created_at, updated_at)
               VALUES (?, ?, '{}', 'published', ?, ?, ?)""",
            (
                f"build-{suffix}" + "0" * 56,
                "1" * 64,
                OLD_MANIFEST_ID,
                NOW,
                NOW,
            ),
        )
    conn.commit()
    with pytest.raises(ValueError, match="multiple published builds"):
        _reconciliation_base(conn)


def test_reconciliation_base_rejects_published_and_legacy_same_manifest(tmp_path):
    from catalyst_data.corpus.streaming_publication import _reconciliation_base

    conn = _make_db(tmp_path / "ambiguous-base.db")
    ensure_streaming_publication_schema(conn)
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status,
            manifest_id, created_at, updated_at)
           VALUES (?, ?, '{}', 'published', ?, ?, ?)""",
        ("build-a" + "0" * 56, "1" * 64, OLD_MANIFEST_ID, NOW, NOW),
    )
    conn.commit()
    # _make_db seeds legacy corpus_chunks rows for the SAME current manifest.
    with pytest.raises(ValueError, match="ambiguous served source"):
        _reconciliation_base(conn)


def test_reconciliation_resume_rejects_base_rows_appearing_after_initialize(tmp_path):
    from catalyst_data.corpus.streaming_publication import resume_candidate_reconciliation

    conn = _make_db(tmp_path / "base-rows-appear.db", article_count=2)
    # The current manifest exists but has no legacy rows at initialize time.
    conn.execute("DELETE FROM corpus_chunks WHERE manifest_id=?", (OLD_MANIFEST_ID,))
    conn.commit()

    def fail(point: str) -> None:
        if point == "after_recon_initialized":
            raise RuntimeError("stop-after-initialized")

    with pytest.raises(RuntimeError, match="stop-after-initialized"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=fail,
        )
    build_id = conn.execute("SELECT build_id FROM corpus_publication_builds").fetchone()[0]
    bound = conn.execute(
        """SELECT reconciliation_base_manifest_id, reconciliation_base_chunk_count,
                  reconciliation_base_inventory_digest
           FROM corpus_publication_builds WHERE build_id=?""",
        (build_id,),
    ).fetchone()
    assert bound[0] == OLD_MANIFEST_ID
    assert int(bound[1]) == 0

    # Legacy rows appear for the same current manifest before resume; the
    # recomputed count no longer matches the bound base and must fail closed.
    content_hash = hashlib.sha256(b"late base row").hexdigest()
    conn.execute(
        """INSERT INTO corpus_chunks
           (chunk_id, document_id, chunk_profile_version, section_key, ordinal,
            content_text, content_hash, metadata_hash, source_class,
            dedup_cluster_id, available_at, ticker_associations, eligibility,
            manifest_id, status, boundary_kind, body_token_start,
            body_token_end, body_overlap_tokens, prefix_token_count,
            prefix_truncated, section_parse_degraded, created_at, updated_at)
           VALUES (?, 'doc-late', 'news_v2', 'body', '0001', 'late text', ?, ?,
                   'reported_news', NULL, ?, '[]', 'eligible', ?,
                   'active', 'document_end', 0, 1, 0, 0, 0, 0, ?, ?)""",
        (
            "late:news_v2:body:0001",
            content_hash,
            hashlib.sha256(b"late meta").hexdigest(),
            NOW,
            OLD_MANIFEST_ID,
            NOW,
            NOW,
        ),
    )
    conn.commit()
    with pytest.raises(ValueError, match="base chunk count changed"):
        resume_candidate_reconciliation(conn, build_id=build_id)


def test_reconciliation_removed_sql_reads_survivors_not_full_previous():
    from catalyst_data.corpus import streaming_publication as sp

    removed = sp._RECON_REMOVED_TOMBSTONE_SQL
    # Removed tombstones read the bounded survivor TEMP, not the full
    # previous-served snapshot, and the INSERT no longer anti-joins the target.
    assert "temp_tombstone_survivors" in removed
    assert "temp_previous_served" not in removed
    assert "n.chunk_id IS NULL" not in removed
    assert "corpus_build_chunks n" not in removed
    # The 1:1 lookups and CASE precedence are preserved.
    assert "ineligible" in removed
    assert "temp_profile_replacements" in removed
    assert "survivor" in removed
    assert removed.index("eligibility_lost") < removed.index(
        "profile_version_replaced"
    )
    assert removed.index("profile_version_replaced") < removed.index(
        "disappeared_child"
    )
    assert removed.index("disappeared_child") < removed.index("document_removed")
    assert "replacement_chunk_id" in removed


def test_reconciliation_profile_replacements_built_from_survivors(tmp_path):
    from catalyst_data.corpus import streaming_publication as sp

    replacements = sp._RECON_PROFILE_REPLACEMENTS_SQL
    # Replacements are built from the bounded survivor profiles x new-doc
    # profiles, never from the full previous-served snapshot, and never by
    # re-joining the target chunks.
    assert "temp_tombstone_survivors" in replacements
    assert "temp_previous_served" not in replacements
    assert "temp_new_doc_profiles" in replacements
    assert "min_chunk_id" in replacements
    assert "GROUP BY" in replacements
    assert "corpus_build_chunks" not in replacements

    conn = _make_db(tmp_path / "survivors.db", article_count=1)
    _seed_legacy_matrix(conn)
    build_id = "a" * 64
    _seed_matrix_build(conn, build_id)
    flags = sp._recon_flags(None)
    sp._materialize_reconciliation_temp(
        conn, build_id=build_id, deadline=900.0, flags=flags
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM temp_previous_served").fetchone()[0]
        == 7
    )
    survivors = conn.execute(
        """SELECT chunk_id, document_id, chunk_profile_version,
                  content_hash, metadata_hash
           FROM temp_tombstone_survivors"""
    ).fetchall()
    # Only the previous chunk missing from the new build survives.
    assert survivors == [
        ("r:news_v2:body:0001", "doc-r", "news_v2", _h64("c1"), _h64("m1"))
    ]
    # doc-r has no replacement profile in the new build, so no replacement row.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM temp_profile_replacements"
        ).fetchone()[0]
        == 0
    )


def test_reconciliation_resume_from_metadata_updates_done_keeps_to_embed_unchanged(
    tmp_path,
):
    from catalyst_data.corpus.streaming_publication import (
        resume_candidate_reconciliation,
    )

    conn = _make_db(tmp_path / "resume-metadata.db", article_count=2)
    with pytest.raises(RuntimeError, match="injected:after_recon_metadata_updates_done"):
        build_streaming_corpus_and_lexical_index(
            conn,
            certified_snapshot_identity=SNAPSHOT_ID,
            clock=lambda: NOW,
            failure_injector=lambda point: (
                (_ for _ in ()).throw(
                    RuntimeError("injected:after_recon_metadata_updates_done")
                )
                if point == "after_recon_metadata_updates_done"
                else None
            ),
        )
    build_id = conn.execute(
        "SELECT build_id FROM corpus_publication_builds"
    ).fetchone()[0]
    assert (
        conn.execute(
            "SELECT reconciliation_checkpoint FROM corpus_publication_builds "
            "WHERE build_id=?",
            (build_id,),
        ).fetchone()[0]
        == "metadata_updates_done"
    )
    to_embed_before = conn.execute(
        "SELECT COUNT(*) FROM corpus_build_deltas "
        "WHERE build_id=? AND delta_kind='to_embed'",
        (build_id,),
    ).fetchone()[0]
    assert to_embed_before > 0

    summary = resume_candidate_reconciliation(conn, build_id=build_id)
    to_embed_after = conn.execute(
        "SELECT COUNT(*) FROM corpus_build_deltas "
        "WHERE build_id=? AND delta_kind='to_embed'",
        (build_id,),
    ).fetchone()[0]
    assert to_embed_after == to_embed_before
    # No duplicated to_embed rows from the remaining phases.
    assert (
        conn.execute(
            """SELECT COUNT(*) FROM (
                 SELECT chunk_id FROM corpus_build_deltas
                 WHERE build_id=? AND delta_kind='to_embed'
                 GROUP BY chunk_id HAVING COUNT(*) > 1
               )""",
            (build_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT status, reconciliation_checkpoint, reconciliation_ready "
            "FROM corpus_publication_builds WHERE build_id=?",
            (build_id,),
        ).fetchone()
        == ("reconciliation_ready", "reconciliation_ready", 1)
    )
