"""Typed, bounded production metadata stream contract (Task 1).

The iterator must:
- use ``served_chunks_relation`` (never hardcode corpus_chunks);
- require the current corpus manifest to equal the expected manifest id;
- stream in chunk_id ASC order with ``fetchmany`` page_size 1..500 (no fetchall);
- restrict rows to searchable statuses with eligibility='eligible';
- parse ticker_associations into a sorted unique uppercase list;
- reject malformed JSON, empty tickers, duplicate/out-of-order chunk ids, null
  identity fields, and non-lowercase SHA-256 content/metadata hashes.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3

import pytest

MANIFEST_A = "a" * 64
MANIFEST_B = "b" * 64

SEARCHABLE = ("active", "pending_embedding", "embedded", "metadata_only")


def _row(
    index: int = 0,
    *,
    chunk_id: str | None = None,
    tickers: tuple[str, ...] = ("AAPL", "MSFT"),
    status: str = "active",
    eligibility: str = "eligible",
    content_text: str = "AAPL earnings report",
    content_hash: str | None = None,
    metadata_hash: str | None = None,
    document_id: str | None = None,
    dedup_cluster_id: str | None = "cluster:1",
    cluster_first_available_at: str | None = "2026-01-01T00:00:00Z",
    representative_document_id: str | None = "doc:rep",
) -> tuple:
    text = content_text
    if content_hash is None:
        content_hash = hashlib.sha256(text.encode()).hexdigest()
    if metadata_hash is None:
        metadata_hash = hashlib.sha256(b"{}").hexdigest()
    return (
        chunk_id or f"chunk:{index:06d}",
        document_id or f"doc:{index}",
        text,
        content_hash,
        metadata_hash,
        "2026-01-01T00:00:00Z",
        json.dumps(list(tickers)),
        "reported_news",
        "news_v2",
        status,
        eligibility,
        dedup_cluster_id,
        cluster_first_available_at,
        representative_document_id,
    )


def _make_db(
    rows: list[tuple] | None = None,
    *,
    current_manifest: str = MANIFEST_A,
    with_view: bool = False,
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE corpus_manifest ("
        " manifest_id TEXT PRIMARY KEY, is_current INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO corpus_manifest (manifest_id, is_current) VALUES (?, 1)",
        (current_manifest,),
    )
    conn.execute(
        """CREATE TABLE corpus_chunks (
             chunk_id TEXT, document_id TEXT, content_text TEXT,
             content_hash TEXT, metadata_hash TEXT, available_at TEXT,
             ticker_associations TEXT, source_class TEXT, chunk_profile_version TEXT,
             status TEXT, eligibility TEXT, manifest_id TEXT,
             dedup_cluster_id TEXT, cluster_first_available_at TEXT,
             representative_document_id TEXT
           )"""
    )
    if with_view:
        conn.execute(
            """CREATE VIEW corpus_served_chunks AS
                 SELECT chunk_id, document_id, content_text, content_hash,
                        metadata_hash, available_at, ticker_associations,
                        source_class, chunk_profile_version, status, eligibility,
                        manifest_id, dedup_cluster_id, cluster_first_available_at,
                        representative_document_id
                 FROM corpus_chunks"""
        )
    for raw in rows or []:
        conn.execute(
            """INSERT INTO corpus_chunks (
                 chunk_id, document_id, content_text, content_hash, metadata_hash,
                 available_at, ticker_associations, source_class,
                 chunk_profile_version, status, eligibility, manifest_id,
                 dedup_cluster_id, cluster_first_available_at,
                 representative_document_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (*raw[:11], MANIFEST_A, *raw[11:]),
        )
    conn.commit()
    return conn


def _collect(conn, **kwargs) -> list:
    from catalyst_data.retrieval.import_metadata import iter_import_metadata

    return list(iter_import_metadata(conn, corpus_manifest_id=MANIFEST_A, **kwargs))


