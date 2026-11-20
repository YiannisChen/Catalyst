"""WP-B B4 — runtime wiring, health and the retriever protocol (Frozen-A3 §2.1/§2.2/§2.5).

B4 replaces the hybrid runtime with the single V1.1 lexical path:

* ``AgentRetrieverAdapter`` is typed to a ``RetrieverProtocol`` (``retrieve`` /
  ``retrieve_with_observation``) and only attributes "the reranker did not
  serve" to a retriever whose observation declares a rerank stage. The lexical
  retriever never returns ``result is None``; any lexical degradation still
  fails closed;
* the loader requires ``retrieval_profile`` (``lexical_display_v1`` only),
  resolves no LanceDB/embedding/reranker, keeps ``index_manifest_id`` optional,
  binds the served FTS digest against an operator-supplied expected digest, and
  builds only ``AgentRetrieverAdapter(LexicalDisplayRetriever(...))``;
* the runtime identity nulls dense/embedding/reranker revisions.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
from catalyst_agents.runtime.retrieval_adapter import (
    AgentRetrieverAdapter,
    RetrievalDegradedError,
    RetrieverProtocol,
)
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import SourceClass, source_role_for
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval.lexical_display import RETRIEVAL_PROFILE
from catalyst_data.retrieval.lexical_retriever import (
    LexicalDisplayObservation,
    ObservedLexicalDisplay,
)
from catalyst_data.retrieval.v1_result import (
    RetrievalHit,
    RetrievalResultSet,
    StageRank,
    StageScore,
    material_capability_for_content_state,
)

# WP-B B7a: the minimal served DB cannot build a real projection.
pytestmark = pytest.mark.usefixtures("stub_served_projection")

MANIFEST = "a" * 64
BUILD = "b" * 64
SNAPSHOT = "s" * 64
SOURCE_BUNDLE = "c" * 64
PROBE = "p" * 64
POSTBUILD = "q" * 64
LIVE_DIGEST = "fts-digest-live"

PROFILE_ENV = "CATALYST_RETRIEVAL_PROFILE"
EXPECTED_DIGEST_ENV = "CATALYST_EXPECTED_FTS_DIGEST"


def _served_db(
    tmp_path: Path,
    *,
    digest: str = LIVE_DIGEST,
    row_count: int = 5,
    build_status: str = "published",
    lexical_ready: int = 1,
) -> Path:
    """A minimal served-build DB: the three tables ``resolve_served_build`` reads."""
    path = tmp_path / "runtime.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE corpus_manifest (
            manifest_id TEXT PRIMARY KEY, is_current INTEGER NOT NULL
        );
        CREATE TABLE corpus_publication_builds (
            build_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL, status TEXT NOT NULL,
            lexical_ready INTEGER NOT NULL, lexical_digest TEXT, lexical_row_count INTEGER
        );
        CREATE TABLE lexical_index_state (
            singleton_id INTEGER PRIMARY KEY, corpus_manifest_id TEXT,
            lexical_generation_id TEXT
        );
        """
    )
    conn.execute("INSERT INTO corpus_manifest VALUES (?, 1)", (MANIFEST,))
    conn.execute(
        "INSERT INTO corpus_publication_builds VALUES (?,?,?,?,?,?)",
        (BUILD, MANIFEST, build_status, lexical_ready, digest, row_count),
    )
    conn.execute(
        "INSERT INTO lexical_index_state VALUES (1,?,?)", (MANIFEST, BUILD)
    )
    conn.commit()
    conn.close()
    return path


def _identity_env(
    monkeypatch,
    *,
    digest: str = LIVE_DIGEST,
    profile: str = RETRIEVAL_PROFILE,
    snapshot: str = SNAPSHOT,
    probe: str = PROBE,
    postbuild: str = POSTBUILD,
    source_bundle: str = SOURCE_BUNDLE,
) -> None:
    monkeypatch.setenv("CATALYST_CORPUS_MANIFEST_ID", MANIFEST)
    monkeypatch.setenv("CATALYST_SNAPSHOT_ID", snapshot)
    monkeypatch.setenv("CATALYST_SOURCE_BUNDLE_ID", source_bundle)
    monkeypatch.setenv("CATALYST_PROBE_REPORT_ID", probe)
    monkeypatch.setenv("CATALYST_POSTBUILD_READINESS_ID", postbuild)
    monkeypatch.setenv(EXPECTED_DIGEST_ENV, digest)
    monkeypatch.setenv(PROFILE_ENV, profile)
    # LanceDB is never required by the V1.1 loader.
    monkeypatch.delenv("CATALYST_LANCEDB_DIR", raising=False)
    monkeypatch.delenv("CATALYST_INDEX_MANIFEST_ID", raising=False)


