import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from catalyst_data.storage import body_versions, snapshots, time_corrections
from catalyst_data.storage import temporal_evidence as T
from catalyst_data.storage.legacy_cohorts import LegacyCohort, LegacyRecord

UTC = timezone.utc


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    body_versions.ensure_schema(db)
    T.ensure_schema(db)
    return db


def test_observation_before_document_first_seen_is_clamped(conn):
    body_versions.put_version(
        conn, source_id="alpaca", document_id="early", title="Title", body="Body",
        observed_at=dt("2026-10-20T10:00:00Z"),
    )
    version_id = body_versions.put_version(
        conn, source_id="alpaca", document_id="early", title="Title", body="Body v2",
        observed_at=dt("2026-10-20T09:00:00Z"),
    )
    assert conn.execute("SELECT observed_at FROM body_versions WHERE version_id=?", (version_id,)).fetchone()[0] == \
        "2026-10-20T10:00:00.000000+00:00"


def add_version(conn, *, doc="a1", observed="2026-10-20T10:00:00Z",
                first_seen=None, published="2026-10-20T09:00:00Z",
                updated="2026-10-20T09:30:00Z", provenance="versioned",
                cohort=None, eligible=None, backfill=False, evidence_id=None,
                ticker="ACME", evidence_role="evidence", published_at_source="api",
                published_at_precision="second", api_published="2026-10-20T09:00:00Z",
                page_published=None, body=None, association_observed_at=None,
                url=None, publisher=None):
    kwargs = {}
    if provenance == "legacy":
        kwargs = {"provenance": "legacy", "legacy_cohort": cohort,
                  "legacy_eligible_at": dt(eligible)}
    version_id = body_versions.put_version(
        conn, source_id="alpaca", document_id=doc, title=f"Title {doc}",
        body=body if body is not None else f"Body {doc}", observed_at=dt(observed),
        first_seen_at=dt(first_seen) if first_seen else None,
        published_at=dt(published) if published else None,
        provider_updated_at=dt(updated) if updated else None, **kwargs,
    )
    T.register_version(conn, version_id=version_id, ticker=ticker, kind="body",
                       evidence_role=evidence_role,
                       association_observed_at=dt(association_observed_at or observed),
                       evidence_id=evidence_id or f"article:{doc}",
                       eligible_at=dt(eligible) if eligible else None,
                       url=url, publisher=publisher,
                       published_at_source=published_at_source,
                       published_at_precision=published_at_precision,
                       api_published=api_published, page_published=page_published,
                       is_backfill=backfill)
    return version_id


def _legacy_cohort(conn, version_id, *, cohort_id, correction_bindings=()):
    row = conn.execute(
        "SELECT v.source_id,v.document_id,v.title,v.body,v.legacy_eligible_at,m.ticker,m.kind,"
        "m.evidence_id,m.evidence_role FROM body_versions v "
        "JOIN temporal_evidence_map m USING(version_id) WHERE v.version_id=?",
        (version_id,),
    ).fetchone()
    source_id, document_id, title, body, eligible_at, ticker, kind, evidence_id, role = row
    member = LegacyRecord(
        source_id=source_id, document_id=document_id, evidence_id=evidence_id, kind=kind,
        tickers=(ticker,), evidence_role=role, eligible_at=eligible_at,
        representation="raw_full_body", text_sha256=hashlib.sha256(body.encode()).hexdigest(),
        title_sha256=hashlib.sha256(title.encode()).hexdigest(),
    )
    return LegacyCohort(
        cohort_id=cohort_id, manifest_sha256="1" * 64, input_manifest_sha256="2" * 64,
        source_store_id="legacy-test-store", source_snapshot_sha256="3" * 64,
        snapshot_captured_at="2026-10-26T16:30:00.000000+00:00",
        source_tables=("legacy_articles",), eligibility_rule_id="legacy-eligibility",
        eligibility_rule_version="v1", membership_sha256="4" * 64,
        records=(member,), correction_bindings=tuple(sorted(
            (binding["table_id"], binding["sha256"], binding["store_id"])
            for binding in correction_bindings
        )),
    )


def _pinned_legacy_catalog(tmp_path, conn, version_id, *, cohort_id,
                           correction_bindings=(), captured_at=None, pin_catalog=True,
                           extra_files=()):
    source_path = tmp_path / f"{cohort_id}-source.sqlite3"
    conn.commit()
    target = sqlite3.connect(source_path)
    conn.backup(target)
    target.close()
    source = sqlite3.connect(source_path)
    row = source.execute(
        "SELECT v.source_id,v.document_id,v.title,v.body,v.legacy_eligible_at,m.ticker,m.kind,"
        "m.evidence_id,m.evidence_role,v.observed_at,d.first_seen_at,m.association_observed_at "
        "FROM body_versions v JOIN body_documents d USING(source_id,document_id) "
        "JOIN temporal_evidence_map m USING(version_id) WHERE v.version_id=?",
        (version_id,),
    ).fetchone()
    (source_id, document_id, title, body, eligible, ticker, kind, evidence_id, role,
     observed_at, first_seen_at, association_observed_at) = row
    source.close()
    member = {
        "source_id": source_id, "document_id": document_id, "evidence_id": evidence_id,
        "kind": kind, "tickers": [ticker], "evidence_role": role, "eligible_at": eligible,
        "representation": "raw_full_body",
        "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
    }
    membership_path = tmp_path / f"{cohort_id}-membership.jsonl"
    membership_path.write_text(json.dumps(member, separators=(",", ":")) + "\n")
    if captured_at is None:
        captured_at = max(observed_at, first_seen_at, association_observed_at)
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    catalog_path = tmp_path / f"{cohort_id}-catalog.json"
    catalog_path.write_text(json.dumps({
        "schema_version": 1, "cohort_id": cohort_id,
        "source_snapshot": {"store_id": "legacy-test-store", "sha256": source_sha,
                            "captured_at": captured_at, "tables": ["body_versions"]},
        "eligibility_rule": {"id": "legacy-test-rule", "version": "v1"},
        "correction_tables": list(correction_bindings),
        "membership": {"path": str(membership_path),
                        "sha256": hashlib.sha256(membership_path.read_bytes()).hexdigest(),
                        "record_count": 1},
    }, separators=(",", ":")))
    files = [source_path, *extra_files]
    if pin_catalog:
        files.extend((catalog_path, membership_path))
    manifest_path = tmp_path / f"{cohort_id}-manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 1, "invocation": ["legacy-read-test"], "environment": {},
        "files": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in files],
    }))
    verified = snapshots.load_verified_stores(
        manifest_path, invocation=("legacy-read-test",),
        required_stores={"legacy-test-store": source_path}, required_env=(), environ={},
    )
    read_conn = sqlite3.connect(source_path)
    read_conn.row_factory = sqlite3.Row
    return read_conn, verified, catalog_path


