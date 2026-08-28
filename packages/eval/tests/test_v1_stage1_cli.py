"""M7-8: Stage-1 CLI {prepare,execute,audit,report,all}.

prepare performs no provider calls. execute requires explicit
--max-provider-calls/--max-cost-usd, refuses an existing incompatible
ledger, and resumes only identity-valid terminal cases. audit validates a
human-provided audit file and cannot author human decisions. report writes
only from sealed inputs. Exit codes: 0 success, 1 gate failure, 2
contract/operator/configuration error.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from catalyst_eval.v1_1.execution_ledger import ExecutionLedger, LedgerRow
from catalyst_eval.v1_1.manifest import EvalManifest

from tests.v1_1_fixtures import make_dataset_manifest, make_stage1_cases, make_stratification


@pytest.fixture()
def cli_env(tmp_path) -> dict:
    from tests.v1_1_fixtures import build_fixture_eval_manifest

    builder_manifest_cli = build_fixture_eval_manifest()
    rows = make_stage1_cases()
    manifest = make_dataset_manifest(rows, make_stratification(rows))
    dataset_path = tmp_path / "v1_1_stage1_cases.jsonl"
    dataset_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )
    manifest_path = tmp_path / "dataset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    strat_path = tmp_path / "stratification.json"
    strat_path.write_text(
        json.dumps(make_stratification(rows), sort_keys=True), encoding="utf-8"
    )
    return {
        "tmp_path": tmp_path,
        "dataset_path": dataset_path,
        "manifest_path": manifest_path,
        "strat_path": strat_path,
        "eval_manifest": builder_manifest_cli,
    }


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_v1_1_stage1.py"
    spec = importlib.util.spec_from_file_location("run_v1_1_stage1", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cli_prepare_makes_no_provider_calls(cli_env, monkeypatch):
    module = _load_cli()

    calls: list[str] = []

    def boom_adapter(case_id):
        calls.append(case_id)
        raise AssertionError("prepare must never call a provider")

    monkeypatch.setattr(module, "_default_runner_adapter_factory", boom_adapter)
    out_dir = cli_env["tmp_path"] / "out"
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(cli_env["manifest_path"]),
        "--stratification", str(cli_env["strat_path"]),
        "--output-dir", str(out_dir),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
    ])
    assert exit_code == 0
    assert calls == []
    assert (out_dir / "prepare_summary.json").is_file()


def test_cli_execute_requires_ceilings(cli_env, monkeypatch):
    module = _load_cli()

    out_dir = cli_env["tmp_path"] / "out"
    exit_code = module.main([
        "execute",
        "--output-dir", str(out_dir),
    ])
    assert exit_code == 2


def test_cli_execute_refuses_incompatible_ledger(cli_env, monkeypatch):
    module = _load_cli()

    out_dir = cli_env["tmp_path"] / "out"
    out_dir.mkdir(parents=True)
    ledger = ExecutionLedger(
        rows=(
            LedgerRow(
                eval_id="eval:other", case_id="v1f-001",
                run_manifest_id="m", run_manifest_hash="b" * 64,
                result_artifact_id="r", result_artifact_hash="c" * 64,
                terminal_status="COMPLETED", attempts=1, provider_calls=1,
                cost_usd=0.01, checksum="x",
            ),
        )
    )
    ledger.write(out_dir / "execution_ledger.jsonl")
    exit_code = module.main([
        "execute",
        "--output-dir", str(out_dir),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
    ])
    assert exit_code == 2


def test_cli_report_missing_audit_is_operator_stop(cli_env, monkeypatch):
    module = _load_cli()

    out_dir = cli_env["tmp_path"] / "out"
    exit_code = module.main([
        "report",
        "--output-dir", str(out_dir),
        "--audit", str(cli_env["tmp_path"] / "missing_audit.jsonl"),
        "--manifest-out", str(cli_env["tmp_path"] / "m.json"),
        "--json-out", str(cli_env["tmp_path"] / "r.json"),
        "--markdown-out", str(cli_env["tmp_path"] / "r.md"),
    ])
    assert exit_code == 2