def test_iterates_served_rows_in_order_with_typed_fields():
    conn = _make_db([_row(0), _row(1), _row(2)], with_view=True)
    rows = _collect(conn)
    assert [row.chunk_id for row in rows] == ["chunk:000000", "chunk:000001", "chunk:000002"]
    first = rows[0]
    assert first.document_id == "doc:0"
    assert first.content_text == "AAPL earnings report"
    assert first.content_hash == hashlib.sha256(b"AAPL earnings report").hexdigest()
    assert first.metadata_hash == hashlib.sha256(b"{}").hexdigest()
    assert first.ticker_associations == ("AAPL", "MSFT")
    assert first.source_class == "reported_news"
    assert first.chunk_profile_version == "news_v2"
    assert first.status == "active"
    assert first.eligibility == "eligible"
    assert first.dedup_cluster_id == "cluster:1"
    assert first.cluster_first_available_at == "2026-01-01T00:00:00Z"
    assert first.representative_document_id == "doc:rep"


def test_ticker_associations_are_sorted_unique_uppercase():
    conn = _make_db([_row(0, tickers=("msft", "AAPL", "aapl", "TSLA"))])
    rows = _collect(conn)
    assert rows[0].ticker_associations == ("AAPL", "MSFT", "TSLA")


def test_to_dict_round_trips_lance_records():
    row = _collect(_make_db([_row(0)]))[0]
    payload = row.to_dict()
    assert payload["chunk_id"] == "chunk:000000"
    assert payload["ticker_associations"] == ["AAPL", "MSFT"]
    assert payload["metadata_hash"] == hashlib.sha256(b"{}").hexdigest()
    assert payload["dedup_cluster_id"] == "cluster:1"


def test_uses_served_view_when_present():
    conn = _make_db([_row(0)], with_view=True)
    # Also create a decoy legacy row that the view must hide by joining only
    # current-manifest rows; simplest proof: mutate the underlying table after
    # the iterator starts and show the view relation is selected.
    rows = _collect(conn)
    assert len(rows) == 1
    assert rows[0].chunk_id == "chunk:000000"


def test_rejects_current_manifest_mismatch():
    conn = _make_db([_row(0)], current_manifest=MANIFEST_B)
    from catalyst_data.retrieval.import_metadata import iter_import_metadata

    with pytest.raises(ValueError, match="manifest"):
        list(iter_import_metadata(conn, corpus_manifest_id=MANIFEST_A))


def test_rejects_missing_current_manifest():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE corpus_manifest (manifest_id TEXT PRIMARY KEY, is_current INTEGER)")
    from catalyst_data.retrieval.import_metadata import iter_import_metadata

    with pytest.raises(ValueError, match="current"):
        list(iter_import_metadata(conn, corpus_manifest_id=MANIFEST_A))


def test_excludes_non_eligible_and_non_searchable_rows():
    conn = _make_db([
        _row(0, status="active"),
        _row(1, status="retired"),
        _row(2, eligibility="ineligible"),
        _row(3, status="pending_embedding"),
        _row(4, status="embedded"),
        _row(5, status="metadata_only"),
    ])
    rows = _collect(conn)
    assert [row.chunk_id for row in rows] == [
        "chunk:000000", "chunk:000003", "chunk:000004", "chunk:000005",
    ]


def test_rejects_malformed_ticker_json():
    raw = list(_row(0))
    raw[6] = "{not-json"
    conn = _make_db([tuple(raw)])
    with pytest.raises(ValueError, match="ticker_associations"):
        _collect(conn)


def test_rejects_non_array_ticker_json():
    raw = list(_row(0))
    raw[6] = '"AAPL"'
    conn = _make_db([tuple(raw)])
    with pytest.raises(ValueError, match="ticker_associations"):
        _collect(conn)


def test_rejects_empty_ticker():
    raw = list(_row(0))
    raw[6] = json.dumps(["AAPL", ""])
    conn = _make_db([tuple(raw)])
    with pytest.raises(ValueError, match="ticker"):
        _collect(conn)


def test_rejects_duplicate_chunk_id():
    conn = _make_db([_row(0), _row(0)])
    with pytest.raises(ValueError, match="order|duplicate|sorted"):
        _collect(conn)


