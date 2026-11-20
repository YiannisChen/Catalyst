from __future__ import annotations

import sqlite3

from catalyst_data.dedup.ingestion import ingest_record
from catalyst_data.dedup.links import EvidenceRecord
from catalyst_data.dedup.origin_assign import record_origin_inputs
from catalyst_data.dedup.relations import record_relation
from catalyst_data.dedup.rules_v3 import (
    RULE_VERSION,
    assign_origin,
    rerun_relations_v3,
)

AT = "2026-11-02T00:00:00Z"


def _add(conn, document_id: str, record: EvidenceRecord, *, publisher: str,
         content_state: str, kind: str) -> str:
    ingest_record(
        conn, record, store_id="fannie-fixture", source_id=document_id,
        version_id="v1", corrected_at=AT, correction_ref="fixture:corrected-at",
    )
    record_key = conn.execute(
        "SELECT record_id FROM dedup_candidate_records WHERE document_id=?",
        (document_id,),
    ).fetchone()[0]
    record_origin_inputs(
        conn, record_key, provider=record.provider, publisher=publisher,
        content_state=content_state, kind=kind, cik=None, recorded_at=AT,
    )
    return record_key


def _fannie_pair(conn):
    summary = _add(
        conn,
        "fannie-fiscal-summary",
        EvidenceRecord(
            record_id="fannie-fiscal-summary", provider="alpaca",
            body_fingerprint="a" * 64, fingerprint_version="sha256-full-v1",
            body_fingerprint_confirmed=True,
            title="Fannie Mae Releases September 2026 Monthly Summary",
        ),
        publisher="fiscal.ai", content_state="full_text", kind="body",
    )
    original = _add(
        conn,
        "fannie-prn-original",
        EvidenceRecord(
            record_id="fannie-prn-original", provider="prnewswire",
            release_id="fannie-summary-fixture",
            release_namespace="prn-fixture", release_id_confirmed=True,
            title="PRN RSS original excerpt",
        ),
        publisher="prnewswire", content_state="headline", kind="headline",
    )
    return summary, original


def test_fannie_fiscal_summary_is_unresolved_without_same_release_original():
    conn = sqlite3.connect(":memory:")
    summary, _original = _fannie_pair(conn)

    rerun_relations_v3(conn, discovered_at=AT)
    result = assign_origin(conn, summary, at=AT, mode="observed")

    assert result["origin"] is None
    assert result["origin_status"] == "unresolved"


def test_fannie_fiscal_summary_inherits_only_after_a_v3_same_release_edge():
    conn = sqlite3.connect(":memory:")
    summary, original = _fannie_pair(conn)

    relation_id = record_relation(
        conn, summary, original, relation_type="same_release",
        rule_id="fixture_fannie_pair", rule_version=RULE_VERSION,
        basis="fixture:fannie-mae-summary-vs-prn", discovered_at=AT,
    )
    result = assign_origin(conn, summary, at=AT, mode="observed")

    assert result["origin"] == "release:prn-fixture:fannie-summary-fixture"
    assert result["origin_status"] == "resolved"
    assert result["origin_basis"] == "confirmed_release"
    published = assign_origin(
        conn, summary, at=AT, mode="published",
        snapshot_relation_ids={relation_id}, registry_as_of=AT,
    )
    assert published["origin"] == result["origin"]
    assert published["relation_ids"] == [relation_id]


def test_published_attribution_role_effectiveness_uses_query_time_at():
    conn = sqlite3.connect(":memory:")
    summary = _add(
        conn,
        "summary-role-effective-at-query-time",
        EvidenceRecord(
            record_id="summary-role-effective-at-query-time", provider="alpaca",
            release_id="must-not-self-originate", release_namespace="fixture",
            release_id_confirmed=True,
            title="Attribution summary",
        ),
        publisher="fiscal.ai", content_state="full_text", kind="body",
    )

    result = assign_origin(
        conn, summary, at=AT, mode="published", snapshot_relation_ids=set(),
        registry_as_of="2026-10-20T00:00:00Z",
    )

    assert result["origin"] is None
    assert result["origin_status"] == "unresolved"


def test_identical_attribution_records_keep_v3_relation_but_cannot_supply_origin():
    conn = sqlite3.connect(":memory:")
    summary_a = _add(
        conn,
        "summary-a",
        EvidenceRecord(
            record_id="summary-a", provider="alpaca",
            body_fingerprint="b" * 64, fingerprint_version="sha256-full-v1",
            body_fingerprint_confirmed=True,
        ),
        publisher="fiscal.ai", content_state="full_text", kind="body",
    )
    summary_b = _add(
        conn,
        "summary-b",
        EvidenceRecord(
            record_id="summary-b", provider="alpaca",
            body_fingerprint="b" * 64, fingerprint_version="sha256-full-v1",
            body_fingerprint_confirmed=True,
        ),
        publisher="fiscal.ai", content_state="full_text", kind="body",
    )

    rerun_relations_v3(conn, discovered_at=AT)

    relation = conn.execute(
        "SELECT relation_type,rule_id,rule_version FROM dedup_relations "
        "WHERE status='active'",
    ).fetchone()
    result = assign_origin(conn, summary_a, at=AT, mode="observed")
    assert relation == ("same_release", "full_body_fingerprint", RULE_VERSION)
    assert result["origin"] is None
    assert result["origin_status"] == "unresolved"
