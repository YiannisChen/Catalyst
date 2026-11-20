import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from catalyst_data.ingestion.input_manifest import PreflightError
from catalyst_data.storage import body_versions, temporal_evidence
from catalyst_data.storage.as_of_stores import as_of_stores
from catalyst_data.storage.snapshots import load_verified_stores
from catalyst_data.storage.time_corrections import load_corrections

INVOCATION = ["python", "-m", "catalyst_data.query_service"]


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_store(path, document_id: str, *, provenance="versioned", legacy_cohort=None,
                 legacy_eligible_at=None, published="2026-10-20T09:00:00Z", url=None,
                 publisher=None, observed="2026-10-20T09:00:00Z",
                 association_observed=None):
    conn = sqlite3.connect(path)
    body_versions.ensure_schema(conn)
    temporal_evidence.ensure_schema(conn)
    observed = _dt(observed)
    eligible = _dt(legacy_eligible_at) if legacy_eligible_at else None
    version_id = body_versions.put_version(
        conn, source_id="archive", document_id=document_id, title=document_id,
        body=f"Body for {document_id}", observed_at=observed,
        published_at=_dt(published) if published else None,
        provider_updated_at=_dt(published) if published and provenance == "versioned" else None,
        provenance=provenance, legacy_cohort=legacy_cohort,
        legacy_eligible_at=eligible,
    )
    canonical_url = url
    temporal_evidence.register_version(
        conn, version_id=version_id, ticker="ACME", kind="body",
        evidence_id=f"article:{document_id}", evidence_role="evidence",
        association_observed_at=_dt(association_observed) if association_observed else observed,
        eligible_at=eligible,
        published_at_source="api" if published else None,
        published_at_precision="second" if published else "unknown",
        api_published=published, url=url, canonical_url=canonical_url,
        publisher=publisher,
    )
    conn.commit()
    conn.close()
    return version_id


def _seal(tmp_path, required, *, optional=(), extra_files=()):
    files = list(dict.fromkeys([path for _, path in required]
                               + [path for _, path in optional if path.exists()]
                               + list(extra_files)))
    manifest = tmp_path / "inputs.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "invocation": INVOCATION,
        "files": [{"path": str(path), "sha256": _sha(path)} for path in files],
        "environment": {},
    }))
    return load_verified_stores(
        manifest, invocation=INVOCATION, required_stores=required,
        optional_stores=optional, environ={},
    )


def _read(verified, *, correction_tables=(), correction_store_ids=None,
          correction_artifacts=None, mode="published", allowed_legacy_cohorts=(), legacy_catalogs=None,
          at="2026-10-20T10:00:00Z"):
    return as_of_stores(
        verified, "ACME", _dt("2026-10-20T00:00:00Z"), _dt("2026-10-21T00:00:00Z"),
        _dt(at), mode=mode, correction_table_id="corr:v1",
        correction_tables=correction_tables, correction_store_ids=correction_store_ids or {},
        correction_artifacts=correction_artifacts or {},
        allowed_legacy_cohorts=allowed_legacy_cohorts, legacy_catalogs=legacy_catalogs,
    )


def test_as_of_requires_manifest_verified_store_set_not_caller_snapshot_labels(tmp_path):
    path = tmp_path / "one.sqlite3"
    _write_store(path, "one")

    with pytest.raises((PreflightError, ValueError, TypeError, AttributeError)):
        _read({"daily": {"path": path, "snapshot_id": "trusted-by-caller"}})

    verified = _seal(tmp_path, [("daily", path)])
    result = _read(verified)
    assert result["audit"]["manifest_sha256"] == verified.manifest_sha256
    assert result["audit"]["data_snapshot_ids"] == {"daily": _sha(path)}


def test_required_missing_and_invalid_temporal_schema_fail_closed(tmp_path):
    absent = tmp_path / "absent.sqlite3"
    manifest = tmp_path / "inputs.json"
    manifest.write_text(json.dumps({"schema_version": 1, "invocation": INVOCATION,
                                    "files": [], "environment": {}}))
    with pytest.raises(PreflightError, match="missing required store"):
        load_verified_stores(manifest, invocation=INVOCATION,
                             required_stores={"daily": absent}, environ={})

    bad = tmp_path / "bad.sqlite3"
    with sqlite3.connect(bad) as conn:
        conn.execute("CREATE TABLE body_versions (version_id TEXT)")
    verified = _seal(tmp_path, [("daily", bad)])
    with pytest.raises(ValueError, match="schema|snapshot|table"):
        _read(verified)


