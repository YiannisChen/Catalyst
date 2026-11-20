"""Synthetic SQLite integration contract for the public data query facade."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from catalyst_data import query_service
from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.coverage_readiness import (
    CoveragePlan,
    CoverageRequirement,
    PlannedPartition,
    SourceCapability,
    expected_partition_id,
)
from catalyst_data.retrieval import evidence_fts_projection
from catalyst_data.storage import body_versions, temporal_evidence
from catalyst_data.storage.snapshots import load_verified_stores
from catalyst_data.storage.time_corrections import load_corrections

INVOCATION = ["python", "-m", "catalyst_data.query_service"]
START = "2026-10-20T09:00:00Z"
END = "2026-10-20T12:00:00Z"
CUTOFF = "2026-10-20T11:00:00Z"
NEWS = "daily:finnhub"
LEGACY_METADATA_SOURCE = "news_archive:finnhub_company"
POLICY = "query-service-integration-v1"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_body_store(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        body_versions.ensure_schema(conn)
        temporal_evidence.ensure_schema(conn)
        evidence_fts_projection.ensure_schema(conn)
        for doc, observed in (
            ("before-cutoff", "2026-10-20T09:30:00Z"),
            ("after-cutoff", "2026-10-20T13:00:00Z"),
        ):
            at = _dt(observed)
            title = f"ACME {doc} update"
            text = f"ACME reported a synthetic operational result for {doc}."
            version_id = body_versions.put_version(
                conn, source_id="synthetic-source", document_id=doc,
                title=title, body=text, observed_at=at, published_at=at,
                provider_updated_at=at,
            )
            evidence_id = f"synthetic-evidence:{doc}"
            temporal_evidence.register_version(
                conn, version_id=version_id, ticker="ACME", kind="body",
                evidence_id=evidence_id, evidence_role="evidence",
                association_observed_at=at, published_at_source="api",
                published_at_precision="second", api_published=observed,
            )
            evidence_fts_projection.project_evidence_text(
                conn, [evidence_fts_projection.EvidenceText(
                    evidence_id=evidence_id, version_id=version_id,
                    source_id="synthetic-source", document_id=doc,
                    provider="synthetic", kind="body", evidence_class="body_evidence",
                    title=title, text=text, origin=None, provenance="versioned",
                    observed_at=at.isoformat(), published_at=at.isoformat(),
                    provider_updated_at=at.isoformat(),
                )],
            )


def _make_metadata_store(path, *, seed_coverage=True):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE news_items (
                provider TEXT NOT NULL, item_key TEXT NOT NULL, url TEXT NOT NULL,
                title TEXT NOT NULL, summary TEXT, publisher TEXT, published_at TEXT,
                tickers_json TEXT NOT NULL, extra_json TEXT, first_seen_at TEXT NOT NULL,
                PRIMARY KEY (provider, item_key)
            );
            CREATE TABLE news_item_tickers (
                provider TEXT NOT NULL, item_key TEXT NOT NULL, ticker TEXT NOT NULL,
                PRIMARY KEY (provider, item_key, ticker)
            );
        """)
        conn.executemany("INSERT INTO news_items VALUES (?,?,?,?,?,?,?,?,?,?)", [
            ("finnhub_company", "article-corrected", "https://news.test/acme", "ACME results",
             "Synthetic summary", "Wire", "2026-10-20T09:00:00Z", '["ACME"]', None,
             "2026-10-20T09:30:00.123456+00:00"),
            ("finnhub_company", "article-future", "https://news.test/future", "Future ACME results",
             "Synthetic future summary", "Wire", "2026-10-20T13:00:00Z", '["ACME"]', None,
             "2026-10-20T10:00:00.123456+00:00"),
        ])
        conn.executemany("INSERT INTO news_item_tickers VALUES (?,?,?)", [
            ("finnhub_company", "article-corrected", "ACME"),
            ("finnhub_company", "article-future", "ACME"),
        ])
        coverage.ensure_schema(conn)
        if seed_coverage:
            partition = _partition("headlines")
            attempt = coverage.start_attempt(
                conn, logical_task_id="task-headlines", attempt_id="attempt-headlines",
                source_id=NEWS, product="headlines", subject_kind="ticker", subject_id="ACME",
                query_spec={}, coverage_policy_version=POLICY,
                requested_from=START, requested_until=END,
            )
            coverage.record_coverage(
                conn, coverage_id="coverage-headlines", attempt_id=attempt.attempt_id,
                status="OK", recorded_at=CUTOFF, coverage_from=START, coverage_until=END,
                enumeration_complete=True,
            )
            # `partition` is computed here to assert the seed uses the same declared ID.
            assert attempt.partition_id == partition.partition_id


