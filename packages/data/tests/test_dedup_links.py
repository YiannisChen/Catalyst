"""Deterministic duplicate links preserve records and unresolved origins."""
from __future__ import annotations

import sqlite3

import pytest
from catalyst_data.dedup.links import (
    EvidenceRecord,
    append_origin_assertion,
    classify_pair,
    ensure_schema,
    list_duplicate_links,
    origin_for,
    persist_pair_links,
)


def _record(record_id: str, **overrides: object) -> EvidenceRecord:
    values: dict[str, object] = {
        "record_id": record_id,
        "provider": "alpaca",
        "title": "Acme reports quarterly results",
    }
    values.update(overrides)
    return EvidenceRecord(**values)  # type: ignore[arg-type]


def test_provider_record_id_is_scoped_to_provider() -> None:
    left = _record("a", provider_record_id="123")
    same = _record("b", provider_record_id="123")
    other_provider = _record("c", provider="benzinga", provider_record_id="123")

    assert classify_pair(left, same) == {("same_article", "provider_record_id")}
    assert classify_pair(left, other_provider) == set()


def test_canonical_url_matches_while_removing_tracking_and_amp_forms() -> None:
    left = _record("a", url="https://www.example.com/story?utm_source=x#top")
    right = _record("b", provider="benzinga", url="https://www.example.com/story/amp")

    assert classify_pair(left, right) == {("same_article", "canonical_url")}


def test_non_default_port_is_not_removed_during_url_identity_normalization() -> None:
    left = _record("a", url="http://www.example.com:443/story")
    right = _record("b", url="http://www.example.com/story")

    assert classify_pair(left, right) == set()


def test_confirmed_release_id_creates_same_release_link() -> None:
    left = _record(
        "a", release_id="BW-42", release_namespace="businesswire", release_id_confirmed=True
    )
    right = _record(
        "b", provider="benzinga", release_id="BW-42", release_namespace="businesswire",
        release_id_confirmed=True,
    )

    assert classify_pair(left, right) == {("same_release", "confirmed_release_id")}


def test_confirmed_versioned_full_body_fingerprint_links_releases() -> None:
    fingerprint = "a" * 64
    left = _record(
        "a", body_fingerprint=fingerprint, fingerprint_version="sha256-full-v1",
        body_fingerprint_confirmed=True,
    )
    right = _record(
        "b", provider="benzinga", body_fingerprint=fingerprint,
        fingerprint_version="sha256-full-v1", body_fingerprint_confirmed=True,
    )

    assert classify_pair(left, right) == {("same_release", "full_body_fingerprint")}


@pytest.mark.parametrize(
    "left,right",
    [
        (_record("a", title="Acme reports results"), _record("b", title="Acme reports results")),
        (
            _record("a", body_fingerprint="a" * 64, fingerprint_version="v1"),
            _record("b", provider="benzinga", body_fingerprint="a" * 64, fingerprint_version="v1"),
        ),
        (
            _record("a", release_id="BW-42", release_namespace="businesswire"),
            _record("b", provider="benzinga", release_id="BW-42", release_namespace="businesswire"),
        ),
    ],
)
def test_unconfirmed_similarity_or_identifiers_never_establish_link(
    left: EvidenceRecord, right: EvidenceRecord
) -> None:
    assert classify_pair(left, right) == set()


def test_pair_links_are_append_only_and_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    left = _record("a", provider_record_id="p-1")
    right = _record("b", provider_record_id="p-1")

    assert persist_pair_links(conn, left, right) == 1
    assert persist_pair_links(conn, right, left) == 0
    assert list_duplicate_links(conn) == [
        ("a", "b", "same_article", "provider_record_id")
    ]
    assert conn.execute("SELECT COUNT(*) FROM duplicate_links").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM duplicate_links")


def test_origins_remain_unresolved_without_explicit_evidence() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    append_origin_assertion(conn, "record-1", origin_id=None, method="unresolved")
    assert origin_for(conn, "record-1") is None


def test_conflicting_origin_assertions_resolve_to_unresolved() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    append_origin_assertion(conn, "record-1", origin_id="publisher:acme", method="byline")
    append_origin_assertion(conn, "record-1", origin_id="publisher:wire", method="release_metadata")

    assert origin_for(conn, "record-1") is None

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE origin_assertions SET origin_id = 'publisher:chosen'")


def test_identical_origin_assertion_is_idempotent_and_distinct_records_share_origin() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    assert append_origin_assertion(
        conn, "record-1", origin_id="release:BW-42", method="confirmed_release_id"
    )
    assert not append_origin_assertion(
        conn, "record-1", origin_id="release:BW-42", method="confirmed_release_id"
    )
    append_origin_assertion(
        conn, "record-2", origin_id="release:BW-42", method="confirmed_release_id"
    )

    assert origin_for(conn, "record-1") == "release:BW-42"
    assert origin_for(conn, "record-2") == "release:BW-42"


def test_schema_setup_never_commits_a_callers_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sentinel (value TEXT)")
    conn.execute("INSERT INTO sentinel VALUES ('uncommitted')")

    ensure_schema(conn)

    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM sentinel").fetchone() == (0,)
