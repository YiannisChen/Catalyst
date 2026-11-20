from __future__ import annotations

import hashlib
import sqlite3

import pytest
from catalyst_data.ingestion.storage import store as store_module
from catalyst_data.ingestion.storage.store import (
    RefreshBusy,
    notify_assets_stored,
    open_writer,
    refresh_lock,
    track_assets,
    write_assets,
)
from catalyst_data.retrieval import web_bodies


def _row(asset_id: str = "asset:1", text: str = "Original body.\n\nSecond paragraph.", **changes):
    row = {
        "asset_id": asset_id,
        "text": text,
        "url": "https://finance.yahoo.com/news/example",
        "title": "Example headline",
        "publisher": "ignored supplied publisher",
        "source_class": "reported_news",
        "eligible_at": "2026-10-01T15:00:00Z",
        "fetched_at": "2026-10-02T00:00:00Z",
        "body_sha256": "a" * 64,
        "tickers": ["ABC"],
    }
    row.update(changes)
    return row


def test_refresh_lock_uses_one_identity_for_symlink_aliases(tmp_path):
    real_db = tmp_path / "archive" / "market.sqlite3"
    real_db.parent.mkdir()
    real_db.touch()
    alias = tmp_path / "market-alias.sqlite3"
    alias.symlink_to(real_db)

    with refresh_lock(real_db):
        with pytest.raises(RefreshBusy):
            with refresh_lock(alias):
                pass


def _count_if_table_exists(conn, table: str) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_first_write_stores_chunks_and_uses_wal(tmp_path):
    db_path = tmp_path / "store.sqlite3"
    conn = open_writer(db_path)
    report = write_assets(conn, [_row()])
    assert report.stored_assets == 1
    assert report.chunks == len(web_bodies.chunk_text(_row()["text"]))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("SELECT COUNT(*) FROM web_body_chunks").fetchone()[0] == report.chunks
    assert conn.execute("SELECT body FROM body_versions").fetchone()[0] == _row()["text"]
    conn.close()