def _bound_loader(monkeypatch, tmp_path: Path, **kwargs) -> RuntimeDependencyLoader:
    db_path = _served_db(tmp_path)
    return RuntimeDependencyLoader(
        sqlite_db_path=db_path,
        require_identity_bound_runtime=True,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 1. retrieval_profile is required and frozen
# --------------------------------------------------------------------------- #
def test_loader_requires_a_retrieval_profile(tmp_path, monkeypatch):
    db_path = _served_db(tmp_path)
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    loader = RuntimeDependencyLoader(
        sqlite_db_path=db_path, require_identity_bound_runtime=True
    )
    health = loader.health()
    assert health["status"] == "failed"
    assert health["retrieval"]["status"] == "failed"
    assert "retrieval_profile" in health["retrieval"]["message"]


def test_loader_rejects_an_unknown_retrieval_profile(tmp_path, monkeypatch):
    db_path = _served_db(tmp_path)
    monkeypatch.setenv(PROFILE_ENV, "hybrid_rerank_v1")
    loader = RuntimeDependencyLoader(
        sqlite_db_path=db_path, require_identity_bound_runtime=True
    )
    health = loader.health()
    assert health["status"] == "failed"
    assert health["retrieval"]["status"] == "failed"
    assert "lexical_display_v1" in health["retrieval"]["message"]


# --------------------------------------------------------------------------- #
# 2. the loader binds the lexical retriever without any LanceDB
# --------------------------------------------------------------------------- #


def test_loader_pins_the_frozen_retrieval_profile_constant(tmp_path, monkeypatch):
    """The retriever's policy version is the data-core constant, never a string
    the caller supplies."""
    _identity_env(monkeypatch)
    deps = _bound_loader(monkeypatch, tmp_path).get_dependencies()
    assert deps.retriever._retriever.retrieval_policy_version == RETRIEVAL_PROFILE
    assert RETRIEVAL_PROFILE == "lexical_display_v1"


def test_loader_reports_dense_components_not_served(tmp_path, monkeypatch):
    _identity_env(monkeypatch)
    health = _bound_loader(monkeypatch, tmp_path).get_dependencies().health
    assert health["lancedb"]["status"] == "not_served"
    assert health["embedding"]["status"] == "not_served"
    assert health["reranker"]["status"] == "not_served"
    assert health["sqlite"]["status"] == "ready"
    assert health["retrieval"]["status"] == "ready"


def test_loader_does_not_require_index_manifest_id(tmp_path, monkeypatch):
    _identity_env(monkeypatch)
    health = _bound_loader(monkeypatch, tmp_path).get_dependencies().health
    assert health["status"] == "ready"
    assert "index_manifest_id" not in health["retrieval"]


def test_loader_identity_nulls_dense_embedding_and_reranker(tmp_path, monkeypatch):
    _identity_env(monkeypatch)
    identity = _bound_loader(monkeypatch, tmp_path).get_dependencies().data_runtime_identity
    assert isinstance(identity, DataRuntimeIdentity)
    assert identity.data_snapshot_id == SNAPSHOT
    assert identity.corpus_manifest_id == MANIFEST
    assert identity.fts_index_version == LIVE_DIGEST
    assert identity.dense_index_version is None
    assert identity.embedding_model_revision is None
    assert identity.reranker_revision is None


def test_loader_uses_a_readonly_connection_factory(tmp_path, monkeypatch):
    _identity_env(monkeypatch)
    deps = _bound_loader(monkeypatch, tmp_path).get_dependencies()
    factory = deps.retriever._retriever._db_conn_factory
    assert callable(factory)
    conn = factory()
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM corpus_manifest").fetchone()[0] == 1
    finally:
        conn.close()


def test_loader_caches_dependencies(tmp_path, monkeypatch):
    _identity_env(monkeypatch)
    loader = _bound_loader(monkeypatch, tmp_path)
    assert loader.get_dependencies() is loader.get_dependencies()


def test_cached_dependencies_never_hold_request_temporal_identity(tmp_path, monkeypatch):
    from dataclasses import fields

    from catalyst_agents.runtime.dependencies import RuntimeDependencies

    _identity_env(monkeypatch)
    deps = _bound_loader(monkeypatch, tmp_path).get_dependencies()
    assert "temporal_identity" not in {field.name for field in fields(RuntimeDependencies)}
    assert not hasattr(deps, "temporal_identity")


# --------------------------------------------------------------------------- #
# 3. the loader fails closed
# --------------------------------------------------------------------------- #
def test_loader_fails_closed_on_a_missing_identity(tmp_path, monkeypatch):
    _identity_env(monkeypatch, postbuild="")
    loader = _bound_loader(monkeypatch, tmp_path)
    health = loader.health()
    assert health["status"] == "failed"
    assert health["retrieval"]["status"] == "failed"
    assert loader.get_dependencies().retriever is None


def test_loader_fails_closed_on_a_digest_mismatch(tmp_path, monkeypatch):
    _identity_env(monkeypatch, digest="an-operator-wrong-digest")
    projection_dir = tmp_path / "projection-on-mismatch"
    health = _bound_loader(
        monkeypatch, tmp_path, projection_dir=projection_dir
    ).health()
    assert health["status"] == "failed"
    assert health["retrieval"]["status"] == "failed"
    assert "digest" in health["retrieval"]["message"].lower()
    assert not projection_dir.exists()


def test_loader_fails_closed_when_no_build_is_published(tmp_path, monkeypatch):
    db_path = _served_db(tmp_path, build_status="lexical_ready")
    _identity_env(monkeypatch)
    loader = RuntimeDependencyLoader(
        sqlite_db_path=db_path, require_identity_bound_runtime=True
    )
    health = loader.health()
    assert health["status"] == "failed"
    assert health["retrieval"]["status"] == "failed"


def test_loader_fails_closed_when_the_expected_digest_is_absent(tmp_path, monkeypatch):
    _identity_env(monkeypatch)
    monkeypatch.delenv(EXPECTED_DIGEST_ENV, raising=False)
    health = _bound_loader(monkeypatch, tmp_path).health()
    assert health["status"] == "failed"
    assert health["retrieval"]["status"] == "failed"


# --------------------------------------------------------------------------- #
# 4. the adapter protocol and the fail-closed branch
# --------------------------------------------------------------------------- #
def test_retriever_protocol_declares_the_two_entry_points():
    assert hasattr(RetrieverProtocol, "retrieve")
    assert hasattr(RetrieverProtocol, "retrieve_with_observation")


def _lexical_hit() -> RetrievalHit:
    temporal = TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=datetime(2026, 1, 6, 14, 30, tzinfo=timezone.utc),
        session_close_at=datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc),
        information_window_start_at=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc),
    )
    runtime = DataRuntimeIdentity(
        data_snapshot_id=SNAPSHOT,
        corpus_manifest_id=MANIFEST,
        fts_index_version=LIVE_DIGEST,
        dense_index_version=None,
        embedding_model_revision=None,
        reranker_revision=None,
        query_policy_version="qp:v1",
    )
    return RetrievalHit(
        evidence_id="chunk:1",
        canonical_asset_id="v1:asset:1",
        content_version_id="v1:content:1",
        corpus_document_id="doc:1",
        chunk_id="chunk:1",
        excerpt="NVDA Item 2.02 results.",
        scores=(StageScore(stage="lexical", value=-1.0),),
        ranks=(StageRank(stage="lexical", value=1),),
        source_class=SourceClass.ISSUER_DISCLOSURE,
        content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc),
        ticker_scope=("NVDA",),
        provider="sec",
        publisher="SEC",
        dedup_cluster_id="v1:dedup:1",
        parse_quality="full",
        retrieval_policy_version=RETRIEVAL_PROFILE,
        temporal_identity=temporal,
        data_runtime_identity=runtime,
        section_key="item_2_02",
        chunk_ordinal=1,
        asset_type="FILING",
        content_hash="c" * 64,
        material_capability=material_capability_for_content_state("FULL_TEXT"),
        serving_status="body_candidate",
        temporal_precision="publication_time",
        independence_group_id="v1:ind:1",
        canonical_url="https://example.test/nvda",
        evidence_role=source_role_for(SourceClass.ISSUER_DISCLOSURE).value,
    )


