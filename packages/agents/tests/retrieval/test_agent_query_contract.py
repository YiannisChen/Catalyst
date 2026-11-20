"""WP-B B3: agent query, filters, limits and observation (Frozen-A3 §2.1).

These tests pin the three B3 behaviours that change the production path:

* the default query builder never leaks the evidence-need label;
* ``source_classes`` and the ``time_scope`` -> temporal-centre mapping are
  threaded from the executor to the retriever;
* the depth/top-k defaults are deleted everywhere on the agent path, so every
  call site must pass the data-core frozen constants explicitly.
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone

import pytest
from catalyst_agents.retrieval.execution import (
    ResearchExecutor,
    ResearchRetriever,
    _default_query,
)
from catalyst_agents.retrieval.task import (
    EvidenceNeed,
    ResearchTask,
    ScenarioType,
    TimeScope,
)
from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import SourceClass
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval.lexical_display import (
    CANDIDATE_DEPTH_CAP,
    DISPLAY_TOP_K,
)
from catalyst_data.trading_calendar import latest_closed_trading_day_for_date


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _temporal(session_date: str = "2026-01-15") -> TemporalIdentity:
    return TemporalIdentity(
        session_date=session_date,
        market_timezone="America/New_York",
        session_open_at=_utc(f"{session_date}T14:30:00Z"),
        session_close_at=_utc(f"{session_date}T21:00:00Z"),
        information_window_start_at=_utc("2026-01-14T21:00:00Z"),
        cutoff_at=_utc(f"{session_date}T21:00:00Z"),
    )


def _runtime() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id="m" * 64,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _task(
    *,
    need: EvidenceNeed = EvidenceNeed.COMPANY_PRIMARY,
    time_scope: TimeScope = TimeScope.SESSION_INFORMATION_WINDOW,
    source_classes: tuple[SourceClass, ...] = (),
    query_hints: tuple[str, ...] = (),
    lookback_sessions: int | None = None,
    priority: int = 0,
    fingerprint: str = "a" * 64,
) -> ResearchTask:
    return ResearchTask(
        schema_version="research_task_v1",
        task_id=f"research:1:{priority}:{fingerprint[:12]}",
        round=1,
        priority=priority,
        scenario=ScenarioType.COMPANY_SPECIFIC,
        evidence_need=need,
        time_scope=time_scope,
        lookback_sessions=lookback_sessions,
        ticker_scope=(),
        source_classes=source_classes,
        evidence_types=(),
        query_hints=query_hints,
        retrieval_policy_id="qp:v1",
        task_fingerprint=fingerprint,
    )


class RecordingRetriever:
    """Retriever that records the exact agent-side call it received."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def retrieve(
        self,
        query,
        *,
        ticker,
        cutoff,
        requested_manifest_id,
        temporal_identity=None,
        top_k,
        candidate_depth,
        source_classes=None,
        temporal_centre_date=None,
    ):
        self.calls.append(
            {
                "query": query,
                "ticker": ticker,
                "top_k": top_k,
                "candidate_depth": candidate_depth,
                "source_classes": source_classes,
                "temporal_centre_date": temporal_centre_date,
            }
        )
        return ()


def _run(executor: ResearchExecutor, tasks: tuple[ResearchTask, ...]):
    return executor.execute(
        tasks=tasks,
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        research_policy_version="sp:v1",
        ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="m" * 64,
    )


def _executor(retriever) -> ResearchExecutor:
    return ResearchExecutor(retriever=retriever, concurrency=1, stage_timeout_seconds=5.0)


# --------------------------------------------------------------------------- #
# 1. The default query builder
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("need", list(EvidenceNeed))
def test_default_query_never_leaks_the_evidence_need_label(need: EvidenceNeed) -> None:
    query = _default_query(_task(need=need), "AAPL")
    label = need.value.lower().replace("_", " ")
    assert "AAPL" in query
    assert label not in query.lower()
    assert "company" not in query.lower()


def test_default_query_is_ticker_plus_sanitized_hints() -> None:
    task = _task(need=EvidenceNeed.COMPANY_NEWS, query_hints=("guidance cut",))
    assert _default_query(task, "AAPL") == "AAPL guidance cut"


def test_default_query_with_no_hints_is_the_ticker_only() -> None:
    assert _default_query(_task(), "AAPL") == "AAPL"


