from __future__ import annotations

import sqlite3

import pytest
from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.coverage_readiness import (
    CoveragePlan,
    CoverageRequirement,
    PlannedPartition,
    SourceBinding,
    SourceCapability,
    evaluate_coverage,
    expected_partition_id,
    validate_requirements,
)

NEWS = "daily:finnhub"
START = "2026-10-23T13:30:00Z"
END = "2026-10-23T20:00:00Z"
POLICY = "coverage-v1"


def capability(products=("headlines", "full_text"), subjects=("ticker",)):
    return SourceCapability(frozenset(products), frozenset(subjects))


def requirement(**changes):
    values = dict(
        requirement_id="abc-target", session_date="2026-10-23", ticker="ABC",
        category="target_news", scope_kind="target", scope_id="ABC",
        subject_kind="ticker", subject_id="ABC",
        product="headlines", requested_from=START, requested_until=END,
        query_filters={}, source_ids=(NEWS,), source_policy="all",
    )
    return CoverageRequirement(**(values | changes))


def plan_partition(**changes):
    values = dict(
        source_id=NEWS, product="headlines", subject_kind="ticker", subject_id="ABC",
        requested_from=START, requested_until=END, query_filters={},
        coverage_policy_version=POLICY, session_ids=(),
    )
    values |= changes
    values["partition_id"] = "pending"
    part = PlannedPartition(**values)
    return PlannedPartition(**(values | {"partition_id": expected_partition_id(part)}))


def fixture(*, reqs=None, partitions=None, capabilities=None, bindings=None):
    conn = sqlite3.connect(":memory:")
    coverage.ensure_schema(conn)
    reqs = reqs or (requirement(),)
    partitions = partitions or (plan_partition(),)
    capabilities = capabilities or {NEWS: capability()}
    plan = CoveragePlan(tuple(partitions), capabilities, bindings or {})
    return conn, reqs, plan


def record(conn, *, subject_kind="ticker", subject_id="ABC", product="headlines",
           begin=START, end=END, filters=None, policy=POLICY, status="OK",
           coverage_begin=START, coverage_end=END, enumeration=True,
           attempt_id="a1", coverage_id="c1", expected_targets=None,
           acquired_targets=None, expected_count=None, acquired_count=None, source_id=NEWS):
    attempt = coverage.start_attempt(
        conn, logical_task_id=attempt_id, attempt_id=attempt_id, source_id=source_id,
        product=product, subject_kind=subject_kind, subject_id=subject_id,
        query_spec=filters or {}, coverage_policy_version=policy,
        requested_from=begin, requested_until=end,
    )
    coverage.record_coverage(
        conn, coverage_id=coverage_id, attempt_id=attempt.attempt_id, status=status,
        recorded_at="2026-10-24T00:00:00Z", reason="ERROR" if status == "ERROR" else None,
        coverage_from=coverage_begin, coverage_until=coverage_end,
        enumeration_complete=enumeration,
        expected_targets=expected_targets, acquired_targets=acquired_targets,
        expected_target_count=expected_count, acquired_target_count=acquired_count,
    )
    return attempt


def test_one_news_fact_satisfies_target_and_peer_requests_without_new_rows():
    conn = sqlite3.connect(":memory:")
    coverage.ensure_schema(conn)
    target = requirement()
    peer = requirement(requirement_id="xyz-peer", ticker="XYZ", category="peer_news",
                       scope_kind="peer", scope_id="ABC")
    plan = CoveragePlan((plan_partition(),), {NEWS: capability()}, {})
    record(conn)

    before = conn.execute("SELECT COUNT(*) FROM source_coverage_attempts").fetchone()[0]
    results = evaluate_coverage((target, peer), plan, coverage.latest_coverage(conn))

    assert [r["complete"] for r in results] == [True, True]
    assert results[0]["requirement_id"] == target.requirement_id
    assert "source_id" not in results[0]
    assert conn.execute("SELECT COUNT(*) FROM source_coverage_attempts").fetchone()[0] == before