def test_same_sha_distinct_logical_stores_keep_scoped_duplicate_version_rows(tmp_path):
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    version_a = _write_store(first, "same-document")
    version_b = _write_store(second, "same-document")
    assert version_a == version_b and _sha(first) == _sha(second)
    verified = _seal(tmp_path, [("archive:a", first), ("archive:b", second)])

    result = _read(verified)

    assert {(row["store_id"], row["version_id"]) for row in result["evidence"]} == {
        ("archive:a", version_a), ("archive:b", version_b),
    }
    assert result["audit"]["data_snapshot_ids"] == {
        "archive:a": _sha(first), "archive:b": _sha(second),
    }


def test_declared_optional_store_is_audited_but_other_unavailable_store_is_not(tmp_path):
    required = tmp_path / "required.sqlite3"
    _write_store(required, "required")
    missing_optional = tmp_path / "optional-missing.sqlite3"
    verified = _seal(tmp_path, [("daily", required)], optional=[("declared-optional", missing_optional)])

    result = _read(verified)

    assert result["audit"]["data_snapshot_ids"] == {"daily": _sha(required)}
    assert result["audit"]["missing_optional_store_ids"] == ["declared-optional"]
    assert "undeclared" not in result["audit"]["missing_optional_store_ids"]


def test_present_bad_optional_snapshot_is_error_instead_of_silently_skipped(tmp_path):
    required = tmp_path / "required.sqlite3"
    optional = tmp_path / "optional.sqlite3"
    _write_store(required, "required")
    with sqlite3.connect(optional) as conn:
        conn.execute("CREATE TABLE source_coverage_attempts (partial TEXT)")
    verified = _seal(tmp_path, [("daily", required)], optional=[("declared-optional", optional)])

    with pytest.raises(ValueError, match="schema|snapshot|table"):
        _read(verified)


def test_correction_csv_uses_explicit_logical_store_binding_and_manifest_scope(tmp_path):
    path = tmp_path / "daily.sqlite3"
    _write_store(path, "doc", published="2026-10-20T09:00:00Z",
                 url="https://news.example.test/a", publisher="Example News")
    csv_path = tmp_path / "corrections.csv"
    csv_path.write_text(
        "url,publisher,stored_eligible_at,corrected_at,stores\n"
        "https://news.example.test/a,Example News,2026-10-20T09:00:00Z,"
        "2026-10-20T23:00:00Z,correction-daily\n"
    )
    table = load_corrections(csv_path.read_bytes(), kind="finnhub", filename=csv_path.name,
                             expected_sha256=_sha(csv_path))
    verified = _seal(tmp_path, [("daily", path)], extra_files=(csv_path,))

    artifacts = {table.table_id: csv_path}
    unbound = _read(verified, correction_tables=(table,), correction_artifacts=artifacts)
    bound = _read(verified, correction_tables=(table,),
                  correction_store_ids={"daily": "correction-daily"}, correction_artifacts=artifacts)

    assert unbound["evidence"][0]["eligible_at"] == "2026-10-20T09:00:00.000000+00:00"
    assert bound["evidence"] == []  # corrected eligibility is later than this cutoff
    after_correction = _read(
        verified, correction_tables=(table,), correction_store_ids={"daily": "correction-daily"},
        correction_artifacts=artifacts, at="2026-10-21T00:00:00Z",
    )
    assert after_correction["evidence"][0]["eligible_at"] == "2026-10-20T23:00:00.000000+00:00"
    assert bound["audit"]["correction_table_ids"] == [table.table_id]
    with pytest.raises(ValueError, match="store|binding|declared"):
        _read(verified, correction_tables=(table,),
              correction_store_ids={"not-declared": "correction-daily"}, correction_artifacts=artifacts)


def test_correction_conflicts_are_aggregated_across_stores_with_scoped_audits(tmp_path):
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    url = "https://news.example.test/shared"
    publisher = "Example News"
    _write_store(first, "first", url=url, publisher=publisher)
    _write_store(second, "second", url=url, publisher=publisher)
    csv_path = tmp_path / "corrections.csv"
    csv_path.write_text(
        "url,publisher,stored_eligible_at,corrected_at,stores\n"
        f"{url},{publisher},2026-10-20T09:00:00Z,2026-10-20T23:00:00Z,correction-first\n"
        f"{url},{publisher},2026-10-20T08:00:00Z,2026-10-20T23:00:00Z,correction-second\n"
    )
    table = load_corrections(csv_path.read_bytes(), kind="finnhub", filename=csv_path.name,
                             expected_sha256=_sha(csv_path))
    verified = _seal(tmp_path, [("first", first), ("second", second)], extra_files=(csv_path,))

    result = _read(
        verified, correction_tables=(table,), correction_artifacts={table.table_id: csv_path},
        correction_store_ids={"first": "correction-first", "second": "correction-second"},
    )

    assert result["audit"]["correction_conflict_count"] == 1
    assert result["audit"]["correction_conflict_count_scope"] == "ticker_kind_candidates_before_window_filter"
    assert result["audit"]["per_store_audits"]["first"]["correction_conflict_count"] == 0
    assert result["audit"]["per_store_audits"]["second"]["correction_conflict_count"] == 1


