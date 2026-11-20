"""Origin assignment fixtures for D1's explicit evidence priorities."""
from __future__ import annotations

import sqlite3

import pytest
from catalyst_data.dedup.ingestion import ingest_record
from catalyst_data.dedup.links import EvidenceRecord
from catalyst_data.dedup.origin_assign import (
    assign_origin,
    link_same_release,
    record_equivalence,
    record_origin_inputs,
)
from catalyst_data.dedup.publisher_registry import PublisherEntry


def _add(conn, document, *, provider="alpaca", publisher="Benzinga",
         content_state="full_text", kind="body", cik=None, **identity):
    record = EvidenceRecord(record_id=document, provider=provider, **identity)
    ingest_record(conn, record, store_id="store", source_id=document, version_id="v1",
                  provider_record_id_kind=("filing_accession" if record.provider == "sec"
                                            and record.provider_record_id else None),
                  corrected_at="2026-01-01T00:00:00Z", correction_ref="fixture:time")
    record_key = conn.execute(
        "SELECT record_id FROM dedup_candidate_records WHERE document_id=?", (document,)
    ).fetchone()[0]
    record_origin_inputs(
        conn, record_key, provider=provider, publisher=publisher,
        content_state=content_state, kind=kind, cik=cik,
        recorded_at="2026-01-01T00:00:00Z",
    )
    return record_key


def test_benzinga_from_alpaca_and_finnhub_has_one_publisher_origin():
    conn = sqlite3.connect(":memory:")
    left = _add(conn, "a", provider="alpaca")
    right = _add(conn, "b", provider="finnhub")
    registry = (
        PublisherEntry("alpaca", "Benzinga", "full_text", "benzinga", "sample-1",
                       effective_from="1900-01-01T00:00:00Z"),
        PublisherEntry("finnhub", "Benzinga", "full_text", "benzinga", "sample-2",
                       effective_from="1900-01-01T00:00:00Z"),
    )
    at = "2026-01-02T00:00:00Z"
    assert assign_origin(conn, left, at=at, mode="observed", registry=registry)["origin"] == "pub:benzinga"
    assert assign_origin(conn, right, at=at, mode="observed", registry=registry)["origin"] == "pub:benzinga"


def test_same_release_fingerprint_joins_pr_ir_and_sec_attachment_but_not_accession_only():
    conn = sqlite3.connect(":memory:")
    fp = "a" * 64
    records = [
        _add(conn, "pr", provider="alpaca", publisher="PR Newswire", body_fingerprint=fp,
             fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True),
        _add(conn, "ir", provider="ir", publisher="IR", body_fingerprint=fp,
             fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True),
        _add(conn, "sec-ex99", provider="sec", publisher="SEC",
             cik="123456789", body_fingerprint=fp, fingerprint_version="sha256-full-v1",
             body_fingerprint_confirmed=True),
    ]
    for a, b in zip(records, records[1:]):
        link_same_release(conn, a, b, rule_id="full_body_fingerprint", rule_version="v1",
                          basis=f"sha256-full-v1:{fp[:16]}", discovered_at="2026-01-01T00:00:00Z")
    result = assign_origin(conn, records[0], at="2026-01-02T00:00:00Z", mode="observed",
        registry=(PublisherEntry("alpaca", "PR Newswire", "full_text", "prnewswire", "sample",
                                 effective_from="1900-01-01T00:00:00Z"),))
    assert result["origin"].startswith("release:fp:")
    assert result["origin_status"] == "resolved"
    assert len(result["relation_ids"]) == 2
    assert {assign_origin(conn, key, at="2026-01-02T00:00:00Z", mode="observed",
                          registry=())["origin"] for key in records} == {result["origin"]}

    accession_only = _add(conn, "sec-accession", provider="sec", publisher="SEC",
                          provider_record_id="0000123456-26-000001", kind="filing",
                          cik="123456789")
    assert assign_origin(conn, records[0], at="2026-01-02T00:00:00Z", mode="observed",
                         registry=())["relation_ids"] == result["relation_ids"]
    assert all(accession_only not in (row[0], row[1]) for row in conn.execute(
        "SELECT record_key_a,record_key_b FROM dedup_relations WHERE relation_type='same_release'"))