def test_strict_subwindow_can_reuse_broader_plan_with_different_request_id():
    conn, _, plan = fixture(reqs=(requirement(requirement_id="new-id",
                                                requested_from="2026-10-23T14:00:00Z",
                                                requested_until="2026-10-23T15:00:00Z"),))
    record(conn)
    result = evaluate_coverage((requirement(requirement_id="new-id", requested_from="2026-10-23T14:00:00Z",
                                            requested_until="2026-10-23T15:00:00Z"),),
                              plan, coverage.latest_coverage(conn))[0]
    assert result["complete"] is True


def test_plan_child_intervals_union_but_gap_and_unplanned_parent_do_not_cover():
    middle = "2026-10-23T16:00:00Z"
    left = plan_partition(requested_until=middle)
    right = plan_partition(requested_from=middle)
    req = requirement()
    conn, _, plan = fixture(partitions=(left, right))
    record(conn, end=middle, coverage_end=middle, attempt_id="left", coverage_id="left-c")
    record(conn, begin=middle, coverage_begin=middle, attempt_id="right", coverage_id="right-c")
    assert evaluate_coverage((req,), plan, coverage.latest_coverage(conn))[0]["complete"]

    gap = "2026-10-23T16:01:00Z"
    gconn, _, gplan = fixture(partitions=(left, plan_partition(requested_from=gap)))
    record(gconn, end=middle, coverage_end=middle, attempt_id="left", coverage_id="left-c")
    record(gconn, begin=gap, coverage_begin=gap, attempt_id="right", coverage_id="right-c")
    assert not evaluate_coverage((req,), gplan, coverage.latest_coverage(gconn))[0]["complete"]

    pconn, _, pplan = fixture(partitions=(left, right))
    record(pconn, attempt_id="parent", coverage_id="parent-c")
    assert not evaluate_coverage((req,), pplan, coverage.latest_coverage(pconn))[0]["complete"]


def test_overlapping_failed_current_child_cannot_be_hidden_by_successful_child():
    broad = plan_partition()
    overlapping = plan_partition(query_filters={"region": "us"})
    conn, _, plan = fixture(partitions=(broad, overlapping))
    record(conn, attempt_id="broad", coverage_id="broad-c")
    record(conn, attempt_id="restricted", coverage_id="restricted-c", status="ERROR",
           filters={"region": "us"})
    result = evaluate_coverage((requirement(),), plan, coverage.latest_coverage(conn))[0]
    assert not result["complete"]


def test_overlapping_mixed_policy_partitions_are_rejected_before_evaluation():
    first = plan_partition(coverage_policy_version="policy-a")
    second = plan_partition(query_filters={"region": "us"}, coverage_policy_version="policy-b")
    plan = CoveragePlan((first, second), {NEWS: capability()}, {})

    with pytest.raises(ValueError, match="mixed coverage policy"):
        validate_requirements((requirement(),), plan)


def test_latest_failed_retry_and_no_event_latest_attempt_hide_previous_success():
    conn, reqs, plan = fixture()
    record(conn, attempt_id="ok", coverage_id="ok-c")
    record(conn, attempt_id="failed", coverage_id="failed-c", status="ERROR")
    snapshots = coverage.latest_coverage(conn)
    result = evaluate_coverage(reqs, plan, snapshots)[0]
    assert not result["complete"] and result["status"] == "ERROR"

    conn2, reqs2, plan2 = fixture()
    record(conn2, attempt_id="ok", coverage_id="ok-c")
    coverage.start_attempt(conn2, logical_task_id="pending", attempt_id="pending",
                           source_id=NEWS, product="headlines", subject_kind="ticker",
                           subject_id="ABC", query_spec={}, coverage_policy_version=POLICY,
                           requested_from=START, requested_until=END)
    pending = evaluate_coverage(reqs2, plan2, coverage.latest_coverage(conn2))[0]
    assert not pending["complete"] and pending["status"] == "PENDING"

    conn3, reqs3, plan3 = fixture()
    attempt = record(conn3, attempt_id="progress", coverage_id="progress-ok")
    coverage.record_coverage(conn3, coverage_id="progress-disabled", attempt_id=attempt.attempt_id,
                             status="NOT_FETCHED", reason="DISABLED",
                             recorded_at="2026-10-24T00:01:00Z")
    disabled = evaluate_coverage(reqs3, plan3, coverage.latest_coverage(conn3))[0]
    assert not disabled["complete"] and disabled["status"] == "DISABLED"