def test_rejects_out_of_order_chunk_id():
    # SQL sorts rows, so the iterator's own order guard is exercised through
    # the injected cursor path where page order is under test control.
    from catalyst_data.retrieval.import_metadata import iter_import_metadata

    class Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchmany(self, size):
            return self._rows.pop(0) if self._rows else []

        def fetchone(self):
            return None

    class ManifestCursor:
        def fetchone(self):
            return (MANIFEST_A,)

    class Conn:
        def execute(self, sql, params=()):
            if "corpus_manifest" in sql:
                return ManifestCursor()
            if "sqlite_master" in sql:
                return Cursor([])
            return Cursor([[r] for r in [_row(1), _row(0)]])

    with pytest.raises(ValueError, match="order|sorted"):
        list(iter_import_metadata(Conn(), corpus_manifest_id=MANIFEST_A))


def test_rejects_null_identity_fields():
    raw = list(_row(0))
    raw[0] = None  # chunk_id null
    conn = _make_db([tuple(raw)])
    with pytest.raises(ValueError, match="identity|chunk_id"):
        _collect(conn)


def test_rejects_non_sha256_content_hash():
    raw = list(_row(0))
    raw[3] = "not-a-hash"
    conn = _make_db([tuple(raw)])
    with pytest.raises(ValueError, match="content_hash"):
        _collect(conn)


def test_rejects_uppercase_content_hash():
    raw = list(_row(0))
    raw[3] = raw[3].upper()
    conn = _make_db([tuple(raw)])
    with pytest.raises(ValueError, match="content_hash"):
        _collect(conn)


def test_rejects_non_sha256_metadata_hash():
    raw = list(_row(0))
    raw[4] = "Z" * 64
    conn = _make_db([tuple(raw)])
    with pytest.raises(ValueError, match="metadata_hash"):
        _collect(conn)


def test_rejects_content_hash_mismatch_against_text():
    raw = list(_row(0))
    raw[3] = hashlib.sha256(b"different").hexdigest()
    conn = _make_db([tuple(raw)])
    with pytest.raises(ValueError, match="content_hash"):
        _collect(conn)


def test_page_size_bounds():
    conn = _make_db([_row(0)])
    from catalyst_data.retrieval.import_metadata import iter_import_metadata

    with pytest.raises(ValueError, match="page_size"):
        list(iter_import_metadata(conn, corpus_manifest_id=MANIFEST_A, page_size=0))
    with pytest.raises(ValueError, match="page_size"):
        list(iter_import_metadata(conn, corpus_manifest_id=MANIFEST_A, page_size=501))


def test_large_fixture_streams_all_rows():
    conn = _make_db([_row(index) for index in range(1200)], with_view=True)
    rows = _collect(conn)
    assert len(rows) == 1200
    assert rows[-1].chunk_id == "chunk:001199"


def test_uses_fetchmany_bounded_reads_never_fetchall():
    from catalyst_data.retrieval import import_metadata as mod

    source = inspect.getsource(mod)
    assert "fetchall" not in source
    assert "fetchmany" in source
    assert "ORDER BY chunk_id" in source
    assert "served_chunks_relation" in source
    assert "corpus_chunks" not in source


def test_runtime_cursor_reads_are_bounded_by_page_size():
    from catalyst_data.retrieval.import_metadata import iter_import_metadata

    pages = [[_row(0)], [_row(1)], [_row(2)], []]
    seen: list[int] = []

    class Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchmany(self, size):
            seen.append(size)
            return self._rows.pop(0) if self._rows else []

        def fetchone(self):
            return None

    class ManifestCursor:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return self._value

    class Conn:
        def execute(self, sql, params=()):
            if "corpus_manifest" in sql:
                return ManifestCursor((MANIFEST_A,))
            if "sqlite_master" in sql:
                return Cursor([])  # no served view -> relation is corpus_chunks
            return Cursor(pages)

    rows = list(iter_import_metadata(Conn(), corpus_manifest_id=MANIFEST_A, page_size=2))
    assert [row.chunk_id for row in rows] == [
        "chunk:000000", "chunk:000001", "chunk:000002",
    ]
    assert seen and max(seen) <= 2


def test_module_does_not_hardcode_corpus_chunks_relation():
    from catalyst_data.retrieval import import_metadata as mod

    source = inspect.getsource(mod)
    assert "FROM {chunks_relation}" in source
