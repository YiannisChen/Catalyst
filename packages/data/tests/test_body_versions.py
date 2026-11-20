"""Synthetic regression contract for full-body append-only version storage."""
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from catalyst_data.storage import body_versions as V

T = datetime(2026, 10, 23, 17, tzinfo=timezone.utc)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    V.ensure_schema(conn)
    conn.commit()
    yield conn
    conn.close()


def put(db, **changes):
    values = dict(source_id="alpaca", document_id="42", title="Synthetic release",
                  body="Full synthetic body.\n\nSecond paragraph.", observed_at=T,
                  published_at=T - timedelta(minutes=10),
                  provider_updated_at=T - timedelta(minutes=5))
    values.update(changes)
    return V.put_version(db, **values)


def versions(db, source="alpaca", document="42"):
    return V.list_versions(db, source_id=source, document_id=document)


def test_complete_exact_body_title_and_time_provenance_are_preserved(db):
    ident = put(db)
    row, = versions(db)
    assert row["version_id"] == ident
    assert row["body"] == "Full synthetic body.\n\nSecond paragraph."
    assert row["body_sha256"] == hashlib.sha256(row["body"].encode()).hexdigest()
    assert row["title"] == "Synthetic release"
    assert row["observed_at"] == row["first_seen_at"] == T.isoformat(timespec="microseconds")
    assert row["provider_updated_at"] == (T - timedelta(minutes=5)).isoformat(timespec="microseconds")
    assert row["published_at"] == (T - timedelta(minutes=10)).isoformat(timespec="microseconds")
    assert row["provenance"] == "versioned"
    assert row["legacy_cohort"] is row["legacy_eligible_at"] is None


def test_repeated_identical_version_retains_first_observation(db):
    ident = put(db)
    assert put(db, observed_at=T + timedelta(hours=1)) == ident
    row, = versions(db)
    assert row["observed_at"] == T.isoformat(timespec="microseconds")


def test_changed_body_appends_without_overwriting_previous_body(db):
    original = put(db)
    changed = put(db, body="A different complete body.", observed_at=T + timedelta(hours=1),
                  provider_updated_at=T + timedelta(minutes=30))
    assert original != changed
    rows = versions(db)
    assert len(rows) == 2
    assert [r["body"] for r in rows] == ["Full synthetic body.\n\nSecond paragraph.", "A different complete body."]
    assert rows[0]["first_seen_at"] == rows[1]["first_seen_at"] == T.isoformat(timespec="microseconds")


def test_unchanged_provider_timestamp_does_not_hide_changed_body(db):
    assert put(db) != put(db, body="Silently changed body.", observed_at=T + timedelta(minutes=1))
    assert len(versions(db)) == 2


def test_title_and_provider_revision_changes_are_versions_even_when_body_is_same(db):
    original = put(db)
    title = put(db, title="Corrected title", observed_at=T + timedelta(minutes=1))
    revision = put(db, observed_at=T + timedelta(minutes=2), provider_updated_at=T)
    assert len({original, title, revision}) == 3
    assert len({r["body_sha256"] for r in versions(db)}) == 1


def test_document_and_source_identity_partition_versions(db):
    identifiers = {put(db), put(db, document_id="43"), put(db, source_id="other")}
    assert len(identifiers) == 3
    assert len(versions(db)) == len(versions(db, document="43")) == len(versions(db, source="other")) == 1
    assert versions(db, document="missing") == []


def test_equivalent_aware_times_are_normalized_to_utc(db):
    other_zone = timezone(timedelta(hours=-7))
    original = put(db)
    assert put(db, observed_at=T.astimezone(other_zone),
               published_at=(T - timedelta(minutes=10)).astimezone(other_zone),
               provider_updated_at=(T - timedelta(minutes=5)).astimezone(other_zone)) == original


@pytest.mark.parametrize("field", ["observed_at", "published_at", "provider_updated_at"])
def test_naive_times_are_rejected_without_partial_records(db, field):
    with pytest.raises(V.BodyVersionError):
        put(db, **{field: T.replace(tzinfo=None)})
    assert versions(db) == []


def test_unknown_provider_publication_times_stay_unknown(db):
    put(db, published_at=None, provider_updated_at=None)
    row, = versions(db)
    assert row["published_at"] is row["provider_updated_at"] is None


