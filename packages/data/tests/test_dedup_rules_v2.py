from __future__ import annotations

import sqlite3
from itertools import combinations

import pytest
from catalyst_data.dedup import origin_assign, relations
from catalyst_data.dedup.ingestion import ingest_record
from catalyst_data.dedup.links import EvidenceRecord
from catalyst_data.dedup.origin_assign import record_origin_inputs
from catalyst_data.dedup.relations import (
    ensure_schema,
    legacy_relation_id,
    record_relation,
)
from catalyst_data.dedup.rules_v2 import (
    RULE_VERSION,
    classify_pair_v2,
    rerun_relations_v2,
)

AT = "2026-11-02T00:00:00Z"
FP = "a" * 64


def _add(conn, name, *, provider="alpaca", publisher="benzinga", content_state="full_text",
         kind="body", record=None, recorded_at=AT):
    record = record or EvidenceRecord(
        record_id=name, provider=provider, url="https://example.test/shared",
    )
    ingest_record(conn, record, store_id="store", source_id=name, version_id="v1",
                  corrected_at="2026-01-01T00:00:00Z", correction_ref="fixture:time")
    key = conn.execute(
        "SELECT record_id FROM dedup_candidate_records WHERE document_id=?", (name,)
    ).fetchone()[0]
    if publisher is not ...:
        record_origin_inputs(conn, key, provider=provider, publisher=publisher,
                             content_state=content_state, kind=kind, cik=None,
                             recorded_at=recorded_at)
    return key


def _relations(conn, version=RULE_VERSION):
    return {
        (row[0], row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT record_key_a,record_key_b,relation_type,rule_id FROM dedup_relations "
            "WHERE rule_version=? AND status='active' ORDER BY record_key_a,record_key_b,rule_id",
            (version,),
        )
    }


def test_v2_relation_set_gates_urls_and_reports_missing_or_null_publishers():
    conn = sqlite3.connect(":memory:")
    fiscal_a = _add(conn, "fiscal-a", publisher="fiscal.ai")
    fiscal_b = _add(conn, "fiscal-b", publisher="fiscal.ai")
    benzinga_a = _add(conn, "benzinga-a")
    benzinga_b = _add(conn, "benzinga-b")
    null_a = _add(conn, "null-a", publisher=None)
    null_b = _add(conn, "null-b", publisher=None)
    missing_a = _add(conn, "missing-a", publisher=...)
    missing_b = _add(conn, "missing-b", publisher=...)

    report = rerun_relations_v2(conn, discovered_at=AT)

    assert _relations(conn) == {
        (benzinga_a, benzinga_b, "same_article", "canonical_url"),
    }
    assert {row[0] for row in conn.execute(
        "SELECT rule_id FROM dedup_relations WHERE rule_version=?", (RULE_VERSION,)
    )} == {"canonical_url"}
    undecidable = {null_a, null_b, missing_a, missing_b}
    assert report["canonical_url_gaps"] == [
        list(pair) for pair in combinations(sorted((fiscal_a, fiscal_b, benzinga_a, benzinga_b,
                                                     null_a, null_b, missing_a, missing_b)), 2)
        if undecidable.intersection(pair)
    ]
    # Only the records whose publisher is missing, not their URL partners.
    assert report["publisher_missing_record_keys"] == sorted(undecidable)


def test_a_publisher_missing_record_without_a_url_partner_is_still_reported():
    conn = sqlite3.connect(":memory:")
    lonely = _add(conn, "lonely", publisher=None, record=EvidenceRecord(
        record_id="lonely", provider="alpaca", url="https://example.test/only-one"))
    _add(conn, "benzinga-a")
    report = rerun_relations_v2(conn, discovered_at=AT)
    assert report["publisher_missing_record_keys"] == [lonely]
    assert report["canonical_url_gaps"] == []


