from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import json
import sqlite3
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


EXPECTED_20_SESSIONS = (
    "2025-12-16", "2025-12-17", "2025-12-18", "2025-12-19", "2025-12-22",
    "2025-12-23", "2025-12-24", "2025-12-26", "2025-12-29", "2025-12-30",
    "2025-12-31", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07",
    "2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14",
)
EXPECTED_VALID_12_SESSION_IDS = (
    "2025-12-16", "2025-12-18", "2025-12-22", "2025-12-24",
    "2025-12-29", "2025-12-31", "2026-01-05", "2026-01-07",
    "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14",
)
VALID_12_VOLUMES = (
    1000.0, 1020.0, 1040.0, 1060.0, 1080.0, 1100.0,
    1120.0, 1140.0, 1160.0, 1180.0, 1200.0, 1220.0,
)


@dataclass(frozen=True)
class FixtureContextInputs:
    ticker: str
    session_date: date
    cutoff: str
    target_close: float | None
    previous_target_close: float | None
    target_volume: float | None
    expected_prior_sessions: tuple[str, ...]
    prior_volumes_by_session: Mapping[str, float | None]
    benchmark_ticker: str | None
    benchmark_return_pct: float | None
    sector_ticker: str | None
    sector_return_pct: float | None
    peer_returns_by_ticker: Mapping[str, float | None]


class FixtureContextProvider:
    def __init__(self, rows: Mapping[tuple[str, str, str], FixtureContextInputs]):
        self._rows = dict(rows)
        self.calls: list[tuple[str, str, str]] = []

    def load_context_inputs(self, *, ticker: str, session_date: str, cutoff: str) -> FixtureContextInputs:
        key = (ticker, session_date, cutoff)
        self.calls.append(key)
        if key not in self._rows:
            raise KeyError(key)
        row = self._rows[key]
        return FixtureContextInputs(
            ticker=row.ticker,
            session_date=row.session_date,
            cutoff=row.cutoff,
            target_close=row.target_close,
            previous_target_close=row.previous_target_close,
            target_volume=row.target_volume,
            expected_prior_sessions=tuple(row.expected_prior_sessions),
            prior_volumes_by_session=MappingProxyType(dict(row.prior_volumes_by_session)),
            benchmark_ticker=row.benchmark_ticker,
            benchmark_return_pct=row.benchmark_return_pct,
            sector_ticker=row.sector_ticker,
            sector_return_pct=row.sector_return_pct,
            peer_returns_by_ticker=MappingProxyType(dict(row.peer_returns_by_ticker)),
        )


class FixtureRetriever:
    def __init__(self, evidence=None, *, fail: bool = False):
        self.evidence = evidence if evidence is not None else fixture_evidence()
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, query: str, *, ticker: str, cutoff: str, requested_manifest_id: str, top_k: int = 8, candidate_depth: int = 20):
        self.calls.append({
            "query": query,
            "ticker": ticker,
            "cutoff": cutoff,
            "requested_manifest_id": requested_manifest_id,
            "top_k": top_k,
            "candidate_depth": candidate_depth,
        })
        if self.fail:
            raise RuntimeError("fixture retrieval failure")
        return tuple(self.evidence[:top_k])


@dataclass
class RecordingCutoffPolicy:
    cutoff: str = "2026-01-15T21:00:00Z"
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def compute_cutoff(self, *, ticker: str, session_date: str, mode: str) -> str:
        self.calls.append((ticker, session_date, mode))
        return self.cutoff


class StubModelClient:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[str] = []

    def invoke(self, prompt: str):
        self.calls.append(prompt)
        content = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return StubResponse(content)


class StubUsage:
    input_tokens = 100
    output_tokens = 40
    total_tokens = 140


class StubResponse:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = StubUsage()


def _base_inputs(**overrides: Any) -> FixtureContextInputs:
    prior = {session: 1000.0 + idx * 10 for idx, session in enumerate(EXPECTED_20_SESSIONS)}
    data = dict(
        ticker="AAPL",
        session_date=date(2026, 1, 15),
        cutoff="2026-01-15T21:00:00Z",
        target_close=150.0,
        previous_target_close=155.0,
        target_volume=1200.0,
        expected_prior_sessions=EXPECTED_20_SESSIONS,
        prior_volumes_by_session=prior,
        benchmark_ticker="SPY",
        benchmark_return_pct=-1.0,
        sector_ticker="XLK",
        sector_return_pct=-2.0,
        peer_returns_by_ticker={"MSFT": -2.5, "GOOGL": -1.5, "META": None},
    )
    data.update(overrides)
    return FixtureContextInputs(**data)