def _partition(product: str) -> PlannedPartition:
    values = dict(
        partition_id="pending", source_id=NEWS, product=product,
        subject_kind="ticker", subject_id="ACME", requested_from=START,
        requested_until=END, query_filters={}, coverage_policy_version=POLICY,
        session_ids=(),
    )
    pending = PlannedPartition(**values)
    return PlannedPartition(**(values | {"partition_id": expected_partition_id(pending)}))


def _requirements():
    shared = dict(
        session_date="2026-10-20", ticker="ACME", scope_kind="target", scope_id="ACME",
        subject_kind="ticker", subject_id="ACME", requested_from=START,
        requested_until=END, query_filters={}, source_ids=(NEWS,), source_policy="all",
    )
    return (
        CoverageRequirement(
            requirement_id="acme-headlines", category="target_news", product="headlines", **shared,
        ),
        CoverageRequirement(
            requirement_id="acme-full-text", category="target_news", product="full_text", **shared,
        ),
    )


def _plan() -> CoveragePlan:
    return CoveragePlan(
        partitions=(_partition("headlines"), _partition("full_text")),
        capabilities={NEWS: SourceCapability(
            products=frozenset({"headlines", "full_text"}),
            subject_kinds=frozenset({"ticker"}),
        )},
        bindings={},
    )


def _seal(tmp_path, *, body, metadata, registry, corrections):
    manifest = tmp_path / "query-inputs.json"
    paths = (body, metadata, registry, corrections)
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "invocation": INVOCATION,
        "files": [{"path": str(path), "sha256": _sha(path)} for path in paths],
        "environment": {},
    }, sort_keys=True))
    return load_verified_stores(
        manifest,
        invocation=INVOCATION,
        required_stores={"body": body, "metadata": metadata},
        environ={},
    )


def _inputs(tmp_path):
    body = tmp_path / "body.sqlite3"
    metadata = tmp_path / "metadata.sqlite3"
    registry = tmp_path / "sources.json"
    corrections = tmp_path / "finnhub-corrections.csv"
    _make_body_store(body)
    _make_metadata_store(metadata)
    registry.write_text(json.dumps({
        "schema_version": 1,
        "sources": [{"id": NEWS}, {"id": LEGACY_METADATA_SOURCE}],
    }, sort_keys=True))
    corrections.write_text(
        "url,publisher,stored_eligible_at,corrected_at,stores\n"
        "https://news.test/acme,Wire,2026-10-20T09:00:00Z,2026-10-20T09:30:00Z,metadata-correction\n"
    )
    table = load_corrections(
        corrections.read_bytes(), kind="finnhub", filename=corrections.name,
        expected_sha256=_sha(corrections),
    )
    verified = _seal(
        tmp_path, body=body, metadata=metadata, registry=registry, corrections=corrections,
    )
    return verified, body, metadata, registry, corrections, table