def test_body_version_error_includes_asset_identity(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    row = _row("asset:bad", text="", source_id="source:bad", document_id="doc:bad")

    with pytest.raises(store_module.body_versions.BodyVersionError) as exc_info:
        write_assets(conn, [row])

    assert type(exc_info.value) is store_module.body_versions.BodyVersionError
    assert "body must contain complete nonempty text" in str(exc_info.value)
    assert "source_id=source:bad" in str(exc_info.value)
    assert "document_id=doc:bad" in str(exc_info.value)
    assert "asset_id=asset:bad" in str(exc_info.value)
    conn.close()


def test_changed_body_does_not_replace_chunks_or_leave_shorter_body_leftovers(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    original = _row(text="\n\n".join(f"Paragraph {i} " + "x " * 500 for i in range(5)))
    first = write_assets(conn, [original])
    before = conn.execute(
        "SELECT ordinal, text FROM web_body_chunks WHERE asset_id = ? ORDER BY ordinal",
        (original["asset_id"],),
    ).fetchall()

    changed = {**original, "text": "Short replacement body.", "body_sha256": "b" * 64}
    report = write_assets(conn, [changed])
    after = conn.execute(
        "SELECT ordinal, text FROM web_body_chunks WHERE asset_id = ? ORDER BY ordinal",
        (original["asset_id"],),
    ).fetchall()
    assert len(before) == first.chunks > 1
    assert after == before
    assert report.body_changed == 1
    assert report.chunks == 0
    assert conn.execute("SELECT COUNT(*) FROM body_versions").fetchone()[0] == 2
    conn.close()


def test_identical_asset_is_counted_as_duplicate_without_new_chunks(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    row = _row()
    write_assets(conn, [row])
    report = write_assets(conn, [{**row, "fetched_at": "2026-10-03T00:00:00Z"}])
    assert report.duplicate_assets == 1
    assert report.chunks == 0
    assert conn.execute("SELECT COUNT(*) FROM body_versions").fetchone()[0] == 1
    assert conn.execute(
        "SELECT association_observed_at FROM temporal_evidence_map"
    ).fetchone()[0] == "2026-10-02T00:00:00.000000+00:00"
    conn.close()


def test_write_assets_can_join_caller_transaction_and_defer_tracking_until_commit(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    tracked = set()
    callbacks = []
    row = _row(structure={"title": "Example reports results", "units": []})
    conn.execute("CREATE TABLE sec_exhibit_units (url TEXT PRIMARY KEY, accession TEXT, units_json TEXT, parsed_at TEXT)")
    conn.commit()
    with track_assets(tracked, callbacks.append):
        conn.execute("BEGIN IMMEDIATE")
        report = write_assets(conn, [row], commit=False)
        assert conn.in_transaction
        assert conn.execute("SELECT count(*) FROM web_body_chunks").fetchone()[0] > 0
        assert conn.execute("SELECT count(*) FROM sec_exhibit_units").fetchone()[0] == 1
        assert tracked == set()
        assert callbacks == []

        conn.rollback()
        assert conn.execute("SELECT count(*) FROM web_body_chunks").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sec_exhibit_units").fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='body_versions'"
        ).fetchone()[0] == 0

        conn.execute("BEGIN IMMEDIATE")
        report = write_assets(conn, [row], commit=False)
        conn.commit()
        notify_assets_stored(report)
        assert tracked == {row["asset_id"]}
        assert callbacks == [row["asset_id"]]
    conn.close()


def test_write_assets_without_caller_transaction_rejects_commit_false(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    with pytest.raises(ValueError, match="caller transaction"):
        write_assets(conn, [_row()], commit=False)
    conn.close()


def test_existing_asset_accepts_new_ticker_tag(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    row = _row(tickers=["ABC"])
    write_assets(conn, [row])
    report = write_assets(conn, [{**row, "tickers": ["ABC", "XYZ"], "fetched_at": "2026-10-03T00:00:00Z"}])
    assert report.tagged == 1
    assert conn.execute(
        "SELECT ticker FROM web_headlines WHERE asset_id = ? ORDER BY ticker",
        (row["asset_id"],),
    ).fetchall() == [("ABC",), ("XYZ",)]
    associations = dict(conn.execute(
        "SELECT ticker,association_observed_at FROM temporal_evidence_map ORDER BY ticker"
    ))
    assert associations == {
        "ABC": "2026-10-02T00:00:00.000000+00:00",
        "XYZ": "2026-10-03T00:00:00.000000+00:00",
    }
    conn.close()


def test_retagging_stored_multichunk_body_reuses_full_version_and_records_arrival(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    url = "https://finance.yahoo.com/news/stored-multichunk"
    asset_id = "web:" + hashlib.sha256(url.encode()).hexdigest()[:32]
    original = _row(
        asset_id=asset_id,
        url=url,
        text="\n\n".join(f"Paragraph {i} " + "detail " * 500 for i in range(5)),
        tickers=["ABC"],
    )
    write_assets(conn, [original])
    original_body = conn.execute("SELECT body FROM body_versions").fetchone()[0]
    assert original_body == original["text"]
    assert conn.execute("SELECT COUNT(*) FROM web_body_chunks").fetchone()[0] > 1

    known = store_module.stored_body(conn, url)
    assert known is not None
    retagged = store_module.retag_row(known, ["XYZ"], "2026-10-03T00:00:00Z")
    assert retagged.get("_retag_only") is True
    write_assets(conn, [retagged])

    assert conn.execute("SELECT COUNT(*) FROM body_versions").fetchone()[0] == 1
    assert conn.execute("SELECT body FROM body_versions").fetchone()[0] == original_body
    assert dict(conn.execute(
        "SELECT ticker,association_observed_at FROM temporal_evidence_map ORDER BY ticker"
    )) == {
        "ABC": "2026-10-02T00:00:00.000000+00:00",
        "XYZ": "2026-10-03T00:00:00.000000+00:00",
    }
    conn.close()


def test_retagging_pre_versioned_legacy_body_does_not_invent_version_metadata(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    url = "https://finance.yahoo.com/news/legacy-multichunk"
    asset_id = "web:" + hashlib.sha256(url.encode()).hexdigest()[:32]
    original = _row(
        asset_id=asset_id,
        url=url,
        text="\n\n".join(f"Legacy paragraph {i} " + "detail " * 500 for i in range(5)),
    )
    for ordinal, chunk in enumerate(web_bodies.chunk_text(original["text"]), start=1):
        conn.execute(
            "INSERT INTO web_body_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                asset_id, ordinal, f"{asset_id}:web_body:{ordinal:04d}", chunk,
                hashlib.sha256(chunk.encode()).hexdigest(),
                original["body_sha256"], url, original["title"], "Yahoo Finance",
                original["source_class"], original["eligible_at"], original["fetched_at"],
                "LIVE_REFETCH",
            ),
        )
    conn.commit()

    known = store_module.stored_body(conn, url)
    assert known is not None
    write_assets(conn, [store_module.retag_row(known, ["XYZ"], "2026-10-03T00:00:00Z")])

    assert _count_if_table_exists(conn, "body_versions") == 0
    assert _count_if_table_exists(conn, "temporal_evidence_versions") == 0
    assert _count_if_table_exists(conn, "temporal_evidence_map") == 0
    assert conn.execute(
        "SELECT ticker FROM web_headlines WHERE asset_id=?", (asset_id,)
    ).fetchall() == [("XYZ",)]
    conn.close()


def test_refresh_lock_rejects_second_file_descriptor(tmp_path):
    db_path = tmp_path / "store.sqlite3"
    with refresh_lock(db_path):
        with pytest.raises(RefreshBusy, match="REFRESH_BUSY"):
            with refresh_lock(db_path):
                pass


def test_excluded_host_is_skipped(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    report = write_assets(conn, [_row(url="https://finnhub.io/api/news")])
    assert report.skipped_no_publisher == 1
    assert conn.execute("SELECT COUNT(*) FROM web_body_chunks").fetchone()[0] == 0
    conn.close()


def test_sec_asset_enters_append_only_filing_version_and_evidence_projection(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    row = _row(
        asset_id="web:sec-exhibit",
        url="https://www.sec.gov/Archives/edgar/data/1/0001/ex99.htm",
        publisher="ignored",
        accession="0000000001-26-000001",
        origin=None,
        source_class="issuer_disclosure",
        eligible_at="2026-10-01T15:00:00Z",
    )

    write_assets(conn, [row])

    version = conn.execute(
        "SELECT source_id,document_id,title,body,provenance FROM body_versions"
    ).fetchone()
    temporal = conn.execute(
        "SELECT kind,evidence_role,origin,sec_accession FROM temporal_evidence_map"
    ).fetchone()
    projected = conn.execute(
        "SELECT kind,evidence_class,text FROM evidence_text_projection"
    ).fetchone()
    assert version[0] == "daily:sec"
    assert version[1] == row["asset_id"]
    assert version[3] == row["text"]
    assert version[4] == "versioned"
    assert tuple(temporal) == ("filing", "evidence", None, "0000000001-26-000001")
    assert tuple(projected) == ("filing", "filing_evidence", row["text"])
    conn.close()


def test_same_second_body_revision_appends_a_distinct_immutable_version(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    original = _row(fetched_at="2026-10-02T00:00:00Z")
    write_assets(conn, [original])

    revised = {
        **original,
        "title": "Revised headline",
        "text": "Revised body.",
        "body_sha256": "b" * 64,
    }
    write_assets(conn, [revised])

    versions = conn.execute(
        "SELECT title,body,observed_at FROM body_versions ORDER BY rowid"
    ).fetchall()
    assert versions == [
        (original["title"], original["text"], "2026-10-02T00:00:00.000000+00:00"),
        ("Revised headline", "Revised body.", "2026-10-02T00:00:00.000000+00:00"),
    ]
    conn.close()


def test_untrusted_source_cannot_promote_explicit_evidence_role(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    row = _row(source_id="unregistered:provider", evidence_role="evidence")

    write_assets(conn, [row])

    assert conn.execute(
        "SELECT evidence_role,origin FROM temporal_evidence_map"
    ).fetchone() == ("lead", None)
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_text_projection"
    ).fetchone()[0] == 0
    conn.close()


def test_malformed_sec_accession_does_not_create_filing_evidence(tmp_path):
    conn = open_writer(tmp_path / "store.sqlite3")
    row = _row(
        url="https://www.sec.gov/Archives/edgar/data/1/invalid/ex99.htm",
        accession="not-an-accession",
        origin=None,
    )

    write_assets(conn, [row])

    assert conn.execute(
        "SELECT kind,evidence_role,origin,sec_accession FROM temporal_evidence_map"
    ).fetchone() == ("body", "lead", None, None)
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_text_projection"
    ).fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
@pytest.mark.parametrize("failing_writer", ["_write_media", "_write_structure"])
def test_media_or_structure_failure_rolls_back_asset_and_defers_callbacks(
    tmp_path, monkeypatch, existing, failing_writer
):
    conn = open_writer(tmp_path / "store.sqlite3")
    tracked = set()
    callbacks = []
    row = _row(image_url="https://images.example.test/a.png", image_source="og")
    if existing:
        with track_assets(tracked, callbacks.append):
            write_assets(conn, [row])
        tracked.clear()
        callbacks.clear()

    before = {
        table: _count_if_table_exists(conn, table)
        for table in (
            "web_body_chunks", "body_documents", "body_versions",
            "temporal_evidence_versions", "temporal_evidence_map",
            "evidence_text_projection", "web_asset_media",
        )
    }

    def fail(*args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(store_module, failing_writer, fail)
    failed_row = {
        **row,
        "text": "Changed body that must roll back.",
        "body_sha256": "c" * 64,
        "structure": {"title": "SEC exhibit", "units": []},
    }
    with track_assets(tracked, callbacks.append):
        with pytest.raises(RuntimeError, match="injected write failure"):
            write_assets(conn, [failed_row])

    after = {
        table: _count_if_table_exists(conn, table)
        for table in before
    }
    assert after == before
    assert tracked == set()
    assert callbacks == []
    assert not conn.in_transaction
    conn.close()


@pytest.mark.parametrize("failing_writer", ["_write_media", "_write_structure"])
def test_caller_transaction_can_rollback_after_media_or_structure_failure(
    tmp_path, monkeypatch, failing_writer
):
    conn = open_writer(tmp_path / "store.sqlite3")
    conn.execute(
        "CREATE TABLE sec_exhibit_units "
        "(url TEXT PRIMARY KEY, accession TEXT, units_json TEXT, parsed_at TEXT)"
    )
    conn.commit()
    tracked = set()
    callbacks = []

    def fail(*args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(store_module, failing_writer, fail)
    row = _row(image_url="https://images.example.test/a.png", image_source="og",
               structure={"title": "SEC exhibit", "units": []})
    with track_assets(tracked, callbacks.append):
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="injected write failure"):
            write_assets(conn, [row], commit=False)
        assert conn.in_transaction
        conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM web_body_chunks").fetchone()[0] == 0
    assert _count_if_table_exists(conn, "body_versions") == 0
    assert _count_if_table_exists(conn, "temporal_evidence_map") == 0
    assert _count_if_table_exists(conn, "evidence_text_projection") == 0
    assert _count_if_table_exists(conn, "web_asset_media") == 0
    assert conn.execute("SELECT COUNT(*) FROM sec_exhibit_units").fetchone()[0] == 0
    assert tracked == set()
    assert callbacks == []
    conn.close()


def test_build_store_returns_new_chunk_count_and_is_idempotent(tmp_path):
    db_path = tmp_path / "store.sqlite3"
    row = _row()
    chunks = len(web_bodies.chunk_text(row["text"]))
    assert web_bodies.build_store(db_path, [row]) == chunks
    assert web_bodies.build_store(db_path, [row]) == 0
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM web_body_chunks").fetchone()[0] == chunks
    conn.close()


def _sec_row(**changes):
    return _row(
        url="https://www.sec.gov/Archives/edgar/data/1/000000000026000001/ex99.htm",
        title="ABC Inc. (ABC) Form 8-K EX-99.1: 8-K", publisher="SEC filing", source_class="issuer_disclosure",
        accession="0000000000-26-000001", provider="sec", **changes,
    )


def test_a_filing_written_again_at_a_later_observation_keeps_its_first_projection(tmp_path):
    """A retried SEC intake re-writes exhibits the failed attempt already stored; the replay is idempotent."""
    conn = open_writer(tmp_path / "store.sqlite3")
    write_assets(conn, [_sec_row(fetched_at="2026-10-02T00:00:00Z")])
    write_assets(conn, [_sec_row(fetched_at="2026-10-02T02:30:00Z")])  # a retry hours later
    assert conn.execute("SELECT COUNT(*) FROM body_versions").fetchone()[0] == 1
    assert conn.execute("SELECT observed_at FROM evidence_text_projection").fetchall() == [
        ("2026-10-02T00:00:00.000000+00:00",)]
    conn.close()