def test_default_query_is_empty_when_ticker_is_empty() -> None:
    # R3: an empty query string short-circuits to match_mode="none".
    assert _default_query(_task(), "") == ""


def test_default_query_sanitizes_unsafe_hints_out() -> None:
    task = _task(
        need=EvidenceNeed.COMPANY_NEWS,
        query_hints=("https://evil.example/x",),
    )
    assert _default_query(task, "AAPL") == "AAPL"


# --------------------------------------------------------------------------- #
# 2. source_classes threading (R11)
# --------------------------------------------------------------------------- #
def test_executor_passes_none_when_task_has_no_source_classes() -> None:
    retriever = RecordingRetriever()
    _run(_executor(retriever), (_task(source_classes=()),))
    assert retriever.calls[0]["source_classes"] is None


def test_executor_passes_source_class_values_when_present() -> None:
    retriever = RecordingRetriever()
    classes = (
        SourceClass.ISSUER_DISCLOSURE,
        SourceClass.CORPORATE_PRESS_RELEASE,
    )
    _run(_executor(retriever), (_task(source_classes=classes),))
    assert retriever.calls[0]["source_classes"] == (
        "issuer_disclosure",
        "corporate_press_release",
    )


# --------------------------------------------------------------------------- #
# 3. time_scope -> temporal centre (R13)
# --------------------------------------------------------------------------- #
def test_temporal_centre_session_window_is_the_asked_session() -> None:
    retriever = RecordingRetriever()
    _run(
        _executor(retriever),
        (_task(time_scope=TimeScope.SESSION_INFORMATION_WINDOW),),
    )
    assert retriever.calls[0]["temporal_centre_date"] == "2026-01-15"


def test_temporal_centre_prior_session_is_the_latest_closed_trading_day() -> None:
    retriever = RecordingRetriever()
    _run(_executor(retriever), (_task(time_scope=TimeScope.PRIOR_SESSION),))
    expected = latest_closed_trading_day_for_date(
        date(2026, 1, 15) - timedelta(days=1)
    ).isoformat()
    assert retriever.calls[0]["temporal_centre_date"] == expected
    assert expected == "2026-01-14"


def test_temporal_centre_lookback_sessions_records_the_asked_session() -> None:
    retriever = RecordingRetriever()
    _run(
        _executor(retriever),
        (
            _task(
                time_scope=TimeScope.LOOKBACK_SESSIONS,
                lookback_sessions=3,
            ),
        ),
    )
    assert retriever.calls[0]["temporal_centre_date"] == "2026-01-15"


# --------------------------------------------------------------------------- #
# 4. depth / top-k defaults are deleted on the agent path (R12)
# --------------------------------------------------------------------------- #
def test_executor_passes_the_frozen_data_core_limits() -> None:
    retriever = RecordingRetriever()
    _run(_executor(retriever), (_task(),))
    assert retriever.calls[0]["top_k"] == DISPLAY_TOP_K
    assert retriever.calls[0]["candidate_depth"] == CANDIDATE_DEPTH_CAP


def test_research_retriever_protocol_has_no_depth_defaults() -> None:
    params = inspect.signature(ResearchRetriever.retrieve).parameters
    assert params["top_k"].default is inspect.Parameter.empty
    assert params["candidate_depth"].default is inspect.Parameter.empty
    assert params["source_classes"].default is None
    assert params["temporal_centre_date"].default is None


def test_adapter_methods_have_no_depth_defaults() -> None:
    for name in ("retrieve", "retrieve_with_observations"):
        params = inspect.signature(
            getattr(AgentRetrieverAdapter, name)
        ).parameters
        assert params["top_k"].default is inspect.Parameter.empty, name
        assert params["candidate_depth"].default is inspect.Parameter.empty, name
        assert params["source_classes"].default is None, name
        assert params["temporal_centre_date"].default is None, name


def test_adapter_retrieve_requires_explicit_limits() -> None:
    class _Retriever:
        def retrieve(self, query, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("retrieve must not be called without limits")

    adapter = AgentRetrieverAdapter(_Retriever())
    with pytest.raises(TypeError):
        adapter.retrieve(
            "AAPL",
            ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z",
            requested_manifest_id="m" * 64,
            temporal_identity=_temporal(),
        )
