"""WP-B B7a §3/§4.1: the projected lexical path is result-identical.

Every test runs ``retrieve_lexical`` twice on the same served DB, once
unprojected (the unchanged code path) and once through the verified
served-build projection, and compares the §3 identity fields (wall-clock
fields excluded).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from catalyst_data.retrieval import fts5
from catalyst_data.retrieval.fts5 import retrieve_lexical
from catalyst_data.retrieval.result import RetrievalContractError
from catalyst_data.retrieval.served_projection import build_served_projection
from catalyst_data.storage.connect import make_readonly_factory
from served_projection_fixtures import (
    BUILD,
    MANIFEST,
    chunk,
    make_served_db,
    old_build_chunks,
    rich_chunks,
)

CANDIDATE_FIELDS = (
    "chunk_id", "document_id", "available_at", "source_class", "content_text",
    "lexical_raw_score", "lexical_rank", "mode_served", "is_degraded",
    "fallback_reason", "temporal_center_date", "query_date",
    "query_date_conflict", "query_date_decision",
)
TRACE_FIELDS = (
    "manifest_row_count", "eligible_row_count", "matched_row_count",
    "candidate_count", "final_count", "mode_requested", "mode_served",
    "fallback_reason", "match_mode", "policy", "temporal_window_days",
    "temporal_center_date", "query_date", "query_date_conflict",
    "query_date_decision",
)


def semantic(result) -> dict:
    return {
        "candidates": [
            tuple(getattr(c, f) for f in CANDIDATE_FIELDS) for c in result.candidates
        ],
        "results": [c.chunk_id for c in result.results],
        "candidate_count": result.candidate_count,
        "mode_served": result.mode_served,
        "is_degraded": result.is_degraded,
        "fallback_reason": result.fallback_reason,
        "trace": {f: getattr(result.trace, f) for f in TRACE_FIELDS},
    }


@pytest.fixture()
def served(tmp_path: Path):
    db = make_served_db(tmp_path / "served.db", rich_chunks(), old_chunks=old_build_chunks())
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    handle = build_served_projection(
        make_readonly_factory(db), expected_manifest_id=MANIFEST, projection_dir=proj_dir
    )
    return db, handle


def _pair(db: Path, handle, query: str, **overrides):
    kwargs = dict(
        ticker="AAPL",
        cutoff="2026-08-15T20:00:00Z",
        requested_manifest_id=MANIFEST,
        top_k=8,
        candidate_depth=100,
        include_trace=True,
        session_date="2026-08-01",
    )
    kwargs.update(overrides)
    factory = make_readonly_factory(db)
    conn = factory()
    sidecar = handle.connect()
    try:
        old = retrieve_lexical(conn, query, **kwargs)
        new = retrieve_lexical(conn, query, served_projection=(handle, sidecar), **kwargs)
    finally:
        sidecar.close()
        conn.close()
    return old, new


QUERIES = [
    "AAPL",
    "AAPL earnings",
    "AAPL earnings guidance",
    "AAPL tariff dividend",
    "AAPL zzqxnonexistent",
    "AAPL buyback revenue earnings",
]


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize(
    "session_date,cutoff",
    [
        ("2026-08-01", "2026-08-15T20:00:00Z"),  # the dense centre: window 2
        ("2026-08-16", "2026-08-16T20:00:00Z"),  # nearest rows 4+ days away
        ("2026-06-20", "2026-09-30T20:00:00Z"),  # only the unbounded/45d windows
        ("2026-10-30", "2026-10-30T20:00:00Z"),  # unbounded window
    ],
)
def test_projected_equals_unprojected(served, query, session_date, cutoff):
    db, handle = served
    old, new = _pair(db, handle, query, session_date=session_date, cutoff=cutoff)
    assert semantic(new) == semantic(old)


def test_post_close_benzinga_update_filter_matches_projected_and_expanded_paths(tmp_path):
    benzinga_chunk = chunk(
        "bz-818-0001", "AAPL surged after lithium guidance", "2026-01-01T15:00:00Z",
        document_id="alpaca_news:818", source_class="reported_news", profile="news_v2",
        provider="alpaca", source_type="benzinga_news",
        provider_updated_at="2026-01-01T17:00:00Z",
    )
    db = make_served_db(tmp_path / "benzinga-served.db", [benzinga_chunk])
    proj_dir = tmp_path / "benzinga-proj"
    proj_dir.mkdir()
    handle = build_served_projection(
        make_readonly_factory(db), expected_manifest_id=MANIFEST, projection_dir=proj_dir
    )

    first_unprojected, first_projected = _pair(
        db, handle, "AAPL surged lithium", cutoff="2026-01-01T16:00:00Z",
        session_date="2026-01-01",
    )
    expanded_unprojected, expanded_projected = _pair(
        db, handle, "AAPL surged lithium", cutoff="2026-01-01T18:00:00Z",
        session_date="2026-01-01", allow_late_updates=True,
    )

    assert semantic(first_projected) == semantic(first_unprojected)
    assert all(hit.document_id != "alpaca_news:818" for hit in first_unprojected.results)
    assert any(hit.document_id == "alpaca_news:818" for hit in expanded_unprojected.results)
    assert semantic(expanded_projected) == semantic(expanded_unprojected)


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_classes": ("reported_news",)},
        {"source_classes": ("issuer_disclosure", "corporate_press_release")},
        {"evidence_types": ("news_v2",)},
        {"ticker": "MSFT"},
        {"ticker": "NFLX"},
        {"candidate_depth": 10, "top_k": 3},
        {"cutoff": "2026-07-01T00:00:00Z", "session_date": "2026-07-01"},
    ],
)
@pytest.mark.parametrize("query", ["AAPL", "AAPL earnings guidance", "MSFT tariff"])
def test_projected_equals_unprojected_filters(served, overrides, query):
    db, handle = served
    old, new = _pair(db, handle, query, **overrides)
    assert semantic(new) == semantic(old)


def test_matched_count_is_not_capped_by_depth(served):
    db, handle = served
    old, new = _pair(
        db, handle, "AAPL", candidate_depth=5, top_k=5,
        session_date="2026-10-30", cutoff="2026-10-30T20:00:00Z",
    )
    assert new.trace.matched_row_count > 5
    assert semantic(new) == semantic(old)


def test_old_build_rows_never_enter_the_match(served):
    """The OLD_BUILD FTS rows share chunk ids and match every term."""
    db, handle = served
    old, new = _pair(db, handle, "AAPL old build tariff")
    assert semantic(new) == semantic(old)
    assert all("old build" not in c.content_text for c in new.candidates)


def test_temporal_ties_order_identically(tmp_path):
    chunks = [
        chunk(f"tie-{i}", "AAPL tie text earnings", "2026-08-02T12:00:00Z",
              document_id=f"doc-{i % 3}", ordinal=f"{i:04d}")
        for i in (7, 3, 9, 1, 5)
    ] + [
        chunk(f"tie-b{i}", "AAPL tie text earnings", "2026-07-31T12:00:00Z",
              document_id=f"doc-b{i}")
        for i in (2, 8)
    ]
    db = make_served_db(tmp_path / "s.db", chunks)
    (tmp_path / "p").mkdir()
    handle = build_served_projection(
        make_readonly_factory(db), expected_manifest_id=MANIFEST, projection_dir=tmp_path / "p"
    )
    for query in ("AAPL", "AAPL earnings", "AAPL earnings tie"):
        old, new = _pair(db, handle, query)
        assert semantic(new) == semantic(old)


def test_window_loop_control_flow_is_frozen(tmp_path):
    """At window 2, AND is empty and OR is not; at window 7, AND is not empty.

    The frozen loop answers OR at window 2 (never AND at window 7).
    """
    chunks = [
        chunk("near-alpha", "AAPL alpha only text", "2026-08-02T12:00:00Z"),
        chunk("mid-both", "AAPL alpha beta text", "2026-08-06T12:00:00Z"),
    ]
    db = make_served_db(tmp_path / "s.db", chunks)
    (tmp_path / "p").mkdir()
    handle = build_served_projection(
        make_readonly_factory(db), expected_manifest_id=MANIFEST, projection_dir=tmp_path / "p"
    )
    old, new = _pair(db, handle, "AAPL alpha beta")
    assert (old.trace.match_mode, old.trace.temporal_window_days) == ("OR", 2)
    assert semantic(new) == semantic(old)


def test_phase_one_runs_once_per_match_expression(served, monkeypatch):
    db, handle = served
    calls: list[str] = []
    original = fts5._ProjectionRoute.hits_table

    def spy(self, match_expr):
        if match_expr not in self._hits:
            calls.append(match_expr)
        return original(self, match_expr)

    monkeypatch.setattr(fts5._ProjectionRoute, "hits_table", spy)
    # A two-term miss walks every window step with AND then OR.
    _pair(db, handle, "AAPL zzqx yyqz", session_date="2026-10-30",
          cutoff="2026-10-30T20:00:00Z")
    assert len(calls) == len(set(calls)) == 2


def test_temp_hits_are_dropped_after_the_call(served):
    db, handle = served
    conn = make_readonly_factory(db)()
    sidecar = handle.connect()
    try:
        retrieve_lexical(
            conn, "AAPL earnings guidance", ticker="AAPL",
            cutoff="2026-08-15T20:00:00Z", requested_manifest_id=MANIFEST,
            top_k=8, candidate_depth=100, session_date="2026-08-01",
            served_projection=(handle, sidecar),
        )
        leftover = sidecar.execute(
            "SELECT name FROM sqlite_temp_master WHERE type='table'"
        ).fetchall()
    finally:
        sidecar.close()
        conn.close()
    assert leftover == []


def test_rejects_projection_with_inactive_build_id(served):
    db, handle = served
    conn = make_readonly_factory(db)()
    sidecar = handle.connect()
    try:
        with pytest.raises(RetrievalContractError) as err:
            retrieve_lexical(
                conn, "AAPL", ticker="AAPL", cutoff="2026-08-15T20:00:00Z",
                requested_manifest_id=MANIFEST, top_k=8, candidate_depth=100,
                inactive_build_id=BUILD, served_projection=(handle, sidecar),
            )
    finally:
        sidecar.close()
        conn.close()
    assert err.value.code == "served_projection_with_inactive_build"


def test_stale_projection_fails_closed(served, tmp_path):
    db, handle = served
    rw = sqlite3.connect(db)
    rw.execute("UPDATE lexical_index_state SET lexical_generation_id = ?", ("e" * 64,))
    rw.commit()
    rw.close()
    conn = make_readonly_factory(db)()
    sidecar = handle.connect()
    try:
        with pytest.raises(RetrievalContractError) as err:
            retrieve_lexical(
                conn, "AAPL", ticker="AAPL", cutoff="2026-08-15T20:00:00Z",
                requested_manifest_id=MANIFEST, top_k=8, candidate_depth=100,
                served_projection=(handle, sidecar),
            )
    finally:
        sidecar.close()
        conn.close()
    assert err.value.code == "served_projection_stale"


def test_missing_served_content_fails_closed(served, monkeypatch):
    db, handle = served

    original = fts5._hydrate_projected_content
    monkeypatch.setattr(
        fts5,
        "_hydrate_projected_content",
        lambda conn, *, build_id, rows: original(conn, build_id="z" * 64, rows=rows),
    )
    conn = make_readonly_factory(db)()
    sidecar = handle.connect()
    try:
        with pytest.raises(RetrievalContractError) as err:
            retrieve_lexical(
                conn, "AAPL", ticker="AAPL", cutoff="2026-08-15T20:00:00Z",
                requested_manifest_id=MANIFEST, top_k=8, candidate_depth=100,
                session_date="2026-08-01", served_projection=(handle, sidecar),
            )
    finally:
        sidecar.close()
        conn.close()
    assert err.value.code == "served_projection_content_missing"


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE lexical_index_state SET mode_served = 'sql_like'",
        "UPDATE corpus_publication_builds SET status = 'retired' WHERE build_id = '"
        + BUILD
        + "'",
        "UPDATE corpus_publication_builds SET lexical_ready = 0 WHERE build_id = '"
        + BUILD
        + "'",
    ],
    ids=["non_fts_mode", "unpublished_build", "not_lexical_ready"],
)
def test_projection_on_a_non_served_state_fails_closed(served, mutation):
    db, handle = served
    rw = sqlite3.connect(db)
    rw.execute(mutation)
    rw.commit()
    rw.close()
    conn = make_readonly_factory(db)()
    sidecar = handle.connect()
    try:
        with pytest.raises(RetrievalContractError) as err:
            retrieve_lexical(
                conn, "AAPL earnings", ticker="AAPL", cutoff="2026-08-15T20:00:00Z",
                requested_manifest_id=MANIFEST, top_k=8, candidate_depth=100,
                session_date="2026-08-01", served_projection=(handle, sidecar),
            )
    finally:
        sidecar.close()
        conn.close()
    assert err.value.code == "served_projection_stale"


@pytest.mark.parametrize(
    "terms,mode",
    [
        (("earnings",), "and"),
        (("earnings", "guidance"), "and"),
        (("earnings", "guidance", "tariff"), "or"),
        (("old", "build"), "or"),
        (("zzqx",), "and"),
    ],
)
@pytest.mark.parametrize("window_days", [2, 7, None])
def test_bridge_oracle_matches_todays_served_fts_join(served, terms, mode, window_days):
    """§4.1 oracle: the bridge rows equal today's ``_fts_from_clause`` rows.

    Compares ``(chunk_id, document_id, available_at, source_class, score)``
    tuples, in order, and the COUNT, before content hydration.
    """
    from catalyst_data.retrieval.query_policy import compile_match

    db, handle = served
    match_expr = compile_match(terms, mode=mode)
    eligibility_sql, eligibility_params = fts5._eligibility_predicate(
        MANIFEST, "2026-08-15T20:00:00Z", "AAPL", None, None
    )
    window_sql, window_params = fts5._window_predicate("2026-08-01", window_days)
    order_sql = fts5._temporal_order_sql("2026-08-01")
    conn = make_readonly_factory(db)()
    sidecar = handle.connect()
    try:
        select_sql, count_sql, _ = fts5._fts_from_clause(
            chunks_relation="corpus_served_chunks",
            lexical_generation_id=BUILD,
            eligibility_sql=eligibility_sql,
            window_sql=window_sql,
            order_sql=order_sql,
        )
        today_rows, today_count = fts5._run_fts_match(
            conn,
            match_expr=match_expr,
            select_sql=select_sql,
            count_sql=count_sql,
            params_prefix=[BUILD],
            eligibility_params=eligibility_params,
            window_params=window_params,
            order_params=["2026-08-01"],
            candidate_depth=100,
        )
        route = fts5._ProjectionRoute(conn, handle, sidecar)
        try:
            bridge_rows, bridge_count = fts5._run_projected_match(
                route,
                match_expr=match_expr,
                lexical_generation_id=BUILD,
                eligibility_sql=eligibility_sql,
                eligibility_params=eligibility_params,
                window_sql=window_sql,
                window_params=window_params,
                order_sql=order_sql,
                order_params=["2026-08-01"],
                candidate_depth=100,
            )
        finally:
            route.close()
    finally:
        sidecar.close()
        conn.close()
    assert bridge_count == today_count
    assert [tuple(r[:5]) for r in bridge_rows] == [tuple(r[:5]) for r in today_rows]
    assert all(r[5] is None for r in bridge_rows)


def test_retriever_closes_the_sidecar_when_the_call_fails(served, monkeypatch):
    from catalyst_data.canonical.identity import DataRuntimeIdentity
    from catalyst_data.retrieval import lexical_retriever as lr
    from catalyst_data.retrieval.served_projection import ServedProjectionHandle

    db, handle = served
    closed: list[bool] = []
    real_connect = ServedProjectionHandle.connect

    class _Tracked:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            closed.append(True)
            self._conn.close()

    monkeypatch.setattr(ServedProjectionHandle, "connect", lambda self: _Tracked(real_connect(self)))

    def boom(*args, **kwargs):
        raise RuntimeError("retrieval failed")

    monkeypatch.setattr(lr, "retrieve_lexical", boom)
    runtime = DataRuntimeIdentity(
        data_snapshot_id="s" * 64, corpus_manifest_id=MANIFEST, fts_index_version="x",
        dense_index_version=None, embedding_model_revision=None,
        reranker_revision=None, query_policy_version="qp:v1",
    )
    retriever = lr.LexicalDisplayRetriever(
        db_conn_factory=make_readonly_factory(db),
        data_runtime_identity=runtime,
        served_projection=handle,
    )
    from datetime import datetime, timezone

    from catalyst_data.canonical.temporal import TemporalIdentity

    cutoff = datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc)
    identity = TemporalIdentity(
        session_date="2026-08-03", market_timezone="America/New_York",
        session_open_at=cutoff, session_close_at=cutoff,
        information_window_start_at=cutoff, cutoff_at=cutoff,
    )
    with pytest.raises(RuntimeError):
        retriever.retrieve_with_observation(
            "AAPL", ticker="AAPL", cutoff="2026-08-03T20:00:00Z",
            top_k=lr.DISPLAY_TOP_K, candidate_depth=lr.CANDIDATE_DEPTH_CAP,
            temporal_identity=identity,
        )
    assert closed == [True]
