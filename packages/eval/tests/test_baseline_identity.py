"""M1-1: sealed V1.1 baseline identity tuple contract tests.

The identity tuple freezes the audited code baseline, the immutable
integration/docs commit SHA, package versions, and every recoverable
data/index/model identity into one reconciliation-fail-closed structure.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from catalyst_eval.baseline.identity import (
    BaselineIdentity,
    BaselineIdentityConflictError,
    BaselineIdentitySourceError,
    PackageVersions,
    is_app_default_db,
    reconcile_identity,
    repository_root,
    sealed_identity_tuple,
)

AUDITED_CODE_BASELINE = "621375bc395e1dee644b335b2abe541a15d62fee"
AUDITED_INTEGRATION_SHA = "549d5ffad0d995b7ce2767461109e19fb0ca5384"

AUDITED_POINTER = {
    "snapshot_id": "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
    "corpus_manifest_id": "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
    "index_manifest_id": "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083",
    "source_bundle_id": "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
    "probe_report_id": "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
    "postbuild_readiness_id": "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
    "table_name": "chunks__staging__b3761f4b943542a8",
    "model_name": "BAAI/bge-m3",
    "dimension": 1024,
}

EXPECTED_FIELDS = {
    "code_git_sha",
    "integration_commit_sha",
    "package_versions",
    "snapshot_id",
    "corpus_manifest_id",
    "index_manifest_id",
    "source_bundle_id",
    "probe_report_id",
    "postbuild_readiness_id",
    "lancedb_table_name",
    "embedding_model",
    "embedding_dim",
    "reranker_model",
    "default_model",
    "app_default_db_marked_non_comparable",
}


def _write_pointer_files(tmp_path: Path) -> tuple[Path, Path]:
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    (lancedb_dir / "active_generation.json").write_text(
        json.dumps(
            {
                "schema_version": "active_generation_v1",
                "chunk_count": 295506,
                "snapshot_id": AUDITED_POINTER["snapshot_id"],
                "corpus_manifest_id": AUDITED_POINTER["corpus_manifest_id"],
                "index_manifest_id": AUDITED_POINTER["index_manifest_id"],
                "source_bundle_id": AUDITED_POINTER["source_bundle_id"],
                "table_name": AUDITED_POINTER["table_name"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "index_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "index_manifest_id": AUDITED_POINTER["index_manifest_id"],
                "snapshot_id": AUDITED_POINTER["snapshot_id"],
                "corpus_manifest_id": AUDITED_POINTER["corpus_manifest_id"],
                "source_bundle_id": AUDITED_POINTER["source_bundle_id"],
                "probe_report_id": AUDITED_POINTER["probe_report_id"],
                "postbuild_readiness_id": AUDITED_POINTER["postbuild_readiness_id"],
                "model_name": AUDITED_POINTER["model_name"],
                "dimension": AUDITED_POINTER["dimension"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return lancedb_dir, manifest_path


def _full_env(tmp_path: Path, **overrides) -> dict[str, str]:
    lancedb_dir, manifest_path = _write_pointer_files(tmp_path)
    env = {
        "CATALYST_INTEGRATION_COMMIT_SHA": AUDITED_INTEGRATION_SHA,
        "CATALYST_DB_PATH": ".local/live_runtime.db",
        "CATALYST_LANCEDB_DIR": str(lancedb_dir),
        "CATALYST_INDEX_MANIFEST_PATH": str(manifest_path),
        "CATALYST_CORPUS_MANIFEST_ID": AUDITED_POINTER["corpus_manifest_id"],
        "CATALYST_INDEX_MANIFEST_ID": AUDITED_POINTER["index_manifest_id"],
        "CATALYST_SOURCE_BUNDLE_ID": AUDITED_POINTER["source_bundle_id"],
        "CATALYST_SNAPSHOT_ID": AUDITED_POINTER["snapshot_id"],
        "CATALYST_PROBE_REPORT_ID": AUDITED_POINTER["probe_report_id"],
        "CATALYST_POSTBUILD_READINESS_ID": AUDITED_POINTER["postbuild_readiness_id"],
        "CATALYST_DEFAULT_MODEL": "gemini-2.5-flash-nothink",
    }
    env.update(overrides)
    return env


def _identity(tmp_path: Path, monkeypatch, **overrides):
    identity_module = pytest.importorskip("catalyst_eval.baseline.identity")
    real_run_git = identity_module._run_git

    def guard_head(repo_root, *args):
        if args and args[0] == "rev-parse" and args[-1] == "HEAD":
            raise AssertionError("sealed identity must never resolve git HEAD")
        return real_run_git(repo_root, *args)

    monkeypatch.setattr(identity_module, "_run_git", guard_head)
    return sealed_identity_tuple(env=_full_env(tmp_path, **overrides))


def test_identity_contains_exactly_expected_fields(tmp_path, monkeypatch):
    identity = _identity(tmp_path, monkeypatch)
    assert {f.name for f in dataclasses.fields(BaselineIdentity)} == EXPECTED_FIELDS
    assert {f.name for f in dataclasses.fields(identity)} == EXPECTED_FIELDS


def test_package_versions_is_frozen_structured_value_not_json_string(tmp_path, monkeypatch):
    identity = _identity(tmp_path, monkeypatch)
    versions = identity.package_versions
    assert isinstance(versions, PackageVersions)
    assert not isinstance(versions, str)
    assert dataclasses.is_dataclass(versions)
    assert versions.__dataclass_params__.frozen
    assert {f.name for f in dataclasses.fields(PackageVersions)} == {
        "data_core", "agents", "app", "eval",
    }
    for value in (versions.data_core, versions.agents, versions.app, versions.eval):
        assert isinstance(value, str) and value
    with pytest.raises(dataclasses.FrozenInstanceError):
        versions.data_core = "changed"  # type: ignore[misc]


def test_integration_commit_sha_comes_from_env_not_head(tmp_path, monkeypatch):
    identity = _identity(tmp_path, monkeypatch)
    assert identity.integration_commit_sha == AUDITED_INTEGRATION_SHA
    assert identity.code_git_sha == AUDITED_CODE_BASELINE


def test_audited_pointer_identity_is_reconciled(tmp_path, monkeypatch):
    identity = _identity(tmp_path, monkeypatch)
    assert identity.snapshot_id == AUDITED_POINTER["snapshot_id"]
    assert identity.corpus_manifest_id == AUDITED_POINTER["corpus_manifest_id"]
    assert identity.index_manifest_id == AUDITED_POINTER["index_manifest_id"]
    assert identity.source_bundle_id == AUDITED_POINTER["source_bundle_id"]
    assert identity.probe_report_id == AUDITED_POINTER["probe_report_id"]
    assert identity.postbuild_readiness_id == AUDITED_POINTER["postbuild_readiness_id"]
    assert identity.lancedb_table_name == AUDITED_POINTER["table_name"]
    assert identity.embedding_model == AUDITED_POINTER["model_name"]
    assert identity.embedding_dim == "1024"
    assert identity.default_model == "gemini-2.5-flash-nothink"
    assert identity.app_default_db_marked_non_comparable is True


def test_missing_integration_sha_fails_closed(tmp_path, monkeypatch):
    identity_module = pytest.importorskip("catalyst_eval.baseline.identity")
    monkeypatch.setattr(identity_module, "_run_git", identity_module._run_git)
    env = _full_env(tmp_path)
    env.pop("CATALYST_INTEGRATION_COMMIT_SHA")
    with pytest.raises(ValueError):
        sealed_identity_tuple(env=env)


def test_malformed_integration_sha_fails_closed(tmp_path, monkeypatch):
    identity_module = pytest.importorskip("catalyst_eval.baseline.identity")
    monkeypatch.setattr(identity_module, "_run_git", identity_module._run_git)
    with pytest.raises(ValueError):
        sealed_identity_tuple(env=_full_env(tmp_path, CATALYST_INTEGRATION_COMMIT_SHA="not-a-sha"))


def test_nonexistent_integration_sha_fails_closed(tmp_path, monkeypatch):
    identity_module = pytest.importorskip("catalyst_eval.baseline.identity")
    monkeypatch.setattr(identity_module, "_run_git", identity_module._run_git)
    with pytest.raises(ValueError):
        sealed_identity_tuple(
            env=_full_env(tmp_path, CATALYST_INTEGRATION_COMMIT_SHA="f" * 40)
        )


def test_wrong_parent_integration_sha_fails_closed(tmp_path, monkeypatch):
    identity_module = pytest.importorskip("catalyst_eval.baseline.identity")
    monkeypatch.setattr(identity_module, "_run_git", identity_module._run_git)
    # f9e4cdb is a real commit whose parent is the seal, not the audited baseline.
    with pytest.raises(ValueError):
        sealed_identity_tuple(
            env=_full_env(tmp_path, CATALYST_INTEGRATION_COMMIT_SHA="f9e4cdb289a524de486a862d89dfa743d5c9b7f9")
        )


def test_non_docs_drift_integration_sha_fails_closed(tmp_path, monkeypatch):
    identity_module = pytest.importorskip("catalyst_eval.baseline.identity")
    real_run_git = identity_module._run_git

    def fake_diff(repo_root, *args):
        if args and args[0] == "diff":
            return "packages/data-core/catalyst_data/config.py | 1 +"
        return real_run_git(repo_root, *args)

    monkeypatch.setattr(identity_module, "_run_git", fake_diff)
    with pytest.raises(ValueError):
        sealed_identity_tuple(env=_full_env(tmp_path))


def test_identity_conflict_fails_closed_never_precedence(tmp_path, monkeypatch):
    identity_module = pytest.importorskip("catalyst_eval.baseline.identity")
    monkeypatch.setattr(identity_module, "_run_git", identity_module._run_git)
    env = _full_env(tmp_path, CATALYST_SNAPSHOT_ID="a" * 64)
    with pytest.raises(BaselineIdentityConflictError) as excinfo:
        sealed_identity_tuple(env=env)
    message = str(excinfo.value)
    assert "snapshot_id" in message
    assert "a" * 64 not in message
    assert AUDITED_POINTER["snapshot_id"] not in message


def test_missing_non_sealed_identity_is_none_and_non_comparable(tmp_path, monkeypatch):
    identity_module = pytest.importorskip("catalyst_eval.baseline.identity")
    monkeypatch.setattr(identity_module, "_run_git", identity_module._run_git)
    env = {
        "CATALYST_INTEGRATION_COMMIT_SHA": AUDITED_INTEGRATION_SHA,
        "CATALYST_DB_PATH": "",
    }
    identity = sealed_identity_tuple(env=env)
    for field in (
        "snapshot_id", "corpus_manifest_id", "index_manifest_id", "source_bundle_id",
        "probe_report_id", "postbuild_readiness_id", "lancedb_table_name",
        "embedding_model", "embedding_dim", "reranker_model", "default_model",
    ):
        assert getattr(identity, field) is None
    assert identity.app_default_db_marked_non_comparable is True
    for value in (identity.package_versions.data_core, identity.package_versions.agents,
                  identity.package_versions.app, identity.package_versions.eval):
        assert isinstance(value, str) and value


def test_reconcile_identity_zero_present_returns_none():
    assert reconcile_identity("snapshot_id", {"env": None, "pointer": None}) is None


def test_reconcile_identity_one_unique_value_accepted():
    assert reconcile_identity("snapshot_id", {"env": "abc", "pointer": None}) == "abc"
    assert reconcile_identity("snapshot_id", {"env": None, "pointer": "abc"}) == "abc"


def test_reconcile_identity_multiple_equal_values_accepted():
    assert reconcile_identity("snapshot_id", {"env": "abc", "pointer": "abc"}) == "abc"


def test_reconcile_identity_distinct_values_raise_without_values():
    with pytest.raises(BaselineIdentityConflictError) as excinfo:
        reconcile_identity("snapshot_id", {"env": "abc", "pointer": "def"})
    message = str(excinfo.value)
    assert "snapshot_id" in message
    assert "env" in message and "pointer" in message
    assert "abc" not in message and "def" not in message


def test_repository_root_invariant_under_cwd(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    root = repository_root()
    assert root == Path(__file__).resolve().parents[3]
    assert (root / "packages" / "eval").is_dir()
    assert (root / ".git").exists()


def test_repository_root_injected_path_fails_closed(tmp_path):
    with pytest.raises(ValueError):
        repository_root(start=tmp_path / "not-a-repo")


def test_repository_root_rejects_ambiguous_nested_repositories(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    for root in (outer, inner):
        (root / ".git").mkdir(parents=True)
        (root / "packages" / "eval").mkdir(parents=True)
    with pytest.raises(ValueError, match="ambiguous"):
        repository_root(start=inner / "packages" / "eval")


def test_identity_paths_resolve_against_repo_root_not_cwd(tmp_path, monkeypatch):
    root = repository_root()
    lancedb_dir, manifest_path = _write_pointer_files(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    env = _full_env(other)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    env["CATALYST_LANCEDB_DIR"] = os.path.relpath(lancedb_dir, root)
    env["CATALYST_INDEX_MANIFEST_PATH"] = os.path.relpath(manifest_path, root)
    identity = sealed_identity_tuple(env=env, repo_root=root)
    assert identity.snapshot_id == AUDITED_POINTER["snapshot_id"]
    assert identity.index_manifest_id == AUDITED_POINTER["index_manifest_id"]


@pytest.mark.parametrize("source", ["pointer", "manifest"])
def test_present_malformed_identity_json_fails_closed(tmp_path, source):
    lancedb_dir, manifest_path = _write_pointer_files(tmp_path)
    target = (
        lancedb_dir / "active_generation.json"
        if source == "pointer"
        else manifest_path
    )
    target.write_text("not-json", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    env = _full_env(other)
    env["CATALYST_LANCEDB_DIR"] = str(lancedb_dir)
    env["CATALYST_INDEX_MANIFEST_PATH"] = str(manifest_path)
    with pytest.raises(BaselineIdentitySourceError, match=source):
        sealed_identity_tuple(env=env)


def test_present_non_object_identity_manifest_fails_closed(tmp_path):
    lancedb_dir, manifest_path = _write_pointer_files(tmp_path)
    manifest_path.write_text("[]", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    env = _full_env(other)
    env["CATALYST_LANCEDB_DIR"] = str(lancedb_dir)
    env["CATALYST_INDEX_MANIFEST_PATH"] = str(manifest_path)
    with pytest.raises(BaselineIdentitySourceError, match="manifest"):
        sealed_identity_tuple(env=env)


def test_is_app_default_db_resolves_relative_against_repo_root(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert is_app_default_db(".local/live_runtime.db") is True
    assert is_app_default_db("some/other.db") is False
    assert is_app_default_db(str(outside / ".local" / "live_runtime.db")) is False
    root = repository_root()
    assert is_app_default_db(str(root / ".local" / "live_runtime.db")) is True


def test_identity_dataclass_is_frozen(tmp_path, monkeypatch):
    identity = _identity(tmp_path, monkeypatch)
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.snapshot_id = "changed"  # type: ignore[misc]
