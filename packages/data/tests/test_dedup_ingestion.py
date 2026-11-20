"""Offline SQLite regressions for deterministic dedup candidate ingestion.

All records here are synthetic. The ingestion boundary receives identity fields
and T1-corrected time plus its audit reference from the caller; it must not
parse raw times, infer links from titles/accessions, or mutate source records.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

import pytest
from catalyst_data.dedup.ingestion import ensure_schema, ingest_record
from catalyst_data.dedup.links import EvidenceRecord, list_duplicate_links

CORRECTED_AT = "2026-01-02T15:04:05Z"
CORRECTION_REF = "synthetic-time-correction:fixture-1"


def _record(record_id: str, **overrides: object) -> EvidenceRecord:
    fields: dict[str, object] = {
        "record_id": record_id,
        "provider": "wire-a",
        "title": "Synthetic Acme quarterly results",
    }
    fields.update(overrides)
    return EvidenceRecord(**fields)  # type: ignore[arg-type]


def _ingest(
    conn: sqlite3.Connection,
    record: EvidenceRecord,
    *,
    store_id: str = "store-a",
    source_id: str = "source-a",
    version_id: str = "version-1",
    provider_record_id_kind: str | None = "article",
) -> None:
    ingest_record(
        conn,
        record,
        store_id=store_id,
        source_id=source_id,
        version_id=version_id,
        provider_record_id_kind=provider_record_id_kind,
        corrected_at=CORRECTED_AT,
        correction_ref=CORRECTION_REF,
    )


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE source_articles (article_key TEXT PRIMARY KEY, title TEXT NOT NULL)"
    )
    yield connection
    connection.close()


def test_provider_article_id_is_provider_scoped_and_record_identity_keeps_store_version(conn):
    conn.executemany(
        "INSERT INTO source_articles VALUES (?, ?)",
        [("doc-1", "source record one"), ("doc-2", "source record two")],
    )
    _ingest(conn, _record("doc-1", provider_record_id="wire-17"), store_id="store-one")
    _ingest(
        conn,
        _record("doc-1", provider="WIRE-A", provider_record_id="wire-17"),
        store_id="store-two",
        source_id="source-b",
        version_id="version-2",
    )
    _ingest(
        conn,
        _record("doc-3", provider="wire-b", provider_record_id="wire-17"),
        store_id="store-three",
    )

    links = list_duplicate_links(conn)

    assert len(links) == 1
    assert links[0][2:] == ("same_article", "provider_record_id")
    assert links[0][0] != links[0][1]
    assert conn.execute(
        "SELECT store_id, source_id, document_id, version_id "
        "FROM dedup_candidate_records ORDER BY store_id"
    ).fetchall() == [
        ("store-one", "source-a", "doc-1", "version-1"),
        ("store-three", "source-a", "doc-3", "version-1"),
        ("store-two", "source-b", "doc-1", "version-2"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM source_articles").fetchone()[0] == 2
    assert conn.execute(
        "SELECT title FROM source_articles WHERE article_key = 'doc-1'"
    ).fetchone() == ("source record one",)


def test_normalized_url_is_indexed_and_links_without_title_agreement(conn):
    _ingest(
        conn,
        _record("doc-a", url="https://Example.test/story/?utm_source=x#top"),
    )
    _ingest(
        conn,
        _record(
            "doc-b",
            provider="wire-b",
            title="Different headline",
            url="https://example.test/story/amp",
        ),
    )
    _ingest(
        conn,
        _record("doc-c", provider="wire-c", url="https://example.test/other"),
    )

    links = list_duplicate_links(conn)
    assert len(links) == 1
    assert links[0][0] != links[0][1]
    assert links[0][2:] == ("same_article", "canonical_url")


def test_keyless_historical_record_is_reindexed_without_trusting_legacy_provider_id(conn):
    historical = _record(
        "historical-doc",
        provider="sec",
        provider_record_id="legacy-filing-accession",
        url="https://sec.example.test/archives/historical.htm",
    )
    conn.execute(
        "CREATE TABLE dedup_candidate_records ("
        "record_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, source_id TEXT NOT NULL, "
        "document_id TEXT NOT NULL, version_id TEXT NOT NULL, provider TEXT NOT NULL, "
        "corrected_at TEXT NOT NULL, correction_ref TEXT NOT NULL, evidence_json TEXT NOT NULL, "
        "UNIQUE (store_id, source_id, document_id, version_id))"
    )
    conn.execute(
        "CREATE TABLE dedup_candidate_keys ("
        "key_kind TEXT NOT NULL, key_namespace TEXT NOT NULL, key_value TEXT NOT NULL, "
        "record_id TEXT NOT NULL, PRIMARY KEY (key_kind, key_namespace, key_value, record_id))"
    )
    record_key = "record:v1:" + json.dumps(
        ["legacy-store", "legacy-source", "historical-doc", "legacy-version"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO dedup_candidate_records "
        "(record_id, store_id, source_id, document_id, version_id, provider, "
        "corrected_at, correction_ref, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_key,
            "legacy-store",
            "legacy-source",
            "historical-doc",
            "legacy-version",
            historical.provider,
            CORRECTED_AT,
            CORRECTION_REF,
            json.dumps(asdict(historical), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")),
        ),
    )

    _ingest(
        conn,
        _record(
            "new-doc",
            provider="sec",
            provider_record_id="legacy-filing-accession",
            url="https://sec.example.test/archives/historical.htm",
        ),
        store_id="new-store",
        provider_record_id_kind="article",
    )

    assert [row[2:] for row in list_duplicate_links(conn)] == [
        ("same_article", "canonical_url")
    ]
    assert conn.execute(
        "SELECT COUNT(*) FROM dedup_candidate_keys "
        "WHERE key_kind = 'canonical_url' AND record_id = ?",
        (record_key,),
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT COUNT(*) FROM dedup_candidate_keys "
        "WHERE key_kind = 'provider_record_id' AND record_id = ?",
        (record_key,),
    ).fetchone() == (0,)


def test_candidate_key_backfill_rolls_back_and_retries_idempotently(conn):
    historical = _record(
        "historical-with-two-keys",
        provider="wire-a",
        url="https://wire.example.test/story/42",
        release_id="release-42",
        release_namespace="wire-a",
        release_id_confirmed=True,
    )
    conn.execute(
        "CREATE TABLE dedup_candidate_records ("
        "record_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, source_id TEXT NOT NULL, "
        "document_id TEXT NOT NULL, version_id TEXT NOT NULL, provider TEXT NOT NULL, "
        "corrected_at TEXT NOT NULL, correction_ref TEXT NOT NULL, evidence_json TEXT NOT NULL, "
        "UNIQUE (store_id, source_id, document_id, version_id))"
    )
    conn.execute(
        "CREATE TABLE dedup_candidate_keys ("
        "key_kind TEXT NOT NULL, key_namespace TEXT NOT NULL, key_value TEXT NOT NULL, "
        "record_id TEXT NOT NULL, PRIMARY KEY (key_kind, key_namespace, key_value, record_id))"
    )
    record_key = "record:v1:" + json.dumps(
        ["legacy-store", "legacy-source", historical.record_id, "legacy-version"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO dedup_candidate_records "
        "(record_id, store_id, source_id, document_id, version_id, provider, "
        "corrected_at, correction_ref, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_key,
            "legacy-store",
            "legacy-source",
            historical.record_id,
            "legacy-version",
            historical.provider,
            CORRECTED_AT,
            CORRECTION_REF,
            json.dumps(asdict(historical), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")),
        ),
    )
    conn.execute(
        "CREATE TRIGGER fail_candidate_backfill BEFORE INSERT ON dedup_candidate_keys "
        "WHEN NEW.key_kind = 'confirmed_release_id' BEGIN "
        "SELECT RAISE(ABORT, 'synthetic backfill failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic backfill failure"):
        ensure_schema(conn)

    assert conn.execute("SELECT COUNT(*) FROM dedup_candidate_keys").fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
        "AND name = 'dedup_ingestion_migrations'"
    ).fetchone() == (0,)
    assert "provider_record_id_kind" not in {
        row[1] for row in conn.execute("PRAGMA table_info(dedup_candidate_records)")
    }

    conn.execute("DROP TRIGGER fail_candidate_backfill")
    ensure_schema(conn)
    expected_keys = conn.execute(
        "SELECT key_kind, key_value FROM dedup_candidate_keys "
        "WHERE record_id = ? ORDER BY key_kind",
        (record_key,),
    ).fetchall()
    assert [row[0] for row in expected_keys] == ["canonical_url", "confirmed_release_id"]

    ensure_schema(conn)
    assert conn.execute(
        "SELECT key_kind, key_value FROM dedup_candidate_keys "
        "WHERE record_id = ? ORDER BY key_kind",
        (record_key,),
    ).fetchall() == expected_keys
    assert conn.execute("SELECT COUNT(*) FROM dedup_ingestion_migrations").fetchone() == (1,)


def test_reordered_duplicate_url_query_values_do_not_establish_same_identity(conn):
    _ingest(conn, _record("query-a", url="https://example.test/story?item=a&item=b"))
    _ingest(conn, _record("query-b", url="https://example.test/story?item=b&item=a"))

    assert list_duplicate_links(conn) == []


def test_only_confirmed_release_ids_and_versioned_full_body_fingerprints_match(conn):
    fingerprint = "a" * 64
    _ingest(
        conn,
        _record("release-a", release_id="R-42", release_namespace="wire-a",
                release_id_confirmed=True),
    )
    _ingest(
        conn,
        _record("release-b", provider="wire-b", release_id="R-42",
                release_namespace="wire-a", release_id_confirmed=True),
    )
    _ingest(
        conn,
        _record("release-c", provider="wire-c", release_id="R-42",
                release_namespace="wire-other", release_id_confirmed=True),
    )
    _ingest(
        conn,
        _record("body-a", body_fingerprint=fingerprint, fingerprint_version="full-v1",
                body_fingerprint_confirmed=True),
    )
    _ingest(
        conn,
        _record("body-b", provider="wire-b", body_fingerprint=fingerprint,
                fingerprint_version="full-v1", body_fingerprint_confirmed=True),
    )
    _ingest(
        conn,
        _record("body-c", provider="wire-c", body_fingerprint=fingerprint,
                fingerprint_version="full-v2", body_fingerprint_confirmed=True),
    )

    links = list_duplicate_links(conn)
    assert [(row[2], row[3]) for row in links] == [
        ("same_release", "full_body_fingerprint"),
        ("same_release", "confirmed_release_id"),
    ]


def test_titles_and_sec_accessions_do_not_establish_article_or_release_links(conn):
    _ingest(
        conn,
        _record("sec:0001:ex99-1", provider="sec", title="Acme announces results"),
    )
    _ingest(
        conn,
        _record("sec:0001:ex99-2", provider="sec", title="Acme announces results"),
    )
    _ingest(
        conn,
        _record("sec:0002:ex99-1", provider="sec", title="Acme announces results",
                release_id="0002", release_namespace="sec-accession"),
    )
    _ingest(
        conn,
        _record("press:acme:1", provider="press", title="Acme announces results",
                release_id="0002", release_namespace="sec-accession"),
    )

    assert list_duplicate_links(conn) == []


def test_sec_accession_identifier_kind_is_not_an_article_id(conn):
    accession = "0000123456-26-000001"
    _ingest(
        conn,
        _record("sec:attachment:ex99-1", provider="sec", provider_record_id=accession),
        provider_record_id_kind="filing_accession",
    )
    _ingest(
        conn,
        _record("sec:attachment:ex99-2", provider="SEC", provider_record_id=accession),
        store_id="store-b",
        provider_record_id_kind="filing_accession",
    )

    assert list_duplicate_links(conn) == []


def test_accession_id_is_not_used_when_another_candidate_key_matches(conn):
    accession = "0000123456-26-000001"
    url = "https://sec.example.test/filing/ex99-1.htm"
    _ingest(
        conn,
        _record(
            "sec:filing:a",
            provider="sec",
            provider_record_id=accession,
            url=url,
        ),
        provider_record_id_kind="filing_accession",
    )
    _ingest(
        conn,
        _record(
            "sec:filing:b",
            provider="SEC",
            provider_record_id=accession,
            url=url,
        ),
        store_id="store-b",
        provider_record_id_kind="filing_accession",
    )

    assert [row[2:] for row in list_duplicate_links(conn)] == [
        ("same_article", "canonical_url")
    ]


def test_sec_concrete_attachment_article_id_can_link(conn):
    attachment_id = "0000123456-26-000001/ex99-1.htm"
    _ingest(
        conn,
        _record("sec:version:a", provider="sec", provider_record_id=attachment_id),
        provider_record_id_kind="article",
    )
    _ingest(
        conn,
        _record("sec:version:b", provider="SEC", provider_record_id=attachment_id),
        store_id="store-b",
        provider_record_id_kind="article",
    )

    links = list_duplicate_links(conn)
    assert len(links) == 1
    assert links[0][2:] == ("same_article", "provider_record_id")


def test_provider_id_requires_explicit_identity_kind(conn):
    with pytest.raises(ValueError, match="provider_record_id_kind is required"):
        ingest_record(
            conn,
            _record("missing-kind", provider_record_id="some-id"),
            store_id="store-a",
            source_id="source-a",
            version_id="version-1",
            corrected_at=CORRECTED_AT,
            correction_ref=CORRECTION_REF,
        )


def test_unconfirmed_hashes_and_empty_identity_values_do_not_create_candidate_keys(conn):
    _ingest(conn, _record("a", provider_record_id="", url="", release_id=""))
    _ingest(conn, _record("b", provider_record_id="", url="", release_id=""))
    _ingest(
        conn,
        _record("c", body_fingerprint="b" * 64, fingerprint_version="full-v1"),
    )
    _ingest(
        conn,
        _record("d", provider="wire-b", body_fingerprint="b" * 64,
                fingerprint_version="full-v1"),
    )

    assert list_duplicate_links(conn) == []


def test_candidate_lookup_has_a_composite_sqlite_index_and_only_links_matching_records(conn):
    for index in range(40):
        _ingest(
            conn,
            _record(f"unrelated-{index}", provider_record_id=f"id-{index}"),
        )
    _ingest(conn, _record("match-a", provider_record_id="target-id"))
    _ingest(
        conn,
        _record("match-b", provider="wire-a", provider_record_id="target-id"),
    )

    indexes = conn.execute("PRAGMA index_list('dedup_candidate_keys')").fetchall()
    index_plans = []
    for row in indexes:
        index_name = row[1]
        columns = [
            info[2]
            for info in conn.execute(
                f"PRAGMA index_info('{index_name}')"
            ).fetchall()
        ]
        if columns[:3] == ["key_kind", "key_namespace", "key_value"]:
            index_plans.append(
                conn.execute(
                    "EXPLAIN QUERY PLAN SELECT record_id FROM dedup_candidate_keys "
                    "WHERE key_kind = ? AND key_namespace = ? AND key_value = ?",
                    ("provider_record_id", "wire-a", "target-id"),
                ).fetchall()
            )

    assert index_plans
    assert all("SCAN dedup_candidate_keys" not in str(plan) for plan in index_plans)
    assert len(list_duplicate_links(conn)) == 1
    assert list_duplicate_links(conn)[0][2:] == ("same_article", "provider_record_id")


def test_corrected_time_and_audit_reference_are_required_at_ingestion_boundary(conn):
    record = _record("doc-1", provider_record_id="p-1")

    with pytest.raises(TypeError):
        ingest_record(
            conn,
            record,
            store_id="store-a",
            source_id="source-a",
            version_id="version-1",
        )

    _ingest(conn, record)

    stored = conn.execute(
        "SELECT corrected_at, correction_ref FROM dedup_candidate_records"
    ).fetchone()
    assert stored == (CORRECTED_AT, CORRECTION_REF)


def test_reingesting_the_same_version_is_idempotent_and_does_not_commit_callers_work(conn):
    conn.execute("INSERT INTO source_articles VALUES ('before', 'pending caller write')")
    record = _record("doc-1", provider_record_id="p-1")

    _ingest(conn, record)
    first_links = list_duplicate_links(conn)
    _ingest(conn, record)
    second_links = list_duplicate_links(conn)

    assert first_links == second_links == []
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM source_articles").fetchone()[0] == 0


def test_same_store_source_document_version_with_different_content_is_rejected(conn):
    _ingest(
        conn,
        _record("doc-1", provider_record_id="provider-id-old"),
        store_id="store-a",
        source_id="source-a",
        version_id="version-1",
    )

    with pytest.raises(ValueError, match="identity collision"):
        _ingest(
            conn,
            _record("doc-1", provider_record_id="provider-id-changed"),
            store_id="store-a",
            source_id="source-a",
            version_id="version-1",
        )


def test_corrected_time_must_have_an_auditable_reference(conn):
    with pytest.raises(ValueError, match="correction_ref"):
        ingest_record(
            conn,
            _record("doc-1", provider_record_id="p-1"),
            store_id="store-a",
            source_id="source-a",
            version_id="version-1",
            provider_record_id_kind="article",
            corrected_at=CORRECTED_AT,
            correction_ref="",
        )
