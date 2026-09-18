"""M7-8: Stage-1 CLI {prepare,execute,audit,report,all}.

prepare performs no provider calls. execute requires explicit
--max-provider-calls/--max-cost-usd, refuses an existing incompatible
ledger, and resumes only identity-valid terminal cases. audit validates a
human-provided audit file and cannot author human decisions. report writes
only from sealed inputs. Exit codes: 0 success, 1 gate failure, 2
contract/operator/configuration error.
"""
from __future__ import annotations

import hashlib
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
    # Phase A: prepare requires a real runtime DB whose streaming SHA-256 must
    # bind to the declared Q-001 identity hash.
    runtime_db = tmp_path / "runtime.db"
    runtime_db.write_bytes(b"catalyst-runtime-db\x00" * 64)
    return {
        "tmp_path": tmp_path,
        "dataset_path": dataset_path,
        "manifest_path": manifest_path,
        "strat_path": strat_path,
        "runtime_db": runtime_db,
        "runtime_db_sha256": hashlib.sha256(runtime_db.read_bytes()).hexdigest(),
        "eval_manifest": builder_manifest_cli,
    }


def _identity_bound_manifest(cli_env, manifest: dict) -> dict:
    manifest = dict(manifest)
    manifest["data_runtime_identity_ref"] = "v1:corpus:fixture"
    manifest["data_runtime_identity_hash"] = cli_env["runtime_db_sha256"]
    manifest["q001_file_sha256"] = cli_env["runtime_db_sha256"]
    return manifest


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
    manifest = _identity_bound_manifest(
        cli_env, json.loads(cli_env["manifest_path"].read_text(encoding="utf-8"))
    )
    cli_env["manifest_path"].write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    out_dir = cli_env["tmp_path"] / "out"
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(cli_env["manifest_path"]),
        "--stratification", str(cli_env["strat_path"]),
        "--output-dir", str(out_dir),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(cli_env["runtime_db"]),
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


def test_provider_budget_rejects_zero_cost_ceiling():
    module = _load_cli()
    with pytest.raises(module.Stage1OperatorError, match="max-cost-usd"):
        module._build_provider_budget(
            max_provider_calls=1, max_cost_usd=0.0, pricing=None
        )


def test_cli_help_describes_benchmark_inputs():
    module = _load_cli()
    help_text = module.build_parser().format_help()
    assert "Benchmark dataset manifest" in help_text
    assert "Benchmark stratification" in help_text
    assert "--derived-leakage-scan" in help_text
    assert "--runtime-evidence-db" in help_text
    assert "--handoff-manifest" in help_text
    assert "--handoff-manifest-sha256" in help_text


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


def test_cli_prepare_empty_a3_a4_creates_no_eligible_experiments(cli_env):
    """Q-011 Option B (2026-09-11): empty a3_eligible_ids and
    a4_readiness_eligible_ids are legal and produce NO A3/A4 eligible
    experiment entry during prepare (readiness-only, no promotion gate)."""
    module = _load_cli()

    rows = make_stage1_cases()
    manifest = make_dataset_manifest(rows, make_stratification(rows))
    manifest["a3_eligible_ids"] = []
    manifest["a4_readiness_eligible_ids"] = []
    manifest = _identity_bound_manifest(cli_env, manifest)
    manifest_path = cli_env["tmp_path"] / "manifest_empty_eligible.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    out_dir = cli_env["tmp_path"] / "out_empty_eligible"
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(cli_env["strat_path"]),
        "--output-dir", str(out_dir),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(cli_env["runtime_db"]),
    ])
    assert exit_code == 0
    summary = json.loads((out_dir / "prepare_summary.json").read_text(encoding="utf-8"))
    assert summary["eval_manifest"]["eligible_experiments"] == []


# ---- M8-C: eval-only candidate-fts retrieval mode -------------------------

def _stage1_cli():
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_v1_1_stage1.py"
    spec = importlib.util.spec_from_file_location("run_v1_1_stage1", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_candidate_fts_mode_requires_passing_recovery_report(tmp_path, capsys):
    module = _stage1_cli()
    with pytest.raises(module.Stage1OperatorError, match="requires"):
        module._load_recovery_retrieval_report(None, None)

    report = tmp_path / "recovery.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": "v1_1_recovery_retrieval_v1",
                "gate_passed": False,
                "build_id": "b" * 64,
                "corpus_manifest_id": "c" * 64,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    with pytest.raises(module.Stage1OperatorError, match="passing M8-B"):
        module._load_recovery_retrieval_report(report, digest)
    with pytest.raises(module.Stage1OperatorError, match="sha256"):
        module._load_recovery_retrieval_report(report, "0" * 64)

    passing = json.loads(report.read_text(encoding="utf-8"))
    passing["gate_passed"] = True
    report.write_text(json.dumps(passing, sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    payload = module._load_recovery_retrieval_report(report, digest)
    assert payload["build_id"] == "b" * 64

    # A candidate-fts execute run still fails closed on the operator bindings.
    output = tmp_path / "out"
    output.mkdir()
    missing = module.main(
        [
            "execute",
            "--output-dir", str(output),
            "--max-provider-calls", "12",
            "--max-cost-usd", "10",
            "--retrieval-mode", "candidate-fts",
        ]
    )
    assert missing == module.EXIT_CONTRACT_ERROR
    assert capsys.readouterr().err


def test_production_mode_is_the_default_and_has_no_candidate_kwargs(tmp_path):
    module = _stage1_cli()
    args = module.build_parser().parse_args(["execute"])
    assert args.retrieval_mode == "production"
    kwargs = module._retrieval_mode_kwargs(args)
    assert kwargs["retrieval_mode"] == "production"
    assert all(
        kwargs[key] is None
        for key in kwargs
        if key != "retrieval_mode"
    )