def _query(
    verified, registry, corrections, table, *, ticker="ACME",
    body_store_ids=("body",), metadata_stores=(("metadata", "news_archive"),),
    mode="observed", window_start=None, window_end=None, cutoff=None,
    correction_tables=None, correction_store_ids=None, correction_artifacts=None,
    registry_path_override="use-default", allowed_legacy_cohorts=(), legacy_catalogs=None,
    requirements=None, plan=None, correction_table_id_override=None,
):
    registry_path = registry if registry_path_override == "use-default" else registry_path_override
    return query_service.query_stores(
        verified_stores=verified,
        body_store_ids=body_store_ids,
        metadata_stores=metadata_stores,
        registry_path=registry_path,
        ticker=ticker,
        query='"ACME"',
        window_start=_dt(START) if window_start is None else window_start,
        window_end=_dt(END) if window_end is None else window_end,
        cutoff=_dt(CUTOFF) if cutoff is None else cutoff,
        mode=mode,
        correction_table_id=(table.table_id if correction_table_id_override is None
                             else correction_table_id_override),
        correction_tables=(table,) if correction_tables is None else correction_tables,
        correction_store_ids=({"metadata": "metadata-correction"}
                              if correction_store_ids is None else correction_store_ids),
        correction_artifacts=({table.table_id: corrections}
                              if correction_artifacts is None else correction_artifacts),
        requirements=_requirements() if requirements is None else requirements,
        plan=_plan() if plan is None else plan,
        allowed_legacy_cohorts=allowed_legacy_cohorts,
        legacy_catalogs={} if legacy_catalogs is None else legacy_catalogs,
    )


def test_query_facade_combines_pinned_stores_and_keeps_missing_readiness_separate(tmp_path):
    verified, body, _metadata, registry, corrections, table = _inputs(tmp_path)

    first = _query(verified, registry, corrections, table)
    second = _query(verified, registry, corrections, table)

    assert [row["document_id"] for row in first["evidence"]] == ["before-cutoff"]
    assert [row["title"] for row in first["leads"]] == ["ACME results"]
    assert first["leads"][0]["published_at"] == "2026-10-20T09:30:00.000000+00:00"
    assert first["facts"] == []
    readiness = first["readiness"]
    assert readiness["progress"] == {
        "completed_requirements": 1,
        "total_declared_requirements": 2,
    }
    assert readiness["audit"]["missing_requirement_ids"] == ["acme-full-text"]
    assert len(readiness["results"]) == 1
    category = readiness["results"][0]
    assert category["category"] == "target_news"
    assert category["complete"] is False
    assert category["missing_requirement_ids"] == ["acme-full-text"]
    assert "sec_filings" not in {row["category"] for row in readiness["results"]}
    assert first["audit"] == second["audit"]
    assert first["audit"]["data_snapshot_ids"] == {
        "body": _sha(body),
        "metadata": _sha(tmp_path / "metadata.sqlite3"),
    }
    assert first["audit"]["read_only"] is True
    assert first["audit"]["mode"] == "observed"
    assert first["audit"]["window_start"] == _dt(START).isoformat(timespec="microseconds")
    assert first["audit"]["window_end"] == _dt(END).isoformat(timespec="microseconds")
    assert first["audit"]["cutoff"] == _dt(CUTOFF).isoformat(timespec="microseconds")


def test_query_facade_rejects_ticker_that_conflicts_with_declared_requirements(tmp_path):
    verified, _body, _metadata, registry, corrections, table = _inputs(tmp_path)

    with pytest.raises(ValueError, match="ticker|requirement|scope"):
        _query(verified, registry, corrections, table, ticker="OTHER")


def test_query_facade_fails_if_a_pinned_database_changes_after_verification(tmp_path):
    verified, _body, metadata, registry, corrections, table = _inputs(tmp_path)
    with metadata.open("ab") as stream:
        stream.write(b"mutation")

    with pytest.raises(ValueError):
        _query(verified, registry, corrections, table)


@pytest.mark.parametrize("mode", ["current", ""])
def test_query_facade_validates_mode_even_when_no_data_stores_are_selected(tmp_path, mode):
    verified, _body, _metadata, registry, corrections, table = _inputs(tmp_path)

    with pytest.raises(ValueError, match="mode"):
        _query(
            verified, registry, corrections, table,
            body_store_ids=(), metadata_stores=(), registry_path_override=None,
            mode=mode, correction_tables=(), correction_store_ids={}, correction_artifacts={},
        )


def test_query_facade_validates_window_even_when_no_data_stores_are_selected(tmp_path):
    verified, _body, _metadata, registry, corrections, table = _inputs(tmp_path)

    with pytest.raises(ValueError, match="window|interval|time"):
        _query(
            verified, registry, corrections, table,
            body_store_ids=(), metadata_stores=(), registry_path_override=None,
            window_start=_dt(END), window_end=_dt(END),
            correction_tables=(), correction_store_ids={}, correction_artifacts={},
        )


