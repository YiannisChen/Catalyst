from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import FrozenInstanceError

import pytest
from catalyst_data.storage.legacy_cohorts import CohortError, load_cohort
from catalyst_data.storage.snapshots import load_verified_stores

INVOCATION = ["legacy-inventory", "--validate"]
STAMP = "2026-10-26T16:30:00+00:00"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, *, records=None, corrections=None, cohort_changes=None, listed=True):
    db = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE web_body_chunks (chunk_id TEXT)")
    member = {
        "source_id": "legacy-web", "document_id": "chunk:c1", "evidence_id": "web:asset-1",
        "kind": "body", "tickers": ["ABC"], "evidence_role": "evidence",
        "eligible_at": "2026-03-02T20:00:00Z", "representation": "legacy_chunk",
        "text_sha256": hashlib.sha256(b"original chunk").hexdigest(),
        "title_sha256": hashlib.sha256(b"original title").hexdigest(),
        "asset_id": "asset-1", "chunk_id": "c1", "ordinal": 0,
        "original_body_sha256": "b" * 64,
    }
    members = tmp_path / "members.jsonl"
    entries = [member] if records is None else records(member)
    members.write_text("".join(json.dumps(row) + "\n" for row in entries))
    spec = {
        "schema_version": 1, "cohort_id": "daily-original-v1",
        "source_snapshot": {"store_id": "daily", "sha256": sha(db), "captured_at": STAMP,
                            "tables": ["web_body_chunks"]},
        "eligibility_rule": {"id": "original-eligible-at", "version": "v1"},
        "correction_tables": [] if corrections is None else corrections,
        "membership": {"path": str(members), "sha256": sha(members), "record_count": len(entries)},
    }
    spec.update(cohort_changes or {})
    catalog = tmp_path / "cohort.json"
    catalog.write_text(json.dumps(spec))
    artifacts = [db, catalog, members] if listed else [db, catalog]
    manifest = tmp_path / "inputs.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "invocation": INVOCATION, "environment": {},
        "files": [{"path": str(path), "sha256": sha(path)} for path in artifacts],
    }))
    stores = load_verified_stores(manifest, invocation=INVOCATION,
                                 required_stores={"daily": db}, environ={})
    return catalog, members, stores, member


def test_load_preserves_exact_membership_representation_and_original_rules(tmp_path):
    catalog, _, stores, member = fixture(tmp_path)
    cohort = load_cohort(catalog, stores)
    assert cohort.cohort_id == "daily-original-v1"
    assert cohort.manifest_sha256 == sha(catalog)
    assert cohort.source_snapshot_sha256 == stores.stores["daily"].sha256
    assert cohort.eligibility_rule_id == "original-eligible-at"
    assert cohort.eligibility_rule_version == "v1"
    record = cohort.record("legacy-web", "chunk:c1")
    assert record.eligible_at == "2026-03-02T20:00:00.000000+00:00"
    assert record.evidence_role == "evidence" and record.tickers == ("ABC",)
    assert record.text_sha256 == member["text_sha256"]
    assert record.original_body_sha256 == member["original_body_sha256"]
    assert (record.representation, record.chunk_id, record.ordinal) == ("legacy_chunk", "c1", 0)
    with pytest.raises(CohortError, match="membership"):
        cohort.record("legacy-web", "unlisted")
    with pytest.raises((FrozenInstanceError, AttributeError)):
        record.ordinal = 1


@pytest.mark.parametrize("change", [
    lambda row: row | {"evidence_role": "unknown"},
    lambda row: row | {"eligible_at": "2026-03-02T20:00:00"},
    lambda row: row | {"text_sha256": "bad"},
    lambda row: row | {"ordinal": True},
    lambda row: row | {"chunk_id": None},
    lambda row: row | {"representation": "reconstructed_full_body"},
    lambda row: row | {"tickers": ["ABC", "ABC"]},
])
def test_invalid_or_unproven_members_are_rejected(tmp_path, change):
    path, _, stores, _ = fixture(tmp_path, records=lambda row: [change(row)])
    with pytest.raises(CohortError):
        load_cohort(path, stores)