def test_any_positive_wins_but_empty_requires_every_alternative_complete():
    other = "daily:finnhub+polygon"
    req = requirement(source_ids=(NEWS, other), source_policy="any")
    caps = {NEWS: capability(), other: capability()}
    parts = (plan_partition(), plan_partition(source_id=other))
    conn, _, plan = fixture(reqs=(req,), partitions=parts, capabilities=caps)
    record(conn)
    result = evaluate_coverage((req,), plan, coverage.latest_coverage(conn))[0]
    assert result["status"] == "OK" and result["complete"]

    empty_conn, _, empty_plan = fixture(reqs=(req,), partitions=parts, capabilities=caps)
    record(empty_conn, status="EMPTY")
    result = evaluate_coverage((req,), empty_plan, coverage.latest_coverage(empty_conn))[0]
    assert result["status"] == "EMPTY" and not result["complete"]

    record(empty_conn, subject_id="ABC", attempt_id="other", coverage_id="other-c", status="EMPTY",
           source_id=other)
    result = evaluate_coverage((req,), empty_plan, coverage.latest_coverage(empty_conn))[0]
    assert result["status"] == "EMPTY" and result["complete"]


def test_registration_rejects_missing_unknown_and_impossible_capabilities():
    with pytest.raises(ValueError, match="capabilit"):
        CoveragePlan((plan_partition(),), {}, {})
    with pytest.raises(ValueError, match="capabilit"):
        CoveragePlan((plan_partition(source_id="not-a-registry-source"),),
                     {"not-a-registry-source": capability()}, {})
    with pytest.raises(ValueError, match="bars"):
        CoveragePlan((plan_partition(product="bars"),), {NEWS: capability()}, {})
    empty_plan = CoveragePlan((), {}, {})
    conn = sqlite3.connect(":memory:"); coverage.ensure_schema(conn)
    with pytest.raises(ValueError, match="capability"):
        validate_requirements((requirement(),), empty_plan)
    assert conn.execute("SELECT COUNT(*) FROM source_coverage_attempts").fetchone()[0] == 0


def test_headlines_do_not_prove_full_text_and_full_text_requires_acquired_memberships():
    req = requirement(product="full_text")
    part = plan_partition(product="full_text")
    conn, _, plan = fixture(reqs=(req,), partitions=(part,),
                            capabilities={NEWS: capability(("headlines", "full_text"))})
    record(conn, product="full_text", expected_targets=("doc-1",), acquired_targets=(),
           expected_count=1, acquired_count=0)
    result = evaluate_coverage((req,), plan, coverage.latest_coverage(conn))[0]
    assert not result["complete"]
    assert result["status"] == "OK"

    forged = coverage.CoverageSnapshot(
        attempt=coverage.latest_coverage(conn)[0].attempt,
        fact=coverage.latest_coverage(conn)[0].fact,
        targets=(),
    )
    assert not evaluate_coverage((req,), plan, (forged,))[0]["complete"]
    acquired_conn = sqlite3.connect(":memory:"); coverage.ensure_schema(acquired_conn)
    record(acquired_conn, product="full_text", expected_targets=("doc-1",), acquired_targets=("doc-1",),
           expected_count=1, acquired_count=1)
    assert evaluate_coverage((req,), plan, coverage.latest_coverage(acquired_conn))[0]["complete"]
    zero_ok_conn = sqlite3.connect(":memory:"); coverage.ensure_schema(zero_ok_conn)
    record(zero_ok_conn, product="full_text", expected_targets=(), acquired_targets=(),
           expected_count=0, acquired_count=0)
    assert not evaluate_coverage((req,), plan, coverage.latest_coverage(zero_ok_conn))[0]["complete"]
    empty_conn = sqlite3.connect(":memory:"); coverage.ensure_schema(empty_conn)
    record(empty_conn, product="full_text", status="EMPTY", expected_targets=(), acquired_targets=(),
           expected_count=0, acquired_count=0)
    empty_result = evaluate_coverage((req,), plan, coverage.latest_coverage(empty_conn))[0]
    assert empty_result["complete"] and empty_result["status"] == "EMPTY"
    headline_req = requirement(product="headlines")
    headline_plan = CoveragePlan((plan_partition(product="headlines"),),
                                 {NEWS: capability()}, {})
    assert not evaluate_coverage((headline_req,), headline_plan,
                                 coverage.latest_coverage(conn))[0]["complete"]
    with pytest.raises(coverage.CoverageError, match="explicit target identities"):
        coverage.record_coverage(conn, coverage_id="counts-only", attempt_id=conn.execute(
            "SELECT attempt_id FROM source_coverage_attempts").fetchone()[0], status="OK",
            recorded_at="2026-10-24T00:02:00Z", expected_target_count=1, acquired_target_count=1)