def test_query_facade_does_not_claim_unbound_correction_input_was_used(tmp_path):
    verified, _body, _metadata, _registry, corrections, table = _inputs(tmp_path)

    try:
        result = _query(
            verified, None, corrections, table,
            body_store_ids=("body",), metadata_stores=(), registry_path_override=None,
            correction_tables=(table,), correction_store_ids={},
            correction_artifacts={table.table_id: corrections},
        )
    except ValueError as exc:
        assert "correction" in str(exc).lower() or "unused" in str(exc).lower()
    else:
        audit = result["audit"]
        assert audit.get("unused_correction_table_ids") == [table.table_id]
        assert table.table_id not in audit.get("correction_table_ids", [])


def test_query_facade_does_not_claim_unselected_legacy_catalog_was_used(tmp_path):
    verified, _body, _metadata, _registry, corrections, table = _inputs(tmp_path)
    catalog = tmp_path / "unused-catalog.json"
    catalog.write_text(json.dumps({"schema_version": 1, "cohort_id": "unused"}))

    try:
        result = _query(
            verified, None, corrections, table,
            body_store_ids=(), metadata_stores=(), registry_path_override=None,
            correction_tables=(), correction_store_ids={}, correction_artifacts={},
            allowed_legacy_cohorts=(), legacy_catalogs={"unused": catalog},
        )
    except ValueError as exc:
        assert "legacy" in str(exc).lower() or "catalog" in str(exc).lower() or "unused" in str(exc).lower()
    else:
        assert result["audit"].get("unused_legacy_catalogs") == ["unused"]
        assert "unused" not in result["audit"].get("legacy_catalog_ids", [])


def _append_headline_attempt(metadata, *, attempt_id, recorded_at=None, status="ERROR"):
    with sqlite3.connect(metadata) as conn:
        attempt = coverage.start_attempt(
            conn, logical_task_id="task-headlines", attempt_id=attempt_id,
            source_id=NEWS, product="headlines", subject_kind="ticker", subject_id="ACME",
            query_spec={}, coverage_policy_version=POLICY,
            requested_from=START, requested_until=END,
        )
        if recorded_at is not None:
            coverage.record_coverage(
                conn, coverage_id=f"coverage-{attempt_id}", attempt_id=attempt.attempt_id,
                status=status, reason="ERROR" if status == "ERROR" else None,
                recorded_at=recorded_at,
            )


def test_readiness_fails_closed_for_retry_without_trusted_start_time(tmp_path):
    _verified, body, metadata, registry, corrections, table = _inputs(tmp_path)
    _append_headline_attempt(
        metadata, attempt_id="attempt-after-cutoff-error",
        recorded_at="2026-10-20T11:30:00Z", status="ERROR",
    )
    verified = _seal(
        tmp_path, body=body, metadata=metadata, registry=registry, corrections=corrections,
    )

    result = _query(verified, registry, corrections, table)

    # A late error fact does not prove that its attempt began after cutoff.
    assert result["readiness"]["progress"]["completed_requirements"] == 0
    assert result["readiness"]["results"][0]["complete"] is False
    assert set(result["readiness"]["results"][0]["missing_requirement_ids"]) == {
        "acme-full-text", "acme-headlines",
    }


def test_readiness_fails_closed_when_latest_attempt_has_no_recorded_state(tmp_path):
    _verified, body, metadata, registry, corrections, table = _inputs(tmp_path)
    # The attempt schema has no started_at. A pending attempt therefore cannot
    # be proven to have started after the cutoff, so the older OK must not be
    # presented as the current as-of state.
    _append_headline_attempt(metadata, attempt_id="attempt-without-state")
    verified = _seal(
        tmp_path, body=body, metadata=metadata, registry=registry, corrections=corrections,
    )

    result = _query(verified, registry, corrections, table)

    assert result["readiness"]["progress"]["completed_requirements"] == 0
    assert result["readiness"]["results"][0]["complete"] is False