def test_observed_requires_document_and_version_to_be_known_by_cutoff(conn):
    visible = add_version(conn, doc="visible", observed="2026-10-20T10:00:00Z",
                          first_seen="2026-10-20T09:00:00Z",
                          page_published="2026-10-20T08:59:00-00:00")
    add_version(conn, doc="late-version", observed="2026-10-20T10:01:00Z",
                 first_seen="2026-10-20T09:00:00Z")
    add_version(conn, doc="late-document", observed="2026-10-20T10:01:00Z",
                 first_seen="2026-10-20T10:01:00Z")

    result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                     mode="observed", data_snapshot_id="db:abc",
                     correction_table_id="corrections:v1", kinds=["body"])

    assert [row["version_id"] for row in result["evidence"]] == [visible]
    assert result["leads"] == []
    assert result["audit"]["mode"] == "observed"
    assert result["audit"]["cutoff"] == "2026-10-20T10:00:00.000000+00:00"
    assert result["audit"]["data_snapshot_id"] == "db:abc"
    assert result["evidence"][0]["published_at_source"] == "api"
    assert result["evidence"][0]["published_at_precision"] == "second"
    assert result["evidence"][0]["api_published"] == "2026-10-20T09:00:00Z"
    assert result["evidence"][0]["page_published"] == "2026-10-20T08:59:00-00:00"
    assert result["evidence"][0]["version"] == 1


def test_version_number_is_stable_across_tickers_and_increments_per_evidence(conn):
    first = add_version(conn, doc="v1", evidence_id="article:stable")
    first_second_ticker = T.register_version(
        conn, version_id=first, ticker="PEER", kind="body", evidence_id="article:stable",
        evidence_role="evidence", association_observed_at=dt("2026-10-20T10:00:00Z"),
        published_at_source="api", published_at_precision="second",
        api_published="2026-10-20T09:00:00Z",
    )
    second = add_version(conn, doc="v2", evidence_id="article:stable",
                         ticker="PEER",
                         observed="2026-10-20T10:05:00Z",
                         published="2026-10-20T09:00:00Z",
                         updated="2026-10-20T09:35:00Z")
    result = T.as_of(conn, "PEER", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:10:00Z"),
                     mode="observed", data_snapshot_id="db:stable",
                     correction_table_id="corr:v1")

    assert first_second_ticker == 1
    assert next(row for row in result["evidence"] if row["version_id"] == first)["version"] == 1
    assert next(row for row in result["evidence"] if row["version_id"] == second)["version"] == 2


def test_temporal_helpers_preserve_caller_transaction(conn):
    conn.commit()
    conn.execute("BEGIN")
    T.ensure_schema(conn)
    version_id = add_version(conn, doc="rolled-back")

    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT 1 FROM body_versions WHERE version_id=?", (version_id,)).fetchone() is None


def test_observed_gates_late_ticker_association_and_keeps_explicit_leads_separate(conn):
    available = add_version(conn, doc="known-association", observed="2026-10-20T09:00:00Z",
                            first_seen="2026-10-20T09:00:00Z")
    explicit_lead = add_version(conn, doc="role-lead", observed="2026-10-20T09:00:00Z",
                                first_seen="2026-10-20T09:00:00Z", evidence_role="lead")
    add_version(conn, doc="late-association", observed="2026-10-20T09:00:00Z",
                first_seen="2026-10-20T09:00:00Z",
                association_observed_at="2026-10-20T10:01:00Z")

    result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                     mode="observed", data_snapshot_id="db:role",
                     correction_table_id="corr:v1")

    assert [row["version_id"] for row in result["evidence"]] == [available]
    assert [row["version_id"] for row in result["leads"]] == [explicit_lead]
    assert result["leads"][0]["evidence_role"] == "lead"
    assert result["leads"][0]["association_observed_at"] == "2026-10-20T09:00:00.000000+00:00"


def test_schema_migration_fails_old_associations_closed(conn):
    # Rebuild an old map shape to exercise in-place migration semantics.
    conn.execute("DROP TABLE temporal_evidence_map")
    conn.execute("""CREATE TABLE temporal_evidence_map (
        version_id TEXT NOT NULL, ticker TEXT NOT NULL,
        kind TEXT NOT NULL, eligible_at TEXT, evidence_id TEXT, url TEXT,
        canonical_url TEXT, publisher TEXT, publisher_family TEXT, provider TEXT,
        source_class TEXT, origin TEXT, is_backfill INTEGER NOT NULL,
        time_flags_json TEXT NOT NULL, PRIMARY KEY (version_id,ticker,kind))""")
    version_id = body_versions.put_version(
        conn, source_id="alpaca", document_id="old", title="Old", body="Old body",
        observed_at=dt("2026-10-20T09:00:00Z"),
        published_at=dt("2026-10-20T08:00:00Z"),
        provider_updated_at=dt("2026-10-20T08:30:00Z"),
    )
    conn.execute(
        "INSERT INTO temporal_evidence_versions VALUES (?,?,?,?,?,?,?)",
        (version_id, "article:old", 1, "api", "second", "2026-10-20T08:00:00Z", None),
    )
    conn.execute(
        "INSERT INTO temporal_evidence_map VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (version_id, "ACME", "body", "2026-10-20T08:00:00.000000+00:00", "article:old",
         None, None, None, None, "alpaca", None, None, 0, "[]"),
    )

    T.ensure_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(temporal_evidence_map)")}
    assert {"evidence_role", "association_observed_at"} <= columns
    migrated = conn.execute(
        "SELECT evidence_role,association_observed_at FROM temporal_evidence_map WHERE version_id=?",
        (version_id,),
    ).fetchone()
    assert tuple(migrated) == ("lead", None)

    published = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                        dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                        mode="published", data_snapshot_id="db:old",
                        correction_table_id="corr:v1")
    observed = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                       dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                       mode="observed", data_snapshot_id="db:old",
                       correction_table_id="corr:v1")
    assert published["evidence"] == []
    assert published["leads"][0]["version_id"] == version_id
    assert published["leads"][0]["lead_reason"] == "evidence_role_lead"
    assert observed["evidence"] == []
    assert observed["leads"] == []


