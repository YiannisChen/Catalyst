from __future__ import annotations

from pathlib import Path

from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader


class FakeTable:
    def __init__(self, vector_dim: int = 3):
        self.vector_dim = vector_dim

    def to_list(self):
        return [{"vector": [0.0] * self.vector_dim, "asset_id": "a1"}]


class FakeSearchQuery:
    def __init__(self, rows):
        self._rows = rows

    def limit(self, _count: int):
        return self

    def to_list(self):
        return self._rows


class FtsFirstFakeTable:
    def __init__(self, vector_dim: int = 5):
        self.vector_dim = vector_dim
        self.to_list_called = False

    def search(self, _query: str, query_type: str = "fts"):
        assert query_type == "fts"
        return FakeSearchQuery([{"vector": [0.0] * self.vector_dim}])

    def to_list(self):
        self.to_list_called = True
        return [{"vector": [1.0] * 999}]


class FakeLanceDB:
    def __init__(self, table: FakeTable | None = None):
        self._table = table or FakeTable()

    def open_table(self, _: str):
        return self._table


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok")


def test_loader_ready_health_when_all_dependencies_available(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(tmp_path / "ldb"))

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        lancedb_table_name="chunks",
        default_model="gpt-4.1-mini",
        embedding_factory=lambda _: (lambda _q: [0.1, 0.2, 0.3], 3),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: FakeLanceDB(FakeTable(vector_dim=3)),
    )

    deps = loader.get_dependencies()

    assert deps.health["status"] == "ready"
    assert deps.health["sqlite"]["status"] == "ready"
    assert deps.health["lancedb"]["status"] == "ready"
    assert deps.health["embedding"]["status"] == "ready"
    assert deps.health["reranker"]["status"] == "ready"
    assert deps.health["default_model"]["model"] == "gpt-4.1-mini"


def test_loader_degraded_when_reranker_unavailable(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(tmp_path / "ldb"))

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        lancedb_table_name="chunks",
        embedding_factory=lambda _: (lambda _q: [0.1, 0.2, 0.3], 3),
        reranker_factory=lambda _: None,
        lancedb_connect_factory=lambda _path: FakeLanceDB(FakeTable(vector_dim=3)),
    )

    health = loader.health()

    assert health["status"] == "degraded"
    assert health["reranker"]["status"] == "degraded"


def test_loader_failed_when_lancedb_env_missing(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    monkeypatch.delenv("CATALYST_LANCEDB_DIR", raising=False)

    loader = RuntimeDependencyLoader(sqlite_db_path=sqlite_path)
    health = loader.health()

    assert health["status"] == "failed"
    assert health["lancedb"]["status"] == "failed"
    assert "CATALYST_LANCEDB_DIR" in health["lancedb"]["message"]


def test_loader_failed_when_embedding_index_dim_incompatible(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(tmp_path / "ldb"))

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        lancedb_table_name="chunks",
        embedding_factory=lambda _: (lambda _q: [0.1] * 4, 4),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: FakeLanceDB(FakeTable(vector_dim=3)),
    )

    health = loader.health()

    assert health["status"] == "failed"
    assert health["embedding"]["status"] == "failed"
    assert "dimension mismatch" in health["embedding"]["message"].lower()


def test_loader_initializes_heavy_dependencies_once(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(tmp_path / "ldb"))

    calls = {"embedding": 0, "lancedb": 0, "reranker": 0}

    def embedding_factory(_):
        calls["embedding"] += 1
        return (lambda _q: [0.0, 0.0, 0.0], 3)

    def connect_factory(_):
        calls["lancedb"] += 1
        return FakeLanceDB(FakeTable(vector_dim=3))

    def reranker_factory(_):
        calls["reranker"] += 1
        return object()

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        lancedb_table_name="chunks",
        embedding_factory=embedding_factory,
        reranker_factory=reranker_factory,
        lancedb_connect_factory=connect_factory,
    )

    first = loader.get_dependencies()
    second = loader.get_dependencies()

    assert first is second
    assert calls == {"embedding": 1, "lancedb": 1, "reranker": 1}


def test_loader_degraded_when_reranker_factory_raises(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(tmp_path / "ldb"))

    def boom(_):
        raise RuntimeError("reranker boom")

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        lancedb_table_name="chunks",
        embedding_factory=lambda _: (lambda _q: [0.1, 0.2, 0.3], 3),
        reranker_factory=boom,
        lancedb_connect_factory=lambda _path: FakeLanceDB(FakeTable(vector_dim=3)),
    )

    health = loader.health()

    assert health["status"] == "degraded"
    assert health["reranker"]["status"] == "degraded"
    assert "boom" in health["reranker"]["message"] or "fallback" in health["reranker"]["message"]


def test_vector_dim_detection_prefers_bounded_fts_sampling(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(tmp_path / "ldb"))
    table = FtsFirstFakeTable(vector_dim=7)

    loader = RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        lancedb_table_name="chunks",
        embedding_factory=lambda _: (lambda _q: [0.1] * 7, 7),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: FakeLanceDB(table),
    )
    deps = loader.get_dependencies()

    assert deps.health["status"] == "ready"
    assert deps.health["lancedb"]["vector_dim"] == 7
    assert table.to_list_called is False
