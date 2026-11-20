import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from catalyst_data import read_api
from catalyst_data.dedup import ingestion, relations, rules_v3
from catalyst_data.dedup.links import EvidenceRecord
from catalyst_data.dedup.origin_assign import record_origin_inputs
from catalyst_data.storage.snapshots import load_verified_stores

AT = datetime(2026, 11, 2, tzinfo=timezone.utc)
INVOCATION = ["read-api-d1"]


def _catalog(path: Path) -> tuple[str, str, str]:
    summary = EvidenceRecord(record_id="summary-doc", provider="alpaca")
    original = EvidenceRecord(
        record_id="original-doc", provider="prnewswire", release_id="release-7",
        release_namespace="prn", release_id_confirmed=True,
    )
    with sqlite3.connect(path) as conn:
        ingestion.ingest_record(
            conn, summary, store_id="bodies", source_id="news", version_id="summary-v1",
            corrected_at="2026-11-02T00:00:00Z", correction_ref="fixture:source-time",
        )
        ingestion.ingest_record(
            conn, original, store_id="bodies", source_id="news", version_id="original-v1",
            corrected_at="2026-11-02T00:00:00Z", correction_ref="fixture:source-time",
        )
        summary_key = conn.execute(
            "SELECT record_id FROM dedup_candidate_records WHERE document_id='summary-doc'",
        ).fetchone()[0]
        original_key = conn.execute(
            "SELECT record_id FROM dedup_candidate_records WHERE document_id='original-doc'",
        ).fetchone()[0]
        record_origin_inputs(
            conn, summary_key, provider="alpaca", publisher="fiscal.ai",
            content_state="full_text", kind="body", cik=None, recorded_at="2026-11-02T00:00:00Z",
        )
        record_origin_inputs(
            conn, original_key, provider="prnewswire", publisher="prnewswire",
            content_state="headline", kind="headline", cik=None, recorded_at="2026-11-02T00:00:00Z",
        )
        relation_id = relations.record_relation(
            conn, summary_key, original_key, relation_type="same_release",
            rule_id="fixture_original_release", rule_version=rules_v3.RULE_VERSION,
            basis="fixture:release-7", discovered_at="2026-11-02T00:00:00Z",
        )
    return summary_key, original_key, relation_id


def _verified(tmp_path):
    bodies = tmp_path / "bodies.sqlite3"
    with sqlite3.connect(bodies):
        pass
    dedup = tmp_path / "dedup.sqlite3"
    summary_key, original_key, relation_id = _catalog(dedup)
    manifest = tmp_path / "inputs.json"
    paths = (bodies, dedup)
    manifest.write_text(json.dumps({
        "schema_version": 1, "invocation": INVOCATION, "environment": {},
        "files": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in paths],
    }))
    verified = load_verified_stores(
        manifest, invocation=INVOCATION,
        required_stores={"bodies": bodies, "dedup": dedup}, environ={},
    )
    return verified, summary_key, original_key, relation_id


def _body_row(verified):
    return {
        "record_key": None, "version_id": "summary-v1", "ticker": "ABC", "kind": "body",
        "title": "Filing summary", "text": "Summary text", "url": "https://example.test/summary",
        "publisher": "fiscal.ai", "source_class": "reported_news", "source_id": "news",
        "provenance": "versioned", "published_at": "2026-11-01T00:00:00Z",
        "effective_published_at": "2026-11-01T00:00:00Z", "observed_at": "2026-11-02T00:00:00Z",
        "first_seen_at": "2026-11-02T00:00:00Z", "temporal_status": "observed",
        "store_id": "bodies", "snapshot_id": verified.stores["bodies"].sha256,
        "document_id": "summary-doc", "origin": "legacy-origin",
    }


