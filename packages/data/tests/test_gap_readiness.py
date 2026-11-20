from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.coverage_readiness import (
    CoveragePlan,
    CoverageRequirement,
    PlannedPartition,
    SourceCapability,
    expected_partition_id,
)
from catalyst_data.ingestion.gap_readiness import evaluate_snapshots
from catalyst_data.ingestion.input_manifest import PreflightError
from catalyst_data.storage.snapshots import load_verified_stores

NEWS = "daily:finnhub"
START = "2026-10-23T13:30:00Z"
END = "2026-10-23T20:00:00Z"
POLICY = "coverage-v1"
INVOCATION = ["python", "-m", "catalyst_data.ingestion.coverage_report"]


def requirement(**changes):
    values = dict(
        requirement_id="abc-target", session_date="2026-10-23", ticker="ABC",
        category="target_news", scope_kind="target", scope_id="ABC",
        subject_kind="ticker", subject_id="ABC", product="headlines",
        requested_from=START, requested_until=END, query_filters={},
        source_ids=(NEWS,), source_policy="all",
    )
    return CoverageRequirement(**(values | changes))


def partition(**changes):
    values = dict(
        source_id=NEWS, product="headlines", subject_kind="ticker", subject_id="ABC",
        requested_from=START, requested_until=END, query_filters={},
        coverage_policy_version=POLICY, session_ids=(), partition_id="pending",
    ) | changes
    pending = PlannedPartition(**values)
    return PlannedPartition(**(values | {"partition_id": expected_partition_id(pending)}))


def plan(parts=None):
    parts = tuple(parts or (partition(),))
    return CoveragePlan(parts,
                        {NEWS: SourceCapability(frozenset(part.product for part in parts),
                                                frozenset({"ticker"}))}, {})


def make_store(path: Path, *, attempt_id="attempt-1", coverage_id="coverage-1", status="OK",
               category_table=False, start=START, end=END):
    conn = sqlite3.connect(path)
    coverage.ensure_schema(conn)
    attempt = coverage.start_attempt(
        conn, logical_task_id="task", attempt_id=attempt_id, source_id=NEWS,
        product="headlines", subject_kind="ticker", subject_id="ABC", query_spec={},
        coverage_policy_version=POLICY, requested_from=start, requested_until=end,
    )
    coverage.record_coverage(
        conn, coverage_id=coverage_id, attempt_id=attempt.attempt_id, status=status,
        reason="ERROR" if status == "ERROR" else None, recorded_at="2026-10-24T00:00:00Z",
        coverage_from=start, coverage_until=end, enumeration_complete=True,
    )
    if category_table:
        conn.execute("CREATE TABLE category_gaps (id INTEGER)")
    conn.commit(); conn.close()
    return path


def seal(tmp_path: Path, paths: list[Path], *, optional=()):
    manifest = tmp_path / "inputs.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "invocation": INVOCATION,
        "files": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths],
        "environment": {},
    }))
    return load_verified_stores(
        manifest, invocation=INVOCATION,
        required_stores=[(f"store-{i}", path) for i, path in enumerate(paths)],
        optional_stores=optional, environ={},
    )


def test_multiple_read_only_stores_merge_disjoint_partition_history_using_g1_tables(tmp_path):
    split = "2026-10-23T16:30:00Z"
    left, right = partition(requested_until=split), partition(requested_from=split)
    first = make_store(tmp_path / "left.sqlite3", attempt_id="left", coverage_id="left-c", end=split)
    second = make_store(tmp_path / "right.sqlite3", attempt_id="right", coverage_id="right-c", start=split)
    verified = seal(tmp_path, [first, second])

    result = evaluate_snapshots((requirement(),), plan((left, right)), verified)

    assert result["results"][0]["complete"] is True
    assert result["results"][0]["category"] == "target_news"
    assert result["progress"]["completed_requirements"] == 1
    assert result["progress"]["total_declared_requirements"] == 1
    assert result["audit"]["read_only"] is True
    assert result["audit"]["manifest_sha256"] == verified.manifest_sha256
    assert result["audit"]["data_snapshot_ids"] == {key: item.sha256 for key, item in verified.stores.items()}
    assert "source_id" not in result["results"][0]
    assert "provider_id" not in result["results"][0]


