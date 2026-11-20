"""Additive contract v1.1 read-envelope and prepared-range coverage."""
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from catalyst_data import read_api
from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.coverage_readiness import (
    CoveragePlan,
    CoverageRequirement,
    PlannedPartition,
    SourceCapability,
    expected_partition_id,
)
from catalyst_data.storage.as_of_stores import as_of_stores
from catalyst_data.storage.snapshots import load_verified_stores

REAL = Path(__file__).parent / "fixtures" / "real_sec_8k_slice.sqlite3"
JOBS = Path(__file__).parent / "fixtures" / "real_update_jobs_slice.sqlite3"
INVOCATION = ["read-api-v11"]
START = datetime(2026, 10, 27, tzinfo=timezone.utc)
END = datetime(2026, 10, 31, tzinfo=timezone.utc)


@pytest.fixture
def verified(tmp_path):
    store = tmp_path / "bodies.sqlite3"
    shutil.copy(REAL, store)
    manifest = tmp_path / "inputs.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "invocation": INVOCATION, "environment": {},
        "files": [{"path": str(store), "sha256": hashlib.sha256(store.read_bytes()).hexdigest()}],
    }))
    return load_verified_stores(
        manifest, invocation=INVOCATION, required_stores={"bodies": store}, environ={},
    )


def _read(verified, *, mode="observed"):
    return read_api.as_of(verified, "AVGO", START, END, END, mode=mode)


def test_envelope_is_additive_and_hashes_are_stable(verified):
    result = _read(verified)
    envelope = result["envelope"]

    assert {
        "contract_version", "mode", "observed_as_of", "window_start", "window_end",
        "snapshot_identity", "correction_table", "dedup", "migration_manifest_sha256",
        "evidence", "leads", "evidence_set_hash", "data_read_identity_hash",
    } <= envelope.keys()
    assert envelope["contract_version"] == "data-core-v1.1"
    assert envelope["correction_table"]["enabled"] is False
    assert envelope["migration_manifest_sha256"] is None
    assert envelope["dedup"] == {
        "rule_version": None, "relation_ids": [], "relation_set_sha256": None, "status": "not_applied",
    }
    identity = envelope["evidence"][0]
    direct = as_of_stores(verified, "AVGO", START, END, END, mode="observed", correction_table_id="none")
    assert identity["content_hash"] is None  # original-byte digest is not stored (contract §1)
    assert identity["record_key"] is None
    assert identity["time_confirmation"] is None
    assert identity["usage_ceiling"] is None
    assert _read(verified)["envelope"] == envelope


def test_existing_result_fields_and_values_are_unchanged(verified):
    result = _read(verified)
    direct = as_of_stores(verified, "AVGO", START, END, END, mode="observed", correction_table_id="none")
    legacy_projection = {key: value for key, value in result.items() if key != "envelope"}

    assert legacy_projection == {
        "mode": "observed",
        "t": END.isoformat(),
        "evidence": [read_api.evidence_dto(row) for row in direct["evidence"]],
        "leads": [read_api.evidence_dto(row) for row in direct["leads"]],
        "audit": direct["audit"],
    }


def test_hash_order_is_independent_of_input_row_order(verified, monkeypatch):
    original = read_api.as_of_stores

    def with_two_rows(*args, reverse=False, **kwargs):
        result = original(*args, **kwargs)
        first = dict(result["evidence"][0])
        second = dict(first, version_id=f"{first['version_id']}-later", document_id="later")
        result["evidence"] = [first, second]
        if reverse:
            result["evidence"].reverse()
        return result

    monkeypatch.setattr(read_api, "as_of_stores", with_two_rows)
    forward = _read(verified)["envelope"]
    reverse = read_api.as_of(
        verified, "AVGO", START, END, END, mode="observed", reverse=True,
    )["envelope"]

    assert forward["evidence"] == reverse["evidence"]
    assert forward["evidence_set_hash"] == reverse["evidence_set_hash"]
    assert forward["data_read_identity_hash"] == reverse["data_read_identity_hash"]


def test_evidence_and_lead_lists_have_separate_set_identity(verified):
    observed = _read(verified)["envelope"]
    published = _read(verified, mode="published")["envelope"]

    assert observed["evidence"] and not observed["leads"]
    assert not published["evidence"] and published["leads"]
    assert observed["evidence_set_hash"] != published["evidence_set_hash"]


def test_nan_in_audited_snapshot_identity_is_rejected(verified, monkeypatch):
    original = read_api.as_of_stores

    def with_nan(*args, **kwargs):
        result = original(*args, **kwargs)
        result["audit"]["data_snapshot_ids"] = {"bodies": float("nan")}
        return result

    monkeypatch.setattr(read_api, "as_of_stores", with_nan)
    with pytest.raises(ValueError, match="JSON compliant"):
        _read(verified)