def mock_provider(**kwargs: Any) -> FixtureContextProvider:
    n_sessions = kwargs.pop("n_sessions", None)
    close_prices = kwargs.pop("close_prices", None)
    peer_data = kwargs.pop("peer_data", None)
    prior = dict(_base_inputs().prior_volumes_by_session)
    if n_sessions is not None:
        prior = {session: prior[session] for session in EXPECTED_20_SESSIONS[: int(n_sessions)]}
    overrides: dict[str, Any] = {}
    if close_prices:
        overrides["target_close"] = close_prices.get("2026-01-15")
        overrides["previous_target_close"] = close_prices.get("2026-01-14")
    if peer_data is not None:
        overrides["peer_returns_by_ticker"] = peer_data
    overrides["prior_volumes_by_session"] = prior
    overrides.update(kwargs)
    row = _base_inputs(**overrides)
    return FixtureContextProvider({("AAPL", "2026-01-15", row.cutoff): row})


def mock_provider_with_ohlcv() -> FixtureContextProvider:
    return mock_provider()


def volume_window_provider(*, expected_prior_sessions: tuple[str, ...], valid_prior_volumes: tuple[float, ...], older_volume: float | None = None, target_volume: float | None = 1200.0) -> FixtureContextProvider:
    prior: dict[str, float | None] = {session: None for session in expected_prior_sessions}
    for session, volume in zip(EXPECTED_VALID_12_SESSION_IDS, valid_prior_volumes, strict=True):
        prior[session] = volume
    if older_volume is not None:
        prior["2025-12-15"] = older_volume
    row = _base_inputs(expected_prior_sessions=expected_prior_sessions, prior_volumes_by_session=prior, target_volume=target_volume)
    return FixtureContextProvider({("AAPL", "2026-01-15", row.cutoff): row})


def fixture_evidence():
    from catalyst_agents.attribution.provider import RetrievedEvidence

    return (
        RetrievedEvidence(
            chunk_id="c1",
            document_id="d1",
            content_text="Issuer guidance was reduced before market close.",
            available_at="2026-01-15T18:00:00Z",
            source_class="issuer_disclosure",
            ticker_associations=("AAPL",),
            dedup_cluster_id="cluster-1",
            cluster_first_available_at="2026-01-15T18:00:00Z",
            representative_document_id="d1",
            is_novel=True,
            lexical_raw_score=12.0,
            lexical_rank=1,
            corpus_manifest_id="corpus-fixture-v1",
            index_manifest_id="index-fixture-v1",
            mode_requested="fts5",
            mode_served="fts5",
            is_degraded=False,
            fallback_reason=None,
        ),
        RetrievedEvidence(
            chunk_id="c2",
            document_id="d2",
            content_text="Reported news described demand weakness.",
            available_at="2026-01-15T19:00:00Z",
            source_class="reported_news",
            ticker_associations=("AAPL", "MSFT"),
            dedup_cluster_id="cluster-2",
            cluster_first_available_at="2026-01-15T19:00:00Z",
            representative_document_id="d2",
            is_novel=True,
            lexical_raw_score=10.0,
            lexical_rank=2,
            corpus_manifest_id="corpus-fixture-v1",
            index_manifest_id="index-fixture-v1",
            mode_requested="fts5",
            mode_served="fts5",
            is_degraded=False,
            fallback_reason=None,
        ),
    )


def make_hypothesis(**kwargs: Any):
    from catalyst_agents.attribution.hypothesis import Hypothesis

    data = dict(
        cause_label=kwargs.pop("cause", "market"),
        direction="negative",
        transmission_mechanism="Fixture mechanism",
        supporting_evidence=(),
        counter_evidence=(),
        supporting_evidence_ids=("c1",),
        counter_evidence_ids=(),
        missing_evidence=(),
        change_condition="Reassess on contrary evidence.",
        facts=(),
        calculations=(),
        inferences=(),
        unavailable_evidence=(),
        prerequisite_gate_passed=kwargs.pop("gate_passed", True),
        prerequisite_gate_reason="fixture",
        direct_support_exists=kwargs.pop("direct_support", False),
        independent_supporting_cluster_count=kwargs.pop("dedup_clusters", 1),
        max_supporting_critic_relevance=kwargs.pop("max_relevance", 0.8),
        source_support_degradation_count=kwargs.pop("degradation_flags", 0),
        max_counter_evidence_relevance=kwargs.pop("max_counter_relevance", 0.0),
        is_novel=kwargs.pop("is_novel", True),
        source_support_flags={},
        validation_violations=(),
    )
    data.update(kwargs)
    return Hypothesis(**data)