def test_duplicate_record_identity_and_snapshot_mismatch_fail_closed(tmp_path):
    path, _, stores, _ = fixture(tmp_path, records=lambda row: [row, row])
    with pytest.raises(CohortError, match="duplicate"):
        load_cohort(path, stores)


def test_source_snapshot_must_match_the_verified_store(tmp_path):
    path, _, stores, _ = fixture(tmp_path, cohort_changes={
        "source_snapshot": {"store_id": "daily", "sha256": "0" * 64,
                            "captured_at": STAMP, "tables": ["web_body_chunks"]},
    })
    with pytest.raises(CohortError, match="snapshot"):
        load_cohort(path, stores)


def test_membership_file_must_be_explicitly_pinned_and_cannot_change(tmp_path):
    path, _, stores, _ = fixture(tmp_path, listed=False)
    with pytest.raises(ValueError):
        load_cohort(path, stores)


def test_mutated_membership_fails_verification(tmp_path):
    path, members, stores, _ = fixture(tmp_path)
    members.write_text(members.read_text() + "{}\n")
    with pytest.raises(ValueError):
        load_cohort(path, stores)


def test_corrections_are_bound_to_exact_table_digest_and_scope(tmp_path):
    binding = {"table_id": "sec-v1", "sha256": "a" * 64, "store_id": "daily-original"}
    path, _, stores, _ = fixture(tmp_path, corrections=[binding])
    cohort = load_cohort(path, stores)
    cohort.require_corrections((binding,))
    with pytest.raises(CohortError, match="correction"):
        cohort.require_corrections((binding | {"sha256": "c" * 64},))
    with pytest.raises(CohortError, match="correction"):
        cohort.require_corrections(())


def test_full_body_representation_cannot_carry_chunk_metadata(tmp_path):
    path, _, stores, _ = fixture(tmp_path, records=lambda row: [row | {"representation": "raw_full_body"}])
    with pytest.raises(CohortError, match="chunk"):
        load_cohort(path, stores)


def test_same_chunk_cannot_be_listed_as_two_distinct_documents(tmp_path):
    path, _, stores, _ = fixture(tmp_path, records=lambda row: [
        row, row | {"document_id": "second-document", "ordinal": 1},
    ])
    with pytest.raises(CohortError, match="duplicate"):
        load_cohort(path, stores)


@pytest.mark.parametrize("change", [
    lambda row: row | {"kind": []},
    lambda row: row | {"evidence_role": []},
])
def test_invalid_json_field_types_have_the_documented_validation_error(tmp_path, change):
    path, _, stores, _ = fixture(tmp_path, records=lambda row: [change(row)])
    with pytest.raises(CohortError):
        load_cohort(path, stores)


def test_original_full_body_is_accepted_without_chunk_fields(tmp_path):
    def full_body(row):
        return [{key: value for key, value in (row | {"representation": "raw_full_body"}).items()
                 if key not in {"asset_id", "chunk_id", "ordinal", "original_body_sha256"}}]
    path, _, stores, _ = fixture(tmp_path, records=full_body)
    cohort = load_cohort(path, stores)
    assert cohort.records[0].representation == "raw_full_body"
    assert cohort.records[0].chunk_id is None


def test_catalog_schema_accepts_loaded_fixture_and_rejects_extra_fields(tmp_path):
    from pathlib import Path

    import jsonschema

    schema_path = Path(__file__).resolve().parents[3] / "docs/data/legacy_cohort_manifest.schema.json"
    schema = json.loads(schema_path.read_text())
    path, _, stores, _ = fixture(tmp_path)
    spec = json.loads(path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(spec, schema)
    load_cohort(path, stores)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(spec | {"automatic_legacy_before": STAMP}, schema)