def test_adapter_maps_a_real_lexical_display_observation():
    hit = _lexical_hit()
    result_set = RetrievalResultSet(
        hits=(hit,),
        temporal_identity=hit.temporal_identity,
        data_runtime_identity=hit.data_runtime_identity,
    )
    observation = LexicalDisplayObservation(
        requested_mode="lexical",
        served_mode="fts5",
        match_mode="temporal",
        policy="temporal_window",
        temporal_window_days=5,
        temporal_centre_date="2026-01-06",
        ordered_candidate_evidence_ids=("chunk:1",),
        ordered_final_ranked_evidence_ids=("chunk:1",),
        slot_exclusions=(("chunk:1", "displayed"),),
        ticker_violations=(),
        cutoff_violations=(),
        latency_ms=3,
        served_build_id=BUILD,
        served_manifest_id=MANIFEST,
    )

    class FakeLexicalRetriever:
        def retrieve_with_observation(self, query, **kwargs):  # noqa: A002
            return ObservedLexicalDisplay(observation=observation, result_set=result_set)

    evidence, observed = AgentRetrieverAdapter(
        FakeLexicalRetriever()
    ).retrieve_with_observations(
        "NVDA",
        ticker="NVDA",
        cutoff="2026-01-06T21:00:00Z",
        requested_manifest_id=MANIFEST,
        temporal_identity=hit.temporal_identity,
        top_k=8,
        candidate_depth=100,
    )
    assert len(evidence) == 1
    assert observed.match_mode == "temporal"
    assert observed.retrieval_policy == "temporal_window"
    assert observed.served_build_id == BUILD
    assert observed.served_mode == "fts5"
    assert observed.ordered_candidate_evidence_ids == ("chunk:1",)
    assert observed.ordered_final_ranked_evidence_ids == ("chunk:1",)