def test_published_returns_only_verified_versioned_evidence_and_separates_leads(conn):
    verified = add_version(conn, doc="verified", observed="2026-10-20T12:00:00Z",
                            published="2026-10-20T08:00:00Z",
                            updated="2026-10-20T09:00:00Z")
    late_update = add_version(conn, doc="late-update", observed="2026-10-20T12:00:00Z",
                              published="2026-10-20T08:00:00Z",
                              updated="2026-10-20T11:00:00Z")
    unknown_update = add_version(conn, doc="unknown-update", observed="2026-10-20T12:00:00Z",
                                 published="2026-10-20T08:00:00Z", updated=None,
                                 backfill=True)
    late_published = add_version(conn, doc="late-published", observed="2026-10-20T12:00:00Z",
                                 published="2026-10-20T11:00:00Z", updated="2026-10-20T11:00:00Z")

    result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                     mode="published", data_snapshot_id="db:def",
                     correction_table_id="corrections:v2")

    assert [row["version_id"] for row in result["evidence"]] == [verified]
    assert {row["version_id"] for row in result["leads"]} == {late_update, unknown_update}
    assert all(row["temporal_status"] == "point_in_time_unverified" for row in result["leads"])
    assert late_published not in {row["version_id"] for row in result["evidence"] + result["leads"]}
    assert result["audit"]["label"] == "回溯查询"
    assert result["audit"]["provenance_counts"] == {"versioned": 3}


def test_published_legacy_requires_explicit_pinned_catalog_and_uses_old_eligible_at(conn, tmp_path):
    legacy = add_version(conn, doc="legacy", observed="2026-10-21T12:00:00Z",
                         first_seen="2026-10-21T12:00:00Z", published=None,
                         updated=None, provenance="legacy", cohort="dev-set-v1",
                         eligible="2026-10-20T08:00:00Z")
    read_conn, verified, catalog_path = _pinned_legacy_catalog(
        tmp_path, conn, legacy, cohort_id="dev-set-v1",
    )

    denied = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                     mode="published", allowed_legacy_cohorts=[],
                     data_snapshot_id="db:ghi", correction_table_id="corr:v1")
    allowed = T.as_of(read_conn, "ACME", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                     mode="published", allowed_legacy_cohorts=["dev-set-v1"],
                     legacy_verified_stores=verified,
                     legacy_catalogs={"dev-set-v1": catalog_path}, data_snapshot_id="db:ghi",
                     correction_table_id="corr:v1")

    assert denied["evidence"] == []
    assert [row["version_id"] for row in allowed["evidence"]] == [legacy]
    assert allowed["evidence"][0]["provenance"] == "legacy"
    assert allowed["evidence"][0]["temporal_status"] == "legacy_eligible_unverified_history"
    assert allowed["audit"]["provenance_counts"] == {"legacy": 1}

    with pytest.raises(T.TemporalReadError, match="catalog|cohort"):
        T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                mode="published", allowed_legacy_cohorts=["dev-set-v1"],
                data_snapshot_id="db:ghi", correction_table_id="corr:v1")


def test_published_legacy_never_returns_eligibility_after_cutoff(conn, tmp_path):
    future = add_version(conn, doc="legacy-future", published=None, updated=None,
                         provenance="legacy", cohort="dev-set-v1",
                         eligible="2026-10-20T10:00:01Z")
    read_conn, verified, catalog_path = _pinned_legacy_catalog(
        tmp_path, conn, future, cohort_id="dev-set-v1",
    )
    result = T.as_of(read_conn, "ACME", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                     mode="published", allowed_legacy_cohorts=["dev-set-v1"],
                     legacy_verified_stores=verified,
                     legacy_catalogs={"dev-set-v1": catalog_path},
                     data_snapshot_id="db:legacy", correction_table_id="corr:v1")
    assert future not in {row["version_id"] for row in result["evidence"]}


def test_legacy_is_excluded_from_observed_reads_without_a_provided_catalog(conn):
    legacy = add_version(conn, doc="legacy-no-catalog", observed="2026-10-20T09:00:00Z",
                         first_seen="2026-10-20T08:00:00Z", published=None, updated=None,
                         provenance="legacy", cohort="original-v1",
                         eligible="2026-10-20T08:30:00Z")

    result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                     mode="observed", data_snapshot_id="db:no-legacy-catalog",
                     correction_table_id="none")

    assert legacy not in {row["version_id"] for row in result["evidence"] + result["leads"]}


def test_hand_constructed_legacy_cohort_is_not_an_authorization(conn):
    legacy = add_version(conn, doc="duplicate-legacy-catalog", published=None, updated=None,
                         provenance="legacy", cohort="duplicate-v1",
                         eligible="2026-10-20T08:30:00Z")
    catalog = _legacy_cohort(conn, legacy, cohort_id="duplicate-v1")

    with pytest.raises(TypeError):
        T.as_of(
            conn, "ACME", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
            dt("2026-10-20T10:00:00Z"), mode="published", data_snapshot_id="db:duplicates",
            correction_table_id="none", allowed_legacy_cohorts=["duplicate-v1"],
            legacy_cohorts=[catalog],
        )


