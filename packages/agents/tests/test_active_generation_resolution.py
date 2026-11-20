"""Frozen-A3 §2.1/§2.5: the V1.1 loader never resolves LanceDB.

The historical loader read ``active_generation.json`` to choose the LanceDB
table. V1.1 has no dense arm, so the pointer is inert: the loader must never
connect to LanceDB, never open a table, and never construct
``ProductionHybridRetriever`` -- whatever ``active_generation.json`` contains
(or does not contain).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
from catalyst_data.retrieval.lexical_display import RETRIEVAL_PROFILE

# WP-B B7a: the minimal served DB cannot build a real projection.
pytestmark = pytest.mark.usefixtures("stub_served_projection")

MANIFEST = "a" * 64
BUILD = "b" * 64
LIVE_DIGEST = "fts-digest-live"


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


def _write_active_generation(lancedb_dir: Path, payload: dict) -> None:
    lancedb_dir.mkdir(parents=True, exist_ok=True)
    (lancedb_dir / "active_generation.json").write_text(json.dumps(payload))


def _bind_env(monkeypatch) -> None:
    monkeypatch.setenv("CATALYST_RETRIEVAL_PROFILE", RETRIEVAL_PROFILE)
    monkeypatch.setenv("CATALYST_CORPUS_MANIFEST_ID", MANIFEST)
    monkeypatch.setenv("CATALYST_SNAPSHOT_ID", "s" * 64)
    monkeypatch.setenv("CATALYST_SOURCE_BUNDLE_ID", "c" * 64)
    monkeypatch.setenv("CATALYST_PROBE_REPORT_ID", "p" * 64)
    monkeypatch.setenv("CATALYST_POSTBUILD_READINESS_ID", "q" * 64)
    monkeypatch.setenv("CATALYST_EXPECTED_FTS_DIGEST", LIVE_DIGEST)




def test_loader_never_requires_lancedb_dir(tmp_path, monkeypatch):
    """No LanceDB directory, no active_generation.json: still ready."""
    monkeypatch.delenv("CATALYST_LANCEDB_DIR", raising=False)
    _bind_env(monkeypatch)

    health = RuntimeDependencyLoader(
        sqlite_db_path=_served_db(tmp_path), require_identity_bound_runtime=True
    ).health()

    assert health["status"] == "ready"
    assert health["lancedb"]["status"] == "not_served"
    assert health["errors"] == []


def test_app_loader_has_no_lancedb_table_name(monkeypatch):
    """The app-owned loader carries no LanceDB table name."""
    import catalyst_app.dependencies as app_dependencies

    monkeypatch.delenv("CATALYST_INDEX_MANIFEST_PATH", raising=False)
    app_dependencies.get_runtime_dependency_loader.cache_clear()
    try:
        loader = app_dependencies.get_runtime_dependency_loader()
    finally:
        app_dependencies.get_runtime_dependency_loader.cache_clear()
    assert not hasattr(loader, "lancedb_table_name")
    assert not hasattr(loader, "index_manifest_path")