NEWS = "daily:finnhub"
POLICY = "coverage-v1"
COVERAGE_START = "2026-10-27T13:30:00Z"
COVERAGE_END = "2026-10-27T20:00:00Z"


def _jobs_store(path, movers):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE job_items (stage TEXT, status TEXT, session_date TEXT, result_json TEXT, finished_at TEXT)")
        conn.execute("INSERT INTO job_items VALUES ('verify','OK','2026-10-27',NULL,'1')")
        conn.execute("INSERT INTO job_items VALUES ('movers','OK','2026-10-27',?,'2')", (json.dumps({"movers": movers}),))
    return path


def _coverage_store(path, states, *, short=(), recorded_at="2026-10-28T00:00:00Z"):
    conn = sqlite3.connect(path)
    coverage.ensure_schema(conn)
    for ticker, status in states:
        attempt = coverage.start_attempt(
            conn, logical_task_id=f"task-{ticker}", attempt_id=f"attempt-{ticker}", source_id=NEWS,
            product="headlines", subject_kind="ticker", subject_id=ticker, query_spec={},
            coverage_policy_version=POLICY, requested_from=COVERAGE_START, requested_until=COVERAGE_END,
        )
        coverage.record_coverage(
            conn, coverage_id=f"coverage-{ticker}", attempt_id=attempt.attempt_id, status=status,
            reason="ERROR" if status == "ERROR" else None, recorded_at=recorded_at,
            coverage_from=COVERAGE_START, enumeration_complete=True,
            coverage_until="2026-10-27T16:00:00Z" if ticker in short else COVERAGE_END,
        )
    conn.commit()
    conn.close()
    return path


def _partition(ticker):
    values = dict(
        source_id=NEWS, product="headlines", subject_kind="ticker", subject_id=ticker,
        requested_from=COVERAGE_START, requested_until=COVERAGE_END, query_filters={},
        coverage_policy_version=POLICY, session_ids=(), partition_id="pending",
    )
    pending = PlannedPartition(**values)
    return PlannedPartition(**(values | {"partition_id": expected_partition_id(pending)}))


def _requirement(pair, *, requirement_id=None, ticker=None, session_date=None, category="target_news",
                 scope_kind="target", scope_id=None):
    ticker = ticker or pair.ticker
    scope_id = scope_id or ticker
    values = dict(
        requirement_id=requirement_id or f"{pair.session_date}-{pair.ticker}-{category}",
        session_date=session_date or pair.session_date, ticker=ticker, category=category,
        scope_kind=scope_kind, scope_id=scope_id, subject_kind="ticker", subject_id=scope_id,
        product="headlines", requested_from=COVERAGE_START, requested_until=COVERAGE_END,
        query_filters={}, source_ids=(NEWS,), source_policy="all",
    )
    return CoverageRequirement(**values)


def _ready_fixture(tmp_path, movers, states, **coverage_options):
    jobs = _jobs_store(tmp_path / "jobs.sqlite3", movers)
    facts = _coverage_store(tmp_path / "coverage.sqlite3", states, **coverage_options)
    manifest = tmp_path / "inputs.json"
    paths = [jobs, facts]
    manifest.write_text(json.dumps({
        "schema_version": 1, "invocation": INVOCATION, "environment": {},
        "files": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths],
    }))
    verified = load_verified_stores(
        manifest, invocation=INVOCATION, required_stores={"jobs": jobs, "facts": facts}, environ={},
    )
    plan = CoveragePlan(
        tuple(_partition(ticker) for ticker, _ in states),
        {NEWS: SourceCapability(frozenset({"headlines"}), frozenset({"ticker"}))}, {},
    )
    return verified, plan, jobs, facts, manifest


def test_ready_range_checks_declared_category_statuses_and_not_declared(tmp_path):
    movers = [{"ticker": "ABC", "return_pct": 1.2}, {"ticker": "XYZ", "return_pct": -2.0}]
    verified, plan, *_ = _ready_fixture(tmp_path, movers, [("ABC", "OK"), ("DEF", "OK"), ("XYZ", "ERROR")])

    def requirements_for(pair):
        requirements = [_requirement(pair)]
        if pair.ticker == "ABC":
            requirements.append(_requirement(pair, category="peer_news", scope_kind="peer", scope_id="DEF"))
        return requirements

    result = read_api.ready_range(
        verified, jobs_store_id="jobs", requirements_for=requirements_for, plan=plan,
        contract_version="data-core-v1.1", required_categories=("target_news", "peer_news"),
    )

    assert result["pairs"] == [{"ticker": "ABC", "session_date": "2026-10-27"}]
    assert result["incomplete_pairs"][0]["ticker"] == "XYZ"
    assert result["incomplete_pairs"][0]["categories"]["target_news"]["status"] == "ERROR"
    assert result["incomplete_pairs"][0]["categories"]["target_news"]["complete"] is False
    assert result["incomplete_pairs"][0]["categories"]["peer_news"] == {
        "status": "NOT_DECLARED", "reason": None, "complete": False, "missing_requirement_ids": [],
    }
    assert result["integrity_basis"]["six_category_check"]["status"] == "checked"