VALID_ASSURANCE_RECORD = {
    "schema_version": "1.0.0",
    "run_id": "run-001",
    "trace_id": "trace-001",
    "output_status": "SUFFICIENT",
    "cutoff": "2026-01-15T21:00:00Z",
    "corpus_manifest_id": "corpus-fixture-v1",
    "index_manifest_id": "index-fixture-v1",
    "model_ids": ["fixture-model"],
    "prompt_versions": ["critic:b5", "judge:b5"],
    "checks": [
        {"check_name": name, "status": "pass", "detail": "fixture", "checked_at": "2026-01-15T21:01:00Z"}
        for name in (
            "cutoff", "citation_resolution", "judge_visibility", "prerequisite_gates",
            "legal_path", "trace_completeness", "identities", "budget_retry_repair",
            "degraded_state", "structured_context_support",
        )
    ],
    "source_support_flags": {"opinion_only_support": False, "unknown_origin_support": False, "issuer_claim_only_support": False},
    "retry_count": 0,
    "repair_count": 0,
    "budget_exhausted": False,
    "is_degraded": False,
    "created_at": "2026-01-15T21:01:00Z",
}


def mock_run_artifacts(**overrides: Any) -> dict[str, Any]:
    data = {
        "run_id": "run-001",
        "trace_id": "trace-001",
        "output_status": "SUFFICIENT",
        "cutoff": "2026-01-15T21:00:00Z",
        "cutoff_observations": (
            "2026-01-15T21:00:00Z",
            "2026-01-15T21:00:00Z",
            "2026-01-15T21:00:00Z",
        ),
        "citations": ["c1"],
        "judge_visible_ids": ["c1", "c2"],
        "gate_results": [("earnings_guidance", True, "pass")],
        "legal_path_ok": True,
        "trace_complete": True,
        "corpus_manifest_id": "corpus-fixture-v1",
        "index_manifest_id": "index-fixture-v1",
        "retrieval_corpus_manifest_id": "corpus-fixture-v1",
        "retrieval_index_manifest_id": "index-fixture-v1",
        "model_ids": ["fixture-model"],
        "prompt_versions": ["critic:b5", "judge:b5"],
        "retry_count": 0,
        "repair_count": 0,
        "budget_exhausted": False,
        "is_degraded": False,
        "context_artifact": {"schema_version": "1.0.0"},
        "checked_at": "2026-01-15T21:01:00Z",
        "evidence": [{"chunk_id": "c1", "source_class": "issuer_disclosure"}],
    }
    data.update(overrides)
    return data


def create_temp_trace_db(tmp_path: Path | None = None, *, schema_version: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:" if tmp_path is None else tmp_path / "trace.db")
    if schema_version is not None:
        conn.execute("CREATE TABLE trace_schema_version (singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1), schema_version TEXT NOT NULL, applied_at TEXT NOT NULL)")
        conn.execute("INSERT INTO trace_schema_version VALUES (1, ?, '2026-01-15T00:00:00Z')", (schema_version,))
        conn.commit()
    return conn


def initialize_trace_schema(db: sqlite3.Connection) -> None:
    from catalyst_agents.trace.schema import init_trace_db

    init_trace_db(db)


def open_trace_reader(db: sqlite3.Connection) -> sqlite3.Connection:
    from catalyst_agents.trace.version import assert_supported_trace_schema

    assert_supported_trace_schema(db)
    return db


def relationship_manifest_fixture() -> dict[str, Any]:
    edges = [
        {
            "edge_id": "edge-aapl-msft-peer",
            "from_ticker": "AAPL",
            "to_ticker": "MSFT",
            "relationship_type": "peer",
            "effective_from": "2020-01-01",
            "effective_to": None,
            "review_source": "fixture-review",
            "reviewed_at": "2026-01-01T00:00:00Z",
        }
    ]
    payload = {"schema_version": "1.0.0", "edges": edges}
    import hashlib

    manifest_id = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"schema_version": "1.0.0", "manifest_id": manifest_id, "edges": edges}