def _mock_as_of(verified, monkeypatch):
    row = _body_row(verified)
    audit = {
        "data_snapshot_ids": {"bodies": verified.stores["bodies"].sha256},
        "input_snapshot_ids": {key: pin.sha256 for key, pin in verified.stores.items()},
        "manifest_sha256": verified.manifest_sha256,
        "correction_table_ids": [], "correction_table_id": "none", "correction_store_ids": {},
        "legacy_catalogs": {},
    }
    calls = []

    def fake_as_of_stores(*args, **kwargs):
        calls.append(kwargs)
        return {"evidence": [dict(row)], "leads": [], "audit": dict(audit)}

    monkeypatch.setattr(read_api, "as_of_stores", fake_as_of_stores)
    return calls


def test_as_of_joins_d1_origin_by_exact_record_identity_and_hashes_rule_identity(tmp_path, monkeypatch):
    verified, summary_key, _original_key, relation_id = _verified(tmp_path)
    calls = _mock_as_of(verified, monkeypatch)

    result = read_api.as_of(
        verified, "ABC", AT, AT.replace(day=3), AT, mode="observed", dedup_store_id="dedup",
    )

    evidence = result["evidence"][0]
    envelope = result["envelope"]
    assert evidence["dedup_record_key"] == summary_key
    assert evidence["origin"] == "release:prn:release-7"
    assert evidence["origin_status"] == "resolved"
    assert evidence["origin_basis"] == "confirmed_release"
    assert evidence["relation_ids"] == [relation_id]
    assert evidence["origin_registry_sha256"]
    assert calls[0]["store_ids"] == ("bodies",)
    assert envelope["evidence"][0]["record_key"] == summary_key
    assert envelope["evidence"][0]["origin"] == "release:prn:release-7"
    assert envelope["dedup"] == {
        "status": "applied", "store_id": "dedup", "relation_ids": [relation_id],
        "relation_set_sha256": relations.relation_set_sha256([relation_id]),
        **rules_v3.identity(),
    }


def test_as_of_rejects_a_missing_d1_record_instead_of_falling_back(tmp_path, monkeypatch):
    verified, _summary_key, _original_key, _relation_id = _verified(tmp_path)
    calls = _mock_as_of(verified, monkeypatch)
    row = _body_row(verified)
    row["document_id"] = "not-in-d1"

    def fake_as_of_stores(*args, **kwargs):
        return {"evidence": [dict(row)], "leads": [], "audit": {
            "data_snapshot_ids": {"bodies": verified.stores["bodies"].sha256},
            "input_snapshot_ids": {key: pin.sha256 for key, pin in verified.stores.items()},
            "manifest_sha256": verified.manifest_sha256,
            "correction_table_ids": [], "correction_table_id": "none", "correction_store_ids": {},
            "legacy_catalogs": {},
        }}

    monkeypatch.setattr(read_api, "as_of_stores", fake_as_of_stores)
    with pytest.raises(ValueError, match="D1 v3 record is not indexed"):
        read_api.as_of(
            verified, "ABC", AT, AT.replace(day=3), AT, mode="observed", dedup_store_id="dedup",
            store_ids=("bodies",),
        )


def test_published_d1_read_requires_explicit_relation_snapshot_and_registry_time(tmp_path, monkeypatch):
    verified, _summary_key, _original_key, _relation_id = _verified(tmp_path)
    _mock_as_of(verified, monkeypatch)
    with pytest.raises(ValueError, match="dedup_snapshot_relation_ids"):
        read_api.as_of(
            verified, "ABC", AT, AT.replace(day=3), AT, mode="published", dedup_store_id="dedup",
            store_ids=("bodies",),
        )


def test_published_d1_read_uses_only_the_supplied_relation_snapshot(tmp_path, monkeypatch):
    verified, summary_key, _original_key, relation_id = _verified(tmp_path)
    _mock_as_of(verified, monkeypatch)

    result = read_api.as_of(
        verified, "ABC", AT, AT.replace(day=3), AT, mode="published", dedup_store_id="dedup",
        dedup_snapshot_relation_ids=(relation_id,), dedup_registry_as_of=AT,
        store_ids=("bodies",),
    )

    evidence = result["evidence"][0]
    assert evidence["dedup_record_key"] == summary_key
    assert evidence["origin"] == "release:prn:release-7"
    assert evidence["relation_ids"] == [relation_id]
    assert result["envelope"]["dedup"]["rule_version"] == "d1-rules-v3"