def test_two_unresolved_records_do_not_become_independent_origin_votes():
    conn = sqlite3.connect(":memory:")
    first = _add(conn, "unknown-a", publisher=None)
    second = _add(conn, "unknown-b", publisher=None)
    at = "2026-01-02T00:00:00Z"
    a = assign_origin(conn, first, at=at, mode="observed", registry=())["origin"]
    b = assign_origin(conn, second, at=at, mode="observed", registry=())["origin"]
    assert a is None and b is None


def test_same_priority_conflict_is_unresolved_and_status_conflict():
    conn = sqlite3.connect(":memory:")
    record = _add(conn, "conflict", provider="alpaca", publisher="Benzinga",
                  release_id="r1", release_namespace="wire", release_id_confirmed=True)
    # Distinct confirmed release IDs in one relation component are the same-priority conflict.
    other = _add(conn, "conflict-2", provider="other", publisher=None,
                 release_id="r2", release_namespace="wire", release_id_confirmed=True)
    from catalyst_data.dedup.relations import record_relation
    record_relation(conn, record, other, relation_type="same_release", rule_id="same_release_registry",
                    rule_version="v1", basis="fixture", discovered_at="2026-01-01T00:00:00Z")
    result = assign_origin(conn, record, at="2026-01-02T00:00:00Z", mode="observed", registry=())
    assert result["origin"] is None and result["origin_status"] == "conflict"


def test_component_merge_applies_equivalence_only_after_its_discovery_time():
    conn = sqlite3.connect(":memory:")
    a = _add(conn, "a", publisher=None, release_id="z", release_namespace="test",
             release_id_confirmed=True)
    b = _add(conn, "b", publisher=None, release_id="a", release_namespace="test",
             release_id_confirmed=True)
    from catalyst_data.dedup.relations import record_relation
    relation_id = record_relation(conn, a, b, relation_type="same_release", rule_id="full_body_fingerprint",
                                  rule_version="v1", basis="fixture:merge",
                                  discovered_at="2026-01-03T00:00:00Z")
    record_equivalence(conn, "release:test:z", "release:test:a", discovered_at="2026-01-03T00:00:00Z",
                       relation_id=relation_id)
    before = assign_origin(conn, a, at="2026-01-02T00:00:00Z", mode="observed", registry=())
    after = assign_origin(conn, a, at="2026-01-04T00:00:00Z", mode="observed", registry=())
    assert before["origin"] == "release:test:z"
    assert after["origin"] == "release:test:a"


def test_link_same_release_records_the_origin_equivalence():
    conn = sqlite3.connect(":memory:")
    left = _add(conn, "merge-left", publisher=None, release_id="z", release_namespace="test",
                release_id_confirmed=True)
    right = _add(conn, "merge-right", publisher=None, release_id="a", release_namespace="test",
                 release_id_confirmed=True)
    linked = link_same_release(conn, left, right, rule_id="confirmed_release_id", rule_version="v1",
                               basis="test:z-a", discovered_at="2026-01-02T00:00:00Z", registry=())
    assert linked["equivalence"] == ("release:test:z", "release:test:a")
    rows = conn.execute("SELECT old_origin,new_origin,relation_id FROM origin_equivalences").fetchall()
    assert rows == [("release:test:z", "release:test:a", linked["relation_id"])]


def test_pending_registry_evidence_is_rejected_and_unknown_origin_prefix_fails():
    conn = sqlite3.connect(":memory:")
    record = _add(conn, "a", publisher="Benzinga")
    pending = (PublisherEntry("alpaca", "Benzinga", "full_text", "benzinga", "PENDING_SAMPLE",
                              effective_from="1900-01-01T00:00:00Z"),)
    with pytest.raises(ValueError, match="PENDING_SAMPLE"):
        assign_origin(conn, record, at="2026-01-02T00:00:00Z", mode="observed", registry=pending)
    with pytest.raises(ValueError, match="origin"):
        record_equivalence(conn, "unknown:x", "pub:benzinga", discovered_at="2026-01-01T00:00:00Z",
                           relation_id="relation:x")