def test_unpinned_legacy_catalog_is_rejected(conn, tmp_path):
    legacy = add_version(conn, doc="unpinned-catalog", published=None, updated=None,
                         provenance="legacy", cohort="unpinned-v1",
                         eligible="2026-10-20T08:30:00Z")
    read_conn, verified, catalog_path = _pinned_legacy_catalog(
        tmp_path, conn, legacy, cohort_id="unpinned-v1", pin_catalog=False,
    )

    with pytest.raises((T.TemporalReadError, ValueError), match="catalog|pinned|manifest"):
        T.as_of(
            read_conn, "ACME", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
            dt("2026-10-20T10:00:00Z"), mode="published", data_snapshot_id="db:unpinned",
            correction_table_id="none", allowed_legacy_cohorts=["unpinned-v1"],
            legacy_verified_stores=verified, legacy_catalogs={"unpinned-v1": catalog_path},
        )


def test_observed_legacy_cannot_precede_catalog_capture_time(conn, tmp_path):
    legacy = add_version(conn, doc="observed-capture-bound", observed="2026-10-20T08:00:00Z",
                         first_seen="2026-10-20T07:00:00Z", association_observed_at="2026-10-20T08:00:00Z",
                         published=None, updated=None, provenance="legacy", cohort="capture-bound-v1",
                         eligible="2026-10-20T07:30:00Z")
    read_conn, verified, catalog_path = _pinned_legacy_catalog(
        tmp_path, conn, legacy, cohort_id="capture-bound-v1", captured_at="2026-10-20T09:00:00Z",
    )

    before_capture = T.as_of(
        read_conn, "ACME", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
        dt("2026-10-20T08:30:00Z"), mode="observed", data_snapshot_id="db:capture-bound",
        correction_table_id="none", allowed_legacy_cohorts=["capture-bound-v1"],
        legacy_verified_stores=verified, legacy_catalogs={"capture-bound-v1": catalog_path},
    )
    after_capture = T.as_of(
        read_conn, "ACME", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
        dt("2026-10-20T09:30:00Z"), mode="observed", data_snapshot_id="db:capture-bound",
        correction_table_id="none", allowed_legacy_cohorts=["capture-bound-v1"],
        legacy_verified_stores=verified, legacy_catalogs={"capture-bound-v1": catalog_path},
    )

    assert before_capture["evidence"] == []
    assert [row["version_id"] for row in after_capture["evidence"]] == [legacy]


def test_catalog_only_raw_legacy_cannot_be_read_from_a_different_database(conn, tmp_path):
    legacy = add_version(conn, doc="raw-cross-store", observed="2026-10-20T08:00:00Z",
                         first_seen="2026-10-20T08:00:00Z", association_observed_at="2026-10-20T08:00:00Z",
                         published=None, updated=None, provenance="legacy", cohort="raw-source-v1",
                         eligible="2026-10-20T07:30:00Z")
    pinned_conn, verified, catalog_path = _pinned_legacy_catalog(
        tmp_path, conn, legacy, cohort_id="raw-source-v1",
    )
    other_path = tmp_path / "different-read-database.sqlite3"
    other = sqlite3.connect(other_path)
    pinned_conn.backup(other)
    add_version(other, doc="different-row")
    other.commit()

    with pytest.raises((T.TemporalReadError, ValueError), match="snapshot|source|raw|catalog"):
        T.as_of(
            other, "ACME", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
            dt("2026-10-20T10:00:00Z"), mode="published", data_snapshot_id="db:other",
            correction_table_id="none", allowed_legacy_cohorts=["raw-source-v1"],
            legacy_verified_stores=verified, legacy_catalogs={"raw-source-v1": catalog_path},
        )


def test_catalog_only_raw_legacy_allows_an_identical_snapshot_copy(conn, tmp_path):
    legacy = add_version(conn, doc="raw-identical-copy", observed="2026-10-20T08:00:00Z",
                         first_seen="2026-10-20T08:00:00Z", association_observed_at="2026-10-20T08:00:00Z",
                         published=None, updated=None, provenance="legacy", cohort="raw-copy-v1",
                         eligible="2026-10-20T07:30:00Z")
    pinned_conn, verified, catalog_path = _pinned_legacy_catalog(
        tmp_path, conn, legacy, cohort_id="raw-copy-v1",
    )
    copy_path = tmp_path / "identical-read-copy.sqlite3"
    copy_path.write_bytes(verified.stores["legacy-test-store"].path.read_bytes())
    copied = sqlite3.connect(copy_path)

    result = T.as_of(
        copied, "ACME", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
        dt("2026-10-20T10:00:00Z"), mode="published", data_snapshot_id="db:copy",
        correction_table_id="none", allowed_legacy_cohorts=["raw-copy-v1"],
        legacy_verified_stores=verified, legacy_catalogs={"raw-copy-v1": catalog_path},
    )
    assert [row["version_id"] for row in result["evidence"]] == [legacy]


def test_observed_temporal_property_holds_for_many_cutoffs_and_versions(conn):
    cutoff_times = [f"2026-10-20T09:{minute:02}:00Z" for minute in range(0, 60, 5)]
    version_ids = []
    for index, minute in enumerate(range(0, 60, 3)):
        observed = f"2026-10-20T09:{minute:02}:00Z"
        first_seen_minute = max(0, minute - 6)
        version_ids.append(add_version(
            conn, doc=f"property-{index}", observed=observed,
            first_seen=f"2026-10-20T09:{first_seen_minute:02}:00Z",
        ))

    for cutoff in cutoff_times:
        result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                         dt("2026-10-21T00:00:00Z"), dt(cutoff), mode="observed",
                         data_snapshot_id="db:property", correction_table_id="corr:v1")
        assert all(dt(row["first_seen_at"]) <= dt(cutoff) for row in result["evidence"])
        assert all(dt(row["observed_at"]) <= dt(cutoff)
                   for row in result["evidence"])
        assert {row["version_id"] for row in result["evidence"]}.issubset(set(version_ids))