def test_adapter_fails_closed_when_a_non_rerank_retriever_returns_no_result():
    """The lexical retriever never returns ``result is None``. If it does, the
    call fails closed and is never described as 'the reranker did not serve'."""
    observation = LexicalDisplayObservation(
        requested_mode="lexical",
        served_mode="fts5",
        match_mode="none",
        policy=None,
        temporal_window_days=None,
        temporal_centre_date=None,
        ordered_candidate_evidence_ids=(),
        ordered_final_ranked_evidence_ids=(),
        slot_exclusions=(),
        ticker_violations=(),
        cutoff_violations=(),
        latency_ms=1,
        served_build_id=BUILD,
        served_manifest_id=MANIFEST,
        is_degraded=True,
    )

    class NoResultLexicalRetriever:
        def retrieve_with_observation(self, query, **kwargs):  # noqa: A002
            return ObservedLexicalDisplay(observation=observation, result_set=None)

    with pytest.raises(RetrievalDegradedError) as excinfo:
        AgentRetrieverAdapter(NoResultLexicalRetriever()).retrieve_with_observations(
            "NVDA",
            ticker="NVDA",
            cutoff="2026-01-06T21:00:00Z",
            requested_manifest_id=MANIFEST,
            temporal_identity=_lexical_hit().temporal_identity,
            top_k=8,
            candidate_depth=100,
        )
    assert "reranker" not in str(excinfo.value).lower()
    assert "no_result" in str(excinfo.value)
    assert excinfo.value.observation is observation


def test_adapter_attributes_reranker_failure_only_when_a_rerank_stage_is_declared():
    observation = SimpleNamespace(
        requested_mode="reranked",
        served_mode="lexical",
        arm_names=("lexical", "dense", "reranked"),
        ordered_candidate_evidence_ids=("chunk:1",),
        ordered_final_ranked_evidence_ids=("chunk:1",),
        rank_changes={},
        degradation_reasons=("reranker_timeout",),
        match_mode=None,
        policy=None,
        temporal_window_days=None,
        temporal_centre_date=None,
        served_build_id=None,
    )

    class DegradedHybridRetriever:
        def retrieve_with_observation(self, query, **kwargs):  # noqa: A002
            return SimpleNamespace(result_set=None, observation=observation)

    with pytest.raises(RetrievalDegradedError) as excinfo:
        AgentRetrieverAdapter(DegradedHybridRetriever()).retrieve_with_observations(
            "NVDA",
            ticker="NVDA",
            cutoff="2026-01-06T21:00:00Z",
            requested_manifest_id=MANIFEST,
            temporal_identity=_lexical_hit().temporal_identity,
            top_k=8,
            candidate_depth=100,
        )
    assert "reranker_timeout" in str(excinfo.value)