def test_legacy_and_versioned_provenance_are_both_retained(tmp_path):
    path = tmp_path / "daily.sqlite3"
    _write_store(path, "versioned-document")
    _write_store(path, "legacy-document", provenance="legacy",
                 legacy_cohort="approved-cohort-v1", legacy_eligible_at="2026-10-20T08:00:00Z",
                 published=None)
    membership = tmp_path / "legacy-members.jsonl"
    membership.write_text(json.dumps({
        "source_id": "archive", "document_id": "legacy-document", "evidence_id": "article:legacy-document",
        "kind": "body", "tickers": ["ACME"], "evidence_role": "evidence",
        "eligible_at": "2026-10-20T08:00:00Z", "representation": "raw_full_body",
        "text_sha256": hashlib.sha256(b"Body for legacy-document").hexdigest(),
        "title_sha256": hashlib.sha256(b"legacy-document").hexdigest(),
    }) + "\n")
    catalog = tmp_path / "legacy-catalog.json"
    catalog.write_text(json.dumps({
        "schema_version": 1, "cohort_id": "approved-cohort-v1",
        "source_snapshot": {"store_id": "daily", "sha256": _sha(path),
                            "captured_at": "2026-10-20T09:00:00Z", "tables": ["body_versions"]},
        "eligibility_rule": {"id": "original", "version": "v1"}, "correction_tables": [],
        "membership": {"path": str(membership), "sha256": _sha(membership), "record_count": 1},
    }))
    verified = _seal(tmp_path, [("daily", path)], extra_files=(catalog, membership))

    with pytest.raises(ValueError, match="catalog"):
        _read(verified, allowed_legacy_cohorts=("approved-cohort-v1",))
    result = _read(verified, allowed_legacy_cohorts=("approved-cohort-v1",),
                   legacy_catalogs={"approved-cohort-v1": catalog})

    assert {row["provenance"] for row in result["evidence"]} == {"legacy", "versioned"}
    legacy = next(row for row in result["evidence"] if row["provenance"] == "legacy")
    assert legacy["migration_cohort_id"] == "approved-cohort-v1"
    assert legacy["legacy_eligible_at"] == "2026-10-20T08:00:00.000000+00:00"


def test_raw_legacy_catalog_is_scoped_to_its_source_store_across_versioned_store(tmp_path):
    legacy_path = tmp_path / "legacy-source.sqlite3"
    versioned_path = tmp_path / "versioned-only.sqlite3"
    _write_store(legacy_path, "legacy-document", provenance="legacy",
                 legacy_cohort="raw-source-v1", legacy_eligible_at="2026-10-20T08:00:00Z",
                 published=None, observed="2026-10-20T09:00:00Z")
    _write_store(versioned_path, "versioned-document", observed="2026-10-20T09:00:00Z")
    membership = tmp_path / "raw-source-members.jsonl"
    membership.write_text(json.dumps({
        "source_id": "archive", "document_id": "legacy-document",
        "evidence_id": "article:legacy-document", "kind": "body", "tickers": ["ACME"],
        "evidence_role": "evidence", "eligible_at": "2026-10-20T08:00:00Z",
        "representation": "raw_full_body",
        "text_sha256": hashlib.sha256(b"Body for legacy-document").hexdigest(),
        "title_sha256": hashlib.sha256(b"legacy-document").hexdigest(),
    }) + "\n")
    catalog = tmp_path / "raw-source-catalog.json"
    catalog.write_text(json.dumps({
        "schema_version": 1, "cohort_id": "raw-source-v1",
        "source_snapshot": {"store_id": "raw-source", "sha256": _sha(legacy_path),
                            "captured_at": "2026-10-20T09:00:00Z", "tables": ["body_versions"]},
        "eligibility_rule": {"id": "original", "version": "v1"}, "correction_tables": [],
        "membership": {"path": str(membership), "sha256": _sha(membership), "record_count": 1},
    }))
    verified = _seal(tmp_path, [("raw-source", legacy_path), ("versioned-only", versioned_path)],
                     extra_files=(catalog, membership))

    result = as_of_stores(
        verified, "ACME", _dt("2026-10-20T00:00:00Z"), _dt("2026-10-21T00:00:00Z"),
        _dt("2026-10-20T10:00:00Z"), mode="published", correction_table_id="none",
        allowed_legacy_cohorts=("raw-source-v1",),
        legacy_catalogs={"raw-source-v1": catalog},
    )

    assert {(row["store_id"], row["document_id"]) for row in result["evidence"]} == {
        ("raw-source", "legacy-document"), ("versioned-only", "versioned-document"),
    }
    assert result["audit"]["data_snapshot_ids"] == {
        "raw-source": _sha(legacy_path), "versioned-only": _sha(versioned_path),
    }