def test_published_temporal_property_never_promotes_post_cutoff_updates(conn):
    cutoff = "2026-10-20T10:00:00Z"
    version_ids = []
    for index, minute in enumerate(range(0, 60, 2)):
        updated = f"2026-10-20T09:{minute:02}:00Z" if minute < 30 else f"2026-10-20T10:{minute - 30:02}:00Z"
        version_ids.append(add_version(
            conn, doc=f"published-property-{index}", observed="2026-10-20T12:00:00Z",
            published="2026-10-20T08:00:00Z", updated=updated,
        ))

    result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt(cutoff), mode="published",
                     data_snapshot_id="db:published-property", correction_table_id="corr:v1")
    evidence_ids = {row["version_id"] for row in result["evidence"]}
    lead_ids = {row["version_id"] for row in result["leads"]}
    assert evidence_ids.isdisjoint(lead_ids)
    assert evidence_ids | lead_ids == set(version_ids)
    assert all(dt(row["provider_updated_at"]) <= dt(cutoff)
               for row in result["evidence"])
    assert all(row["lead_reason"] == "modified_after_cutoff"
               for row in result["leads"])


def test_published_selects_latest_provider_revision_even_when_received_out_of_order(conn):
    provider_latest = add_version(
        conn, doc="out-of-order", observed="2026-10-20T11:00:00Z",
        published="2026-10-20T08:00:00Z", updated="2026-10-20T09:30:00Z",
    )
    observed_latest = add_version(
        conn, doc="out-of-order", observed="2026-10-20T12:00:00Z",
        published="2026-10-20T08:00:00Z", updated="2026-10-20T09:00:00Z",
    )

    published = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                        dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                        mode="published", data_snapshot_id="db:order",
                        correction_table_id="corr:v1")
    observed = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                       dt("2026-10-21T00:00:00Z"), dt("2026-10-20T13:00:00Z"),
                       mode="observed", data_snapshot_id="db:order",
                       correction_table_id="corr:v1")

    assert [row["version_id"] for row in published["evidence"]] == [provider_latest]
    assert [row["version_id"] for row in observed["evidence"]] == [observed_latest]


def test_observed_tie_uses_storage_revision_order_not_hash_order(conn):
    first = add_version(conn, doc="same-observed", observed="2026-10-20T10:00:00Z",
                         updated="2026-10-20T09:00:00Z", body="first body")
    second = add_version(conn, doc="same-observed", observed="2026-10-20T10:00:00Z",
                         updated="2026-10-20T09:00:00Z", body="second body")
    assert first != second
    result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt("2026-10-20T11:00:00Z"),
                     mode="observed", data_snapshot_id="db:tie", correction_table_id="corr:v1")
    assert [row["version_id"] for row in result["evidence"]] == [second]


def test_observed_does_not_leak_later_ticker_association(conn):
    version_id = add_version(conn, doc="ticker-added-later", observed="2026-10-20T10:00:00Z")
    T.register_version(
        conn, version_id=version_id, ticker="PEER", kind="body", evidence_role="evidence",
        association_observed_at=dt("2026-10-20T11:00:00Z"), evidence_id="article:ticker-added-later",
        published_at_source="api", published_at_precision="second",
        api_published="2026-10-20T09:00:00Z",
    )
    early = T.as_of(conn, "PEER", dt("2026-10-20T00:00:00Z"),
                    dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:30:00Z"),
                    mode="observed", data_snapshot_id="db:association", correction_table_id="corr:v1")
    later = T.as_of(conn, "PEER", dt("2026-10-20T00:00:00Z"),
                    dt("2026-10-21T00:00:00Z"), dt("2026-10-20T11:00:00Z"),
                    mode="observed", data_snapshot_id="db:association", correction_table_id="corr:v1")
    assert early["evidence"] == []
    assert [row["version_id"] for row in later["evidence"]] == [version_id]


def test_source_role_lead_never_enters_evidence_in_either_mode(conn):
    version_id = add_version(conn, doc="role-lead", evidence_role="lead")
    for mode, cutoff in (("observed", "2026-10-20T11:00:00Z"),
                         ("published", "2026-10-20T10:00:00Z")):
        result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                         dt("2026-10-21T00:00:00Z"), dt(cutoff), mode=mode,
                         data_snapshot_id="db:role", correction_table_id="corr:v1")
        assert result["evidence"] == []
        lead = next(row for row in result["leads"] if row["version_id"] == version_id)
        assert lead["lead_reason"] == "evidence_role_lead"


def test_unknown_update_is_unverified_lead_even_when_not_backfill(conn):
    version_id = add_version(conn, doc="unknown-non-backfill", published="2026-10-20T08:00:00Z",
                             updated=None, backfill=False)
    result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                     dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                     mode="published", data_snapshot_id="db:jkl",
                     correction_table_id="corr:v1")
    lead = next(row for row in result["leads"] if row["version_id"] == version_id)
    assert lead["lead_reason"] == "provider_updated_at_unknown"


@pytest.mark.parametrize("mode", ["other", None])
def test_mode_must_be_explicit_and_valid(conn, mode):
    with pytest.raises(T.TemporalReadError):
        T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"),
                 dt("2026-10-21T00:00:00Z"), dt("2026-10-20T10:00:00Z"),
                 mode=mode, data_snapshot_id="db", correction_table_id="corr")