def test_bar_session_sets_and_feed_binding_are_typed_and_explicit():
    bars_source = "daily:price_bars"
    req = requirement(category="price_bars", product="bars", scope_id="ESM6",
                      subject_kind="instrument", subject_id="ESM6",
                      source_ids=(bars_source,),
                      requested_from=START, requested_until=END, session_ids=("2026-10-23",))
    p = plan_partition(source_id=bars_source, product="bars", subject_kind="instrument",
                       subject_id="ESM6", session_ids=("2026-10-23",),
                       query_filters={"session_ids": ["2026-10-23"]})
    bars_plan = CoveragePlan((p,), {bars_source: capability(("bars",), ("instrument",))}, {})
    bars_conn = sqlite3.connect(":memory:"); coverage.ensure_schema(bars_conn)
    record(bars_conn, source_id=bars_source, product="bars", subject_kind="instrument", subject_id="ESM6",
           filters={"session_ids": ["2026-10-23"]})
    peer_req = requirement(requirement_id="peer-instrument-bars", ticker="XYZ", category="price_bars",
                           scope_kind="peer", scope_id="ESM6", subject_kind="instrument",
                           subject_id="ESM6", source_ids=(bars_source,), product="bars",
                           session_ids=("2026-10-23",))
    before = bars_conn.execute("SELECT COUNT(*) FROM source_coverage_attempts").fetchone()[0]
    results = evaluate_coverage((req, peer_req), bars_plan, coverage.latest_coverage(bars_conn))
    assert [item["complete"] for item in results] == [True, True]
    assert bars_conn.execute("SELECT COUNT(*) FROM source_coverage_attempts").fetchone()[0] == before
    unanchored = plan_partition(source_id=bars_source, product="bars", subject_kind="instrument",
                                subject_id="ESM6", session_ids=("2026-10-23",))
    unanchored_plan = CoveragePlan((unanchored,), {bars_source: capability(("bars",), ("instrument",))}, {})
    no_session_conn = sqlite3.connect(":memory:"); coverage.ensure_schema(no_session_conn)
    record(no_session_conn, source_id=bars_source, product="bars", subject_kind="instrument", subject_id="ESM6")
    assert not evaluate_coverage((req,), unanchored_plan,
                                coverage.latest_coverage(no_session_conn))[0]["complete"]
    wrong_session_req = requirement(category="price_bars", product="bars", scope_id="ESM6",
                                    subject_kind="instrument", subject_id="ESM6",
                                    source_ids=(bars_source,),
                                    session_ids=("2026-10-24",))
    assert not evaluate_coverage((wrong_session_req,), bars_plan,
                                 coverage.latest_coverage(bars_conn))[0]["complete"]
    feed_plan = CoveragePlan((plan_partition(source_id="daily:kitco", product="headlines",
                                             subject_kind="feed", subject_id="kitco"),),
                             {"daily:kitco": capability(("headlines",), ("ticker", "feed"))}, {})
    ticker_req = requirement(source_ids=("daily:kitco",), category="supplementary_news")
    assert not evaluate_coverage((ticker_req,), feed_plan, ())[0]["complete"]
    feed_req = requirement(source_ids=("daily:kitco",), category="supplementary_news",
                           subject_kind="feed", subject_id="kitco")
    feed_conn = sqlite3.connect(":memory:"); coverage.ensure_schema(feed_conn)
    record(feed_conn, source_id="daily:kitco", subject_kind="feed", subject_id="kitco")
    assert evaluate_coverage((feed_req,), feed_plan,
                             coverage.latest_coverage(feed_conn))[0]["complete"]
    conflicting = CoveragePlan(feed_plan.partitions,
                               feed_plan.capabilities, {"daily:kitco": SourceBinding("other-feed")})
    with pytest.raises(ValueError, match="binding"):
        validate_requirements((feed_req,), conflicting)