def test_v2_derivation_gate_rejects_times_outside_both_registry_windows(monkeypatch):
    from catalyst_data.dedup import rules_v2, url_identity_registry
    from catalyst_data.dedup.publisher_registry import PublisherEntry

    monkeypatch.setattr(url_identity_registry, "URL_IDENTITY_REGISTRY", (
        url_identity_registry.UrlIdentityEntry("x", "y", "article", "2026-10-21T00:00:00Z",
                                               effective_until="2026-11-04T00:00:00Z"),
    ))
    monkeypatch.setattr(rules_v2, "PUBLISHER_REGISTRY", (
        PublisherEntry("x", "y", "headline", "y", "e", "2026-11-01T00:00:00Z",
                       effective_until="2026-11-03T00:00:00Z"),
    ))
    from catalyst_data.dedup import publisher_registry
    monkeypatch.setattr(publisher_registry, "PUBLISHER_REGISTRY", rules_v2.PUBLISHER_REGISTRY)
    monkeypatch.setattr(rules_v2, "PINNED_URL_REGISTRY_SHA256",
                        url_identity_registry.url_registry_sha256())
    monkeypatch.setattr(rules_v2, "PINNED_PUBLISHER_REGISTRY_SHA256",
                        publisher_registry.registry_sha256())
    conn = sqlite3.connect(":memory:")
    for discovered_at in (
        "2026-10-20T00:00:00Z", "2026-10-31T00:00:00Z",
        "2026-11-03T00:00:00Z", "2026-11-04T00:00:00Z",
    ):
        with pytest.raises(ValueError):
            rules_v2.rerun_relations_v2(conn, discovered_at=discovered_at)
    rules_v2.rerun_relations_v2(conn, discovered_at=AT)


def test_v2_registry_pin_mismatch_raises_without_rewriting_existing_rows(monkeypatch):
    from catalyst_data.dedup import url_identity_registry

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    prior = record_relation(conn, "a", "b", relation_type="same_article", rule_id="canonical_url",
                            rule_version=RULE_VERSION, basis="https://example.test/shared",
                            discovered_at=AT)
    before = conn.execute("SELECT * FROM dedup_relations ORDER BY relation_id,status").fetchall()
    original_url_registry = url_identity_registry.URL_IDENTITY_REGISTRY
    monkeypatch.setattr(url_identity_registry, "URL_IDENTITY_REGISTRY", ())
    with pytest.raises(ValueError):
        rerun_relations_v2(conn, discovered_at=AT)
    assert conn.execute("SELECT * FROM dedup_relations ORDER BY relation_id,status").fetchall() == before
    monkeypatch.setattr(url_identity_registry, "URL_IDENTITY_REGISTRY",
                        original_url_registry)
    monkeypatch.setattr(url_identity_registry, "URL_IDENTITY_REGISTRY", (
        url_identity_registry.UrlIdentityEntry(
            "alpaca", "fiscal.ai", "attribution", "2026-10-21T00:00:00Z",
            evidence_ref="changed registry evidence",
        ),
    ))
    from catalyst_data.dedup import publisher_registry
    monkeypatch.setattr(publisher_registry, "PUBLISHER_REGISTRY", (
        *publisher_registry.PUBLISHER_REGISTRY,
        publisher_registry.PublisherEntry("new", "publisher", "headline", "new", "evidence",
                                          "2026-11-01T00:00:00Z"),
    ))
    with pytest.raises(ValueError):
        rerun_relations_v2(conn, discovered_at=AT)
    assert conn.execute("SELECT * FROM dedup_relations ORDER BY relation_id,status").fetchall() == before
    assert prior


def test_same_version_rerun_keeps_original_discovery_time_and_adds_new_identity():
    conn = sqlite3.connect(":memory:")
    a = _add(conn, "a")
    b = _add(conn, "b")
    rerun_relations_v2(conn, discovered_at=AT)
    first = conn.execute("SELECT relation_id,discovered_at FROM dedup_relations WHERE rule_version=?",
                         (RULE_VERSION,)).fetchall()
    c = _add(conn, "c")
    rerun_relations_v2(conn, discovered_at="2026-11-02T01:00:00Z")
    rows = conn.execute("SELECT record_key_a,record_key_b,discovered_at FROM dedup_relations "
                        "WHERE rule_version=? AND status='active' ORDER BY record_key_a,record_key_b",
                        (RULE_VERSION,)).fetchall()
    assert {(x[0], x[1]) for x in rows} == {(a, b), (a, c), (b, c)}
    assert {(x[0], x[1]) for x in rows if x[2] == AT} == {(a, b)}
    assert {(x[0], x[1]) for x in rows if x[2] == "2026-11-02T01:00:00Z"} == {(a, c), (b, c)}
    assert len(first) == 1


def test_v2_published_snapshot_rejects_legacy_other_version_and_missing_ids():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    v2 = record_relation(conn, "a", "b", relation_type="same_release", rule_id="fixture",
                         rule_version=RULE_VERSION, basis="fixture", discovered_at=AT)
    v1 = record_relation(conn, "c", "d", relation_type="same_release", rule_id="fixture",
                         rule_version="d1-rules-v1", basis="fixture", discovered_at=AT)
    for ids in ({legacy_relation_id("e", "f", "same_release", "fixture")}, {v1}, {"missing"}):
        with pytest.raises(ValueError):
            relations.visible_relations(conn, mode="published", at=AT,
                                        snapshot_relation_ids=ids, rule_version=RULE_VERSION)
    assert [r["relation_id"] for r in relations.visible_relations(
        conn, mode="published", at=AT, snapshot_relation_ids={v2}, rule_version=RULE_VERSION
    )] == [v2]