def test_future_first_ok_coverage_does_not_satisfy_cutoff_readiness(tmp_path):
    body = tmp_path / "body.sqlite3"
    metadata = tmp_path / "metadata.sqlite3"
    registry = tmp_path / "sources.json"
    corrections = tmp_path / "finnhub-corrections.csv"
    _make_body_store(body)
    _make_metadata_store(metadata, seed_coverage=False)
    registry.write_text(json.dumps({"schema_version": 1, "sources": [
        {"id": NEWS}, {"id": LEGACY_METADATA_SOURCE},
    ]}, sort_keys=True))
    corrections.write_text(
        "url,publisher,stored_eligible_at,corrected_at,stores\n"
        "https://news.test/acme,Wire,2026-10-20T09:00:00Z,2026-10-20T09:30:00Z,metadata-correction\n"
    )
    table = load_corrections(
        corrections.read_bytes(), kind="finnhub", filename=corrections.name,
        expected_sha256=_sha(corrections),
    )
    with sqlite3.connect(metadata) as conn:
        attempt = coverage.start_attempt(
            conn, logical_task_id="task-headlines", attempt_id="attempt-first-after-cutoff",
            source_id=NEWS, product="headlines", subject_kind="ticker", subject_id="ACME",
            query_spec={}, coverage_policy_version=POLICY,
            requested_from=START, requested_until=END,
        )
        coverage.record_coverage(
            conn, coverage_id="coverage-first-after-cutoff", attempt_id=attempt.attempt_id,
            status="OK", recorded_at="2026-10-20T11:30:00Z",
            coverage_from=START, coverage_until=END, enumeration_complete=True,
        )
    verified = _seal(
        tmp_path, body=body, metadata=metadata, registry=registry, corrections=corrections,
    )

    result = _query(verified, registry, corrections, table)

    assert result["readiness"]["progress"]["completed_requirements"] == 0
    assert result["readiness"]["results"][0]["complete"] is False


def test_query_audit_binds_deterministic_requirement_and_plan_identities(tmp_path):
    verified, _body, _metadata, registry, corrections, table = _inputs(tmp_path)
    first = _query(verified, registry, corrections, table)
    repeated = _query(verified, registry, corrections, table)

    assert first["audit"]["coverage_requirements_sha256"]
    assert first["audit"]["coverage_plan_sha256"]
    assert first["audit"]["coverage_identity_format"] == "canonical-json-sha256"
    assert first["audit"]["coverage_identity_version"] == 1
    assert "coverage_plan" not in first["audit"]
    assert "coverage_requirements" not in first["audit"]
    assert first["audit"]["coverage_requirements_sha256"] == repeated["audit"]["coverage_requirements_sha256"]
    assert first["audit"]["coverage_plan_sha256"] == repeated["audit"]["coverage_plan_sha256"]

    base = _plan()
    later_start, later_end = "2026-10-20T13:00:00Z", "2026-10-20T14:00:00Z"
    later = PlannedPartition(
        partition_id="pending", source_id=NEWS, product="headlines",
        subject_kind="ticker", subject_id="ACME", requested_from=later_start,
        requested_until=later_end, query_filters={}, coverage_policy_version=POLICY,
        session_ids=(),
    )
    later = PlannedPartition(**(later.__dict__ | {"partition_id": expected_partition_id(later)}))
    changed_plan = CoveragePlan(
        partitions=base.partitions + (later,), capabilities=base.capabilities,
        bindings=base.bindings,
    )
    changed = _query(verified, registry, corrections, table, plan=changed_plan)

    assert changed["audit"]["coverage_requirements_sha256"] == first["audit"]["coverage_requirements_sha256"]
    assert changed["audit"]["coverage_plan_sha256"] != first["audit"]["coverage_plan_sha256"]


def test_body_correction_binding_rejects_mismatched_selected_table_id(tmp_path):
    verified, _body, _metadata, _registry, corrections, table = _inputs(tmp_path)

    with pytest.raises(ValueError, match="correction|table|identity|id"):
        _query(
            verified, None, corrections, table,
            body_store_ids=("body",), metadata_stores=(), registry_path_override=None,
            correction_tables=(table,), correction_store_ids={"body": "body-correction"},
            correction_artifacts={table.table_id: corrections},
            correction_table_id_override="different-table-id",
        )