def test_ready_range_rejects_mismatched_requirement_and_invalid_categories(tmp_path):
    verified, plan, *_ = _ready_fixture(tmp_path, [{"ticker": "ABC", "return_pct": 1}], [("ABC", "OK")])
    with pytest.raises(ValueError, match="must match"):
        read_api.ready_range(
            verified, jobs_store_id="jobs", requirements_for=lambda p: [
                _requirement(p, ticker="XYZ")], plan=plan, contract_version="v1",
            required_categories=("target_news",),
        )
    for categories in (("unknown",), ("target_news", "target_news"), ()):
        with pytest.raises(ValueError, match="required_categories"):
            read_api.ready_range(verified, jobs_store_id="jobs", requirements_for=lambda p: [], plan=plan,
                                 contract_version="v1", required_categories=categories)


def test_ready_range_snapshot_identity_is_deterministic_and_data_derived(tmp_path):
    verified, plan, jobs, facts, manifest = _ready_fixture(
        tmp_path, [{"ticker": "ABC", "return_pct": 1}], [("ABC", "OK")],
    )
    kwargs = dict(jobs_store_id="jobs", requirements_for=lambda pair: [_requirement(pair)], plan=plan,
                  contract_version="v1", required_categories=("target_news",))
    first = read_api.ready_range(verified, **kwargs)
    again = read_api.ready_range(verified, **kwargs)
    assert first["snapshot_id"].startswith("rr1:")
    assert first["snapshot_id"] == again["snapshot_id"]
    assert read_api.ready_range(verified, snapshot_id=first["snapshot_id"], **kwargs)["snapshot_id"] == first["snapshot_id"]
    with pytest.raises(ValueError, match="does not match the data"):
        read_api.ready_range(verified, snapshot_id="wrong", **kwargs)
    assert read_api.ready_range(verified, **(kwargs | {"required_categories": ("target_news", "peer_news")}))["snapshot_id"] != first["snapshot_id"]

    with facts.open("ab") as stream:
        stream.write(b"changed")
    digest = hashlib.sha256(facts.read_bytes()).hexdigest()
    manifest.write_text(json.dumps({
        "schema_version": 1, "invocation": INVOCATION, "environment": {},
        "files": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in (jobs, facts)],
    }))
    repinned = load_verified_stores(manifest, invocation=INVOCATION,
                                    required_stores={"jobs": jobs, "facts": facts}, environ={})
    assert repinned.stores["facts"].sha256 == digest
    assert read_api.ready_range(repinned, **kwargs)["snapshot_id"] != first["snapshot_id"]


def test_ready_range_requires_pinned_jobs_store_and_skips_evaluation_for_empty_scope(tmp_path, monkeypatch):
    verified, plan, *_ = _ready_fixture(tmp_path, [], [])
    with pytest.raises(ValueError, match="jobs_store_id"):
        read_api.ready_range(verified, jobs_store_id="missing", requirements_for=lambda pair: [], plan=plan,
                             contract_version="v1")
    monkeypatch.setattr(read_api.gap_readiness, "evaluate_snapshots", lambda *args, **kwargs: pytest.fail("unexpected evaluation"))
    result = read_api.ready_range(verified, jobs_store_id="jobs", requirements_for=lambda pair: [], plan=plan,
                                  contract_version="v1")
    assert result["pairs"] == []
    assert result["incomplete_pairs"] == []
    assert result["snapshot_id"].startswith("rr1:")
    assert result["integrity_basis"]["six_category_check"]["coverage_requirements_sha256"] is not None
    assert result["integrity_basis"]["six_category_check"]["coverage_plan_sha256"] is not None


