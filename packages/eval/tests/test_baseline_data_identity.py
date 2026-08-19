"""M1-3: sealed data/index identity reader contract tests."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from catalyst_eval.baseline.data_identity import lancedb_identity, snapshot_identity
from catalyst_eval.baseline.identity import (
    BaselineIdentityConflictError,
    BaselineIdentitySourceError,
)

FIXTURE_SQL = Path(__file__).parent / "fixtures" / "baseline_snapshot_fixture.sql"

AUDITED = {
    "snapshot_id": "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
    "corpus_manifest_id": "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
    "index_manifest_id": "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083",
    "source_bundle_id": "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
    "probe_report_id": "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
    "postbuild_readiness_id": "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
    "table_name": "chunks__staging__b3761f4b943542a8",
}


@pytest.fixture()
def fixture_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "baseline_snapshot_fixture.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(FIXTURE_SQL.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
    return db_path


def _lancedb_fixture(tmp_path: Path) -> tuple[Path, Path]:
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    (lancedb_dir / "active_generation.json").write_text(
        json.dumps({
            "schema_version": "active_generation_v1",
            "chunk_count": 295506,
            "snapshot_id": AUDITED["snapshot_id"],
            "corpus_manifest_id": AUDITED["corpus_manifest_id"],
            "index_manifest_id": AUDITED["index_manifest_id"],
            "source_bundle_id": AUDITED["source_bundle_id"],
            "table_name": AUDITED["table_name"],
        }, sort_keys=True),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "index_manifest.json"
    manifest_path.write_text(
        json.dumps({
            "schema_version": "1.0.0",
            "index_manifest_id": AUDITED["index_manifest_id"],
            "snapshot_id": AUDITED["snapshot_id"],
            "corpus_manifest_id": AUDITED["corpus_manifest_id"],
            "source_bundle_id": AUDITED["source_bundle_id"],
            "probe_report_id": AUDITED["probe_report_id"],
            "postbuild_readiness_id": AUDITED["postbuild_readiness_id"],
            "model_name": "BAAI/bge-m3",
            "dimension": 1024,
            "vector_count": 295506,
        }, sort_keys=True),
        encoding="utf-8",
    )
    return lancedb_dir, manifest_path


def test_snapshot_identity_reads_fixture_facts(fixture_db: Path):
    result = snapshot_identity(fixture_db)
    assert result["schema_version"] == "snapshot_identity_v1"
    assert result["corpus_manifest_id"] == AUDITED["corpus_manifest_id"]
    assert result["snapshot_id"] == AUDITED["snapshot_id"]
    assert result["inventory_row_count"] == 295506
    assert result["tokenizer_model_id"] == "BAAI/bge-m3"
    assert result["tokenizer_revision"] == "5617a9f61b028005a4858fdac845db406aefb181"
    assert result["lexical_corpus_manifest_id"] == AUDITED["corpus_manifest_id"]
    assert result["lexical_mode_served"] == "fts5"
    assert result["lexical_row_count"] == 295506
    assert result["lexical_generation_id"] == "3839ca95828ccc95b25f18b4ec9b0600fdf5d5abb6fa8dcf903a806392fed51c"
    assert result["lexical_digest"] == "54d547ff53e8e5e2e8a5aad9822c264d4cf0917781b0c99183e44611fa7d7a4e"
    assert result["corpus_served_chunks_count"] == 2
    assert result["corpus_build_chunks_fts_count"] == 3
    assert result["articles_count"] == 2
    assert result["filings_count"] == 1


def test_lancedb_identity_reads_pointer_and_manifest(tmp_path: Path):
    lancedb_dir, manifest_path = _lancedb_fixture(tmp_path)
    result = lancedb_identity(lancedb_dir, index_manifest_path=manifest_path)
    assert result["schema_version"] == "lancedb_identity_v1"
    assert result["lancedb_table_name"] == AUDITED["table_name"]
    assert result["snapshot_id"] == AUDITED["snapshot_id"]
    assert result["corpus_manifest_id"] == AUDITED["corpus_manifest_id"]
    assert result["index_manifest_id"] == AUDITED["index_manifest_id"]
    assert result["source_bundle_id"] == AUDITED["source_bundle_id"]
    assert result["probe_report_id"] == AUDITED["probe_report_id"]
    assert result["postbuild_readiness_id"] == AUDITED["postbuild_readiness_id"]
    assert result["embedding_model"] == "BAAI/bge-m3"
    assert result["embedding_dim"] == "1024"
    assert result["chunk_count"] == 295506
    assert result["vector_count"] == 295506
    assert result["index_manifest_path"] == str(manifest_path)


def test_lancedb_identity_falls_back_to_dir_manifest(tmp_path: Path):
    lancedb_dir, _ = _lancedb_fixture(tmp_path)
    (tmp_path / "index_manifest.json").rename(lancedb_dir / "index_manifest.json")
    result = lancedb_identity(lancedb_dir)
    assert result["index_manifest_id"] == AUDITED["index_manifest_id"]
    assert result["embedding_model"] == "BAAI/bge-m3"


def test_missing_db_returns_none_facts(tmp_path: Path):
    result = snapshot_identity(tmp_path / "does-not-exist.db")
    assert result["corpus_manifest_id"] is None
    assert result["snapshot_id"] is None
    assert result["corpus_served_chunks_count"] is None
    assert result["articles_count"] is None


def test_none_db_path_returns_none_facts():
    result = snapshot_identity(None)
    assert result["corpus_manifest_id"] is None
    assert result["snapshot_id"] is None


def test_missing_lancedb_dir_returns_none_facts(tmp_path: Path):
    result = lancedb_identity(tmp_path / "missing")
    assert result["lancedb_table_name"] is None
    assert result["index_manifest_id"] is None
    assert result["embedding_model"] is None


def test_lancedb_identity_conflict_fails_closed(tmp_path: Path):
    lancedb_dir, manifest_path = _lancedb_fixture(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["snapshot_id"] = "a" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaselineIdentityConflictError) as excinfo:
        lancedb_identity(lancedb_dir, index_manifest_path=manifest_path)
    assert excinfo.value.field == "snapshot_id"
    assert set(excinfo.value.sources) == {"active_generation", "index_manifest"}
    assert "a" * 64 not in str(excinfo.value)


def test_relative_data_paths_resolve_against_repo_root_not_cwd(tmp_path, monkeypatch):
    fake_root = tmp_path / "repo"
    (fake_root / ".git").mkdir(parents=True)
    (fake_root / "packages" / "eval").mkdir(parents=True)
    data_dir = fake_root / "fixtures"
    data_dir.mkdir()
    lancedb_dir, manifest_path = _lancedb_fixture(data_dir)
    db_path = data_dir / "snapshot.db"
    db_path.write_bytes(b"")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    result = lancedb_identity(
        os.path.relpath(lancedb_dir, fake_root),
        index_manifest_path=os.path.relpath(manifest_path, fake_root),
        repo_root=fake_root,
    )
    assert result["snapshot_id"] == AUDITED["snapshot_id"]
    assert result["index_manifest_path"] == str(manifest_path)


@pytest.mark.parametrize("payload", ["not-json", "[]"])
def test_present_malformed_lancedb_pointer_fails_closed(tmp_path: Path, payload: str):
    lancedb_dir, manifest_path = _lancedb_fixture(tmp_path)
    (lancedb_dir / "active_generation.json").write_text(payload, encoding="utf-8")
    with pytest.raises(BaselineIdentitySourceError, match="active_generation"):
        lancedb_identity(lancedb_dir, index_manifest_path=manifest_path)


def test_present_malformed_lancedb_manifest_fails_closed(tmp_path: Path):
    lancedb_dir, manifest_path = _lancedb_fixture(tmp_path)
    manifest_path.write_text("{", encoding="utf-8")
    with pytest.raises(BaselineIdentitySourceError, match="index_manifest"):
        lancedb_identity(lancedb_dir, index_manifest_path=manifest_path)
