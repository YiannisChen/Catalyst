"""Synthetic manifest-pinned tests for outbox-to-catalog projection."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from catalyst_data.dedup import links, outbox
from catalyst_data.dedup.links import EvidenceRecord
from catalyst_data.ingestion.input_manifest import PreflightError
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage import body_versions, time_corrections
from catalyst_data.storage.snapshots import (
    load_verified_stores,
    readonly_snapshot,
)

INVOCATION = ["python", "-m", "catalyst_data.dedup.outbox"]
NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
NOW_TEXT = "2026-10-27T12:00:00.000000+00:00"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count_if_table_exists(conn: sqlite3.Connection, table: str) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return 0 if exists is None else conn.execute(
        f"SELECT count(*) FROM {table}"
    ).fetchone()[0]


def _new_source(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    body_versions.ensure_schema(conn)
    outbox.ensure_schema(conn)
    conn.commit()
    return conn


def _append_candidate(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    provider: str = "alpaca",
    provider_record_id: str | None = None,
    provider_record_id_kind: str | None = None,
    url: str | None = None,
    body: str = "Synthetic complete article body.",
    fingerprint_confirmed: bool = False,
    body_fingerprint: str | None = None,
    published_at: datetime | None = NOW,
    corrected_at: str = NOW_TEXT,
    time_evidence: dict | None = None,
) -> str:
    source_id = "synthetic_news"
    version_id = body_versions.put_version(
        conn,
        source_id=source_id,
        document_id=document_id,
        title=f"Synthetic title for {document_id}",
        body=body,
        observed_at=NOW,
        published_at=published_at,
    )
    record = EvidenceRecord(
        record_id=document_id,
        provider=provider,
        provider_record_id=provider_record_id,
        url=url,
        body_fingerprint=body_fingerprint or hashlib.sha256(body.encode()).hexdigest(),
        fingerprint_version="sha256-full-body-v1",
        body_fingerprint_confirmed=fingerprint_confirmed,
        title=f"Synthetic title for {document_id}",
    )
    return outbox.enqueue_candidate(
        conn,
        record,
        source_id=source_id,
        source_version_id=version_id,
        provider_record_id_kind=provider_record_id_kind,
        corrected_at=corrected_at,
        time_evidence=time_evidence or {
            "kind": "source_field",
            "field": "published_at",
            "version_id": version_id,
        },
    )


def _snapshot(source_path: Path, snapshot_path: Path) -> Path:
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(snapshot_path)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    return snapshot_path


def _verified(
    tmp_path: Path,
    snapshot_path: Path,
    *,
    store_id: str = "alpaca-store-v1",
    extra_files: tuple[Path, ...] = (),
    manifest_name: str | None = None,
):
    pinned_files = (snapshot_path, *extra_files)
    manifest_path = tmp_path / (manifest_name or f"{store_id}-inputs.json")
    manifest_path.write_text(json.dumps({
        "schema_version": 1,
        "invocation": INVOCATION,
        "files": [
            {"path": str(path.resolve()), "sha256": _sha(path)}
            for path in pinned_files
        ],
        "environment": {},
    }, sort_keys=True))
    verified = load_verified_stores(
        manifest_path,
        invocation=INVOCATION,
        required_stores={store_id: snapshot_path.resolve()},
        environ={},
    )
    return verified


def test_source_body_version_and_outbox_event_rollback_together(tmp_path):
    source = _new_source(tmp_path / "source.sqlite3")
    source.execute("BEGIN")

    _append_candidate(
        source,
        document_id="article-atomic",
        provider_record_id="alpaca-article-atomic",
        provider_record_id_kind="article",
        url="https://news.example.test/atomic",
    )
    source.rollback()

    assert _count_if_table_exists(source, "body_versions") == 0
    assert _count_if_table_exists(source, "dedup_relation_outbox") == 0


def test_projection_uses_real_verified_pin_and_leaves_source_snapshot_unchanged(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    _append_candidate(
        source,
        document_id="article-pinned",
        provider_record_id="alpaca-article-pinned",
        provider_record_id_kind="article",
        url="https://news.example.test/pinned",
    )
    source.commit()
    source.close()
    snapshot_path = _snapshot(source_path, tmp_path / "source.snapshot.sqlite3")
    pinned_sha = _sha(snapshot_path)
    verified = _verified(tmp_path, snapshot_path)
    catalog = sqlite3.connect(tmp_path / "relations.sqlite3", isolation_level=None)

    projected = outbox.project_verified_snapshot(
        verified, "alpaca-store-v1", catalog
    )

    assert projected == 1
    assert verified.stores["alpaca-store-v1"].sha256 == pinned_sha
    assert _sha(snapshot_path) == pinned_sha
    with readonly_snapshot(verified, verified.stores["alpaca-store-v1"]) as source_read:
        assert source_read.execute("PRAGMA query_only").fetchone()[0] == 1
        assert source_read.execute("SELECT count(*) FROM dedup_relation_outbox").fetchone()[0] == 1


def test_catalog_projection_is_atomic_on_failure_and_idempotent_on_replay(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    for document_id in ("article-a", "article-b"):
        _append_candidate(
            source,
            document_id=document_id,
            provider="shared-provider",
            provider_record_id="shared-article-55",
            provider_record_id_kind="article",
            url=f"https://{document_id}.example.test/story",
        )
    source.commit()
    source.close()
    snapshot_path = _snapshot(source_path, tmp_path / "source.snapshot.sqlite3")
    verified = _verified(tmp_path, snapshot_path)
    catalog = sqlite3.connect(tmp_path / "relations.sqlite3", isolation_level=None)
    links.ensure_schema(catalog)
    catalog.commit()
    catalog.execute(
        "CREATE TRIGGER reject_link BEFORE INSERT ON duplicate_links "
        "BEGIN SELECT RAISE(ABORT, 'synthetic catalog failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic catalog failure"):
        outbox.project_verified_snapshot(verified, "alpaca-store-v1", catalog)
    assert _count_if_table_exists(catalog, "dedup_candidate_records") == 0
    assert _count_if_table_exists(catalog, "duplicate_links") == 0

    catalog.execute("DROP TRIGGER reject_link")
    assert outbox.project_verified_snapshot(verified, "alpaca-store-v1", catalog) == 2
    assert outbox.project_verified_snapshot(verified, "alpaca-store-v1", catalog) == 0
    assert catalog.execute("SELECT count(*) FROM dedup_candidate_records").fetchone()[0] == 2
    assert catalog.execute("SELECT count(*) FROM duplicate_links").fetchone()[0] == 1


def test_projection_rejects_a_forged_or_unlisted_store_pin(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    _append_candidate(source, document_id="article-pin", url="https://news.example.test/pin")
    source.commit()
    source.close()
    snapshot_path = _snapshot(source_path, tmp_path / "source.snapshot.sqlite3")
    verified = _verified(tmp_path, snapshot_path)
    catalog = sqlite3.connect(tmp_path / "relations.sqlite3", isolation_level=None)

    forged_pin = replace(verified.stores["alpaca-store-v1"], sha256="0" * 64)
    forged = replace(verified, stores={"alpaca-store-v1": forged_pin})
    with pytest.raises(PreflightError):
        outbox.project_verified_snapshot(forged, "alpaca-store-v1", catalog)
    with pytest.raises(ValueError, match="verified|store|manifest"):
        outbox.project_verified_snapshot(verified, "unlisted-store", catalog)
    assert _count_if_table_exists(catalog, "dedup_candidate_records") == 0


def test_provider_id_is_used_only_when_explicitly_typed_as_article(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    _append_candidate(
        source,
        document_id="article-provider-id",
        provider="shared-provider",
        provider_record_id="explicit-article-id",
        provider_record_id_kind="article",
        url="https://news.example.test/id",
    )
    _append_candidate(
        source,
        document_id="filing-accession",
        provider="sec",
        provider_record_id="0000000001-26-000001",
        provider_record_id_kind="filing_accession",
        url="https://www.sec.gov/Archives/edgar/data/1/fixture.htm",
    )
    _append_candidate(
        source,
        document_id="url-only-document",
        provider="wire",
        provider_record_id=None,
        provider_record_id_kind=None,
        url="https://wire.example.test/story/123",
    )
    source.commit()
    source.close()
    snapshot_path = _snapshot(source_path, tmp_path / "source.snapshot.sqlite3")
    verified = _verified(tmp_path, snapshot_path)
    catalog = sqlite3.connect(tmp_path / "relations.sqlite3", isolation_level=None)

    outbox.project_verified_snapshot(verified, "alpaca-store-v1", catalog)

    keys = catalog.execute(
        "SELECT key_kind, key_namespace, key_value FROM dedup_candidate_keys "
        "WHERE key_kind = 'provider_record_id' ORDER BY key_value"
    ).fetchall()
    assert keys == [("provider_record_id", "shared-provider", "explicit-article-id")]
    accession = catalog.execute(
        "SELECT provider_record_id_kind, evidence_json FROM dedup_candidate_records "
        "WHERE document_id = 'filing-accession'"
    ).fetchone()
    assert accession[0] == "filing_accession"
    accession_evidence = json.loads(accession[1])
    assert accession_evidence["provider_record_id"] == "0000000001-26-000001"
    url_only = catalog.execute(
        "SELECT evidence_json FROM dedup_candidate_records WHERE document_id = 'url-only-document'"
    ).fetchone()[0]
    assert json.loads(url_only)["provider_record_id"] is None


def test_confirmed_fingerprint_must_match_complete_persisted_body(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    body = "Synthetic complete article body."
    _append_candidate(
        source,
        document_id="complete-body",
        provider_record_id="article-complete-body",
        provider_record_id_kind="article",
        body=body,
        fingerprint_confirmed=True,
    )
    source.commit()
    source.execute("BEGIN")
    with pytest.raises(ValueError, match="fingerprint|body hash"):
        _append_candidate(
            source,
            document_id="wrong-body-hash",
            provider_record_id="article-wrong-body-hash",
            provider_record_id_kind="article",
            body=body,
            fingerprint_confirmed=True,
            body_fingerprint="0" * 64,
        )
    source.rollback()
    assert source.execute("SELECT count(*) FROM body_versions").fetchone()[0] == 1
    assert source.execute("SELECT count(*) FROM dedup_relation_outbox").fetchone()[0] == 1
    source.close()
    snapshot_path = _snapshot(source_path, tmp_path / "source.snapshot.sqlite3")
    verified = _verified(tmp_path, snapshot_path)
    catalog = sqlite3.connect(tmp_path / "relations.sqlite3", isolation_level=None)

    outbox.project_verified_snapshot(verified, "alpaca-store-v1", catalog)

    assert catalog.execute(
        "SELECT key_kind, key_value FROM dedup_candidate_keys "
        "WHERE key_kind = 'full_body_fingerprint'"
    ).fetchall() == [
        ("full_body_fingerprint", hashlib.sha256(body.encode()).hexdigest())
    ]


def _sec_correction_csv(store_id: str) -> bytes:
    return (
        "accession,cik,form,items,json_acceptance_utc,edgar_accepted_et,"
        "edgar_accepted_utc,json_minus_edgar_h,in_stores\n"
        f"0000000001-26-000001,1,8-K,,{NOW_TEXT},,2026-10-27T11:00:00Z,1,{store_id}\n"
    ).encode()


def test_corrected_time_requires_a_correction_row_pinned_by_the_same_manifest(tmp_path):
    store_id = "sec-store-v1"
    csv_bytes = _sec_correction_csv(store_id)
    csv_path = tmp_path / "sec_acceptance_v1.csv"
    csv_path.write_bytes(csv_bytes)
    table = time_corrections.load_corrections(
        csv_bytes,
        kind="sec",
        filename=csv_path.name,
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )

    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    source_id = "synthetic_news"
    version_id = body_versions.put_version(
        source,
        source_id=source_id,
        document_id="sec-filing-document",
        title="Synthetic SEC filing",
        body="Synthetic complete filing body.",
        observed_at=NOW,
        published_at=NOW,
    )
    record = EvidenceRecord(
        record_id="sec-filing-document",
        provider="sec",
        provider_record_id="0000000001-26-000001",
        url="https://www.sec.gov/Archives/edgar/data/1/fixture.htm",
        title="Synthetic SEC filing",
    )
    outbox.enqueue_candidate(
        source,
        record,
        source_id=source_id,
        source_version_id=version_id,
        provider_record_id_kind="filing_accession",
        corrected_at="2026-10-27T11:00:00Z",
        time_evidence={
            "kind": "correction_table",
            "table_id": table.table_id,
            "sha256": table.sha256,
            "accession": "0000000001-26-000001",
            "current_at": NOW_TEXT,
        },
    )
    source.commit()
    source.close()
    snapshot_path = _snapshot(source_path, tmp_path / "source.snapshot.sqlite3")
    verified = _verified(
        tmp_path,
        snapshot_path,
        store_id=store_id,
        extra_files=(csv_path,),
    )
    catalog = sqlite3.connect(tmp_path / "relations.sqlite3", isolation_level=None)

    assert outbox.project_verified_snapshot(
        verified, store_id, catalog, correction_tables=(table,)
    ) == 1
    corrected_at, correction_ref = catalog.execute(
        "SELECT corrected_at, correction_ref FROM dedup_candidate_records"
    ).fetchone()
    assert timefields.stored(corrected_at) == timefields.stored("2026-10-27T11:00:00Z")
    assert table.table_id in correction_ref
    assert table.sha256 in correction_ref

    unpinned = _verified(
        tmp_path,
        snapshot_path,
        store_id=store_id,
        manifest_name="manifest-without-correction.csv.json",
    )
    unpinned_catalog = sqlite3.connect(
        tmp_path / "relations-unpinned.sqlite3", isolation_level=None
    )
    with pytest.raises((PreflightError, ValueError), match="pinned|manifest|correction"):
        outbox.project_verified_snapshot(
            unpinned,
            store_id,
            unpinned_catalog,
            correction_tables=(table,),
        )


@pytest.mark.parametrize(
    ("provider_record_id_kind", "provider_record_id"),
    [
        ("article", "0000000001-26-000001"),
        ("filing_accession", "0000000001-26-000099"),
    ],
)
def test_sec_correction_accession_must_match_typed_record_identity(
    tmp_path, provider_record_id_kind, provider_record_id
):
    store_id = "sec-store-v1"
    csv_bytes = _sec_correction_csv(store_id)
    csv_path = tmp_path / "sec_acceptance_v1.csv"
    csv_path.write_bytes(csv_bytes)
    table = time_corrections.load_corrections(
        csv_bytes,
        kind="sec",
        filename=csv_path.name,
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )

    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    source_id = "synthetic_news"
    document_id = "sec-filing-document"
    title = "Synthetic SEC filing"
    version_id = body_versions.put_version(
        source,
        source_id=source_id,
        document_id=document_id,
        title=title,
        body="Synthetic complete filing body.",
        observed_at=NOW,
        published_at=NOW,
    )
    record = EvidenceRecord(
        record_id=document_id,
        provider="sec",
        provider_record_id=provider_record_id,
        url="https://www.sec.gov/Archives/edgar/data/1/fixture.htm",
        title=title,
    )
    with pytest.raises(ValueError, match="accession|filing|identity|record"):
        outbox.enqueue_candidate(
            source,
            record,
            source_id=source_id,
            source_version_id=version_id,
            provider_record_id_kind=provider_record_id_kind,
            corrected_at="2026-10-27T11:00:00Z",
            time_evidence={
                "kind": "correction_table",
                "table_id": table.table_id,
                "sha256": table.sha256,
                "accession": "0000000001-26-000001",
                "current_at": NOW_TEXT,
            },
        )
    source.rollback()
    source.close()


def test_finnhub_correction_url_must_match_record_url_after_normalization(tmp_path):
    store_id = "finnhub-store-v1"
    csv_bytes = (
        "url,publisher,stored_eligible_at,corrected_at,offset_h,basis,stores\n"
        f"https://wire.example.test/story/123,Benzinga,{NOW_TEXT},"
        f"2026-10-27T11:00:00Z,1,synthetic,{store_id}\n"
    ).encode()
    csv_path = tmp_path / "finnhub_corrections_v1.csv"
    csv_path.write_bytes(csv_bytes)
    table = time_corrections.load_corrections(
        csv_bytes,
        kind="finnhub",
        filename=csv_path.name,
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )

    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    source_id = "synthetic_news"
    document_id = "finnhub-article"
    title = "Synthetic Finnhub article"
    version_id = body_versions.put_version(
        source,
        source_id=source_id,
        document_id=document_id,
        title=title,
        body="Synthetic complete article body.",
        observed_at=NOW,
        published_at=NOW,
    )
    record = EvidenceRecord(
        record_id=document_id,
        provider="finnhub",
        url="https://other-wire.example.test/story/123",
        title=title,
    )
    with pytest.raises(ValueError, match="URL|url|record|identity"):
        outbox.enqueue_candidate(
            source,
            record,
            source_id=source_id,
            source_version_id=version_id,
            provider_record_id_kind=None,
            corrected_at="2026-10-27T11:00:00Z",
            time_evidence={
                "kind": "correction_table",
                "table_id": table.table_id,
                "sha256": table.sha256,
                "url": "https://wire.example.test/story/123?utm_source=fixture",
                "publisher": "Benzinga",
                "current_at": NOW_TEXT,
            },
        )
    source.rollback()
    source.close()


@pytest.mark.parametrize("claimed_publisher", [None, "Reuters", "Benzinga"])
def test_finnhub_correction_requires_verified_source_specific_publisher(
    tmp_path, claimed_publisher
):
    store_id = "finnhub-store-v1"
    csv_bytes = (
        "url,publisher,stored_eligible_at,corrected_at,offset_h,basis,stores\n"
        f"https://wire.example.test/story/123,Benzinga,{NOW_TEXT},"
        f"2026-10-27T11:00:00Z,1,synthetic,{store_id}\n"
    ).encode()
    csv_path = tmp_path / "finnhub_corrections_v1.csv"
    csv_path.write_bytes(csv_bytes)
    table = time_corrections.load_corrections(
        csv_bytes,
        kind="finnhub",
        filename=csv_path.name,
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )

    source = _new_source(tmp_path / "source.sqlite3")
    source_id = "synthetic_news"
    document_id = "finnhub-publisher-article"
    version_id = body_versions.put_version(
        source,
        source_id=source_id,
        document_id=document_id,
        title="Synthetic Finnhub article",
        body="Synthetic complete article body.",
        observed_at=NOW,
        published_at=NOW,
    )
    if claimed_publisher is not None:
        # A caller-created table can claim either the correct or wrong name,
        # but is not a trusted source-specific publisher binding.
        source.execute(
            "CREATE TABLE dedup_source_version_publishers ("
            "source_id TEXT, source_version_id TEXT, publisher TEXT)"
        )
        source.execute(
            "INSERT INTO dedup_source_version_publishers VALUES (?,?,?)",
            (source_id, version_id, claimed_publisher),
        )
    record = EvidenceRecord(
        record_id=document_id,
        provider="finnhub",
        url="https://wire.example.test/story/123",
        title="Synthetic Finnhub article",
    )

    with pytest.raises(ValueError, match="publisher|source version|identity"):
        outbox.enqueue_candidate(
            source,
            record,
            source_id=source_id,
            source_version_id=version_id,
            provider_record_id_kind=None,
            corrected_at="2026-10-27T11:00:00Z",
            time_evidence={
                "kind": "correction_table",
                "table_id": table.table_id,
                "sha256": table.sha256,
                "url": "https://wire.example.test/story/123",
                "publisher": "Benzinga",
                "current_at": NOW_TEXT,
            },
        )


def test_finnhub_correction_table_cannot_be_applied_to_another_provider(tmp_path):
    store_id = "alpaca-store-v1"
    csv_bytes = (
        "url,publisher,stored_eligible_at,corrected_at,offset_h,basis,stores\n"
        f"https://wire.example.test/story/123,Benzinga,{NOW_TEXT},"
        f"2026-10-27T11:00:00Z,1,synthetic,{store_id}\n"
    ).encode()
    csv_path = tmp_path / "finnhub_corrections_v1.csv"
    csv_path.write_bytes(csv_bytes)
    table = time_corrections.load_corrections(
        csv_bytes,
        kind="finnhub",
        filename=csv_path.name,
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )

    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    source_id = "synthetic_news"
    document_id = "other-provider-article"
    version_id = body_versions.put_version(
        source,
        source_id=source_id,
        document_id=document_id,
        title="Synthetic article from another provider",
        body="Synthetic complete article body.",
        observed_at=NOW,
        published_at=NOW,
    )
    with pytest.raises(ValueError, match="Finnhub|provider|correction"):
        outbox.enqueue_candidate(
            source,
            EvidenceRecord(
                record_id=document_id,
                provider="alpaca",
                url="https://wire.example.test/story/123",
                title="Synthetic article from another provider",
            ),
            source_id=source_id,
            source_version_id=version_id,
            provider_record_id_kind=None,
            corrected_at="2026-10-27T11:00:00Z",
            time_evidence={
                "kind": "correction_table",
                "table_id": table.table_id,
                "sha256": table.sha256,
                "url": "https://wire.example.test/story/123",
                "publisher": "Benzinga",
                "current_at": NOW_TEXT,
            },
        )
    source.rollback()
    source.close()


def test_projection_rejects_tampered_finnhub_correction_even_with_recomputed_digest(
    tmp_path,
):
    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    source_id = "synthetic_news"
    document_id = "tampered-provider-article"
    version_id = body_versions.put_version(
        source,
        source_id=source_id,
        document_id=document_id,
        title="Synthetic article from another provider",
        body="Synthetic complete article body.",
        observed_at=NOW,
        published_at=NOW,
    )
    outbox.enqueue_candidate(
        source,
        EvidenceRecord(
            record_id=document_id,
            provider="alpaca",
            url="https://wire.example.test/story/123",
            title="Synthetic article from another provider",
        ),
        source_id=source_id,
        source_version_id=version_id,
        provider_record_id_kind=None,
        corrected_at=NOW_TEXT,
        time_evidence={
            "kind": "source_field",
            "field": "published_at",
            "version_id": version_id,
        },
    )
    source.commit()
    source.close()

    snapshot_path = _snapshot(source_path, tmp_path / "source.snapshot.sqlite3")
    snapshot = sqlite3.connect(snapshot_path)
    event = snapshot.execute(
        "SELECT event_id,source_id,source_version_id,record_json,"
        "provider_record_id_kind,corrected_at,time_evidence_json "
        "FROM dedup_relation_outbox"
    ).fetchone()
    _, event_source, event_version, record_json, provider_kind, corrected, _ = event
    digest = "a" * 64
    tampered_evidence = json.dumps(
        {
            "current_at": NOW_TEXT,
            "kind": "correction_table",
            "publisher": "Benzinga",
            "sha256": digest,
            "table_id": f"finnhub_corrections_v1.csv:{digest}",
            "url": "https://wire.example.test/story/123",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = {
        "source_id": event_source,
        "source_version_id": event_version,
        "record_json": record_json,
        "provider_record_id_kind": provider_kind,
        "corrected_at": corrected,
        "time_evidence_json": tampered_evidence,
    }
    tampered_event_id = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    snapshot.execute("DROP TRIGGER dedup_relation_outbox_no_update")
    snapshot.execute(
        "UPDATE dedup_relation_outbox SET event_id=?,time_evidence_json=?",
        (tampered_event_id, tampered_evidence),
    )
    snapshot.commit()
    snapshot.close()

    verified = _verified(tmp_path, snapshot_path)
    catalog = sqlite3.connect(tmp_path / "relations.sqlite3", isolation_level=None)
    with pytest.raises(ValueError, match="Finnhub|publisher|correction"):
        outbox.project_verified_snapshot(verified, "alpaca-store-v1", catalog)
    assert _count_if_table_exists(catalog, "dedup_candidate_records") == 0


def test_source_field_time_evidence_is_audited_as_source_assertion(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    source_id = "synthetic_news"
    document_id = "source-assertion-article"
    version_id = body_versions.put_version(
        source,
        source_id=source_id,
        document_id=document_id,
        title="Synthetic source assertion",
        body="Synthetic complete source assertion body.",
        observed_at=NOW,
        published_at=NOW,
    )
    outbox.enqueue_candidate(
        source,
        EvidenceRecord(
            record_id=document_id,
            provider="alpaca",
            provider_record_id="alpaca-source-assertion",
            url="https://news.example.test/source-assertion",
            title="Synthetic source assertion",
        ),
        source_id=source_id,
        source_version_id=version_id,
        provider_record_id_kind="article",
        corrected_at=NOW_TEXT,
        time_evidence={
            "kind": "source_field",
            "field": "published_at",
            "version_id": version_id,
        },
    )
    source.commit()
    source.close()
    snapshot_path = _snapshot(source_path, tmp_path / "source.snapshot.sqlite3")
    verified = _verified(tmp_path, snapshot_path)
    catalog = sqlite3.connect(tmp_path / "relations.sqlite3", isolation_level=None)

    assert outbox.project_verified_snapshot(verified, "alpaca-store-v1", catalog) == 1
    audit_json = catalog.execute(
        "SELECT correction_ref FROM dedup_candidate_records"
    ).fetchone()[0]
    audit = json.loads(audit_json)
    assert audit["kind"] == "source_assertion"
    assert "as_of" not in audit


def test_projection_revalidates_tampered_correction_even_with_recomputed_event_digest(
    tmp_path,
):
    store_id = "sec-store-v1"
    csv_bytes = _sec_correction_csv(store_id)
    csv_path = tmp_path / "sec_acceptance_v1.csv"
    csv_path.write_bytes(csv_bytes)
    table = time_corrections.load_corrections(
        csv_bytes,
        kind="sec",
        filename=csv_path.name,
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )

    source_path = tmp_path / "source.sqlite3"
    source = _new_source(source_path)
    source_id = "synthetic_news"
    document_id = "sec-filing-document"
    version_id = body_versions.put_version(
        source,
        source_id=source_id,
        document_id=document_id,
        title="Synthetic SEC filing",
        body="Synthetic complete filing body.",
        observed_at=NOW,
        published_at=NOW,
    )
    record = EvidenceRecord(
        record_id=document_id,
        provider="sec",
        provider_record_id="0000000001-26-000001",
        url="https://www.sec.gov/Archives/edgar/data/1/fixture.htm",
        title="Synthetic SEC filing",
    )
    outbox.enqueue_candidate(
        source,
        record,
        source_id=source_id,
        source_version_id=version_id,
        provider_record_id_kind="filing_accession",
        corrected_at="2026-10-27T11:00:00Z",
        time_evidence={
            "kind": "correction_table",
            "table_id": table.table_id,
            "sha256": table.sha256,
            "accession": "0000000001-26-000001",
            "current_at": NOW_TEXT,
        },
    )
    source.commit()
    source.close()

    snapshot_path = _snapshot(source_path, tmp_path / "source.snapshot.sqlite3")
    snapshot = sqlite3.connect(snapshot_path)
    event = snapshot.execute(
        "SELECT event_id,source_id,source_version_id,record_json,"
        "provider_record_id_kind,corrected_at,time_evidence_json "
        "FROM dedup_relation_outbox"
    ).fetchone()
    _, event_source, version, record_json, provider_kind, corrected, _ = event
    tampered_evidence = json.dumps(
        {
            "accession": "0000000001-26-000099",
            "current_at": NOW_TEXT,
            "kind": "correction_table",
            "sha256": table.sha256,
            "table_id": table.table_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest_payload = {
        "source_id": event_source,
        "source_version_id": version,
        "record_json": record_json,
        "provider_record_id_kind": provider_kind,
        "corrected_at": corrected,
        "time_evidence_json": tampered_evidence,
    }
    tampered_event_id = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    snapshot.execute("DROP TRIGGER dedup_relation_outbox_no_update")
    snapshot.execute(
        "UPDATE dedup_relation_outbox SET event_id=?,time_evidence_json=?",
        (tampered_event_id, tampered_evidence),
    )
    snapshot.commit()
    snapshot.close()

    verified = _verified(
        tmp_path,
        snapshot_path,
        store_id=store_id,
        extra_files=(csv_path,),
    )
    catalog = sqlite3.connect(tmp_path / "relations.sqlite3", isolation_level=None)
    with pytest.raises(ValueError, match="accession|identity"):
        outbox.project_verified_snapshot(
            verified, store_id, catalog, correction_tables=(table,)
        )
    assert _count_if_table_exists(catalog, "dedup_candidate_records") == 0
