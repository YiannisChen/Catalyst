"""M1-2: sealed V1.1 baseline report writer contract tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from catalyst_eval.baseline.identity import sealed_identity_tuple
from catalyst_eval.baseline.report import (
    BaselineReportConflictError,
    write_baseline_report,
)

AUDITED_INTEGRATION_SHA = "549d5ffad0d995b7ce2767461109e19fb0ca5384"
SNAPSHOT_ID = "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49"
CORPUS_ID = "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc"
INDEX_ID = "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083"
SOURCE_ID = "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2"
PROBE_ID = "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23"
POSTBUILD_ID = "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b"
TABLE_NAME = "chunks__staging__b3761f4b943542a8"
FIXED_GENERATED_AT = "2026-08-19T00:00:00+00:00"


def _full_identity(tmp_path, monkeypatch):
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    (lancedb_dir / "active_generation.json").write_text(
        json.dumps({
            "schema_version": "active_generation_v1",
            "chunk_count": 295506,
            "snapshot_id": SNAPSHOT_ID,
            "corpus_manifest_id": CORPUS_ID,
            "index_manifest_id": INDEX_ID,
            "source_bundle_id": SOURCE_ID,
            "table_name": TABLE_NAME,
        }, sort_keys=True),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "index_manifest.json"
    manifest_path.write_text(
        json.dumps({
            "schema_version": "1.0.0",
            "index_manifest_id": INDEX_ID,
            "snapshot_id": SNAPSHOT_ID,
            "corpus_manifest_id": CORPUS_ID,
            "source_bundle_id": SOURCE_ID,
            "probe_report_id": PROBE_ID,
            "postbuild_readiness_id": POSTBUILD_ID,
            "model_name": "BAAI/bge-m3",
            "dimension": 1024,
        }, sort_keys=True),
        encoding="utf-8",
    )
    identity_module = pytest.importorskip("catalyst_eval.baseline.identity")
    real_run_git = identity_module._run_git
    monkeypatch.setattr(identity_module, "_run_git", real_run_git)
    return sealed_identity_tuple(env={
        "CATALYST_INTEGRATION_COMMIT_SHA": AUDITED_INTEGRATION_SHA,
        "CATALYST_DB_PATH": ".local/live_runtime.db",
        "CATALYST_LANCEDB_DIR": str(lancedb_dir),
        "CATALYST_INDEX_MANIFEST_PATH": str(manifest_path),
        "CATALYST_CORPUS_MANIFEST_ID": CORPUS_ID,
        "CATALYST_INDEX_MANIFEST_ID": INDEX_ID,
        "CATALYST_SOURCE_BUNDLE_ID": SOURCE_ID,
        "CATALYST_SNAPSHOT_ID": SNAPSHOT_ID,
        "CATALYST_PROBE_REPORT_ID": PROBE_ID,
        "CATALYST_POSTBUILD_READINESS_ID": POSTBUILD_ID,
        "CATALYST_DEFAULT_MODEL": "gemini-2.5-flash-nothink",
    })


def _matching_run() -> dict:
    return {
        "run_id": "run-1",
        "snapshot_id": SNAPSHOT_ID,
        "corpus_manifest_id": CORPUS_ID,
        "index_manifest_id": INDEX_ID,
        "source_bundle_id": SOURCE_ID,
        "probe_report_id": PROBE_ID,
        "postbuild_readiness_id": POSTBUILD_ID,
        "lancedb_table_name": TABLE_NAME,
        "embedding_model": "BAAI/bge-m3",
        "embedding_dim": "1024",
    }


def test_write_baseline_report_creates_canonical_report(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    identity = _full_identity(tmp_path, monkeypatch)
    target = write_baseline_report(
        report_dir, identity, [_matching_run()], generated_at=FIXED_GENERATED_AT
    )
    assert target == report_dir / f"v1_1_baseline_{identity.code_git_sha[:8]}.json"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert text.endswith("\n")
    payload = json.loads(text)
    assert payload["schema_version"] == "baseline_v1"
    assert payload["identity"]["code_git_sha"] == identity.code_git_sha
    assert payload["identity"]["integration_commit_sha"] == AUDITED_INTEGRATION_SHA
    assert isinstance(payload["identity"]["package_versions"], dict)
    assert set(payload["identity"]["package_versions"]) == {"data_core", "agents", "app", "eval"}
    assert payload["runs"][0]["run_id"] == "run-1"
    comparability = payload["comparability"]
    assert comparability["data_identity_comparable"] is True
    assert comparability["model_identity_comparable"] is True
    assert comparability["app_default_db_non_comparable"] is True
    assert comparability["generated_at"] == FIXED_GENERATED_AT
    assert re.fullmatch(r"[0-9a-f]{40}", comparability["git_revision"])
    assert comparability["git_revision"] != identity.integration_commit_sha


def test_run_with_differing_data_identity_is_non_comparable(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    identity = _full_identity(tmp_path, monkeypatch)
    run = _matching_run()
    run["snapshot_id"] = "a" * 64
    target = write_baseline_report(
        report_dir, identity, [run], generated_at=FIXED_GENERATED_AT
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["comparability"]["data_identity_comparable"] is False


def test_missing_identity_fields_are_non_comparable(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    identity_module = pytest.importorskip("catalyst_eval.baseline.identity")
    monkeypatch.setattr(identity_module, "_run_git", identity_module._run_git)
    identity = sealed_identity_tuple(env={
        "CATALYST_INTEGRATION_COMMIT_SHA": AUDITED_INTEGRATION_SHA,
    })
    target = write_baseline_report(report_dir, identity, [], generated_at=FIXED_GENERATED_AT)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["comparability"]["data_identity_comparable"] is False
    assert payload["comparability"]["model_identity_comparable"] is False


def test_no_secret_like_values_appear_in_report_text(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    identity = _full_identity(tmp_path, monkeypatch)
    run = _matching_run()
    run["api_key"] = "sk-test-1234567890"
    run["provider_secret"] = "hidden"
    target = write_baseline_report(
        report_dir, identity, [run], generated_at=FIXED_GENERATED_AT
    )
    text = target.read_text(encoding="utf-8")
    assert "api_key" not in text
    assert "sk-test-1234567890" not in text
    assert "provider_secret" not in text


def test_absent_target_is_created_atomically_without_temp_leftovers(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    identity = _full_identity(tmp_path, monkeypatch)
    write_baseline_report(report_dir, identity, [_matching_run()], generated_at=FIXED_GENERATED_AT)
    assert list(report_dir.glob("*.tmp")) == []
    assert list(report_dir.glob(".*.tmp")) == []


def test_existing_identical_report_is_idempotent_success(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    identity = _full_identity(tmp_path, monkeypatch)
    first = write_baseline_report(
        report_dir, identity, [_matching_run()], generated_at=FIXED_GENERATED_AT
    )
    before = first.read_bytes()
    second = write_baseline_report(
        report_dir, identity, [_matching_run()], generated_at=FIXED_GENERATED_AT
    )
    assert second == first
    assert first.read_bytes() == before


def test_existing_different_report_raises_conflict_unchanged(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    identity = _full_identity(tmp_path, monkeypatch)
    first = write_baseline_report(
        report_dir, identity, [_matching_run()], generated_at=FIXED_GENERATED_AT
    )
    before = first.read_bytes()
    other_run = _matching_run()
    other_run["run_id"] = "run-2"
    with pytest.raises(BaselineReportConflictError):
        write_baseline_report(report_dir, identity, [other_run], generated_at=FIXED_GENERATED_AT)
    assert first.read_bytes() == before