def test_latest_attempt_and_no_event_semantics_match_g1_without_old_gap_rows(tmp_path):
    path = tmp_path / "daily.sqlite3"
    conn = sqlite3.connect(path)
    coverage.ensure_schema(conn)
    old = coverage.start_attempt(conn, logical_task_id="task", attempt_id="old", source_id=NEWS,
                                 product="headlines", subject_kind="ticker", subject_id="ABC",
                                 query_spec={}, coverage_policy_version=POLICY,
                                 requested_from=START, requested_until=END)
    coverage.record_coverage(conn, coverage_id="old-c", attempt_id=old.attempt_id, status="OK",
                             recorded_at="2026-10-24T00:00:00Z", coverage_from=START,
                             coverage_until=END, enumeration_complete=True)
    coverage.start_attempt(conn, logical_task_id="task", attempt_id="new", source_id=NEWS,
                           product="headlines", subject_kind="ticker", subject_id="ABC",
                           query_spec={}, coverage_policy_version=POLICY,
                           requested_from=START, requested_until=END)
    conn.commit(); conn.close()
    verified = seal(tmp_path, [path])

    result = evaluate_snapshots((requirement(),), plan(), verified)

    assert result["results"][0]["status"] == "NOT_FETCHED"
    assert result["results"][0]["complete"] is False
    assert result["results"][0]["missing_requirement_ids"] == ["abc-target"]


def test_missing_all_coverage_tables_is_not_fetched_but_partial_or_bad_schema_raises(tmp_path):
    empty = tmp_path / "empty.sqlite3"
    with sqlite3.connect(empty):
        pass
    verified = seal(tmp_path, [empty])
    result = evaluate_snapshots((requirement(),), plan(), verified)
    assert result["results"][0]["status"] == "NOT_FETCHED"
    assert result["results"][0]["complete"] is False

    partial = tmp_path / "partial.sqlite3"
    with sqlite3.connect(partial) as conn:
        conn.execute("CREATE TABLE source_coverage_attempts (attempt_id TEXT)")
    with pytest.raises(ValueError, match="schema"):
        evaluate_snapshots((requirement(),), plan(), seal(tmp_path, [partial]))

    bad = tmp_path / "bad.sqlite3"
    with sqlite3.connect(bad) as conn:
        conn.execute("CREATE TABLE source_coverage_attempts (wrong TEXT)")
        conn.execute("CREATE TABLE source_coverage_facts (wrong TEXT)")
        conn.execute("CREATE TABLE source_coverage_targets (wrong TEXT)")
    with pytest.raises(ValueError, match="schema"):
        evaluate_snapshots((requirement(),), plan(), seal(tmp_path, [bad]))


def test_exact_duplicate_full_history_deduplicates_but_conflicts_fail_closed(tmp_path):
    first = make_store(tmp_path / "first.sqlite3")
    second = tmp_path / "second.sqlite3"
    second.write_bytes(first.read_bytes())
    verified = seal(tmp_path, [first, second])
    result = evaluate_snapshots((requirement(),), plan(), verified)
    assert result["results"][0]["complete"] is True

    conflict = make_store(tmp_path / "conflict.sqlite3", attempt_id="attempt-2", coverage_id="coverage-2", status="ERROR")
    with pytest.raises(ValueError, match="conflict"):
        evaluate_snapshots((requirement(),), plan(), seal(tmp_path, [first, conflict]))


def test_duplicate_ids_across_distinct_histories_and_global_id_collisions_fail_closed(tmp_path):
    one = make_store(tmp_path / "one.sqlite3")
    two = make_store(tmp_path / "two.sqlite3", attempt_id="attempt-2", coverage_id="coverage-2")
    # Same partition identity with different attempt history is ambiguous even though local seq values match.
    with pytest.raises(ValueError, match="conflict"):
        evaluate_snapshots((requirement(),), plan(), seal(tmp_path, [one, two]))

    other = make_store(tmp_path / "other.sqlite3", attempt_id="another", coverage_id="coverage-1")
    with pytest.raises(ValueError, match="identity"):
        evaluate_snapshots((requirement(),), plan(), seal(tmp_path, [one, other]))


def test_optional_absence_is_audited_and_undeclared_legacy_gap_tables_are_ignored(tmp_path):
    path = make_store(tmp_path / "daily.sqlite3", category_table=True)
    absent = tmp_path / "optional.sqlite3"
    verified = seal(tmp_path, [path], optional=(("archive", absent),))
    result = evaluate_snapshots((requirement(),), plan(), verified)
    assert result["audit"]["missing_optional_store_ids"] == ["archive"]
    assert result["audit"]["store_count"] == 1


def test_manifest_or_database_change_after_load_fails_before_publishing_results(tmp_path):
    path = make_store(tmp_path / "daily.sqlite3")
    verified = seal(tmp_path, [path])
    with path.open("ab") as stream:
        stream.write(b"mutation")
    with pytest.raises(PreflightError):
        evaluate_snapshots((requirement(),), plan(), verified)


