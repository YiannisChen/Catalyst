"""Point-in-time relation ledger regressions for D1."""
from __future__ import annotations

import sqlite3

import pytest
from catalyst_data.dedup.ingestion import ingest_record
from catalyst_data.dedup.links import EvidenceRecord
from catalyst_data.dedup.origin_assign import ensure_schema as ensure_origin_schema
from catalyst_data.dedup.relations import (
    ensure_schema,
    legacy_relation_id,
    record_relation,
    relation_set_sha256,
    rerun_legacy_relations,
    visible_relations,
)


def _ingest(conn, record, *, source="source", version="v1"):
    return ingest_record(
        conn,
        record,
        store_id="store",
        source_id=source,
        version_id=version,
        provider_record_id_kind="filing_accession" if record.provider == "sec" else None,
        corrected_at="2026-01-01T00:00:00Z",
        correction_ref="fixture:time",
    )


def test_candidate_and_active_relation_have_time_bounded_visibility():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    record_relation(conn, "a", "b", relation_type="same_article", rule_id="canonical_url",
                    rule_version="v1", basis="https://example.test/x", discovered_at="2026-01-02T00:00:00Z")
    assert visible_relations(conn, mode="observed", at="2026-01-01T23:59:59Z") == []


def test_relation_discovered_after_observed_cutoff_requires_published_snapshot():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    relation_id = record_relation(
        conn, "a", "b", relation_type="same_release", rule_id="full_body_fingerprint",
        rule_version="v1", basis="sha256-full-v1:abcd", discovered_at="2026-01-02T00:00:00Z",
    )
    assert visible_relations(conn, mode="observed", at="2026-01-01T00:00:00Z") == []
    assert visible_relations(conn, mode="published", at="2026-01-01T00:00:00Z",
                             snapshot_relation_ids={relation_id})[0]["relation_id"] == relation_id


def test_legacy_links_are_never_observed_and_need_explicit_published_snapshot_membership():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    conn.execute("INSERT INTO duplicate_links VALUES ('legacy-a','legacy-b','same_release','confirmed_release_id')")
    legacy_id = legacy_relation_id("legacy-a", "legacy-b", "same_release", "confirmed_release_id")
    assert visible_relations(conn, mode="observed", at="2026-01-01T00:00:00Z") == []
    assert visible_relations(conn, mode="published", at="2026-01-01T00:00:00Z",
                             snapshot_relation_ids=set()) == []
    result = visible_relations(conn, mode="published", at="2026-01-01T00:00:00Z",
                               snapshot_relation_ids={legacy_id})
    assert result[0]["relation_id"] == legacy_id
    assert result[0]["discovered_at"] is None


def test_published_relation_snapshot_without_legacy_ids_skips_legacy_link_scan():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    relation_id = record_relation(
        conn, "a", "b", relation_type="same_release", rule_id="fixture",
        rule_version="v1", basis="fixture", discovered_at="2026-01-02T00:00:00Z",
    )
    statements = []
    conn.set_trace_callback(statements.append)

    result = visible_relations(conn, mode="published", at="2026-01-03T00:00:00Z",
                               snapshot_relation_ids={relation_id})

    assert [row["relation_id"] for row in result] == [relation_id]
    assert all("FROM duplicate_links" not in statement for statement in statements)


def test_similar_title_different_body_stays_candidate_and_not_visible():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    relation_id = record_relation(
        conn, "a", "b", relation_type="same_article", rule_id="title_similarity",
        rule_version="v1", basis="similar titles", discovered_at="2026-01-02T00:00:00Z",
        status="candidate",
    )
    assert relation_id
    assert visible_relations(conn, mode="observed", at="2026-01-03T00:00:00Z") == []


def test_legacy_relation_rerun_uses_passed_instant_and_never_old_link_times():
    conn = sqlite3.connect(":memory:")
    _ingest(conn, EvidenceRecord(record_id="a", provider="wire", url="https://x.test/r"))
    _ingest(conn, EvidenceRecord(record_id="b", provider="wire", url="https://x.test/r"), source="other")
    conn.execute("INSERT INTO duplicate_links VALUES ('legacy-a','legacy-b','same_article','canonical_url')")
    legacy_links = conn.execute("SELECT * FROM duplicate_links ORDER BY 1,2,3,4").fetchall()
    rerun_legacy_relations(conn, rule_version="v1",
                           discovered_at="2026-02-03T04:05:06Z")
    rows = conn.execute("SELECT discovered_at FROM dedup_relations").fetchall()
    assert rows and {row[0] for row in rows} == {"2026-02-03T04:05:06Z"}
    classified = conn.execute("SELECT rule_id,basis FROM dedup_relations").fetchall()
    assert classified == [("canonical_url", "https://x.test/r")]
    assert rerun_legacy_relations(conn, rule_version="v1",
                                  discovered_at="2026-02-03T04:05:06Z") == 0
    assert conn.execute("SELECT * FROM duplicate_links ORDER BY 1,2,3,4").fetchall() == legacy_links


