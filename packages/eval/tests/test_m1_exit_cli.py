"""M1 exit operator CLI orchestration contract tests (FAST only)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "eval"
    / "scripts"
    / "run_v1_1_m1_exit.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("run_v1_1_m1_exit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _argv(tmp_path: Path) -> list[str]:
    paths = {
        "frozen": tmp_path / "frozen.db",
        "runtime": tmp_path / "runtime.db",
        "lance": tmp_path / "lancedb",
        "manifest": tmp_path / "index_manifest.json",
        "output": tmp_path / "evidence",
        "report": tmp_path / "reports",
    }
    return [
        "--expected-head", "a" * 40,
        "--integration-commit-sha", "b" * 40,
        "--frozen-db", str(paths["frozen"]),
        "--runtime-db", str(paths["runtime"]),
        "--lancedb-dir", str(paths["lance"]),
        "--index-manifest", str(paths["manifest"]),
        "--output-root", str(paths["output"]),
        "--report-dir", str(paths["report"]),
        "--t4-run-id", "m1-t4",
        "--four-arm-run-id", "m1-four",
        "--user-smoke-run-id", "m1-smoke",
    ]


def _bound_row(gate_kind: str, run_id: str, tmp_path: Path) -> dict:
    evidence = tmp_path / f"{gate_kind}-evidence"
    return {
        "gate_kind": gate_kind,
        "run_id": run_id,
        "evidence_ref": str(evidence),
        "evidence_meta_ref": str(evidence / "meta.json"),
        "evidence_meta_sha256": "1" * 64,
        "success_token": (
            "FOUR_ARM_E2E_OK" if gate_kind == "four_arm" else "USER_SMOKE_OK"
        ),
        "success_token_sha256": "2" * 64,
        "t4_evidence_ref": str(tmp_path / "evidence" / "m1-t4"),
        "t4_meta_sha256": "3" * 64,
        "promoted_env_recovered": False,
    }


def _result(tmp_path: Path, *, ok: bool = True, leak: bool = False):
    identity = SimpleNamespace(code_git_sha="6" * 40)
    rows = [
        _bound_row("four_arm", "m1-four", tmp_path),
        _bound_row("user_smoke", "m1-smoke", tmp_path),
    ]
    if leak:
        rows[0]["oracle_answer"] = "golden answer must never be sealed"
    return SimpleNamespace(
        identity=identity,
        ok=ok,
        comparable=False,
        promoted_env_recovered=False,
        runs=tuple(rows),
    )


def _install_success_seams(module, tmp_path, monkeypatch, *, result=None):
    calls: list[str] = []
    monkeypatch.setattr(module, "_preflight_environment", lambda args, env: calls.append("preflight"))

    def run_t4(args):
        calls.append("t4")
        return Path(args.output_root) / args.t4_run_id

    monkeypatch.setattr(module, "_run_t4", run_t4)
    monkeypatch.setattr(
        module,
        "run_baseline_repro",
        lambda **kwargs: calls.append("gates") or (result or _result(tmp_path)),
    )
    monkeypatch.setattr(
        module,
        "_preview_report",
        lambda result, generated_at, git_revision: {
            "runs": list(result.runs),
            "comparability": {
                "data_identity_comparable": False,
                "model_identity_comparable": False,
                "promoted_env_recovered": False,
                "git_revision": git_revision,
            },
        },
    )

    def write_report(report_dir, identity, runs, **kwargs):
        calls.append("write")
        target = Path(report_dir) / "v1_1_baseline_66666666.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(module._LAST_PREVIEW), encoding="utf-8")
        return target

    monkeypatch.setattr(module, "write_baseline_report", write_report)
    return calls


def test_exit_cli_success_orders_t4_gates_and_single_report_write(
    tmp_path, monkeypatch, capsys
):
    module = _load_script()
    calls = _install_success_seams(module, tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-print-this-secret")
    rc = module.main(_argv(tmp_path))
    output = capsys.readouterr().out
    assert rc == 0
    assert calls == ["preflight", "t4", "gates", "write"]
    assert "never-print-this-secret" not in output
    assert json.loads(output)["ok"] is True


def test_t4_failure_short_circuits_gates_and_report(tmp_path, monkeypatch):
    module = _load_script()
    monkeypatch.setattr(module, "_preflight_environment", lambda args, env: None)
    monkeypatch.setattr(
        module,
        "_run_t4",
        lambda args: (_ for _ in ()).throw(module.ExitGateError("t4_failed")),
    )
    monkeypatch.setattr(
        module,
        "run_baseline_repro",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("gates must not run")),
    )
    monkeypatch.setattr(
        module,
        "write_baseline_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not write")),
    )
    assert module.main(_argv(tmp_path)) == 2


def test_gate_failure_does_not_write_report(tmp_path, monkeypatch):
    module = _load_script()
    calls = _install_success_seams(
        module, tmp_path, monkeypatch, result=_result(tmp_path, ok=False)
    )
    assert module.main(_argv(tmp_path)) == 2
    assert calls == ["preflight", "t4", "gates"]


def test_leakage_fails_before_report_write(tmp_path, monkeypatch):
    module = _load_script()
    result = _result(tmp_path, leak=True)
    calls = _install_success_seams(module, tmp_path, monkeypatch, result=result)
    monkeypatch.setattr(
        module,
        "_preview_report",
        lambda result, generated_at, git_revision: {
            "runs": list(result.runs),
            "comparability": {
                "data_identity_comparable": False,
                "model_identity_comparable": False,
                "promoted_env_recovered": False,
            },
        },
    )
    assert module.main(_argv(tmp_path)) == 2
    assert calls == ["preflight", "t4", "gates"]
    assert not (tmp_path / "reports").exists()

def test_exit_cli_stdout_is_secret_free_single_object(tmp_path, monkeypatch, capsys):
    module = _load_script()
    _install_success_seams(module, tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-dscodex-secret-free-check-12345")
    rc = module.main(_argv(tmp_path))
    output = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(output)  # must be exactly one JSON object
    assert isinstance(payload, dict)
    assert payload["ok"] is True
    assert "sk-dscodex-secret-free-check-12345" not in output
    secret_key = re.compile(
        r"(?i)(api[_-]?key|token|secret|password|credential|authorization)"
    )
    assert [key for key in payload if secret_key.search(key)] == []


def test_preflight_environment_fast_success(tmp_path, monkeypatch):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: "a" * 40)
    monkeypatch.setattr(module, "_git_status_porcelain", lambda repo: "")
    monkeypatch.setattr(
        module, "_validate_runtime_derivative", lambda *args, **kwargs: None
    )
    args = module._parse_args(_argv(tmp_path))
    env = {"DEEPSEEK_API_KEY": "presence-only"}
    module._preflight_environment(args, env)
    assert env["CATALYST_INTEGRATION_COMMIT_SHA"] == "b" * 40


def test_preflight_environment_rejects_head_mismatch(tmp_path, monkeypatch):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: "c" * 40)
    args = module._parse_args(_argv(tmp_path))
    with pytest.raises(ValueError, match="expected-head"):
        module._preflight_environment(args, {"DEEPSEEK_API_KEY": "presence-only"})


def test_preflight_environment_requires_deepseek_key(tmp_path, monkeypatch):
    module = _load_script()
    args = module._parse_args(_argv(tmp_path))
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        module._preflight_environment(args, {})


def test_preflight_environment_rejects_app_default_runtime_db(tmp_path, monkeypatch):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: "a" * 40)
    monkeypatch.setattr(module, "_git_status_porcelain", lambda repo: "")
    monkeypatch.setattr(
        module, "_validate_runtime_derivative", lambda *args, **kwargs: None
    )
    argv = _argv(tmp_path)
    argv[argv.index("--runtime-db") + 1] = str(
        Path(module.REPO_ROOT) / ".local" / "live_runtime.db"
    )
    args = module._parse_args(argv)
    with pytest.raises(ValueError, match="live_runtime"):
        module._preflight_environment(args, {"DEEPSEEK_API_KEY": "presence-only"})


def test_exit_cli_binds_fresh_t4_case_pack_for_gates(tmp_path, monkeypatch):
    """The operator CLI must pass the fresh T4 case_pack.jsonl to the gates
    via CATALYST_BASELINE_CASE_PACK instead of depending on the gitignored
    repo-default case pack path."""
    module = _load_script()
    captured: dict = {}
    monkeypatch.setattr(module, "_preflight_environment", lambda args, env: None)

    def run_t4(args):
        evidence_dir = Path(args.output_root) / args.t4_run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "meta.json").write_text("{}", encoding="utf-8")
        (evidence_dir / "case_pack.jsonl").write_text("", encoding="utf-8")
        return evidence_dir

    monkeypatch.setattr(module, "_run_t4", run_t4)
    monkeypatch.setattr(
        module,
        "run_baseline_repro",
        lambda **kwargs: captured.update(kwargs) or _result(tmp_path),
    )
    monkeypatch.setattr(
        module,
        "_preview_report",
        lambda result, generated_at, git_revision: {"runs": []},
    )

    def write_report(report_dir, identity, runs, **kwargs):
        target = Path(report_dir) / "v1_1_baseline_66666666.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(module._LAST_PREVIEW), encoding="utf-8")
        return target

    monkeypatch.setattr(module, "write_baseline_report", write_report)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-print-this-secret")

    rc = module.main(_argv(tmp_path))

    assert rc == 0
    gate_env = captured["env"]
    assert gate_env["CATALYST_BASELINE_CASE_PACK"] == str(
        tmp_path / "evidence" / "m1-t4" / "case_pack.jsonl"
    )