def test_v2_visible_relations_never_reads_duplicate_links_or_other_versions():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    rid = record_relation(conn, "a", "b", relation_type="same_release", rule_id="fixture",
                          rule_version=RULE_VERSION, basis="fixture", discovered_at=AT)
    record_relation(conn, "c", "d", relation_type="same_release", rule_id="fixture",
                    rule_version="d1-rules-v1", basis="fixture", discovered_at=AT)
    conn.execute("INSERT INTO duplicate_links VALUES ('legacy-a','legacy-b','same_release','fixture')")
    statements = []
    conn.set_trace_callback(statements.append)
    rows = relations.visible_relations(conn, mode="observed", at=AT, rule_version=RULE_VERSION)
    assert [row["relation_id"] for row in rows] == [rid]
    assert all("FROM duplicate_links" not in stmt for stmt in statements)


def test_v2_observed_read_ignores_retractions_of_other_versions():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    keep = record_relation(conn, "a", "b", relation_type="same_release", rule_id="fixture",
                           rule_version=RULE_VERSION, basis="fixture", discovered_at=AT)
    old = record_relation(conn, "c", "d", relation_type="same_release", rule_id="fixture",
                          rule_version="d1-rules-v1", basis="fixture", discovered_at=AT)
    record_relation(conn, "c", "d", relation_type="same_release", rule_id="fixture",
                    rule_version="d1-rules-v1", basis="fixture", discovered_at=AT,
                    status="retracted", retracted_at=AT, retraction_reason="fixture")
    statements = []
    conn.set_trace_callback(statements.append)
    rows = relations.visible_relations(conn, mode="observed", at=AT, rule_version=RULE_VERSION)
    assert [row["relation_id"] for row in rows] == [keep]
    selected = [stmt for stmt in statements if stmt.startswith("SELECT relation_id")]
    assert selected and all("rule_version=" in stmt and "((status='active'" in stmt for stmt in selected)
    assert old != keep


def test_a_publisher_registry_change_alone_breaks_the_v2_pin(monkeypatch):
    from catalyst_data.dedup import publisher_registry

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    monkeypatch.setattr(publisher_registry, "PUBLISHER_REGISTRY", (
        *publisher_registry.PUBLISHER_REGISTRY,
        publisher_registry.PublisherEntry("new", "publisher", "headline", "new", "evidence",
                                          "2026-11-01T00:00:00Z"),
    ))
    with pytest.raises(ValueError, match="publisher registry SHA"):
        rerun_relations_v2(conn, discovered_at=AT)


def test_fingerprint_relation_type_uses_exact_registered_family_match():
    left = EvidenceRecord(record_id="left", provider="alpaca", body_fingerprint=FP,
                          fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True)
    same = EvidenceRecord(record_id="same", provider="alpaca", body_fingerprint=FP,
                          fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True)
    other_family = EvidenceRecord(record_id="other", provider="polygon", body_fingerprint=FP,
                                  fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True)
    unknown = EvidenceRecord(record_id="unknown", provider="fiscal", body_fingerprint=FP,
                             fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True)
    assert classify_pair_v2(left, same, ("alpaca", "benzinga", "full_text", "body"),
                            ("alpaca", "benzinga", "full_text", "body")) == {
        ("same_article", "full_body_fingerprint")
    }
    assert classify_pair_v2(left, other_family, ("alpaca", "benzinga", "full_text", "body"),
                            ("polygon", "GlobeNewswire Inc.", "headline", "headline")) == {
        ("same_release", "full_body_fingerprint")
    }
    assert classify_pair_v2(left, unknown, ("alpaca", "benzinga", "full_text", "body"), None) == {
        ("same_release", "full_body_fingerprint")
    }