def test_legacy_rerun_only_classifies_candidate_key_pairs():
    conn = sqlite3.connect(":memory:")
    _ingest(conn, EvidenceRecord(record_id="a", provider="wire", url="https://x.test/shared"))
    _ingest(conn, EvidenceRecord(record_id="b", provider="wire", url="https://x.test/shared"),
            source="other")
    _ingest(conn, EvidenceRecord(record_id="c", provider="wire", url="https://x.test/unique"),
            source="third")
    assert rerun_legacy_relations(
        conn, rule_version="v1", discovered_at="2026-02-03T04:05:06Z"
    ) == 1
    rows = conn.execute("SELECT record_key_a,record_key_b FROM dedup_relations").fetchall()
    assert len(rows) == 1
    assert {key for pair in rows for key in pair} == {
        conn.execute("SELECT record_id FROM dedup_candidate_records WHERE document_id=?", (doc,)).fetchone()[0]
        for doc in ("a", "b")
    }


def test_ingestion_replay_identity_is_unchanged_after_relation_and_origin_schemas():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    ensure_origin_schema(conn)
    record = EvidenceRecord(record_id="replay", provider="wire", url="https://x.test/replay")
    assert _ingest(conn, record) == 0
    assert _ingest(conn, record) == 0


def test_relation_replay_is_idempotent_and_hash_is_order_independent():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    args = ("a", "b")
    kwargs = dict(relation_type="same_article", rule_id="canonical_url", rule_version="v1",
                  basis="https://example.test/r", discovered_at="2026-01-01T00:00:00Z")
    first = record_relation(conn, *args, **kwargs)
    second = record_relation(conn, *args, **kwargs)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM dedup_relations").fetchone()[0] == 1
    assert relation_set_sha256([first, second]) == relation_set_sha256([second, first])


def test_relation_validation_and_append_only_ledger():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    relation_id = record_relation(conn, "b", "a", relation_type="same_article", rule_id="r",
                                  rule_version="v1", basis="x", discovered_at="2026-01-01T00:00:00Z")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM dedup_relations WHERE relation_id=?", (relation_id,))


def test_retraction_is_visible_only_at_and_after_retracted_at():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    relation_id = record_relation(
        conn, "a", "b", relation_type="same_release", rule_id="fixture",
        rule_version="v1", basis="x", discovered_at="2026-01-01T00:00:00Z",
    )
    record_relation(
        conn, "a", "b", relation_type="same_release", rule_id="fixture",
        rule_version="v1", basis="x", discovered_at="2026-01-04T00:00:00Z",
        status="retracted", retracted_at="2026-01-03T00:00:00Z", retraction_reason="test",
    )
    assert visible_relations(conn, mode="observed", at="2026-01-02T23:59:59Z")
    assert visible_relations(conn, mode="observed", at="2026-01-03T00:00:00Z") == []
    assert relation_id


def test_retraction_cannot_precede_existing_active_relation_discovery():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    record_relation(conn, "a", "b", relation_type="same_release", rule_id="fixture",
                    rule_version="v1", basis="x", discovered_at="2026-01-02T00:00:00Z")
    with pytest.raises(ValueError, match="active relation discovery"):
        record_relation(conn, "a", "b", relation_type="same_release", rule_id="fixture",
                        rule_version="v1", basis="x", discovered_at="2026-01-04T00:00:00Z",
                        status="retracted", retracted_at="2026-01-01T00:00:00Z",
                        retraction_reason="invalid")


def test_read_path_reports_missing_schema_without_creating_it():
    conn = sqlite3.connect(":memory:")
    with pytest.raises(LookupError, match="dedup_relations"):
        visible_relations(conn, mode="observed", at="2026-01-01T00:00:00Z")
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