def test_adjacent_bars_children_union_required_sessions_and_missing_session_fails():
    source = "daily:price_bars"
    midpoint = "2026-10-24T00:00:00Z"
    final = "2026-10-25T00:00:00Z"
    day_one = plan_partition(source_id=source, product="bars", subject_kind="instrument",
                             subject_id="ESM6", requested_from=START, requested_until=midpoint,
                             session_ids=("2026-10-23",),
                             query_filters={"session_ids": ["2026-10-23"]})
    day_two = plan_partition(source_id=source, product="bars", subject_kind="instrument",
                             subject_id="ESM6", requested_from=midpoint, requested_until=final,
                             session_ids=("2026-10-24",),
                             query_filters={"session_ids": ["2026-10-24"]})
    req = requirement(category="price_bars", product="bars", scope_id="ESM6",
                      subject_kind="instrument", subject_id="ESM6", source_ids=(source,),
                      requested_from=START, requested_until=final,
                      session_ids=("2026-10-23", "2026-10-24"))
    plan = CoveragePlan((day_one, day_two), {source: capability(("bars",), ("instrument",))}, {})
    conn = sqlite3.connect(":memory:"); coverage.ensure_schema(conn)
    record(conn, source_id=source, product="bars", subject_kind="instrument", subject_id="ESM6",
           begin=START, end=midpoint, coverage_begin=START, coverage_end=midpoint,
           filters={"session_ids": ["2026-10-23"]}, attempt_id="day-one", coverage_id="day-one-c")
    record(conn, source_id=source, product="bars", subject_kind="instrument", subject_id="ESM6",
           begin=midpoint, end=final, coverage_begin=midpoint, coverage_end=final,
           filters={"session_ids": ["2026-10-24"]}, attempt_id="day-two", coverage_id="day-two-c")
    assert evaluate_coverage((req,), plan, coverage.latest_coverage(conn))[0]["complete"]

    incomplete_conn = sqlite3.connect(":memory:"); coverage.ensure_schema(incomplete_conn)
    record(incomplete_conn, source_id=source, product="bars", subject_kind="instrument", subject_id="ESM6",
           begin=START, end=midpoint, coverage_begin=START, coverage_end=midpoint,
           filters={"session_ids": ["2026-10-23"]}, attempt_id="day-one", coverage_id="day-one-c")
    result = evaluate_coverage((req,), plan, coverage.latest_coverage(incomplete_conn))[0]
    assert not result["complete"]