def test_v2_components_merge_only_same_release_edges_of_selected_version():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    record_relation(conn, "a", "b", relation_type="same_article", rule_id="full_body_fingerprint",
                    rule_version=RULE_VERSION, basis="fingerprint", discovered_at=AT)
    record_relation(conn, "b", "c", relation_type="same_release", rule_id="full_body_fingerprint",
                    rule_version="d1-rules-v1", basis="fingerprint", discovered_at=AT)
    record_relation(conn, "c", "d", relation_type="same_release", rule_id="fixture",
                    rule_version=RULE_VERSION, basis="fixture", discovered_at=AT)
    conn.execute("INSERT INTO duplicate_links VALUES ('d','e','same_release','legacy')")
    members, relation_ids = origin_assign._components(
        conn, "a", mode="observed", at=AT, snapshot_relation_ids=None, rule_version=RULE_VERSION
    )
    assert members == {"a"}
    assert relation_ids == []
    members, relation_ids = origin_assign._components(
        conn, "d", mode="observed", at=AT, snapshot_relation_ids=None, rule_version=RULE_VERSION
    )
    assert members == {"c", "d"}
    assert relation_ids == [conn.execute(
        "SELECT relation_id FROM dedup_relations WHERE record_key_a='c' AND record_key_b='d' "
        "AND rule_version=?", (RULE_VERSION,)
    ).fetchone()[0]]


def test_alpaca_benzinga_same_url_remains_v2_canonical_url():
    conn = sqlite3.connect(":memory:")
    a = _add(conn, "benzinga-a", provider="alpaca", publisher="benzinga")
    b = _add(conn, "benzinga-b", provider="alpaca", publisher="benzinga")
    report = rerun_relations_v2(conn, discovered_at=AT)
    assert _relations(conn) == {(a, b, "same_article", "canonical_url")}
    assert report["counts_by_rule"] == {"canonical_url": 1}


def test_fiscal_shared_url_is_excluded_but_identical_fiscal_bodies_link():
    conn = sqlite3.connect(":memory:")
    a = _add(conn, "fiscal-a", publisher="fiscal.ai", record=EvidenceRecord(
        record_id="fiscal-a", provider="alpaca", url="https://example.test/shared",
        body_fingerprint=FP, fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True))
    b = _add(conn, "fiscal-b", publisher="fiscal.ai", record=EvidenceRecord(
        record_id="fiscal-b", provider="alpaca", url="https://example.test/shared",
        body_fingerprint=FP, fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True))
    rerun_relations_v2(conn, discovered_at=AT)
    assert _relations(conn) == {(a, b, "same_release", "full_body_fingerprint")}
    from catalyst_data.dedup.relations import rerun_legacy_relations
    legacy_conn = sqlite3.connect(":memory:")
    la = _add(legacy_conn, "fiscal-a", publisher="fiscal.ai", record=EvidenceRecord(
        record_id="fiscal-a", provider="alpaca", url="https://example.test/shared",
        body_fingerprint=FP, fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True))
    lb = _add(legacy_conn, "fiscal-b", publisher="fiscal.ai", record=EvidenceRecord(
        record_id="fiscal-b", provider="alpaca", url="https://example.test/shared",
        body_fingerprint=FP, fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True))
    rerun_legacy_relations(legacy_conn, rule_version="d1-rules-v1", discovered_at=AT)
    assert {(row[0], row[1], row[2], row[3]) for row in legacy_conn.execute(
        "SELECT record_key_a,record_key_b,relation_type,rule_id FROM dedup_relations"
    )} == {
        (la, lb, "same_article", "canonical_url"),
        (la, lb, "same_release", "full_body_fingerprint"),
    }
    assert classify_pair_v2(
        EvidenceRecord(record_id="x", provider="alpaca", url="https://x.test/shared"),
        EvidenceRecord(record_id="y", provider="alpaca", url="https://x.test/shared"),
        ("alpaca", "fiscal.ai", "full_text", "body"),
        ("alpaca", "fiscal.ai", "full_text", "body"),
    ) == set()


def test_v2_origin_component_and_supersede_reads_ignore_legacy_and_v1_rows():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    a = _add(conn, "a", publisher="benzinga")
    b = _add(conn, "b", publisher="benzinga")
    c = _add(conn, "c", publisher="benzinga")
    v2 = record_relation(conn, a, b, relation_type="same_release", rule_id="fixture",
                         rule_version=RULE_VERSION, basis="fixture", discovered_at=AT)
    record_relation(conn, b, c, relation_type="same_release", rule_id="fixture",
                    rule_version="d1-rules-v1", basis="fixture", discovered_at=AT)
    conn.execute("INSERT INTO duplicate_links VALUES (?,?,'same_release','legacy')", (b, c))
    members, ids = origin_assign._components(
        conn, a, mode="observed", at=AT, snapshot_relation_ids=None, rule_version=RULE_VERSION
    )
    assert members == {a, b}
    assert ids == [v2]
    from catalyst_data.dedup.origins import supersede_release_group
    assert supersede_release_group(conn, a, release_origin="release:test:r", evidence_ref="fixture",
                                   rule_version=RULE_VERSION) == tuple(sorted((a, b)))