def test_raw_legacy_member_in_a_different_store_must_match_catalog_source_snapshot(tmp_path):
    source_path = tmp_path / "raw-source.sqlite3"
    other_path = tmp_path / "other-legacy.sqlite3"
    _write_store(source_path, "legacy-document", provenance="legacy",
                 legacy_cohort="raw-source-v1", legacy_eligible_at="2026-10-20T08:00:00Z",
                 published=None, observed="2026-10-20T09:00:00Z")
    other_path.write_bytes(source_path.read_bytes())
    _write_store(other_path, "extra-versioned-document", observed="2026-10-20T09:00:00Z")
    membership = tmp_path / "raw-source-members.jsonl"
    membership.write_text(json.dumps({
        "source_id": "archive", "document_id": "legacy-document",
        "evidence_id": "article:legacy-document", "kind": "body", "tickers": ["ACME"],
        "evidence_role": "evidence", "eligible_at": "2026-10-20T08:00:00Z",
        "representation": "raw_full_body",
        "text_sha256": hashlib.sha256(b"Body for legacy-document").hexdigest(),
        "title_sha256": hashlib.sha256(b"legacy-document").hexdigest(),
    }) + "\n")
    catalog = tmp_path / "raw-source-catalog.json"
    catalog.write_text(json.dumps({
        "schema_version": 1, "cohort_id": "raw-source-v1",
        "source_snapshot": {"store_id": "raw-source", "sha256": _sha(source_path),
                            "captured_at": "2026-10-20T09:00:00Z", "tables": ["body_versions"]},
        "eligibility_rule": {"id": "original", "version": "v1"}, "correction_tables": [],
        "membership": {"path": str(membership), "sha256": _sha(membership), "record_count": 1},
    }))
    verified = _seal(tmp_path, [("raw-source", source_path), ("other", other_path)],
                     extra_files=(catalog, membership))

    with pytest.raises(ValueError, match="snapshot|source|raw|cohort"):
        as_of_stores(
            verified, "ACME", _dt("2026-10-20T00:00:00Z"), _dt("2026-10-21T00:00:00Z"),
            _dt("2026-10-20T10:00:00Z"), mode="published", correction_table_id="none",
            allowed_legacy_cohorts=("raw-source-v1",),
            legacy_catalogs={"raw-source-v1": catalog},
        )


def test_read_does_not_change_verified_source(tmp_path):
    path = tmp_path / "daily.sqlite3"
    _write_store(path, "one")
    verified = _seal(tmp_path, [("daily", path)])
    before = _sha(path)

    _read(verified)

    assert _sha(path) == before


def test_observed_reads_retain_future_version_and_future_association_gates(tmp_path):
    path = tmp_path / "daily.sqlite3"
    _write_store(path, "visible", observed="2026-10-20T09:00:00Z")
    _write_store(path, "future-version", observed="2026-10-20T11:00:00Z")
    _write_store(path, "future-association", observed="2026-10-20T09:00:00Z",
                 association_observed="2026-10-20T11:00:00Z")
    verified = _seal(tmp_path, [("daily", path)])

    result = _read(verified, mode="observed")

    assert [row["document_id"] for row in result["evidence"]] == ["visible"]


def test_explicit_body_store_selection_keeps_auxiliary_pins_verified(tmp_path):
    body = tmp_path / 'body.sqlite3'
    auxiliary = tmp_path / 'coverage.sqlite3'
    _write_store(body, 'article')
    with sqlite3.connect(auxiliary) as conn:
        conn.execute('CREATE TABLE source_marker (id TEXT)')
    verified = _seal(tmp_path, [('body',body),('coverage',auxiliary)])
    result = as_of_stores(verified, 'ACME', _dt('2026-10-20T00:00:00Z'),
        _dt('2026-10-21T00:00:00Z'), _dt('2026-10-20T10:00:00Z'),
        mode='published', correction_table_id='none', store_ids=('body',))
    assert {row['store_id'] for row in result['evidence']} == {'body'}
    assert result['audit']['data_snapshot_ids'] == {'body':_sha(body)}
    assert result['audit']['input_snapshot_ids'] == {'body':_sha(body),'coverage':_sha(auxiliary)}
    with pytest.raises(ValueError, match='store'):
        as_of_stores(verified, 'ACME', _dt('2026-10-20T00:00:00Z'),
            _dt('2026-10-21T00:00:00Z'), _dt('2026-10-20T10:00:00Z'),
            mode='published', correction_table_id='none', store_ids=('unlisted',))