def test_registry_content_state_mismatch_does_not_assign_publisher_origin():
    conn = sqlite3.connect(":memory:")
    record = _add(conn, "headline-only", content_state="headline")
    registry = (PublisherEntry("alpaca", "Benzinga", "full_text", "benzinga", "sample",
                               effective_from="1900-01-01T00:00:00Z"),)
    result = assign_origin(conn, record, at="2026-01-02T00:00:00Z", mode="observed",
                           registry=registry)
    assert result["origin"] is None


def test_conflicting_origin_input_replay_raises():
    conn = sqlite3.connect(":memory:")
    record = _add(conn, "replay")
    record_origin_inputs(conn, record, provider="alpaca", publisher="Benzinga",
                         content_state="full_text", kind="body", cik=None,
                         recorded_at="2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="conflicts"):
        record_origin_inputs(conn, record, provider="alpaca", publisher="Other",
                             content_state="full_text", kind="body", cik=None,
                             recorded_at="2026-01-01T00:00:00Z")


def test_shipped_registry_is_evidenced_and_unambiguous():
    from catalyst_data.dedup.publisher_registry import PUBLISHER_REGISTRY

    keys = [(e.provider, e.publisher, e.content_state) for e in PUBLISHER_REGISTRY]
    assert len(keys) == len(set(keys))
    assert {entry.effective_from for entry in PUBLISHER_REGISTRY} == {"2026-11-01T00:00:00Z"}
    assert all(entry.effective_until is None for entry in PUBLISHER_REGISTRY)
    assert all(e.evidence_ref and e.evidence_ref != "PENDING_SAMPLE" for e in PUBLISHER_REGISTRY)
    assert {e.family for e in PUBLISHER_REGISTRY} == {"benzinga", "globenewswire", "businesswire", "prnewswire"}


def test_shipped_registry_unifies_alpaca_and_finnhub_benzinga_inputs():
    conn = sqlite3.connect(":memory:")
    alpaca = _add(conn, "alpaca", provider="alpaca", publisher="benzinga")
    finnhub = _add(conn, "finnhub", provider="finnhub", publisher="Benzinga",
                   content_state="headline", kind="headline")
    at = "2026-11-02T00:00:00Z"
    assert assign_origin(conn, alpaca, at=at, mode="observed")["origin"] == "pub:benzinga"
    assert assign_origin(conn, finnhub, at=at, mode="observed")["origin"] == "pub:benzinga"


def test_published_equivalence_requires_its_relation_in_the_snapshot():
    conn = sqlite3.connect(":memory:")
    a = _add(conn, "a", publisher=None, release_id="z", release_namespace="test",
             release_id_confirmed=True)
    b = _add(conn, "b", publisher=None, release_id="a", release_namespace="test",
             release_id_confirmed=True)
    from catalyst_data.dedup.relations import record_relation
    relation_id = record_relation(conn, a, b, relation_type="same_release", rule_id="fixture",
                                  rule_version="v1", basis="fixture", discovered_at="2026-01-01T00:00:00Z")
    record_equivalence(conn, "release:test:z", "release:test:a", discovered_at="2026-01-01T00:00:00Z",
                       relation_id=relation_id)
    outside = assign_origin(conn, a, at="2026-01-02T00:00:00Z", mode="published",
                            registry=(), snapshot_relation_ids=set(),
                            registry_as_of="2026-11-01T00:00:00Z")
    inside = assign_origin(conn, a, at="2026-01-02T00:00:00Z", mode="published",
                           registry=(), snapshot_relation_ids={relation_id},
                           registry_as_of="2026-11-01T00:00:00Z")
    assert outside["origin"] == "release:test:z"
    assert inside["origin"] == "release:test:a"


def test_observed_equivalence_is_hidden_when_its_relation_is_after_at():
    conn = sqlite3.connect(":memory:")
    a = _add(conn, "a", publisher=None, release_id="z", release_namespace="test",
             release_id_confirmed=True)
    b = _add(conn, "b", publisher=None, release_id="a", release_namespace="test",
             release_id_confirmed=True)
    from catalyst_data.dedup.relations import record_relation
    relation_id = record_relation(conn, a, b, relation_type="same_release", rule_id="fixture",
                                  rule_version="v1", basis="fixture", discovered_at="2026-01-03T00:00:00Z")
    record_equivalence(conn, "release:test:z", "release:test:a", discovered_at="2026-01-03T00:00:00Z",
                       relation_id=relation_id)
    result = assign_origin(conn, a, at="2026-01-02T00:00:00Z", mode="observed", registry=())
    assert result["origin"] == "release:test:z"


def test_origin_input_recorded_after_cutoff_cannot_contribute_member_evidence():
    conn = sqlite3.connect(":memory:")
    left = _add(conn, "left", provider="unknown", publisher=None, release_id="release-x",
                release_namespace="test", release_id_confirmed=True)
    future_record = EvidenceRecord(record_id="right", provider="unknown",
                                   release_id="release-y", release_namespace="test",
                                   release_id_confirmed=True)
    ingest_record(conn, future_record, store_id="store", source_id="right", version_id="v1",
                  corrected_at="2026-01-01T00:00:00Z", correction_ref="fixture:time")
    right = conn.execute(
        "SELECT record_id FROM dedup_candidate_records WHERE document_id='right'"
    ).fetchone()[0]
    from catalyst_data.dedup.relations import record_relation
    record_relation(conn, left, right, relation_type="same_release", rule_id="fixture",
                    rule_version="v1", basis="fixture", discovered_at="2026-01-01T00:00:00Z")
    record_origin_inputs(conn, right, provider="unknown", publisher=None, content_state=None,
                         kind="body", cik=None, recorded_at="2026-01-03T00:00:00Z")
    result = assign_origin(conn, left, at="2026-01-02T00:00:00Z", mode="observed", registry=())
    assert result["origin"] == "release:test:release-x"
    assert result["origin_status"] == "resolved"
    published = assign_origin(conn, left, at="2026-01-02T00:00:00Z", mode="published",
                              registry=(), snapshot_relation_ids={
                                  conn.execute("SELECT relation_id FROM dedup_relations").fetchone()[0]
                              }, registry_as_of="2026-11-01T00:00:00Z")
    assert published["origin_status"] == "conflict"


def test_priority_conflicts_and_higher_priority_precedence():
    conn = sqlite3.connect(":memory:")
    publisher_a = _add(conn, "publisher-a", provider="a", publisher="A")
    publisher_b = _add(conn, "publisher-b", provider="b", publisher="B")
    from catalyst_data.dedup.relations import record_relation
    record_relation(conn, publisher_a, publisher_b, relation_type="same_release", rule_id="fixture",
                    rule_version="v1", basis="fixture", discovered_at="2026-01-01T00:00:00Z")
    registry = (PublisherEntry("a", "A", "full_text", "family-a", "sample-a",
                               effective_from="1900-01-01T00:00:00Z"),
                PublisherEntry("b", "B", "full_text", "family-b", "sample-b",
                               effective_from="1900-01-01T00:00:00Z"))
    assert assign_origin(conn, publisher_a, at="2026-01-02T00:00:00Z", mode="observed",
                         registry=registry)["origin_status"] == "conflict"

    release = _add(conn, "release", provider="sec", publisher="SEC", kind="filing", cik="123",
                   release_id="r", release_namespace="test", release_id_confirmed=True)
    issuer = _add(conn, "issuer", provider="daily:sec", publisher="SEC", kind="filing", cik="456")
    record_relation(conn, release, issuer, relation_type="same_release", rule_id="fixture",
                    rule_version="v1", basis="fixture2", discovered_at="2026-01-01T00:00:00Z")
    result = assign_origin(conn, release, at="2026-01-02T00:00:00Z", mode="observed", registry=())
    assert result["origin"] == "release:test:r"

    cik_a = _add(conn, "cik-a", provider="sec", publisher=None, kind="filing", cik="123")
    cik_b = _add(conn, "cik-b", provider="daily:sec", publisher=None, kind="filing", cik="456")
    record_relation(conn, cik_a, cik_b, relation_type="same_release", rule_id="fixture",
                    rule_version="v1", basis="fixture3", discovered_at="2026-01-01T00:00:00Z")
    assert assign_origin(conn, cik_a, at="2026-01-02T00:00:00Z", mode="observed",
                         registry=())["origin_status"] == "conflict"


@pytest.mark.parametrize("cik", ["bad", "0000000000"])
def test_malformed_present_sec_filing_cik_is_rejected(cik):
    conn = sqlite3.connect(":memory:")
    record = _add(conn, "bad-cik", provider="sec", publisher=None, kind="filing", cik=cik,
                  release_id="r", release_namespace="test", release_id_confirmed=True)
    with pytest.raises(ValueError, match="CIK"):
        assign_origin(conn, record, at="2026-01-02T00:00:00Z", mode="observed", registry=())


def test_origin_equivalence_requires_same_priority_class_and_single_successor():
    conn = sqlite3.connect(":memory:")
    left = _add(conn, "left", publisher=None)
    right = _add(conn, "right", publisher=None)
    from catalyst_data.dedup.relations import record_relation
    relation_id = record_relation(conn, left, right, relation_type="same_release", rule_id="fixture",
                                  rule_version="v1", basis="fixture", discovered_at="2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="same priority"):
        record_equivalence(conn, "release:test:z", "pub:benzinga", discovered_at="2026-01-01T00:00:00Z",
                           relation_id=relation_id)
    record_equivalence(conn, "release:test:z", "release:test:a", discovered_at="2026-01-01T00:00:00Z",
                       relation_id=relation_id)
    with pytest.raises(ValueError, match="successor"):
        record_equivalence(conn, "release:test:z", "release:test:b", discovered_at="2026-01-01T00:00:00Z",
                           relation_id=relation_id)


def test_equivalence_discovery_cannot_precede_its_relation():
    conn = sqlite3.connect(":memory:")
    left = _add(conn, "left", publisher=None)
    right = _add(conn, "right", publisher=None)
    from catalyst_data.dedup.relations import record_relation
    relation_id = record_relation(conn, left, right, relation_type="same_release", rule_id="fixture",
                                  rule_version="v1", basis="fixture", discovered_at="2026-01-02T00:00:00Z")
    with pytest.raises(ValueError, match="cannot precede"):
        record_equivalence(conn, "release:test:z", "release:test:a",
                           discovered_at="2026-01-01T00:00:00Z", relation_id=relation_id)


def test_converging_equivalence_chains_are_not_cycles():
    conn = sqlite3.connect(":memory:")
    left = _add(conn, "left", publisher=None)
    right = _add(conn, "right", publisher=None)
    from catalyst_data.dedup.relations import record_relation
    relation_id = record_relation(conn, left, right, relation_type="same_release", rule_id="fixture",
                                  rule_version="v1", basis="fixture", discovered_at="2026-01-01T00:00:00Z")
    record_equivalence(conn, "release:test:z", "release:test:a", discovered_at="2026-01-01T00:00:00Z",
                       relation_id=relation_id)
    assert record_equivalence(conn, "release:test:y", "release:test:a",
                              discovered_at="2026-01-01T00:00:00Z", relation_id=relation_id)


def test_origin_reads_do_not_create_missing_tables_and_registry_hash_is_returned():
    conn = sqlite3.connect(":memory:")
    from catalyst_data.dedup.relations import ensure_schema
    ensure_schema(conn)
    with pytest.raises(LookupError, match="origin_inputs"):
        assign_origin(conn, "missing", at="2026-01-01T00:00:00Z", mode="observed")
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='origin_inputs'").fetchall() == []
    record = _add(conn, "hash")
    result = assign_origin(conn, record, at="2026-01-02T00:00:00Z", mode="observed")
    from catalyst_data.dedup.publisher_registry import registry_sha256
    assert result["registry_sha256"] == registry_sha256(())


def test_origin_inputs_and_equivalences_are_append_only():
    conn = sqlite3.connect(":memory:")
    record = _add(conn, "append-only", publisher=None, release_id="z", release_namespace="test",
                  release_id_confirmed=True)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM origin_inputs WHERE record_key=?", (record,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE origin_inputs SET provider='changed' WHERE record_key=?", (record,))
    other = _add(conn, "append-only-other", publisher=None, release_id="a", release_namespace="test",
                 release_id_confirmed=True)
    from catalyst_data.dedup.relations import record_relation
    relation_id = record_relation(conn, record, other, relation_type="same_release", rule_id="fixture",
                                  rule_version="v1", basis="fixture", discovered_at="2026-01-01T00:00:00Z")
    record_equivalence(conn, "release:test:z", "release:test:a", discovered_at="2026-01-01T00:00:00Z",
                       relation_id=relation_id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM origin_equivalences WHERE relation_id=?", (relation_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE origin_equivalences SET new_origin='release:test:b' "
                     "WHERE relation_id=?", (relation_id,))


def test_publisher_registry_observed_effective_interval_and_hash():
    conn = sqlite3.connect(":memory:")
    record = _add(conn, "effective", provider="later", publisher="Publisher")
    entry = PublisherEntry("later", "Publisher", "full_text", "publisher", "sample",
                           effective_from="2026-02-01T00:00:00Z",
                           effective_until="2026-03-01T00:00:00Z")

    before = assign_origin(conn, record, at="2026-01-31T23:59:59Z", mode="observed",
                           registry=(entry,))
    starts = assign_origin(conn, record, at="2026-02-01T00:00:00Z", mode="observed",
                           registry=(entry,))
    ends = assign_origin(conn, record, at="2026-03-01T00:00:00Z", mode="observed",
                         registry=(entry,))

    assert before["origin"] is None
    assert starts["origin"] == "pub:publisher"
    assert ends["origin"] is None
    from catalyst_data.dedup.publisher_registry import registry_sha256
    assert before["registry_sha256"] == registry_sha256(())
    assert starts["registry_sha256"] == registry_sha256((entry,))


def test_published_registry_requires_as_of_and_uses_pinned_effective_time():
    conn = sqlite3.connect(":memory:")
    record = _add(conn, "published-effective", provider="later", publisher="Publisher")
    entry = PublisherEntry("later", "Publisher", "full_text", "publisher", "sample",
                           effective_from="2026-02-01T00:00:00Z")
    with pytest.raises(ValueError, match="registry_as_of"):
        assign_origin(conn, record, at="2026-03-01T00:00:00Z", mode="published",
                      registry=(entry,), snapshot_relation_ids=set())
    before_effective = assign_origin(conn, record, at="2026-01-01T00:00:00Z", mode="published",
                                     registry=(entry,), snapshot_relation_ids=set(),
                                     registry_as_of="2026-01-01T00:00:00Z")
    pinned_effective = assign_origin(conn, record, at="2026-01-01T00:00:00Z", mode="published",
                                     registry=(entry,), snapshot_relation_ids=set(),
                                     registry_as_of="2026-02-01T00:00:00Z")
    assert before_effective["origin"] is None
    assert pinned_effective["origin"] == "pub:publisher"


def test_published_registry_hash_excludes_later_successor_interval():
    conn = sqlite3.connect(":memory:")
    record = _add(conn, "published-successor", provider="later", publisher="Publisher")
    first = PublisherEntry("later", "Publisher", "full_text", "publisher", "first",
                           effective_from="2026-01-01T00:00:00Z",
                           effective_until="2026-02-01T00:00:00Z")
    successor = PublisherEntry("later", "Publisher", "full_text", "publisher", "second",
                               effective_from="2026-02-01T00:00:00Z")
    at_first = assign_origin(conn, record, at="2026-01-15T00:00:00Z", mode="observed",
                             registry=(first,))
    with_successor = assign_origin(conn, record, at="2026-01-15T00:00:00Z", mode="observed",
                                   registry=(first, successor))

    assert at_first["origin"] == with_successor["origin"] == "pub:publisher"
    assert at_first["registry_sha256"] == with_successor["registry_sha256"]


def test_publisher_registry_rejects_overlapping_duplicates_and_allows_successors():
    conn = sqlite3.connect(":memory:")
    record = _add(conn, "successor", provider="later", publisher="Publisher")
    first = PublisherEntry("later", "Publisher", "full_text", "publisher", "first",
                           effective_from="2026-01-01T00:00:00Z",
                           effective_until="2026-02-01T00:00:00Z")
    successor = PublisherEntry("later", "Publisher", "full_text", "publisher", "second",
                               effective_from="2026-02-01T00:00:00Z")
    overlapping = PublisherEntry("later", "Publisher", "full_text", "publisher", "overlap",
                                 effective_from="2026-01-15T00:00:00Z")
    with pytest.raises(ValueError, match="overlapping duplicate"):
        assign_origin(conn, record, at="2026-01-20T00:00:00Z", mode="observed",
                      registry=(first, overlapping))
    result = assign_origin(conn, record, at="2026-02-01T00:00:00Z", mode="observed",
                           registry=(first, successor))
    assert result["origin"] == "pub:publisher"
    from catalyst_data.dedup.publisher_registry import registry_sha256
    assert result["registry_sha256"] == registry_sha256((successor,))


def test_origin_read_rows_are_bounded_by_reachable_component_and_equivalences():
    def count_reads(unrelated_relation_count):
        conn = sqlite3.connect(":memory:")
        target = _add(conn, f"target-{unrelated_relation_count}", publisher=None,
                      release_id="z", release_namespace="test", release_id_confirmed=True)
        peer = _add(conn, f"peer-{unrelated_relation_count}", publisher=None,
                    release_id="a", release_namespace="test", release_id_confirmed=True)
        from catalyst_data.dedup.relations import record_relation
        component_relation = record_relation(
            conn, target, peer, relation_type="same_release", rule_id="component",
            rule_version="v1", basis="component", discovered_at="2026-01-01T00:00:00Z",
        )
        record_equivalence(conn, "release:test:z", "release:test:a",
                           discovered_at="2026-01-01T00:00:00Z",
                           relation_id=component_relation)
        unrelated_relations = [
            (f"unrelated-relation-{index:04d}", f"other-{index:04d}",
             f"other-peer-{index:04d}", f"unrelated-{index:04d}")
            for index in range(unrelated_relation_count)
        ]
        conn.executemany(
            "INSERT INTO dedup_relations "
            "(relation_id,record_key_a,record_key_b,relation_type,rule_id,rule_version,basis,"
            "discovered_at,status,retracted_at,retraction_reason) "
            "VALUES (?, ?, ?, 'same_release', ?, 'v1', 'unrelated', "
            "'2026-01-01T00:00:00Z', 'active', NULL, NULL)", unrelated_relations,
        )
        conn.executemany(
            "INSERT INTO origin_equivalences VALUES (?, ?, ?, ?)",
            [(f"release:unrelated:{index}", f"release:unrelated-next:{index}",
              "2026-01-01T00:00:00Z", f"unrelated-relation-{index:04d}")
             for index in range(unrelated_relation_count)],
        )
        fetched = {"count": 0}
        statements = []
        def count_row(_cursor, row):
            fetched["count"] += 1
            return row
        conn.row_factory = count_row
        conn.set_trace_callback(statements.append)
        result = assign_origin(conn, target, at="2026-01-02T00:00:00Z",
                               mode="observed", registry=())
        assert result["origin"] == "release:test:a"
        bounded_rows = fetched["count"]
        assert any("FROM origin_equivalences" in sql for sql in statements)
        assert any("FROM dedup_relations" in sql for sql in statements)
        fetched["count"] = 0
        full_relations = conn.execute("SELECT * FROM dedup_relations").fetchall()
        full_scan_rows = fetched["count"]
        assert full_scan_rows == len(full_relations)
        conn.close()
        return bounded_rows, full_scan_rows

    small = count_reads(100)
    large = count_reads(2000)
    assert small[0] == large[0]
    assert large[1] > small[1]


def test_legacy_conflicting_visible_equivalence_successors_return_conflict():
    conn = sqlite3.connect(":memory:")
    a = _add(conn, "a", publisher=None, release_id="z", release_namespace="test",
             release_id_confirmed=True)
    b = _add(conn, "b", publisher=None, release_id="a", release_namespace="test",
             release_id_confirmed=True)
    d = _add(conn, "d", publisher=None)
    e = _add(conn, "e", publisher=None)
    from catalyst_data.dedup.relations import record_relation
    relation_a = record_relation(conn, a, b, relation_type="same_release", rule_id="fixture-a",
                                 rule_version="v1", basis="a", discovered_at="2026-01-01T00:00:00Z")
    relation_b = record_relation(conn, d, e, relation_type="same_release", rule_id="fixture-b",
                                 rule_version="v1", basis="b", discovered_at="2026-01-01T00:00:00Z")
    # Simulate legacy rows that predate the one-successor writer validation.
    conn.execute("INSERT INTO origin_equivalences VALUES (?,?,?,?)",
                 ("release:test:z", "release:test:a", "2026-01-01T00:00:00Z", relation_a))
    conn.execute("INSERT INTO origin_equivalences VALUES (?,?,?,?)",
                 ("release:test:z", "release:test:b", "2026-01-01T00:00:00Z", relation_b))
    result = assign_origin(conn, a, at="2026-01-02T00:00:00Z", mode="observed", registry=())
    assert result["origin"] is None
    assert result["origin_status"] == "conflict"


def test_equivalence_visibility_uses_its_relation_outside_record_component():
    conn = sqlite3.connect(":memory:")
    a = _add(conn, "equiv-a", publisher=None, release_id="a", release_namespace="test",
             release_id_confirmed=True)
    b = _add(conn, "equiv-b", publisher=None, release_id="b", release_namespace="test",
             release_id_confirmed=True)
    c = _add(conn, "equiv-c", publisher=None, release_id="z", release_namespace="test",
             release_id_confirmed=True)
    from catalyst_data.dedup.relations import record_relation
    relation_id = record_relation(
        conn, a, b, relation_type="same_release", rule_id="fixture",
        rule_version="v1", basis="ab", discovered_at="2026-01-03T00:00:00Z",
    )
    record_equivalence(conn, "release:test:z", "release:test:a",
                       discovered_at="2026-01-03T00:00:00Z", relation_id=relation_id)

    before = assign_origin(conn, c, at="2026-01-02T00:00:00Z", mode="observed", registry=())
    visible = assign_origin(conn, c, at="2026-01-04T00:00:00Z", mode="observed", registry=())

    assert relation_id not in before["relation_ids"]
    assert relation_id not in visible["relation_ids"]
    assert before["origin"] == "release:test:z"
    assert visible["origin"] == "release:test:a"


def test_release_equivalence_accepts_colon_ids_and_fingerprint_versions():
    from catalyst_data.dedup.origin_assign import _valid_origin

    assert _valid_origin("release:sec:accn:with:colons")
    assert _valid_origin("release:fp:sha256.full-v1:abcdef12")
    assert not _valid_origin("release:fp:bad version:abcdef12")


def test_fingerprint_origin_cannot_be_equivalent_to_a_release_id():
    from catalyst_data.dedup.origin_assign import _origin_class

    assert _origin_class("release:fp:sha256-full-v1:" + "a" * 16) != _origin_class("release:prn:123")


def test_release_namespace_with_colon_is_rejected():
    from catalyst_data.dedup.origin_assign import _release_origins

    with pytest.raises(ValueError, match="namespace"):
        _release_origins([{"release_id_confirmed": True, "release_namespace": "sec:accn", "release_id": "x"}])
    assert _release_origins([{"release_id_confirmed": True, "release_namespace": "SEC",
                              "release_id": "a:b"}])[0] == {"release:sec:a:b"}


def test_closing_an_interval_keeps_the_read_hash_recorded_while_it_was_open():
    from catalyst_data.dedup.publisher_registry import registry_sha256

    open_entry = PublisherEntry("alpaca", "benzinga", "full_text", "benzinga", "sample-1",
                                effective_from="2026-11-01T00:00:00Z")
    closed = PublisherEntry("alpaca", "benzinga", "full_text", "benzinga", "sample-1",
                            effective_from="2026-11-01T00:00:00Z", effective_until="2026-12-01T00:00:00Z")
    assert registry_sha256((open_entry,)) == registry_sha256((closed,))
    moved = PublisherEntry("alpaca", "benzinga", "full_text", "benzinga", "sample-1",
                           effective_from="2026-11-02T00:00:00Z")
    assert registry_sha256((open_entry,)) != registry_sha256((moved,))
