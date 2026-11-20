from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

import pytest
from catalyst_data.retrieval import web_bodies
from catalyst_data.storage import body_versions, legacy_import, temporal_evidence
from catalyst_data.storage.legacy_cohorts import LegacyCohort, LegacyRecord
from catalyst_data.storage.legacy_read import validate_legacy_version

CAPTURED = "2026-10-26T16:30:00.000000+00:00"
ELIGIBLE = "2026-03-02T20:00:00.000000+00:00"
TITLE = "Original article title"
TEXT = "first original paragraph"
CORRECTIONS = ({"table_id": "corrections-2026", "sha256": "5" * 64,
                "store_id": "daily-snapshot"},)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _chunk_fixture(tmp_path, *, role="evidence", correction_bindings=()):
    source = sqlite3.connect(tmp_path / "source.sqlite3")
    source.executescript(web_bodies.SCHEMA)
    source.execute(
        "INSERT INTO web_body_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("asset-1", 0, "chunk-1", TEXT, _sha(TEXT), _sha("whole original body"),
         "https://example.test/article", TITLE, "Example News", "reported_news", ELIGIBLE,
         "2026-03-02T20:05:00Z", "LIVE_REFETCH"),
    )
    source.execute("INSERT INTO web_headlines VALUES (?,?,?)", ("asset-1", "ACME", ELIGIBLE))
    source.commit()

    member = LegacyRecord(
        source_id="daily:legacy", document_id="document:chunk-1", evidence_id="evidence:article",
        kind="body", tickers=("ACME",), evidence_role=role, eligible_at=ELIGIBLE,
        representation="legacy_chunk", text_sha256=_sha(TEXT), title_sha256=_sha(TITLE),
        asset_id="asset-1", chunk_id="chunk-1", ordinal=0,
        original_body_sha256=_sha("whole original body"),
    )
    source_sha = hashlib.sha256((tmp_path / "source.sqlite3").read_bytes()).hexdigest()
    cohort = LegacyCohort(
        cohort_id="daily-original-v1", manifest_sha256="1" * 64,
        input_manifest_sha256="2" * 64, source_store_id="daily-snapshot",
        source_snapshot_sha256=source_sha, snapshot_captured_at=CAPTURED,
        source_tables=("web_body_chunks", "web_headlines"),
        eligibility_rule_id="daily-original-eligibility", eligibility_rule_version="v1",
        membership_sha256="4" * 64, records=(member,), correction_bindings=tuple(
            (row["table_id"], row["sha256"], row["store_id"]) for row in correction_bindings
        ),
    )
    destination = sqlite3.connect(tmp_path / "destination.sqlite3")
    legacy_import.migrate_legacy_chunks(source, destination, cohort=cohort)
    row = destination.execute(
        "SELECT v.version_id,v.source_id,v.document_id,v.title,v.body,v.body_sha256,v.provenance,"
        "v.legacy_cohort,v.legacy_eligible_at,m.ticker,m.kind,m.eligible_at,m.evidence_id,m.evidence_role "
        "FROM body_versions v JOIN temporal_evidence_map m USING(version_id)"
    ).fetchone()
    record = dict(zip((
        "version_id", "source_id", "document_id", "title", "text", "body_sha256", "provenance",
        "migration_cohort_id", "legacy_eligible_at", "ticker", "kind", "eligible_at",
        "evidence_id", "evidence_role",
    ), row))
    return source, destination, cohort, record


def test_validate_imported_chunk_returns_pinned_representation_metadata(tmp_path):
    source, destination, cohort, record = _chunk_fixture(
        tmp_path, correction_bindings=CORRECTIONS,
    )

    metadata = validate_legacy_version(
        destination, record, cohort, CORRECTIONS,
    )

    assert metadata == {
        "representation": "legacy_chunk",
        "binding_scope": "persistent_import_metadata",
        "asset_id": "asset-1",
        "chunk_id": "chunk-1",
        "ordinal": 0,
        "original_body_sha256": _sha("whole original body"),
        "cohort_manifest_sha256": "1" * 64,
        "membership_sha256": "4" * 64,
        "source_snapshot_sha256": cohort.source_snapshot_sha256,
        "eligibility_rule_id": "daily-original-eligibility",
        "eligibility_rule_version": "v1",
    }
    source.close()
    destination.close()