def test_finnhub_correction_is_applied_before_window_filtering(conn):
    csv_bytes = (
        b"url,publisher,stored_eligible_at,corrected_at,stores\n"
        b"https://example.com/story,Benzinga,2026-10-20T12:00:00Z,"
        b"2026-10-20T16:00:00Z,news.sqlite3\n"
    )
    corrections = time_corrections.load_corrections(
        csv_bytes, kind="finnhub", filename="time_corrections_v1.csv",
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )
    observed = dt("2026-10-20T12:00:00Z")
    version_id = body_versions.put_version(
        conn, source_id="finnhub", document_id="story-1", title="Story",
        body="Synthetic story body", observed_at=observed, published_at=observed,
        provider_updated_at=observed,
    )
    T.register_version(
        conn, version_id=version_id, ticker="ACME", kind="body", evidence_id="story-1",
        evidence_role="evidence", association_observed_at=observed, eligible_at=observed,
        url="https://example.com/story", publisher="Benzinga", provider="finnhub",
        published_at_source="api", published_at_precision="second",
        api_published="2026-10-20T12:00:00Z",
    )

    result = T.as_of(
        conn, "ACME", dt("2026-10-20T15:00:00Z"), dt("2026-10-20T17:00:00Z"),
        dt("2026-10-20T18:00:00Z"), mode="observed", data_snapshot_id="db:finnhub",
        correction_table_id=corrections.table_id,
        correction_tables=[(corrections, "news.sqlite3")],
    )

    assert [row["version_id"] for row in result["evidence"]] == [version_id]
    assert result["evidence"][0]["eligible_at"] == "2026-10-20T16:00:00.000000+00:00"
    assert "corrected:" + corrections.table_id in result["evidence"][0]["time_flags"]


def test_sec_correction_uses_independent_accession_before_window_filtering(conn):
    csv_bytes = (
        b"accession,json_acceptance_utc,edgar_accepted_utc,in_stores\n"
        b"0000000001-26-000001,2026-10-20T22:00:00Z,2026-10-20T20:30:00Z,daily\n"
    )
    corrections = time_corrections.load_corrections(
        csv_bytes, kind="sec", filename="sec_acceptance_v1.csv",
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )
    observed = dt("2026-10-21T00:00:00Z")
    version_id = body_versions.put_version(
        conn, source_id="daily:sec", document_id="asset:exhibit-1", title="SEC filing",
        body="Filing body", observed_at=observed, published_at=dt("2026-10-20T22:00:00Z"),
        provider_updated_at=observed,
    )
    T.register_version(
        conn, version_id=version_id, ticker="ACME", kind="filing", evidence_id="filing:1",
        evidence_role="evidence", association_observed_at=observed,
        eligible_at=dt("2026-10-20T22:00:00Z"), origin="issuer:cik:0000000001",
        sec_accession="0000000001-26-000001",
        url="https://www.sec.gov/Archives/edgar/data/1/ex99.htm",
    )

    result = T.as_of(
        conn, "ACME", dt("2026-10-20T20:00:00Z"), dt("2026-10-20T21:00:00Z"),
        dt("2026-10-21T01:00:00Z"), mode="observed", data_snapshot_id="db:daily",
        correction_table_id=corrections.table_id,
        correction_tables=[(corrections, "daily")],
    )

    assert [row["version_id"] for row in result["evidence"]] == [version_id]
    assert result["evidence"][0]["eligible_at"] == "2026-10-20T20:30:00.000000+00:00"


def test_sec_accession_is_independent_immutable_metadata(conn):
    version_id = body_versions.put_version(
        conn, source_id="daily:sec", document_id="independent-accession", title="Filing",
        body="Official exhibit", observed_at=dt("2026-10-20T10:00:00Z"),
        published_at=dt("2026-10-20T09:00:00Z"),
        provider_updated_at=dt("2026-10-20T09:00:00Z"),
    )
    fields = dict(version_id=version_id, ticker="ACME", kind="filing", evidence_id="exhibit:1",
                  evidence_role="evidence", association_observed_at=dt("2026-10-20T10:00:00Z"),
                  origin="issuer:cik:0000000001", sec_accession="0000000001-26-000001")
    T.register_version(conn, **fields)
    result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
                     dt("2026-10-20T11:00:00Z"), mode="observed", data_snapshot_id="db",
                     correction_table_id="none")
    assert result["evidence"][0]["sec_accession"] == fields["sec_accession"]
    assert result["evidence"][0]["origin"] == fields["origin"]
    with pytest.raises(T.TemporalReadError, match="immutable"):
        T.register_version(conn, **(fields | {"sec_accession": "0000000001-26-000002"}))


@pytest.mark.parametrize("original,corrected,expected", [
    ("2026-10-20T12:00:00Z", "2026-10-20T09:00:00Z", 1),
    ("2026-10-20T09:00:00Z", "2026-10-20T12:00:00Z", 0),
])
def test_correction_effective_publication_controls_published_cutoff(conn, original, corrected, expected):
    csv_bytes = ("url,publisher,stored_eligible_at,corrected_at,stores\n"
                 f"https://example.com/cutoff,Benzinga,{original},{corrected},news\n").encode()
    table = time_corrections.load_corrections(csv_bytes, kind="finnhub", filename="cutoff.csv",
                                            expected_sha256=hashlib.sha256(csv_bytes).hexdigest())
    version_id = body_versions.put_version(conn, source_id="finnhub", document_id="cutoff", title="T",
        body="Body", observed_at=dt("2026-10-21T00:00:00Z"), published_at=dt(original),
        provider_updated_at=dt("2026-10-20T08:00:00Z"))
    T.register_version(conn, version_id=version_id, ticker="ACME", kind="body", evidence_id="cutoff",
        evidence_role="evidence", association_observed_at=dt("2026-10-21T00:00:00Z"),
        url="https://example.com/cutoff", publisher="Benzinga")
    result = T.as_of(conn, "ACME", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
        dt("2026-10-20T10:00:00Z"), mode="published", data_snapshot_id="db",
        correction_table_id=table.table_id, correction_tables=[(table, "news")])
    assert len(result["evidence"]) == expected
    if expected:
        assert result["evidence"][0]["published_at"] == dt(original).isoformat(timespec="microseconds")
        assert result["evidence"][0]["effective_published_at"] == dt(corrected).isoformat(timespec="microseconds")


def test_legacy_and_versioned_representations_are_both_retained(conn, tmp_path):
    common = dict(doc="shared-document", evidence_id="same-article", body="Exact shared text")
    legacy = add_version(conn, **common, provenance="legacy", cohort="original-v1",
                         eligible="2026-10-20T09:00:00Z")
    versioned = add_version(conn, **common, observed="2026-10-20T11:00:00Z")
    read_conn, verified, catalog_path = _pinned_legacy_catalog(
        tmp_path, conn, legacy, cohort_id="original-v1",
    )
    result = T.as_of(read_conn, "ACME", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
        dt("2026-10-20T12:00:00Z"), mode="published", data_snapshot_id="db",
        correction_table_id="none", allowed_legacy_cohorts=["original-v1"],
        legacy_verified_stores=verified, legacy_catalogs={"original-v1": catalog_path})
    assert {row["version_id"] for row in result["evidence"]} == {legacy, versioned}
    assert {row["provenance"] for row in result["evidence"]} == {"legacy", "versioned"}