def test_corrupt_target_boolean_is_rejected_even_when_column_names_match(tmp_path):
    path = make_store(tmp_path / "corrupt-target.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "INSERT INTO source_coverage_targets (coverage_id,identity,acquired) VALUES (?,?,?)",
            ("coverage-1", "provider:daily:finnhub:doc-1", 2),
        )
        conn.commit()

    with pytest.raises(ValueError, match="target"):
        evaluate_snapshots((requirement(),), plan(), seal(tmp_path, [path]))


def test_manifest_mutation_after_last_read_is_rechecked_before_results_publish(tmp_path, monkeypatch):
    path = make_store(tmp_path / "daily.sqlite3")
    verified = seal(tmp_path, [path])
    from catalyst_data.ingestion import gap_readiness

    original = gap_readiness.evaluate_coverage

    def mutate_manifest_then_evaluate(requirements, plan, snapshots):
        verified.manifest_path.write_text(verified.manifest_path.read_text() + " ")
        return original(requirements, plan, snapshots)

    monkeypatch.setattr(gap_readiness, "evaluate_coverage", mutate_manifest_then_evaluate)
    with pytest.raises(PreflightError, match="manifest changed"):
        gap_readiness.evaluate_snapshots((requirement(),), plan(), verified)


def test_named_g1_view_is_rejected_instead_of_being_treated_as_missing_schema(tmp_path):
    path = tmp_path / "g1-view.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE VIEW source_coverage_attempts AS SELECT 1 AS attempt_id")
    with pytest.raises(ValueError, match="schema"):
        evaluate_snapshots((requirement(),), plan(), seal(tmp_path, [path]))


def _make_full_text_store(path: Path, *, begin: str, end: str, suffix: str):
    conn = sqlite3.connect(path)
    coverage.ensure_schema(conn)
    attempt = coverage.start_attempt(
        conn, logical_task_id=f"task-{suffix}", attempt_id=f"attempt-{suffix}",
        source_id=NEWS, product="full_text", subject_kind="ticker", subject_id="ABC",
        query_spec={}, coverage_policy_version=POLICY,
        requested_from=begin, requested_until=end,
    )
    coverage.record_coverage(
        conn, coverage_id=f"coverage-{suffix}", attempt_id=attempt.attempt_id, status="OK",
        recorded_at="2026-10-24T00:00:00Z", coverage_from=begin, coverage_until=end,
        enumeration_complete=True, expected_targets=(f"doc-{suffix}",),
        acquired_targets=(f"doc-{suffix}",),
    )
    conn.commit()
    conn.close()
    return path


def test_many_disjoint_full_text_partitions_merge_their_target_memberships(tmp_path):
    boundaries = [START, "2026-10-23T15:00:00Z", "2026-10-23T16:30:00Z",
                  "2026-10-23T18:00:00Z", END]
    parts = []
    paths = []
    for index, (begin, end) in enumerate(zip(boundaries, boundaries[1:])):
        parts.append(partition(product="full_text", requested_from=begin, requested_until=end))
        paths.append(_make_full_text_store(
            tmp_path / f"full-text-{index}.sqlite3", begin=begin, end=end, suffix=str(index),
        ))
    req = requirement(product="full_text")

    result = evaluate_snapshots((req,), plan(parts), seal(tmp_path, paths))

    assert result["results"][0]["complete"] is True
    assert result["results"][0]["status"] == "OK"


def test_invalid_recorded_at_and_nonmonotonic_state_times_are_rejected(tmp_path):
    malformed = make_store(tmp_path / "bad-time.sqlite3")
    with sqlite3.connect(malformed) as conn:
        conn.execute("DROP TRIGGER source_coverage_facts_no_update")
        conn.execute("UPDATE source_coverage_facts SET recorded_at='not-a-time'")
        conn.commit()
    with pytest.raises(ValueError, match="recorded_at"):
        evaluate_snapshots((requirement(),), plan(), seal(tmp_path, [malformed]))

    decreasing = tmp_path / "decreasing-time.sqlite3"
    conn = sqlite3.connect(decreasing)
    coverage.ensure_schema(conn)
    attempt = coverage.start_attempt(
        conn, logical_task_id="task", attempt_id="attempt", source_id=NEWS,
        product="headlines", subject_kind="ticker", subject_id="ABC", query_spec={},
        coverage_policy_version=POLICY, requested_from=START, requested_until=END,
    )
    for index, stamp in enumerate(("2026-10-24T00:00:00Z", "2026-10-24T00:01:00Z")):
        coverage.record_coverage(
            conn, coverage_id=f"coverage-{index}", attempt_id=attempt.attempt_id,
            status="OK", recorded_at=stamp, coverage_from=START, coverage_until=END,
            enumeration_complete=True,
        )
    conn.execute("DROP TRIGGER source_coverage_facts_no_update")
    conn.execute("UPDATE source_coverage_facts SET recorded_at='2026-10-23T23:59:00Z' WHERE state_seq=2")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="nondecreasing"):
        evaluate_snapshots((requirement(),), plan(), seal(tmp_path, [decreasing]))