@pytest.mark.parametrize("changes", [
    {"provenance": "legacy"},
    {"provenance": "legacy", "legacy_cohort": "archive_20261023"},
    {"provenance": "legacy", "legacy_eligible_at": T - timedelta(days=3)},
    {"provenance": "legacy", "legacy_cohort": "", "legacy_eligible_at": T},
    {"legacy_cohort": "unexpected"},
    {"legacy_eligible_at": T},
    {"provenance": "unspecified"},
])
def test_legacy_requires_explicit_cohort_and_original_eligibility_only_for_legacy(db, changes):
    with pytest.raises(V.BodyVersionError):
        put(db, **changes)
    assert versions(db) == []


def test_legacy_metadata_is_preserved_without_fabricating_old_observation(db):
    eligible = T - timedelta(days=30)
    put(db, provenance="legacy", legacy_cohort="bodies_before_versions_v1",
        legacy_eligible_at=eligible, provider_updated_at=None)
    row, = versions(db)
    assert row["provenance"] == "legacy"
    assert row["legacy_cohort"] == "bodies_before_versions_v1"
    assert row["legacy_eligible_at"] == eligible.isoformat(timespec="microseconds")
    assert row["observed_at"] == T.isoformat(timespec="microseconds")
    assert row["first_seen_at"] != row["legacy_eligible_at"]


def test_historical_migration_has_no_wall_clock_assumption(db):
    old = datetime(2001, 1, 1, tzinfo=timezone.utc)
    put(db, observed_at=old, published_at=None, provider_updated_at=None)
    assert versions(db)[0]["observed_at"] == old.isoformat(timespec="microseconds")


def test_later_insert_cannot_rewrite_document_first_seen_into_the_past(db):
    put(db)
    put(db, body="Earlier claimed observation.", observed_at=T - timedelta(seconds=1))
    first_seen = db.execute("SELECT first_seen_at FROM body_documents").fetchone()[0]
    assert first_seen == T.isoformat(timespec="microseconds")
    assert [row["observed_at"] for row in versions(db)] == [first_seen, first_seen]


def test_caller_rollback_removes_all_new_records(db):
    db.execute("BEGIN")
    put(db)
    db.rollback()
    assert versions(db) == []


def test_schema_helper_does_not_commit_unrelated_caller_work(db):
    db.execute("CREATE TABLE sentinel (value INTEGER)")
    db.execute("INSERT INTO sentinel VALUES (1)")
    V.ensure_schema(db)
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0] == 0


def test_version_write_failure_rolls_back_document_and_preserves_outer_work(db):
    db.execute("CREATE TABLE sentinel (value INTEGER)")
    db.commit()
    # SQLite authorizer rejects only version INSERT after document creation.
    def deny_version_insert(action, table, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "body_versions" else sqlite3.SQLITE_OK
    db.execute("INSERT INTO sentinel VALUES (1)")
    db.set_authorizer(deny_version_insert)
    with pytest.raises(sqlite3.DatabaseError):
        put(db)
    db.set_authorizer(None)
    assert versions(db) == []
    assert db.execute("SELECT COUNT(*) FROM body_documents").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0] == 1


@pytest.mark.parametrize("changes", [{"body": " "}, {"body": None}, {"source_id": ""},
                                     {"document_id": ""}, {"title": None}])
def test_invalid_payloads_fail_before_partial_document_insert(db, changes):
    with pytest.raises(V.BodyVersionError):
        put(db, **changes)
    assert db.execute("SELECT COUNT(*) FROM body_documents").fetchone()[0] == 0


@pytest.mark.parametrize("statement", [
    "UPDATE body_versions SET body='replacement'",
    "DELETE FROM body_versions",
    "INSERT OR REPLACE INTO body_versions SELECT * FROM body_versions",
    "UPDATE body_documents SET first_seen_at='2000-01-01T00:00:00+00:00'",
    "DELETE FROM body_documents",
    "INSERT OR REPLACE INTO body_documents SELECT * FROM body_documents",
])
def test_direct_sql_cannot_overwrite_or_remove_persisted_history(db, statement):
    put(db)
    before = versions(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(statement)
    assert versions(db) == before


def test_migration_keeps_document_first_seen_separate_from_current_body_observation(db):
    put(db, first_seen_at=T - timedelta(days=7))
    row, = versions(db)
    assert row['first_seen_at'] == (T - timedelta(days=7)).isoformat(timespec='microseconds')
    assert row['observed_at'] == T.isoformat(timespec='microseconds')


def test_first_seen_cannot_follow_body_observation(db):
    with pytest.raises(V.BodyVersionError):
        put(db, first_seen_at=T + timedelta(seconds=1))


def test_document_first_seen_cannot_be_replaced_on_later_intake(db):
    put(db, first_seen_at=T - timedelta(days=7))
    with pytest.raises(V.BodyVersionError):
        put(db, first_seen_at=T - timedelta(days=2), observed_at=T + timedelta(hours=1))
