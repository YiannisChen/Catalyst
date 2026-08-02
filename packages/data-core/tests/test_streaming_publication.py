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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version_before == 13


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