def test_ready_range_with_no_declared_requirement_reports_every_pair_not_declared(tmp_path):
    verified, plan, *_ = _ready_fixture(tmp_path, [{"ticker": "ABC", "return_pct": 1}], [("ABC", "OK")])
    result = read_api.ready_range(verified, jobs_store_id="jobs", requirements_for=lambda pair: [], plan=plan,
                                  contract_version="v1", required_categories=("target_news",))
    assert result["pairs"] == []
    assert result["incomplete_pairs"][0]["categories"]["target_news"]["status"] == "NOT_DECLARED"
    assert result["integrity_basis"]["six_category_check"]["status"] == "not_evaluated_no_requirements"


def test_ready_range_rejects_a_naive_cutoff(tmp_path):
    from datetime import datetime

    verified, plan, *_ = _ready_fixture(tmp_path, [{"ticker": "ABC", "return_pct": 1}], [("ABC", "OK")])
    with pytest.raises(ValueError, match="cutoff"):
        read_api.ready_range(verified, jobs_store_id="jobs", requirements_for=lambda pair: [], plan=plan,
                             contract_version="v1", cutoff=datetime(2026, 10, 28))


def test_ready_range_keeps_a_pair_out_when_one_required_category_is_ok_but_not_complete(tmp_path):
    verified, plan, *_ = _ready_fixture(
        tmp_path, [{"ticker": "ABC", "return_pct": 1}], [("ABC", "OK"), ("DEF", "OK")], short=("DEF",),
    )

    def requirements_for(pair):
        return [_requirement(pair),
                _requirement(pair, category="peer_news", scope_kind="peer", scope_id="DEF")]

    result = read_api.ready_range(verified, jobs_store_id="jobs", requirements_for=requirements_for, plan=plan,
                                  contract_version="v1", required_categories=("target_news", "peer_news"))
    assert result["pairs"] == []
    categories = result["incomplete_pairs"][0]["categories"]
    assert categories["target_news"]["complete"] is True
    assert categories["peer_news"]["status"] == "OK"
    assert categories["peer_news"]["complete"] is False


def test_ready_range_identity_separates_cutoffs_within_one_second(tmp_path):
    from datetime import datetime, timezone

    verified, plan, *_ = _ready_fixture(
        tmp_path, [{"ticker": "ABC", "return_pct": 1}], [("ABC", "OK")],
        recorded_at="2026-10-28T20:00:00.400000Z",
    )
    kwargs = dict(jobs_store_id="jobs", requirements_for=lambda pair: [_requirement(pair)], plan=plan,
                  contract_version="v1", required_categories=("target_news",))
    early = read_api.ready_range(verified, cutoff=datetime(2026, 10, 28, 20, 0, 0, 100000, tzinfo=timezone.utc), **kwargs)
    late = read_api.ready_range(verified, cutoff=datetime(2026, 10, 28, 20, 0, 0, 800000, tzinfo=timezone.utc), **kwargs)
    assert early["pairs"] == [] and late["pairs"] == [{"ticker": "ABC", "session_date": "2026-10-27"}]
    assert early["snapshot_id"] != late["snapshot_id"]
    with pytest.raises(ValueError, match="does not match the data"):
        read_api.ready_range(verified, cutoff=datetime(2026, 10, 28, 20, 0, 0, 800000, tzinfo=timezone.utc),
                             snapshot_id=early["snapshot_id"], **kwargs)


def test_ready_range_identity_binds_the_plan_even_without_requirements(tmp_path):
    verified, plan, *_ = _ready_fixture(tmp_path, [{"ticker": "ABC", "return_pct": 1}], [("ABC", "OK")])
    other = CoveragePlan((), plan.capabilities, {})
    kwargs = dict(jobs_store_id="jobs", requirements_for=lambda pair: [], contract_version="v1",
                  required_categories=("target_news",))
    first = read_api.ready_range(verified, plan=plan, **kwargs)
    with pytest.raises(ValueError, match="does not match the data"):
        read_api.ready_range(verified, plan=other, snapshot_id=first["snapshot_id"], **kwargs)


def test_multi_cohort_read_does_not_hash_like_a_read_without_cohorts():
    from datetime import datetime, timezone

    from catalyst_data import read_api

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    bare = {"audit": {}, "evidence": [], "leads": []}
    two = {"audit": {"legacy_catalogs": {"a": {"catalog_sha256": "1" * 64}, "b": {"catalog_sha256": "2" * 64}}},
           "evidence": [], "leads": []}
    env_bare = read_api._envelope(bare, None, start, end, "published", "data-core-v1.1")
    env_two = read_api._envelope(two, None, start, end, "published", "data-core-v1.1")
    assert env_two["migration_manifest_sha256"] is None
    assert env_two["legacy_catalog_sha256"] == {"a": "1" * 64, "b": "2" * 64}
    assert env_bare["data_read_identity_hash"] != env_two["data_read_identity_hash"]
