"""Typed production query embedding factory contract (Finding 2).

No model download, CUDA, or provider access: every path is exercised with
injected fakes while the production factory must fail closed on revision,
dimension, CUDA, and loader failures.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

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


class FakeQueryEmbedder:
    def __init__(self, *, dimension: int = BGE_M3_DIMENSION, revision: str = BGE_M3_REVISION, vector: np.ndarray | None = None):
        self._dimension = dimension
        self._revision = revision
        self._vector = vector
        self.calls: list[str] = []

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_revision(self) -> str:
        return self._revision

    def embed_query(self, query: str) -> np.ndarray:
        self.calls.append(query)
        if self._vector is not None:
            return np.asarray(self._vector, dtype=np.float32)
        values = np.arange(self._dimension, dtype=np.float32) + 1.0
        return (values / np.linalg.norm(values)).astype(np.float32)


class FakeQueryEmbeddingFactory:
    def __init__(self, embedder: FakeQueryEmbedder | None = None):
        self._embedder = embedder or FakeQueryEmbedder()
        self.created: list[str] = []

    def create(self, *, model_name: str) -> FakeQueryEmbedder:
        self.created.append(model_name)
        return self._embedder


class _FakeLanceTable:
    def __init__(self, vector_dim: int = BGE_M3_DIMENSION):
        self._vector_dim = vector_dim

    def search(self, _query, query_type: str = "fts"):
        class _Query:
            def __init__(self, rows):
                self.rows = rows

            def limit(self, _count):
                return self

            def to_list(self):
                return self.rows

        return _Query([{"vector": [0.0] * self._vector_dim}])


class _FakeLanceDb:
    def __init__(self, table):
        self._table = table

    def open_table(self, _name):
        return self._table


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok")


def test_production_factory_requires_pinned_model_identity():
    from catalyst_agents.runtime.query_embedding import ProductionBgeM3QueryEmbeddingFactory

    factory = ProductionBgeM3QueryEmbeddingFactory(cuda_check=lambda: True, embedder_loader=lambda **kwargs: lambda texts: np.ones((len(texts), BGE_M3_DIMENSION), dtype=np.float32))
    with pytest.raises(ValueError, match="pinned"):
        factory.create(model_name="not-bge-m3")


def test_production_factory_fails_closed_on_revision_mismatch():
    from catalyst_agents.runtime.query_embedding import ProductionBgeM3QueryEmbeddingFactory

    with pytest.raises(ValueError, match="revision"):
        ProductionBgeM3QueryEmbeddingFactory(
            model_revision="0" * 40, cuda_check=lambda: True,
            embedder_loader=lambda **kwargs: lambda texts: np.ones((len(texts), BGE_M3_DIMENSION), dtype=np.float32),
        )


def test_production_factory_fails_closed_on_dimension_mismatch():
    from catalyst_agents.runtime.query_embedding import ProductionBgeM3QueryEmbeddingFactory

    with pytest.raises(ValueError, match="dimension"):
        ProductionBgeM3QueryEmbeddingFactory(
            dimension=512, cuda_check=lambda: True,
            embedder_loader=lambda **kwargs: lambda texts: np.ones((len(texts), 512), dtype=np.float32),
        )


def test_production_factory_fails_closed_when_cuda_unavailable():
    from catalyst_agents.runtime.query_embedding import ProductionBgeM3QueryEmbeddingFactory

    factory = ProductionBgeM3QueryEmbeddingFactory(
        cuda_check=lambda: False,
        embedder_loader=lambda **kwargs: pytest.fail("loader must not run without CUDA"),
    )
    with pytest.raises(RuntimeError, match="CUDA"):
        factory.create(model_name=BGE_M3_MODEL)


def test_production_factory_fails_closed_when_loader_raises():
    from catalyst_agents.runtime.query_embedding import ProductionBgeM3QueryEmbeddingFactory

    def loader_failure(**kwargs):
        raise RuntimeError("model load failed")

    factory = ProductionBgeM3QueryEmbeddingFactory(cuda_check=lambda: True, embedder_loader=loader_failure)
    with pytest.raises(RuntimeError, match="model load failed"):
        factory.create(model_name=BGE_M3_MODEL)


def test_production_factory_embed_query_contract_and_normalization():
    from catalyst_agents.runtime.query_embedding import ProductionBgeM3QueryEmbeddingFactory

    loader_calls = []

    def fake_loader(**kwargs):
        loader_calls.append(kwargs["model_revision"])
        return lambda texts: np.ones((len(texts), BGE_M3_DIMENSION), dtype=np.float32)

    factory = ProductionBgeM3QueryEmbeddingFactory(cuda_check=lambda: True, embedder_loader=fake_loader)
    embedder = factory.create(model_name=BGE_M3_MODEL)
    vector = embedder.embed_query("why did AAPL drop?")
    assert vector.shape == (BGE_M3_DIMENSION,)
    assert vector.dtype == np.float32
    assert np.allclose(np.linalg.norm(vector), 1.0, atol=1e-4)
    assert loader_calls == [BGE_M3_REVISION]


def test_runtime_loader_ready_with_identity_manifest_table_and_fake_query_factory(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_data.retrieval.index_manifest import IndexManifest

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

    fake_factory = FakeQueryEmbeddingFactory()
    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        require_identity_bound_runtime=True,
        query_embedding_factory=fake_factory,
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: _FakeLanceDb(_FakeLanceTable(BGE_M3_DIMENSION)),
    )
    deps = loader.get_dependencies()
    assert deps.health["status"] == "ready"
    assert deps.retriever is not None
    assert fake_factory.created == [BGE_M3_MODEL]
    assert deps.embedding_dim == BGE_M3_DIMENSION


def test_runtime_loader_rejects_snapshot_mismatch(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_data.retrieval.index_manifest import IndexManifest

    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    lancedb_dir = tmp_path / "ldb"
    lancedb_dir.mkdir(parents=True)
    manifest = IndexManifest.from_dict(_valid_manifest_dict())
    (lancedb_dir / "index_manifest.json").write_text(json.dumps(manifest.to_dict()))

    monkeypatch.setenv("CATALYST_CORPUS_MANIFEST_ID", manifest.corpus_manifest_id)
    monkeypatch.setenv("CATALYST_INDEX_MANIFEST_ID", manifest.index_manifest_id)
    monkeypatch.setenv("CATALYST_SOURCE_BUNDLE_ID", manifest.source_bundle_id)
    monkeypatch.setenv("CATALYST_SNAPSHOT_ID", "9" * 64)  # wrong snapshot
    monkeypatch.setenv("CATALYST_PROBE_REPORT_ID", manifest.probe_report_id)
    monkeypatch.setenv("CATALYST_POSTBUILD_READINESS_ID", manifest.postbuild_readiness_id)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(lancedb_dir))

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        require_identity_bound_runtime=True,
        query_embedding_factory=FakeQueryEmbeddingFactory(),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: _FakeLanceDb(_FakeLanceTable(BGE_M3_DIMENSION)),
    )
    health = loader.health()
    assert health["status"] == "failed"
    assert health["retrieval"]["status"] == "failed"
    assert "snapshot" in health["retrieval"]["message"]


def test_runtime_loader_rejects_source_bundle_mismatch(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_data.retrieval.index_manifest import IndexManifest

    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    lancedb_dir = tmp_path / "ldb"
    lancedb_dir.mkdir(parents=True)
    manifest = IndexManifest.from_dict(_valid_manifest_dict())
    (lancedb_dir / "index_manifest.json").write_text(json.dumps(manifest.to_dict()))

    monkeypatch.setenv("CATALYST_CORPUS_MANIFEST_ID", manifest.corpus_manifest_id)
    monkeypatch.setenv("CATALYST_INDEX_MANIFEST_ID", manifest.index_manifest_id)
    monkeypatch.setenv("CATALYST_SOURCE_BUNDLE_ID", "0" * 64)  # wrong bundle
    monkeypatch.setenv("CATALYST_SNAPSHOT_ID", manifest.snapshot_id)
    monkeypatch.setenv("CATALYST_PROBE_REPORT_ID", manifest.probe_report_id)
    monkeypatch.setenv("CATALYST_POSTBUILD_READINESS_ID", manifest.postbuild_readiness_id)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(lancedb_dir))

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        require_identity_bound_runtime=True,
        query_embedding_factory=FakeQueryEmbeddingFactory(),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: _FakeLanceDb(_FakeLanceTable(BGE_M3_DIMENSION)),
    )
    health = loader.health()
    assert health["status"] == "failed"
    assert "source_bundle" in health["retrieval"]["message"]


def test_runtime_loader_rejects_index_manifest_id_mismatch(tmp_path, monkeypatch):
    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_data.retrieval.index_manifest import IndexManifest

    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    lancedb_dir = tmp_path / "ldb"
    lancedb_dir.mkdir(parents=True)
    manifest = IndexManifest.from_dict(_valid_manifest_dict())
    (lancedb_dir / "index_manifest.json").write_text(json.dumps(manifest.to_dict()))

    monkeypatch.setenv("CATALYST_CORPUS_MANIFEST_ID", manifest.corpus_manifest_id)
    monkeypatch.setenv("CATALYST_INDEX_MANIFEST_ID", "a" * 64)  # wrong index manifest
    monkeypatch.setenv("CATALYST_SOURCE_BUNDLE_ID", manifest.source_bundle_id)
    monkeypatch.setenv("CATALYST_SNAPSHOT_ID", manifest.snapshot_id)
    monkeypatch.setenv("CATALYST_PROBE_REPORT_ID", manifest.probe_report_id)
    monkeypatch.setenv("CATALYST_POSTBUILD_READINESS_ID", manifest.postbuild_readiness_id)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(lancedb_dir))

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        require_identity_bound_runtime=True,
        query_embedding_factory=FakeQueryEmbeddingFactory(),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: _FakeLanceDb(_FakeLanceTable(BGE_M3_DIMENSION)),
    )
    health = loader.health()
    assert health["status"] == "failed"
    assert "IndexManifest" in health["retrieval"]["message"]