@pytest.mark.parametrize("field,value", [
    ("text", "different body"),
    ("title", "different title"),
    ("evidence_role", "lead"),
    ("ticker", "OTHER"),
    ("legacy_eligible_at", "2026-03-02T21:00:00.000000+00:00"),
    ("migration_cohort_id", "other-cohort"),
])
def test_validate_legacy_version_rejects_record_not_matching_member(tmp_path, field, value):
    source, destination, cohort, record = _chunk_fixture(tmp_path)
    invalid = dict(record)
    invalid[field] = value

    with pytest.raises(ValueError):
        validate_legacy_version(destination, invalid, cohort, ())

    source.close()
    destination.close()


def test_validate_legacy_version_requires_exact_correction_table_and_scope(tmp_path):
    source, destination, cohort, record = _chunk_fixture(
        tmp_path, correction_bindings=CORRECTIONS,
    )
    wrong = ({"table_id": "other-corrections", "sha256": "6" * 64,
              "store_id": "daily-snapshot"},)

    with pytest.raises(ValueError):
        validate_legacy_version(destination, record, cohort, wrong)
    with pytest.raises(ValueError):
        validate_legacy_version(destination, record, cohort, ())

    source.close()
    destination.close()


def test_validate_explicit_raw_full_body_without_invented_chunk_metadata(tmp_path):
    conn = sqlite3.connect(tmp_path / "raw.sqlite3")
    raw_text = "complete original article body"
    member = LegacyRecord(
        source_id="daily:legacy", document_id="raw-document", evidence_id="raw-evidence",
        kind="body", tickers=("ACME",), evidence_role="lead", eligible_at=ELIGIBLE,
        representation="raw_full_body", text_sha256=_sha(raw_text), title_sha256=_sha(TITLE),
    )
    cohort = LegacyCohort(
        cohort_id="raw-original-v1", manifest_sha256="1" * 64,
        input_manifest_sha256="2" * 64, source_store_id="daily-snapshot",
        source_snapshot_sha256="3" * 64, snapshot_captured_at=CAPTURED,
        source_tables=("legacy_articles",), eligibility_rule_id="original-rule",
        eligibility_rule_version="v1", membership_sha256="4" * 64,
        records=(member,), correction_bindings=(),
    )
    body_versions.ensure_schema(conn)
    temporal_evidence.ensure_schema(conn)
    version_id = body_versions.put_version(
        conn, source_id=member.source_id, document_id=member.document_id,
        title=TITLE, body=raw_text, observed_at=datetime.fromisoformat(CAPTURED),
        provenance="legacy", legacy_cohort=cohort.cohort_id,
        legacy_eligible_at=datetime.fromisoformat(ELIGIBLE),
    )
    temporal_evidence.register_version(
        conn, version_id=version_id, ticker="ACME", kind="body", evidence_id=member.evidence_id,
        evidence_role="lead", association_observed_at=datetime.fromisoformat(CAPTURED),
        eligible_at=datetime.fromisoformat(ELIGIBLE), provider="daily:legacy", is_backfill=True,
    )
    conn.commit()
    record = {
        "version_id": version_id, "source_id": member.source_id,
        "document_id": member.document_id, "migration_cohort_id": cohort.cohort_id,
        "legacy_eligible_at": ELIGIBLE, "eligible_at": ELIGIBLE,
        "provenance": "legacy", "body_sha256": _sha(raw_text),
        "title": TITLE, "text": raw_text,
        "ticker": "ACME", "kind": "body", "evidence_id": member.evidence_id,
        "evidence_role": "lead",
    }

    metadata = validate_legacy_version(conn, record, cohort, ())

    assert metadata["representation"] == "raw_full_body"
    assert metadata["binding_scope"] == "catalog_only"
    assert metadata["asset_id"] is None and metadata["chunk_id"] is None and metadata["ordinal"] is None
    assert metadata["original_body_sha256"] is None
    assert metadata["cohort_manifest_sha256"] == "1" * 64
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_version_metadata'"
    ).fetchone()
    conn.close()
