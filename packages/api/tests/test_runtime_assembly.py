"""App dependency factory assembles the V1.1 lexical retriever (Frozen-A3 §2.1).

The app-owned loader binds exactly one retriever:
``AgentRetrieverAdapter(LexicalDisplayRetriever(...))``. LanceDB, embeddings and
the reranker are not resolved (``not_served``), and the app graph factory uses
the bound lexical retriever.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from catalyst_data.retrieval.lexical_display import RETRIEVAL_PROFILE
from catalyst_data.retrieval.lexical_retriever import LexicalDisplayRetriever

# WP-B B7a: the minimal served DB cannot build a real projection.
pytestmark = pytest.mark.usefixtures("stub_served_projection")

MANIFEST = "a" * 64
BUILD = "b" * 64
LIVE_DIGEST = "fts-digest-live"


def test_event_time_p1_keeps_the_pre_close_analyst_prompt_identity():
    from catalyst_app.runtime.manifest_factory import production_prompt_identity

    assert production_prompt_identity("event_time_p1") == production_prompt_identity("pre_close_v1")


def _served_db(tmp_path: Path) -> Path:
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
        (BUILD, MANIFEST, "published", 1, LIVE_DIGEST, 5),
    )
    conn.execute("INSERT INTO lexical_index_state VALUES (1,?,?)", (MANIFEST, BUILD))
    conn.commit()
    conn.close()
    return path


def _bind_env(monkeypatch) -> None:
    monkeypatch.setenv("CATALYST_RETRIEVAL_PROFILE", RETRIEVAL_PROFILE)
    monkeypatch.setenv("CATALYST_CORPUS_MANIFEST_ID", MANIFEST)
    monkeypatch.setenv("CATALYST_SNAPSHOT_ID", "s" * 64)
    monkeypatch.setenv("CATALYST_SOURCE_BUNDLE_ID", "c" * 64)
    monkeypatch.setenv("CATALYST_PROBE_REPORT_ID", "p" * 64)
    monkeypatch.setenv("CATALYST_POSTBUILD_READINESS_ID", "q" * 64)
    monkeypatch.setenv("CATALYST_EXPECTED_FTS_DIGEST", LIVE_DIGEST)
    monkeypatch.delenv("CATALYST_LANCEDB_DIR", raising=False)
    monkeypatch.delenv("CATALYST_V2_RULES", raising=False)
    monkeypatch.delenv("CATALYST_NEWS_BODIES_DB", raising=False)


def test_app_dependency_factory_assembles_the_lexical_retriever(tmp_path, monkeypatch):
    import catalyst_app.dependencies as app_dependencies
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader

    _bind_env(monkeypatch)
    loader = RuntimeDependencyLoader(
        sqlite_db_path=_served_db(tmp_path), require_identity_bound_runtime=True
    )
    deps = loader.get_dependencies()
    assert deps.health["status"] == "ready"
    assert isinstance(deps.retriever._retriever, LexicalDisplayRetriever)
    assert deps.lancedb_table is None
    assert deps.reranker is None

    monkeypatch.setattr(app_dependencies, "get_runtime_dependency_loader", lambda: loader)
    # Stub the provider client: this test asserts the retrieval wiring, not the
    # (network-bound) LLM construction.
    monkeypatch.setattr(app_dependencies, "build_v1_llm", lambda **_: object())
    graph = app_dependencies._graph_factory(
        {"provider": "openai", "model_id": "model-default"},
        api_key="fixture-key-not-a-secret",
    )
    assert graph is not None
    assert deps.retriever is not None


def test_loader_health_validates_against_the_app_schema(tmp_path, monkeypatch):
    """The real V1.1 loader health payload must validate against the public
    health contract (not_served dense components + a bound retrieval)."""
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_app.schemas import RuntimeHealthResponse

    _bind_env(monkeypatch)
    health = RuntimeDependencyLoader(
        sqlite_db_path=_served_db(tmp_path), require_identity_bound_runtime=True
    ).health()
    response = RuntimeHealthResponse.model_validate(health)
    assert response.status.value == "ready"
    assert response.lancedb.status.value == "not_served"
    assert response.embedding.status.value == "not_served"
    assert response.reranker.status.value == "not_served"
    assert response.retrieval is not None
    assert response.retrieval.status.value == "ready"
    assert response.retrieval.served_build_id == BUILD


@pytest.mark.parametrize(
    ("create_table", "insert_row", "expected_count"),
    [(False, False, None), (True, False, 0)],
)
def test_filing_rule_makes_health_unready_without_unit_rows(
    tmp_path, monkeypatch, create_table, insert_row, expected_count,
):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader

    _bind_env(monkeypatch)
    monkeypatch.setenv("CATALYST_V2_RULES", "filing")
    bodies = tmp_path / "news-bodies.sqlite3"
    conn = sqlite3.connect(bodies)
    if create_table:
        conn.execute("CREATE TABLE sec_exhibit_units (url TEXT PRIMARY KEY)")
        if insert_row:
            conn.execute("INSERT INTO sec_exhibit_units VALUES ('https://sec.example/exhibit')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))

    health = RuntimeDependencyLoader(
        sqlite_db_path=_served_db(tmp_path), require_identity_bound_runtime=True
    ).health()

    assert health["status"] == "failed"
    failure = next(error for error in health["errors"] if error["component"] == "sec_exhibit_units")
    assert str(bodies) in failure["message"]
    if not create_table:
        assert "no such table: sec_exhibit_units" in failure["message"]
    else:
        assert f"row_count={expected_count}" in failure["message"]
    from catalyst_app.schemas import RuntimeHealthResponse

    response = RuntimeHealthResponse.model_validate(health)
    assert response.status.value == "failed"
    assert response.sec_exhibit_units.status.value == "failed"


def test_filing_units_check_can_only_be_waived_explicitly(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader

    _bind_env(monkeypatch)
    monkeypatch.setenv("CATALYST_V2_RULES", "filing")
    bodies = tmp_path / "news-bodies.sqlite3"
    with sqlite3.connect(bodies):
        pass
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))

    # The default remains strict even if an environment variable suggests a waiver.
    monkeypatch.setenv("CATALYST_REQUIRE_FILING_UNITS", "0")
    runtime_db = _served_db(tmp_path)
    strict = RuntimeDependencyLoader(
        sqlite_db_path=runtime_db, require_identity_bound_runtime=True
    ).health()
    assert strict["status"] == "failed"
    assert any(error["component"] == "sec_exhibit_units" for error in strict["errors"])

    evaluation = RuntimeDependencyLoader(
        sqlite_db_path=runtime_db,
        retrieval_profile=RETRIEVAL_PROFILE,
        require_filing_units=False,
    ).health()
    assert evaluation["status"] == "degraded"
    assert not any(error["component"] == "sec_exhibit_units" for error in evaluation["errors"])


def test_served_http_readiness_reports_failed_when_filing_units_are_missing(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_app.main import create_app
    from fastapi.testclient import TestClient

    _bind_env(monkeypatch)
    monkeypatch.setenv("CATALYST_V2_RULES", "filing")
    bodies = tmp_path / "news-bodies.sqlite3"
    with sqlite3.connect(bodies):
        pass
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))
    runtime_db = tmp_path / "health-runtime.db"
    with sqlite3.connect(runtime_db) as conn:
        conn.executescript("CREATE TABLE runs (run_id TEXT); CREATE TABLE run_events (run_id TEXT);")
    loader = RuntimeDependencyLoader(
        sqlite_db_path=runtime_db, require_identity_bound_runtime=True
    )
    app = create_app(
        runtime_composition=SimpleNamespace(dependency_loader=loader),
        dependency_loader_override=loader,
        db_path=runtime_db,
    )

    client = TestClient(app)
    health = client.get("/api/health")
    runtime_health = client.get("/api/health/runtime")

    assert health.status_code == 200
    assert health.json()["status"] == "failed"
    assert runtime_health.status_code == 200
    assert runtime_health.json()["status"] == "failed"
    assert runtime_health.json()["sec_exhibit_units"]["status"] == "failed"


def test_api_health_reports_disabled_press_release_archive(tmp_path, monkeypatch):
    from catalyst_app.main import create_app
    from fastapi.testclient import TestClient

    runtime_db = tmp_path / "health-runtime.db"
    with sqlite3.connect(runtime_db) as conn:
        conn.executescript("CREATE TABLE runs (run_id TEXT); CREATE TABLE run_events (run_id TEXT);")
    response = TestClient(create_app(db_path=runtime_db)).get("/api/health")

    assert response.status_code == 200
    assert response.json()["press_release_archive"] == {
        "status": "disabled", "reason": "retired",
    }


def test_filing_rule_health_is_ready_with_nonempty_unit_table(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader

    _bind_env(monkeypatch)
    monkeypatch.setenv("CATALYST_V2_RULES", "filing")
    bodies = tmp_path / "news-bodies.sqlite3"
    with sqlite3.connect(bodies) as conn:
        conn.execute("CREATE TABLE sec_exhibit_units (url TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sec_exhibit_units VALUES ('https://sec.example/exhibit')")
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))

    health = RuntimeDependencyLoader(
        sqlite_db_path=_served_db(tmp_path), require_identity_bound_runtime=True
    ).health()

    assert health["status"] == "ready"
    assert not any(error["component"] == "sec_exhibit_units" for error in health["errors"])


def test_filing_rule_health_fails_for_unreadable_body_store(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader

    _bind_env(monkeypatch)
    monkeypatch.setenv("CATALYST_V2_RULES", "filing")
    bodies = tmp_path / "not-sqlite.sqlite3"
    bodies.write_text("not a sqlite database", encoding="utf-8")
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))

    health = RuntimeDependencyLoader(
        sqlite_db_path=_served_db(tmp_path), require_identity_bound_runtime=True
    ).health()

    assert health["status"] == "failed"
    failure = next(error for error in health["errors"] if error["component"] == "sec_exhibit_units")
    assert "not-sqlite.sqlite3" in failure["message"]
    assert "file is not a database" in failure["message"]


def test_units_count_store_failure_propagates_to_run_and_health_reports_reason(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_app.main import create_app
    from catalyst_data.read import sec_units
    from catalyst_data.storage.errors import StoreReadError
    from fastapi.testclient import TestClient

    _bind_env(monkeypatch)
    monkeypatch.setenv("CATALYST_V2_RULES", "filing")
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(tmp_path / "body.sqlite3"))
    loader = RuntimeDependencyLoader(
        sqlite_db_path=_served_db(tmp_path), require_identity_bound_runtime=True
    )
    monkeypatch.setattr(
        sec_units,
        "units_count",
        lambda *_: (_ for _ in ()).throw(
            StoreReadError("body.sqlite3", "read sec_exhibit_units count", "injected failure")
        ),
    )
    with pytest.raises(StoreReadError, match="injected failure"):
        loader.get_dependencies()
    assert loader.health()["status"] == "failed"
    assert loader.health()["sec_exhibit_units"]["status"] == "failed"

    runtime_db = tmp_path / "health-runtime.db"
    with sqlite3.connect(runtime_db) as conn:
        conn.executescript("CREATE TABLE runs (run_id TEXT); CREATE TABLE run_events (run_id TEXT);")
    app = create_app(
        runtime_composition=SimpleNamespace(dependency_loader=loader),
        dependency_loader_override=loader,
        db_path=runtime_db,
    )
    client = TestClient(app)
    response = client.get("/api/health")
    runtime_response = client.get("/api/health/runtime")
    assert response.json()["status"] == "failed"
    assert "injected failure" in response.json()["reason"]
    assert runtime_response.json()["sec_exhibit_units"]["status"] == "failed"
    assert "injected failure" in runtime_response.json()["sec_exhibit_units"]["message"]


def test_app_loader_wires_the_retrieval_profile_env(monkeypatch):
    import catalyst_app.dependencies as app_dependencies

    monkeypatch.setenv("CATALYST_RETRIEVAL_PROFILE", RETRIEVAL_PROFILE)
    monkeypatch.setenv("CATALYST_EXPECTED_FTS_DIGEST", "expected-digest")
    app_dependencies.get_runtime_dependency_loader.cache_clear()
    try:
        loader = app_dependencies.get_runtime_dependency_loader()
    finally:
        app_dependencies.get_runtime_dependency_loader.cache_clear()
    assert loader.retrieval_profile == RETRIEVAL_PROFILE
    assert loader.expected_fts_digest == "expected-digest"
    assert loader.require_identity_bound_runtime is True
    assert loader.require_filing_units is True


def test_app_loader_reports_unready_health_without_the_serving_tuple(monkeypatch):
    """With no identity/tuple configured the app loader fails closed."""
    import catalyst_app.dependencies as app_dependencies

    for name in (
        "CATALYST_RETRIEVAL_PROFILE",
        "CATALYST_CORPUS_MANIFEST_ID",
        "CATALYST_SNAPSHOT_ID",
        "CATALYST_SOURCE_BUNDLE_ID",
        "CATALYST_PROBE_REPORT_ID",
        "CATALYST_POSTBUILD_READINESS_ID",
        "CATALYST_EXPECTED_FTS_DIGEST",
    ):
        monkeypatch.delenv(name, raising=False)
    app_dependencies.get_runtime_dependency_loader.cache_clear()
    try:
        health = app_dependencies.get_runtime_dependency_loader().health()
    finally:
        app_dependencies.get_runtime_dependency_loader.cache_clear()
    assert health["status"] == "failed"
    assert health["retrieval"]["status"] == "failed"
