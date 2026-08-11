"""App dependency factory can assemble ProductionHybridRetriever (Finding 2)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION

CODE_REVISION = "04dabde9f44e9638d1b5ebad0eed002f5475c1a3"


def _valid_manifest_dict() -> dict:
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION

    return {
        "schema_version": "1.0.0",
        "model_name": BGE_M3_MODEL,
        "model_revision": BGE_M3_REVISION,
        "tokenizer_revision": TOKENIZER_REVISION,
        "normalization_mode": "l2",
        "dtype": "float32",
        "dimension": BGE_M3_DIMENSION,
        "corpus_manifest_id": "c" * 64,
        "source_bundle_id": "b" * 64,
        "snapshot_id": "6" * 64,
        "probe_report_id": "7" * 64,
        "postbuild_readiness_id": "8" * 64,
        "artifact_hashes": {
            "vectors.npy": "1" * 64,
            "chunk_ids.json": "2" * 64,
            "lancedb_table": "3" * 64,
        },
        "code_revision": CODE_REVISION,
        "vector_count": 4,
        "chunk_order_checksum": "4" * 64,
        "vectors_checksum": "5" * 64,
        "artifact_state": "lancedb_imported",
    }


class _FakeQueryEmbedder:
    @property
    def dimension(self) -> int:
        return BGE_M3_DIMENSION

    @property
    def model_revision(self) -> str:
        return BGE_M3_REVISION

    def embed_query(self, query: str) -> np.ndarray:
        values = np.arange(BGE_M3_DIMENSION, dtype=np.float32) + 1.0
        return (values / np.linalg.norm(values)).astype(np.float32)


class _FakeQueryEmbeddingFactory:
    def create(self, *, model_name: str):
        assert model_name == BGE_M3_MODEL
        return _FakeQueryEmbedder()


class _FakeLanceTable:
    def search(self, _query, query_type: str = "fts"):
        class _Query:
            def limit(self, _count):
                return self

            def to_list(self):
                return [{"vector": [0.0] * BGE_M3_DIMENSION}]

        return _Query()


class _FakeLanceDb:
    def open_table(self, _name):
        return _FakeLanceTable()


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok")


def test_app_dependency_factory_assembles_production_hybrid_retriever(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_data.retrieval.hybrid import ProductionHybridRetriever
    from catalyst_data.retrieval.index_manifest import IndexManifest

    import catalyst_app.dependencies as app_dependencies

    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    lancedb_dir = tmp_path / "ldb"
    lancedb_dir.mkdir(parents=True)
    manifest = IndexManifest.from_dict(_valid_manifest_dict())
    (lancedb_dir / "index_manifest.json").write_text(json.dumps(manifest.to_dict()))

    monkeypatch.setenv("CATALYST_CORPUS_MANIFEST_ID", manifest.corpus_manifest_id)
    monkeypatch.setenv("CATALYST_INDEX_MANIFEST_ID", manifest.index_manifest_id)
    monkeypatch.setenv("CATALYST_SOURCE_BUNDLE_ID", manifest.source_bundle_id)
    monkeypatch.setenv("CATALYST_SNAPSHOT_ID", manifest.snapshot_id)
    monkeypatch.setenv("CATALYST_PROBE_REPORT_ID", manifest.probe_report_id)
    monkeypatch.setenv("CATALYST_POSTBUILD_READINESS_ID", manifest.postbuild_readiness_id)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(lancedb_dir))

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        lancedb_table_name="chunks",
        require_identity_bound_runtime=True,
        query_embedding_factory=_FakeQueryEmbeddingFactory(),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: _FakeLanceDb(),
    )
    deps = loader.get_dependencies()
    assert deps.health["status"] == "ready"
    assert isinstance(deps.retriever._retriever, ProductionHybridRetriever)

    monkeypatch.setattr(app_dependencies, "get_runtime_dependency_loader", lambda: loader)
    graph = app_dependencies._graph_factory(
        {"provider": "openai", "model_id": "model-default"},
        api_key="fixture-key-not-a-secret",
    )
    assert graph is not None
    assert deps.retriever is not None
    assert deps.retriever._retriever.lancedb_table is not None
    assert deps.retriever._retriever.embedding_fn is not None


def test_app_loader_wires_production_query_embedding_factory(monkeypatch):
    import catalyst_app.dependencies as app_dependencies
    from catalyst_agents.runtime.query_embedding import ProductionBgeM3QueryEmbeddingFactory

    app_dependencies.get_runtime_dependency_loader.cache_clear()
    try:
        loader = app_dependencies.get_runtime_dependency_loader()
    finally:
        app_dependencies.get_runtime_dependency_loader.cache_clear()
    assert isinstance(loader._query_embedding_factory, ProductionBgeM3QueryEmbeddingFactory)


def test_app_loader_wires_catalyst_index_manifest_path_from_env(monkeypatch):
    """App factory must pass CATALYST_INDEX_MANIFEST_PATH into the loader.

    The authoritative clean-import manifest lives outside the LanceDB gold
    directory (data/embeddings/<code_revision>/index_manifest.json); the app
    runtime must not silently fall back to <lancedb_dir>/index_manifest.json.
    """
    import catalyst_app.dependencies as app_dependencies

    manifest_path = Path("/tmp/index_manifest.json")
    monkeypatch.setenv("CATALYST_INDEX_MANIFEST_PATH", str(manifest_path))
    app_dependencies.get_runtime_dependency_loader.cache_clear()
    try:
        loader = app_dependencies.get_runtime_dependency_loader()
    finally:
        app_dependencies.get_runtime_dependency_loader.cache_clear()
    assert loader.index_manifest_path == manifest_path


def test_app_loader_index_manifest_path_defaults_to_none(monkeypatch):
    import catalyst_app.dependencies as app_dependencies

    monkeypatch.delenv("CATALYST_INDEX_MANIFEST_PATH", raising=False)
    app_dependencies.get_runtime_dependency_loader.cache_clear()
    try:
        loader = app_dependencies.get_runtime_dependency_loader()
    finally:
        app_dependencies.get_runtime_dependency_loader.cache_clear()
    assert loader.index_manifest_path is None


def test_loader_uses_explicit_manifest_path_outside_lancedb_dir(tmp_path, monkeypatch):
    """Correct manifest wired via index_manifest_path (env contract) => ready."""
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_data.retrieval.index_manifest import IndexManifest

    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    lancedb_dir = tmp_path / "ldb"
    lancedb_dir.mkdir(parents=True)
    manifest = IndexManifest.from_dict(_valid_manifest_dict())
    manifest_dir = tmp_path / "clean_import"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "index_manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict()))

    monkeypatch.setenv("CATALYST_CORPUS_MANIFEST_ID", manifest.corpus_manifest_id)
    monkeypatch.setenv("CATALYST_INDEX_MANIFEST_ID", manifest.index_manifest_id)
    monkeypatch.setenv("CATALYST_SOURCE_BUNDLE_ID", manifest.source_bundle_id)
    monkeypatch.setenv("CATALYST_SNAPSHOT_ID", manifest.snapshot_id)
    monkeypatch.setenv("CATALYST_PROBE_REPORT_ID", manifest.probe_report_id)
    monkeypatch.setenv("CATALYST_POSTBUILD_READINESS_ID", manifest.postbuild_readiness_id)

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        lancedb_dir=lancedb_dir,
        lancedb_table_name="chunks",
        require_identity_bound_runtime=True,
        query_embedding_factory=_FakeQueryEmbeddingFactory(),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: _FakeLanceDb(),
        index_manifest_path=manifest_path,
    )
    deps = loader.get_dependencies()
    assert deps.health["status"] == "ready"
    assert deps.health["retrieval"]["index_manifest_id"] == manifest.index_manifest_id


def test_loader_fails_when_explicit_manifest_missing(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_data.retrieval.index_manifest import IndexManifest

    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    manifest = IndexManifest.from_dict(_valid_manifest_dict())
    monkeypatch.setenv("CATALYST_CORPUS_MANIFEST_ID", manifest.corpus_manifest_id)
    monkeypatch.setenv("CATALYST_INDEX_MANIFEST_ID", manifest.index_manifest_id)
    monkeypatch.setenv("CATALYST_SOURCE_BUNDLE_ID", manifest.source_bundle_id)
    monkeypatch.setenv("CATALYST_SNAPSHOT_ID", manifest.snapshot_id)
    monkeypatch.setenv("CATALYST_PROBE_REPORT_ID", manifest.probe_report_id)
    monkeypatch.setenv("CATALYST_POSTBUILD_READINESS_ID", manifest.postbuild_readiness_id)

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        lancedb_dir=tmp_path / "ldb",
        lancedb_table_name="chunks",
        require_identity_bound_runtime=True,
        query_embedding_factory=_FakeQueryEmbeddingFactory(),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: _FakeLanceDb(),
        index_manifest_path=tmp_path / "missing" / "index_manifest.json",
    )
    health = loader.health()
    assert health["status"] == "failed"
    assert health["retrieval"]["status"] == "failed"
    assert "IndexManifest" in health["retrieval"]["message"]


def test_loader_fails_when_explicit_manifest_identity_wrong(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_data.retrieval.index_manifest import IndexManifest

    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    manifest = IndexManifest.from_dict(_valid_manifest_dict())
    wrong = IndexManifest.from_dict({**_valid_manifest_dict(), "code_revision": "0" * 40})
    manifest_dir = tmp_path / "clean_import"
    manifest_dir.mkdir(parents=True)
    wrong_path = manifest_dir / "index_manifest.json"
    wrong_path.write_text(json.dumps(wrong.to_dict()))

    monkeypatch.setenv("CATALYST_CORPUS_MANIFEST_ID", manifest.corpus_manifest_id)
    monkeypatch.setenv("CATALYST_INDEX_MANIFEST_ID", manifest.index_manifest_id)
    monkeypatch.setenv("CATALYST_SOURCE_BUNDLE_ID", manifest.source_bundle_id)
    monkeypatch.setenv("CATALYST_SNAPSHOT_ID", manifest.snapshot_id)
    monkeypatch.setenv("CATALYST_PROBE_REPORT_ID", manifest.probe_report_id)
    monkeypatch.setenv("CATALYST_POSTBUILD_READINESS_ID", manifest.postbuild_readiness_id)

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        lancedb_dir=tmp_path / "ldb",
        lancedb_table_name="chunks",
        require_identity_bound_runtime=True,
        query_embedding_factory=_FakeQueryEmbeddingFactory(),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: _FakeLanceDb(),
        index_manifest_path=wrong_path,
    )
    health = loader.health()
    assert health["status"] == "failed"
    assert health["retrieval"]["status"] == "failed"
    assert "mismatch" in health["retrieval"]["message"].lower() or "failed" in health["retrieval"]["message"].lower()
