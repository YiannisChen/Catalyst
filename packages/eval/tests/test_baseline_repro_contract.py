"""M1-6: baseline reproduction wrapper contract tests.

CI never runs the EXPENSIVE four-arm/user-smoke gates; the gate seam is
monkeypatched so this suite exercises orchestration, identity fail-closed
behavior, comparability, and success-token collection only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalyst_eval.baseline.repro import BaselineReproResult, run_baseline_repro

AUDITED_INTEGRATION_SHA = "549d5ffad0d995b7ce2767461109e19fb0ca5384"
SNAPSHOT_ID = "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49"
CORPUS_ID = "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc"
INDEX_ID = "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083"
SOURCE_ID = "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2"
PROBE_ID = "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23"
POSTBUILD_ID = "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b"
TABLE_NAME = "chunks__staging__b3761f4b943542a8"


def _pointer_files(tmp_path: Path) -> tuple[Path, Path]:
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
            "vector_count": 295506,
        }, sort_keys=True),
        encoding="utf-8",
    )
    return lancedb_dir, manifest_path


def _full_env(tmp_path: Path) -> dict[str, str]:
    lancedb_dir, manifest_path = _pointer_files(tmp_path)
    return {
        "CATALYST_INTEGRATION_COMMIT_SHA": AUDITED_INTEGRATION_SHA,
        "CATALYST_DB_PATH": str(tmp_path / "runtime.db"),
        "CATALYST_LANCEDB_DIR": str(lancedb_dir),
        "CATALYST_INDEX_MANIFEST_PATH": str(manifest_path),
        "CATALYST_CORPUS_MANIFEST_ID": CORPUS_ID,
        "CATALYST_INDEX_MANIFEST_ID": INDEX_ID,
        "CATALYST_SOURCE_BUNDLE_ID": SOURCE_ID,
        "CATALYST_SNAPSHOT_ID": SNAPSHOT_ID,
        "CATALYST_PROBE_REPORT_ID": PROBE_ID,
        "CATALYST_POSTBUILD_READINESS_ID": POSTBUILD_ID,
        "CATALYST_DEFAULT_MODEL": "gemini-2.5-flash-nothink",
    }


def _ok_four_arm_gate(env, identity):
    return {
        "ok": True,
        "exit_code": 0,
        "token": "FOUR_ARM_E2E_OK",
        "run_row": {
            "run_id": "four-arm-1",
            "snapshot_id": SNAPSHOT_ID,
            "corpus_manifest_id": CORPUS_ID,
            "index_manifest_id": INDEX_ID,
            "source_bundle_id": SOURCE_ID,
            "probe_report_id": PROBE_ID,
            "postbuild_readiness_id": POSTBUILD_ID,
            "lancedb_table_name": TABLE_NAME,
            "embedding_model": "BAAI/bge-m3",
            "embedding_dim": "1024",
        },
    }


def _ok_user_smoke_gate(env, identity):
    return {
        "ok": True,
        "exit_code": 0,
        "evidence_dir": "/tmp/baseline_user_smoke_evidence",
        "run_row": {
            "run_id": "user-smoke-1",
            "snapshot_id": SNAPSHOT_ID,
            "corpus_manifest_id": CORPUS_ID,
            "index_manifest_id": INDEX_ID,
            "source_bundle_id": SOURCE_ID,
            "probe_report_id": PROBE_ID,
            "postbuild_readiness_id": POSTBUILD_ID,
            "lancedb_table_name": TABLE_NAME,
            "embedding_model": "BAAI/bge-m3",
            "embedding_dim": "1024",
            "default_model": "deepseek-v4-flash",
        },
    }


def test_missing_sealed_sha_fails_before_gates_run(tmp_path, monkeypatch):
    repro = pytest.importorskip("catalyst_eval.baseline.repro")

    def _boom_gate(env, identity):
        raise AssertionError("gate must not run before sealed identity validates")

    monkeypatch.setattr(repro, "_run_four_arm_gate", _boom_gate)
    monkeypatch.setattr(repro, "_run_user_smoke_gate", _boom_gate)
    env = _full_env(tmp_path)
    env.pop("CATALYST_INTEGRATION_COMMIT_SHA")
    with pytest.raises(ValueError):
        run_baseline_repro(four_arm=True, user_smoke=True, env=env)


def test_conflicting_sealed_identity_fails_before_gates_run(tmp_path, monkeypatch):
    repro = pytest.importorskip("catalyst_eval.baseline.repro")

    def _boom_gate(env, identity):
        raise AssertionError("gate must not run on conflicting identity")

    monkeypatch.setattr(repro, "_run_four_arm_gate", _boom_gate)
    env = _full_env(tmp_path, ) if False else _full_env(tmp_path)
    env["CATALYST_SNAPSHOT_ID"] = "a" * 64
    with pytest.raises(Exception):
        run_baseline_repro(four_arm=True, user_smoke=False, env=env)


def test_incomplete_identity_is_non_comparable_but_gates_complete(tmp_path, monkeypatch):
    repro = pytest.importorskip("catalyst_eval.baseline.repro")
    monkeypatch.setattr(repro, "_run_four_arm_gate", _ok_four_arm_gate)
    monkeypatch.setattr(repro, "_run_user_smoke_gate", _ok_user_smoke_gate)
    env = {"CATALYST_INTEGRATION_COMMIT_SHA": AUDITED_INTEGRATION_SHA}
    result = run_baseline_repro(four_arm=True, user_smoke=True, env=env)
    assert isinstance(result, BaselineReproResult)
    assert result.ok is True
    assert result.comparable is False
    assert result.missing_ids
    assert result.four_arm_token == "FOUR_ARM_E2E_OK"
    assert result.user_smoke_evidence_dir == "/tmp/baseline_user_smoke_evidence"
    assert len(result.runs) == 2


def test_full_identity_comparable_path_mocked(tmp_path, monkeypatch):
    repro = pytest.importorskip("catalyst_eval.baseline.repro")
    monkeypatch.setattr(repro, "_run_four_arm_gate", _ok_four_arm_gate)
    result = run_baseline_repro(four_arm=True, user_smoke=False, env=_full_env(tmp_path))
    assert result.ok is True
    assert result.comparable is True
    assert result.missing_ids == ()
    assert result.four_arm_token == "FOUR_ARM_E2E_OK"
    assert result.runs[0]["lancedb_table_name"] == TABLE_NAME


def test_gate_failure_is_reflected_in_ok(tmp_path, monkeypatch):
    repro = pytest.importorskip("catalyst_eval.baseline.repro")

    def _failing_four_arm(env, identity):
        return {"ok": False, "exit_code": 2, "token": None, "run_row": {}}

    monkeypatch.setattr(repro, "_run_four_arm_gate", _failing_four_arm)
    result = run_baseline_repro(four_arm=True, user_smoke=False, env=_full_env(tmp_path))
    assert result.ok is False
    assert result.four_arm_token is None


def test_env_sha_forwarded_unchanged_to_gates(tmp_path, monkeypatch):
    repro = pytest.importorskip("catalyst_eval.baseline.repro")
    captured: dict[str, str] = {}

    def _capturing_four_arm(env, identity):
        captured.update(env)
        return _ok_four_arm_gate(env, identity)

    monkeypatch.setattr(repro, "_run_four_arm_gate", _capturing_four_arm)
    env = _full_env(tmp_path)
    run_baseline_repro(four_arm=True, user_smoke=False, env=env)
    assert captured["CATALYST_INTEGRATION_COMMIT_SHA"] == AUDITED_INTEGRATION_SHA
    assert captured["CATALYST_SNAPSHOT_ID"] == SNAPSHOT_ID