@pytest.mark.parametrize("mode", ["observed", "published"])
@pytest.mark.parametrize(
    "original,corrected,start,end,cutoff,expected",
    [
        ("2026-10-19T08:30:00Z", "2026-10-20T09:30:00Z",
         "2026-10-20T09:00:00Z", "2026-10-20T10:00:00Z", "2026-10-20T10:00:00Z", 1),
        ("2026-10-20T09:30:00Z", "2026-10-21T09:30:00Z",
         "2026-10-20T09:00:00Z", "2026-10-20T10:00:00Z", "2026-10-21T23:00:00Z", 0),
        ("2026-10-20T09:30:00Z", "2026-10-20T10:30:00Z",
         "2026-10-20T09:00:00Z", "2026-10-20T11:00:00Z", "2026-10-20T10:00:00Z", 0),
        ("2026-10-20T10:30:00Z", "2026-10-20T09:30:00Z",
         "2026-10-20T09:00:00Z", "2026-10-20T11:00:00Z", "2026-10-20T10:00:00Z", 1),
    ],
)
def test_pinned_legacy_correction_controls_window_and_cutoff(
    conn, tmp_path, mode, original, corrected, start, end, cutoff, expected,
):
    store_id = "legacy-test-store"
    csv_bytes = (
        "url,publisher,stored_eligible_at,corrected_at,stores\n"
        f"https://example.com/legacy,Benzinga,{original},{corrected},{store_id}\n"
    ).encode()
    table = time_corrections.load_corrections(
        csv_bytes, kind="finnhub", filename="legacy-corrections.csv",
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )
    correction_path = tmp_path / "legacy-corrections.csv"
    correction_path.write_bytes(csv_bytes)
    version_id = add_version(
        conn, doc=f"legacy-correction-{original}-{corrected}",
        observed="2026-10-20T08:00:00Z", first_seen="2026-10-20T08:00:00Z",
        association_observed_at="2026-10-20T08:00:00Z", published=None, updated=None,
        provenance="legacy", cohort="legacy-corrections-v1", eligible=original,
        url="https://example.com/legacy", publisher="Benzinga",
    )
    binding = {"table_id": table.table_id, "sha256": table.sha256, "store_id": store_id}
    read_conn, verified, catalog_path = _pinned_legacy_catalog(
        tmp_path, conn, version_id, cohort_id="legacy-corrections-v1",
        correction_bindings=(binding,), captured_at="2026-10-20T08:00:00Z",
        extra_files=(correction_path,),
    )
    result = T.as_of(
        read_conn, "ACME", dt(start), dt(end),
        None if mode == "published" and cutoff > end else dt(cutoff), mode=mode,
        data_snapshot_id="db:legacy-correction", correction_table_id=table.table_id,
        allowed_legacy_cohorts=["legacy-corrections-v1"],
        legacy_verified_stores=verified,
        legacy_catalogs={"legacy-corrections-v1": catalog_path},
        correction_tables=[(table, store_id)],
    )

    assert len(result["evidence"]) == expected
    if expected:
        row = next(row for row in result["evidence"] if row["version_id"] == version_id)
        assert row["legacy_eligible_at"] == dt(original).isoformat(timespec="microseconds")
        assert row["eligible_at"] == dt(corrected).isoformat(timespec="microseconds")
        assert row["published_at"] is None
        assert row["effective_published_at"] == dt(corrected).isoformat(timespec="microseconds")


@pytest.mark.parametrize("wrong_binding", ["equivalent_table", "wrong_scope", "missing_table"])
def test_legacy_correction_binding_must_match_cohort_exactly_and_not_mutate_source(
        conn, tmp_path, wrong_binding):
    store_id = "legacy-test-store"
    csv_bytes = (
        b"url,publisher,stored_eligible_at,corrected_at,stores\n"
        b"https://example.com/legacy,Benzinga,2026-10-20T08:30:00Z,"
        b"2026-10-20T09:30:00Z,legacy-test-store\n"
    )
    table = time_corrections.load_corrections(
        csv_bytes, kind="finnhub", filename="legacy-corrections.csv",
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )
    correction_path = tmp_path / "legacy-corrections.csv"
    correction_path.write_bytes(csv_bytes)
    version_id = add_version(
        conn, doc="legacy-wrong-correction", observed="2026-10-20T08:00:00Z",
        first_seen="2026-10-20T08:00:00Z", association_observed_at="2026-10-20T08:00:00Z",
        published=None, updated=None, provenance="legacy", cohort="legacy-corrections-v1",
        eligible="2026-10-20T08:30:00Z", url="https://example.com/legacy", publisher="Benzinga",
    )
    binding = {"table_id": table.table_id, "sha256": table.sha256, "store_id": store_id}
    read_conn, verified, catalog_path = _pinned_legacy_catalog(
        tmp_path, conn, version_id, cohort_id="legacy-corrections-v1",
        correction_bindings=(binding,), captured_at="2026-10-20T08:00:00Z",
        extra_files=(correction_path,),
    )
    actual = table
    actual_store = store_id
    if wrong_binding == "equivalent_table":
        equivalent_bytes = csv_bytes + b"\n"
        actual = time_corrections.load_corrections(
            equivalent_bytes, kind="finnhub", filename="replacement.csv",
            expected_sha256=hashlib.sha256(equivalent_bytes).hexdigest(),
        )
    elif wrong_binding == "wrong_scope":
        actual_store = "other-store"
    elif wrong_binding == "missing_table":
        actual = None

    before = conn.execute(
        "SELECT body,legacy_eligible_at FROM body_versions WHERE version_id=?", (version_id,)
    ).fetchone()
    with pytest.raises(T.TemporalReadError, match="correction|legacy|cohort"):
        T.as_of(
            read_conn, "ACME", dt("2026-10-20T09:00:00Z"), dt("2026-10-20T10:00:00Z"),
            dt("2026-10-20T10:00:00Z"), mode="published",
            data_snapshot_id="db:wrong-legacy-correction", correction_table_id=table.table_id,
            allowed_legacy_cohorts=["legacy-corrections-v1"],
            legacy_verified_stores=verified,
            legacy_catalogs={"legacy-corrections-v1": catalog_path},
            correction_tables=[] if actual is None else [(actual, actual_store)],
        )
    assert read_conn.execute(
        "SELECT body,legacy_eligible_at FROM body_versions WHERE version_id=?", (version_id,)
    ).fetchone() == before


