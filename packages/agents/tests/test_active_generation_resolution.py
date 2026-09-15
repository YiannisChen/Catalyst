from __future__ import annotations

import json
from pathlib import Path

from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader


class FakeTable:
    def __init__(self, vector_dim: int = 3):
        self.vector_dim = vector_dim

    def to_list(self):
        return [{"vector": [0.0] * self.vector_dim, "asset_id": "a1"}]


class FakeLanceDB:
    def __init__(self):
        self.opened: list[str] = []

    def open_table(self, name: str):
        self.opened.append(name)
        return FakeTable()


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok")


def _write_active_generation(
    lancedb_dir: Path,
    *,
    table_name: str = "chunks__staging__abc",
    schema_version: str = "active_generation_v1",
    index_manifest_id: str | None = None,
) -> None:
    lancedb_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": schema_version,
        "table_name": table_name,
        "chunk_count": 1,
    }
    if index_manifest_id is not None:
        payload["index_manifest_id"] = index_manifest_id
    (lancedb_dir / "active_generation.json").write_text(
        json.dumps(payload)
    )


def _loader(sqlite_path: Path, lancedb_connect_factory, **kwargs) -> RuntimeDependencyLoader:
    return RuntimeDependencyLoader(
        sqlite_db_path=sqlite_path,
        embedding_factory=lambda _: (lambda _q: [0.1, 0.2, 0.3], 3),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lancedb_connect_factory,
        **kwargs,
    )


def test_resolves_table_name_from_active_generation(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    lancedb_dir = tmp_path / "ldb"
    _write_active_generation(lancedb_dir, table_name="chunks__staging__abc")
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(lancedb_dir))
    db = FakeLanceDB()
    loader = _loader(sqlite_path, lambda _path: db)
    deps = loader.get_dependencies()
    assert deps.health["status"] == "ready"
    assert db.opened == ["chunks__staging__abc"]
    assert deps.health["lancedb"]["table"] == "chunks__staging__abc"
    assert "active_generation" in deps.health["lancedb"]


def test_resolves_promoted_table_from_pointer_bound_candidate_dir(
    tmp_path, monkeypatch
):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    lancedb_dir = tmp_path / "ldb"
    index_manifest_id = "a" * 64
    candidate_dir = lancedb_dir / "candidates" / index_manifest_id
    candidate_dir.mkdir(parents=True)
    _write_active_generation(
        lancedb_dir,
        table_name="candidate_aaaaaaaaaaaaaaaa",
        index_manifest_id=index_manifest_id,
    )
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(lancedb_dir))
    db = FakeLanceDB()
    connected: list[str] = []

    def connect(path: str):
        connected.append(path)
        return db

    deps = _loader(sqlite_path, connect).get_dependencies()
    assert deps.health["status"] == "ready"
    assert connected == [str(candidate_dir)]
    assert deps.lancedb_dir == candidate_dir
    assert deps.health["lancedb"]["authority_path"] == str(lancedb_dir)
    assert deps.health["lancedb"]["path"] == str(candidate_dir)


def test_fails_closed_when_active_generation_missing(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    lancedb_dir = tmp_path / "ldb"
    lancedb_dir.mkdir(parents=True)
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(lancedb_dir))
    db = FakeLanceDB()
    loader = _loader(sqlite_path, lambda _path: db)
    health = loader.health()
    assert health["status"] == "failed"
    assert health["lancedb"]["status"] == "failed"
    assert "active_generation.json not found" in health["lancedb"]["message"]
    assert "refusing to fall back to table 'chunks'" in health["lancedb"]["message"]
    assert db.opened == []


def test_fails_closed_on_bad_schema_version(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    lancedb_dir = tmp_path / "ldb"
    _write_active_generation(lancedb_dir, schema_version="other_v1")
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(lancedb_dir))
    db = FakeLanceDB()
    loader = _loader(sqlite_path, lambda _path: db)
    health = loader.health()
    assert health["status"] == "failed"
    assert "schema_version" in health["lancedb"]["message"]
    assert db.opened == []


def test_explicit_table_name_bypasses_active_generation(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.db"
    _touch(sqlite_path)
    lancedb_dir = tmp_path / "ldb"
    _write_active_generation(lancedb_dir, table_name="chunks__staging__pointer")
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", str(lancedb_dir))
    db = FakeLanceDB()
    loader = _loader(sqlite_path, lambda _path: db, lancedb_table_name="chunks")
    deps = loader.get_dependencies()
    assert deps.health["status"] == "ready"
    assert db.opened == ["chunks"]
    assert deps.health["lancedb"]["table"] == "chunks"


def test_app_loader_resolves_table_from_active_pointer(monkeypatch):
    import catalyst_app.dependencies as app_dependencies

    app_dependencies.get_runtime_dependency_loader.cache_clear()
    try:
        loader = app_dependencies.get_runtime_dependency_loader()
    finally:
        app_dependencies.get_runtime_dependency_loader.cache_clear()
    assert loader.lancedb_table_name is None