def test_filters_require_collection_superset_and_unknown_operators_fail_closed():
    req = requirement(query_filters={"language": "en", "regions": ["us", "ca"]})
    plan = CoveragePlan((plan_partition(query_filters={"regions": ["ca", "us", "mx"]}),),
                        {NEWS: capability()}, {})
    conn = sqlite3.connect(":memory:"); coverage.ensure_schema(conn)
    record(conn, filters={"regions": ["ca", "us", "mx"]})
    assert evaluate_coverage((req,), plan, coverage.latest_coverage(conn))[0]["complete"]
    restrictive_part = plan_partition(query_filters={"regions": ["us"]})
    restrictive = CoveragePlan((restrictive_part,), {NEWS: capability()}, {})
    restrictive_conn = sqlite3.connect(":memory:"); coverage.ensure_schema(restrictive_conn)
    record(restrictive_conn, filters={"regions": ["us"]})
    assert not evaluate_coverage((req,), restrictive,
                                 coverage.latest_coverage(restrictive_conn))[0]["complete"]
    # A collection restriction absent from the request makes the request broader.
    broad_req = requirement(query_filters={})
    assert not evaluate_coverage((broad_req,), restrictive,
                                 coverage.latest_coverage(restrictive_conn))[0]["complete"]


def test_session_restricted_headlines_do_not_prove_unfiltered_multi_day_request():
    part = plan_partition(requested_from="2026-10-22T00:00:00Z",
                          requested_until="2026-10-24T00:00:00Z",
                          query_filters={"session_date": "2026-10-23"})
    plan = CoveragePlan((part,), {NEWS: capability()}, {})
    conn = sqlite3.connect(":memory:"); coverage.ensure_schema(conn)
    record(conn, begin="2026-10-22T00:00:00Z", end="2026-10-24T00:00:00Z",
           coverage_begin="2026-10-22T00:00:00Z", coverage_end="2026-10-24T00:00:00Z",
           filters={"session_date": "2026-10-23"})
    req = requirement(requested_from="2026-10-22T00:00:00Z",
                      requested_until="2026-10-24T00:00:00Z")
    assert not evaluate_coverage((req,), plan, coverage.latest_coverage(conn))[0]["complete"]
    with pytest.raises(ValueError, match="filter"):
        requirement(query_filters={"$gte": 3})


def test_irrelevant_unstarted_partition_outside_narrow_request_does_not_block():
    request_start = "2026-10-23T14:00:00Z"
    request_end = "2026-10-23T15:00:00Z"
    relevant = plan_partition()
    unrelated = plan_partition(requested_from="2026-10-23T16:00:00Z",
                               requested_until="2026-10-23T17:00:00Z")
    conn, _, plan = fixture(partitions=(relevant, unrelated))
    record(conn)
    coverage.start_attempt(conn, logical_task_id="later", attempt_id="later", source_id=NEWS,
                           product="headlines", subject_kind="ticker", subject_id="ABC",
                           query_spec={}, coverage_policy_version=POLICY,
                           requested_from=unrelated.requested_from,
                           requested_until=unrelated.requested_until)
    req = requirement(requested_from=request_start, requested_until=request_end)
    assert evaluate_coverage((req,), plan, coverage.latest_coverage(conn))[0]["complete"]


def test_duplicate_conflicting_plan_and_snapshot_identities_fail_closed():
    p = plan_partition()
    with pytest.raises(ValueError, match="duplicate"):
        CoveragePlan((p, p), {NEWS: capability()}, {})
    conn, reqs, plan = fixture(); record(conn)
    snapshot = coverage.latest_coverage(conn)[0]
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_coverage(reqs, plan, (snapshot, snapshot))


def test_requirement_and_plan_snapshot_mutable_configuration_at_registration():
    filters = {"regions": ["us", "ca"]}
    req = requirement(query_filters=filters)
    filters["regions"].append("mx")
    assert set(req.query_filters["regions"]) == {"us", "ca"}

    cap_products = {"headlines"}
    cap_subjects = {"ticker"}
    caps = {NEWS: SourceCapability(cap_products, cap_subjects)}
    part = plan_partition()
    plan = CoveragePlan((part,), caps, {})
    cap_products.add("bars")
    caps.clear()
    assert plan.capabilities[NEWS].products == frozenset({"headlines"})
    assert NEWS in plan.capabilities
    with pytest.raises(TypeError):
        plan.capabilities[NEWS] = capability()