def test_correction_object_must_match_manifest_pinned_csv_bytes(conn, tmp_path):
    csv_bytes = (
        b"url,publisher,stored_eligible_at,corrected_at,stores\n"
        b"https://example.com/legacy,Benzinga,2026-10-20T08:30:00Z,"
        b"2026-10-20T09:30:00Z,legacy-test-store\n"
    )
    table = time_corrections.load_corrections(
        csv_bytes, kind="finnhub", filename="legacy-corrections.csv",
        expected_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )
    correction_path = tmp_path / "legacy-corrections.csv"
    correction_path.write_bytes(csv_bytes)
    version_id = add_version(
        conn, doc="legacy-fake-correction-object", observed="2026-10-20T08:00:00Z",
        first_seen="2026-10-20T08:00:00Z", association_observed_at="2026-10-20T08:00:00Z",
        published=None, updated=None, provenance="legacy", cohort="legacy-corrections-v1",
        eligible="2026-10-20T08:30:00Z", url="https://example.com/legacy", publisher="Benzinga",
    )
    binding = {"table_id": table.table_id, "sha256": table.sha256, "store_id": "legacy-test-store"}
    read_conn, verified, catalog_path = _pinned_legacy_catalog(
        tmp_path, conn, version_id, cohort_id="legacy-corrections-v1",
        correction_bindings=(binding,), captured_at="2026-10-20T08:00:00Z",
        extra_files=(correction_path,),
    )
    forged = time_corrections.CorrectionTable(
        kind=table.kind, table_id=table.table_id, sha256=table.sha256, entries=(),
    )

    with pytest.raises(T.TemporalReadError, match="correction|manifest|pinned"):
        T.as_of(
            read_conn, "ACME", dt("2026-10-20T09:00:00Z"), dt("2026-10-20T10:00:00Z"),
            dt("2026-10-20T10:00:00Z"), mode="published",
            data_snapshot_id="db:forged-correction", correction_table_id=table.table_id,
            allowed_legacy_cohorts=["legacy-corrections-v1"],
            legacy_verified_stores=verified,
            legacy_catalogs={"legacy-corrections-v1": catalog_path},
            correction_tables=[(forged, "legacy-test-store")],
        )


@pytest.mark.parametrize("accession", ["1", "000000000126000001", [], "0000000001-26-000001 "])
def test_invalid_sec_accession_rejected_before_any_metadata_write(conn, accession):
    version_id = add_version(conn)
    count = conn.execute("SELECT COUNT(*) FROM temporal_evidence_map").fetchone()[0]
    with pytest.raises(T.TemporalReadError, match="accession"):
        T.register_version(conn, version_id=version_id, ticker="OTHER", kind="filing",
            evidence_id="article:a1", evidence_role="evidence",
            association_observed_at=dt("2026-10-20T10:00:00Z"), sec_accession=accession)
    assert conn.execute("SELECT COUNT(*) FROM temporal_evidence_map").fetchone()[0] == count


# --- contract v1.1: published is cut off at the stage window_end -------------------------------------------------

_W0, _W1 = "2026-10-20T00:00:00Z", "2026-10-20T12:00:00Z"


def _read(conn, at, mode="published"):
    return T.as_of(conn, "ACME", dt(_W0), dt(_W1), None if at is None else dt(at), mode=mode,
                   data_snapshot_id="db:v11", correction_table_id="none")


def test_published_at_equal_to_window_end_is_accepted_and_omitted_at_means_window_end(conn):
    add_version(conn, updated="2026-10-20T09:30:00Z")
    equal, omitted = _read(conn, _W1), _read(conn, None)
    assert [r["version_id"] for r in equal["evidence"]] == [r["version_id"] for r in omitted["evidence"]]
    assert len(equal["evidence"]) == 1


def test_published_at_later_than_window_end_raises(conn):
    add_version(conn)
    with pytest.raises(T.TemporalReadError, match="not be later than window_end"):
        _read(conn, "2026-10-20T12:00:01Z")


def test_text_rewritten_after_window_end_is_not_returned_as_evidence(conn):
    add_version(conn, doc="rewritten", published="2026-10-20T09:00:00Z", updated="2026-10-20T13:00:00Z",
                observed="2026-10-20T13:05:00Z")
    result = _read(conn, None)
    assert result["evidence"] == []
    assert [r["time_confirmation_reason"] if "time_confirmation_reason" in r else r.get("lead_reason")
            for r in result["leads"]] == ["modified_after_cutoff"]


def test_observed_boundary_first_seen_equal_to_observed_as_of_is_returned_and_one_second_later_is_not(conn):
    add_version(conn, doc="edge", first_seen="2026-10-20T10:00:00Z", observed="2026-10-20T10:00:00Z",
                association_observed_at="2026-10-20T10:00:00Z", updated=None)
    add_version(conn, doc="late", first_seen="2026-10-20T10:00:01Z", observed="2026-10-20T10:00:01Z",
                association_observed_at="2026-10-20T10:00:01Z", updated=None)
    result = _read(conn, "2026-10-20T10:00:00Z", mode="observed")
    assert [r["document_id"] for r in result["evidence"]] == ["edge"]
    assert result["leads"] == []  # the later-seen version is neither evidence nor a lead