def test_category_result_requires_every_peer_scope_and_uses_public_statuses(tmp_path):
    available = requirement(
        requirement_id="peer-available", ticker="XYZ", category="peer_news",
        scope_kind="peer", scope_id="ABC",
    )
    unavailable = requirement(
        requirement_id="peer-unavailable", ticker="XYZ", category="peer_news",
        scope_kind="peer", scope_id="DEF", subject_id="DEF",
    )
    path = make_store(tmp_path / "peer.sqlite3")
    peer_plan = plan((partition(subject_id="ABC"), partition(subject_id="DEF")))

    result = evaluate_snapshots(
        (available, unavailable), peer_plan, seal(tmp_path, [path]),
    )

    category = result["results"][0]
    assert category["category"] == "peer_news"
    assert category["complete"] is False
    assert category["status"] in {"OK", "EMPTY", "ERROR", "NOT_FETCHED"}
    assert category["missing_requirement_ids"] == ["peer-unavailable"]
    product = category["products"][0]
    assert product["product"] == "headlines"
    assert [item["requirement_id"] for item in product["requirements"]] == [
        "peer-available", "peer-unavailable",
    ]
    assert product["requirements"][1]["scope_kind"] == "peer"
    assert product["requirements"][1]["requested_from"] == START
    assert product["requirements"][1]["status"] == "NOT_FETCHED"
    assert all(item["status"] in {"OK", "EMPTY", "ERROR", "NOT_FETCHED"}
               for item in product["requirements"])
    assert result["progress"]["completed_requirements"] == 1
    assert result["progress"]["total_declared_requirements"] == 2


def test_category_groups_keep_products_separate_and_disabled_maps_to_not_fetched(tmp_path):
    headline = requirement(requirement_id="headlines", product="headlines")
    full_text = requirement(requirement_id="full-text", product="full_text")
    configured_plan = CoveragePlan(
        (partition(),),
        {NEWS: SourceCapability(frozenset({"headlines", "full_text"}), frozenset({"ticker"}))},
        {},
    )
    path = make_store(tmp_path / "products.sqlite3")

    result = evaluate_snapshots(
        (headline, full_text), configured_plan, seal(tmp_path, [path]),
    )

    category = result["results"][0]
    products = {item["product"]: item for item in category["products"]}
    assert set(products) == {"headlines", "full_text"}
    assert products["headlines"]["complete"] is True
    assert products["full_text"]["complete"] is False
    assert products["full_text"]["requirements"][0]["status"] == "NOT_FETCHED"


def test_disabled_requirement_preserves_reason_under_public_not_fetched_status(tmp_path):
    path = tmp_path / "disabled.sqlite3"
    conn = sqlite3.connect(path)
    coverage.ensure_schema(conn)
    attempt = coverage.start_attempt(
        conn, logical_task_id="task", attempt_id="disabled", source_id=NEWS,
        product="headlines", subject_kind="ticker", subject_id="ABC", query_spec={},
        coverage_policy_version=POLICY, requested_from=START, requested_until=END,
    )
    coverage.record_coverage(
        conn, coverage_id="disabled-c", attempt_id=attempt.attempt_id,
        status="NOT_FETCHED", reason="DISABLED", recorded_at="2026-10-24T00:00:00Z",
    )
    conn.commit(); conn.close()

    result = evaluate_snapshots((requirement(),), plan(), seal(tmp_path, [path]))
    public_requirement = result["results"][0]["products"][0]["requirements"][0]
    assert public_requirement["status"] == "NOT_FETCHED"
    assert public_requirement["reason"] == "DISABLED"


def test_category_results_do_not_merge_different_tickers(tmp_path):
    first = requirement(
        requirement_id="peer-xyz", ticker="XYZ", category="peer_news",
        scope_kind="peer", scope_id="ABC",
    )
    second = requirement(
        requirement_id="peer-uvw", ticker="UVW", category="peer_news",
        scope_kind="peer", scope_id="ABC",
    )
    path = make_store(tmp_path / "peers.sqlite3")

    result = evaluate_snapshots((first, second), plan(), seal(tmp_path, [path]))

    assert len(result["results"]) == 2
    assert {item["ticker"] for item in result["results"]} == {"XYZ", "UVW"}
    assert all(item["complete"] for item in result["results"])
