"""M7 Batch-B corrective: CLI dispatch false-greens.

The prior CLI scaffold returned exit 2 for every execute/audit/report path,
so tests only proved "not exit 0". These tests require REAL orchestration:
- type-correct typed dispatch for prepare/execute/audit/report/all;
- an injectable runner adapter (M6 app/SSE boundary) that is fully
  fixture-testable without providers;
- identity-valid COMPLETED ledger rows resume without rerun; FAILED,
  CANCELLED, corrupt, nonterminal, or identity-mismatched rows never count
  as completed success;
- provider-call and cost ceilings enforced before every dispatch;
- audit runs full validation against ledger/run artifacts;
- report computes real gates and never fabricates empty gates or counts.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from catalyst_eval.v1_1.execution_ledger import ExecutionLedger, LedgerRow
from catalyst_eval.v1_1.manifest import EvalManifest
from catalyst_eval.v1_1.output_audit import AuditClaimDecision, Stage1OutputAudit

from tests.v1_1_fixtures import (
    make_dataset_manifest,
    make_stage1_cases,
    make_stratification,
)

EVAL_ID = "eval:stage1:v1"


def _real_hash(payload) -> str:
    from catalyst_eval.v1_1.loader import canonical_bytes
    import hashlib

    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _manifest_hash(case_id: str) -> str:
    return _real_hash({"run_id": f"manifest:{case_id}", "case_id": case_id})


def _result_hash(case_id: str) -> str:
    return _real_hash({"result": case_id})


def _utc_iso() -> str:
    return "2026-08-19T00:00:00Z"


@pytest.fixture()
def cli_env(tmp_path) -> dict:
    from catalyst_eval.v1_1.case import GoldenCase
    from tests.v1_1_fixtures import build_fixture_eval_manifest

    rows = make_stage1_cases()
    cases = [GoldenCase.model_validate(row) for row in rows]
    stratification = make_stratification(rows)
    manifest = make_dataset_manifest(rows, None, dataset_id="v1_1_stage1_fixture")
    dataset_path = tmp_path / "v1_1_stage1_cases.jsonl"
    dataset_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )
    manifest_path = tmp_path / "dataset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    strat_path = tmp_path / "stratification.json"
    strat_path.write_text(json.dumps(stratification, sort_keys=True), encoding="utf-8")
    return {
        "tmp_path": tmp_path,
        "dataset_path": dataset_path,
        "manifest_path": manifest_path,
        "strat_path": strat_path,
        "cases": cases,
        "case_ids": [case.case_id for case in cases],
        "eval_manifest": build_fixture_eval_manifest(),
    }


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_v1_1_stage1.py"
    spec = importlib.util.spec_from_file_location("run_v1_1_stage1", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_adapter_factory(cli_env, *, calls: list[str] | None = None):
    """Inject an adapter whose run facts make all attribution gates pass."""
    calls = [] if calls is None else calls

    def adapter_factory():
        from catalyst_eval.benchmark.pool_manifest import PoolArm, PoolManifest

        def _pool_for(case) -> PoolManifest:
            data = dict(
                schema_version="1.0.0",
                case_id=case.case_id,
                arms=(
                    PoolArm(arm="lexical", version="1.0.0", top_k=8),
                    PoolArm(arm="dense", version="1.0.0", top_k=8),
                    PoolArm(arm="hybrid", version="1.0.0", top_k=8),
                    PoolArm(arm="reranked", version="1.0.0", top_k=8),
                ),
                chunk_inventory=tuple(sorted(set(case.expected_primary_evidence))),
                corpus_manifest_id="c" * 64,
                index_manifest_id="e" * 64,
                source_artifact_id="f" * 64,
                created_at=_utc_datetime(),
            )
            import hashlib
            import json as _json

            pool_id = hashlib.sha256(
                _json.dumps(
                    {
                        "schema_version": data["schema_version"],
                        "case_id": data["case_id"],
                        "arms": [
                            {"arm": a.arm, "version": a.version, "top_k": a.top_k}
                            for a in data["arms"]
                        ],
                        "chunk_inventory": list(data["chunk_inventory"]),
                        "corpus_manifest_id": data["corpus_manifest_id"],
                        "index_manifest_id": data["index_manifest_id"],
                        "source_artifact_id": data["source_artifact_id"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            return PoolManifest(pool_id=pool_id, **data)

        def run_case(case) -> object:
            from catalyst_eval.v1_1.runner import CaseRunOutcome

            calls.append(case.case_id)
            run_manifest_payload = {
                "run_id": f"manifest:{case.case_id}", "case_id": case.case_id,
            }
            result_artifact_payload = {"result": case.case_id}
            pool = _pool_for(case)
            ranked_ids = [j.evidence_id for j in case.evidence_judgments]
            return CaseRunOutcome(
                case_id=case.case_id,
                run_manifest_id=f"manifest:{case.case_id}",
                run_manifest_hash=_real_hash(run_manifest_payload),
                result_artifact_id=f"result:{case.case_id}",
                result_artifact_hash=_real_hash(result_artifact_payload),
                terminal_status="COMPLETED",
                run_manifest_payload=run_manifest_payload,
                result_artifact_payload=result_artifact_payload,
                context_pack_ref={
                    "artifact_id": f"pack:{case.case_id}",
                    "artifact_hash": _real_hash(run_manifest_payload),
                },
                claim_plan_ref={
                    "artifact_id": f"claimplan:{case.case_id}",
                    "artifact_hash": _real_hash(result_artifact_payload),
                },
                assurance_ref={
                    "artifact_id": f"assurance:{case.case_id}",
                    "artifact_hash": _real_hash(run_manifest_payload),
                },
                provider_calls=2,
                cost_usd=0.01,
                latency_ms=100,
                tokens=200,
                run_facts={
                    "schema_version": "v1_1_stage1_run_facts_v1",
                    "case_id": case.case_id,
                    "output_status": case.oracle_status,
                    "attribution_type": case.expected_attribution_type
                    or "EVIDENCE_BACKED_CAUSAL",
                    "refusal_reason": case.expected_refusal_reason,
                    "claims": [
                        {
                            "claim_id": f"claim:{case.case_id}:1",
                            "material": True,
                            "citation_ids": list(case.expected_primary_evidence)[:1]
                            or [],
                            "role": "PRIMARY",
                            "statement": "fixture claim",
                        }
                    ],
                    "sanity_tasks_completed": ["COMPANY_PRIMARY"],
                    "trajectory": {
                        "corrective_triggered": False,
                        "gap_ids": [],
                        "corrective_actions": [],
                        "stop_correct": True,
                        "new_structure_created": False,
                        "corrected": False,
                    },
                    "retrieval": {
                        "pool_id": "a" * 64,
                        "ranked_evidence_ids": ranked_ids,
                        "reranker_contributed": False,
                        "ticker_violations": [],
                        "cutoff_violations": [],
                        "degraded": False,
                        "pool": pool.model_dump(mode="json"),
                    },
                },
            )

        return run_case

    return adapter_factory, calls


def _utc_datetime():
    from datetime import datetime, timezone

    return datetime(2026, 8, 19, tzinfo=timezone.utc)


def _valid_audit(cli_env, *, run_manifest_id=None, eval_id=None) -> Path:
    path = cli_env["tmp_path"] / "audit.jsonl"
    lines = []
    for case in cli_env["cases"]:
        claims = [
            {
                "claim_id": f"claim:{case.case_id}:1",
                "material": True,
                "citation_ids": list(case.expected_primary_evidence)[:1] or [],
                "decision": "SUPPORT",
                "reason_code": "citations_resolve",
            }
        ]
        lines.append(
            json.dumps(
                {
                    "schema_version": "v1_1_stage1_output_audit_v1",
                    "eval_id": eval_id or EVAL_ID,
                    "case_id": case.case_id,
                    "run_manifest_id": run_manifest_id
                    or f"manifest:{case.case_id}",
                    "run_manifest_hash": _manifest_hash(case.case_id),
                    "result_artifact_id": f"result:{case.case_id}",
                    "result_artifact_hash": _result_hash(case.case_id),
                    "auditor_id": "fixture-auditor",
                    "audited_at": _utc_iso(),
                    "adjudication_state": "resolved",
                    "claims": claims,
                },
                sort_keys=True,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run_prepare(module, cli_env, out_dir: Path, **extra):
    argv = [
        "prepare",
        "--dataset-manifest", str(cli_env["manifest_path"]),
        "--stratification", str(cli_env["strat_path"]),
        "--output-dir", str(out_dir),
        "--max-provider-calls", str(extra.get("max_provider_calls", 100)),
        "--max-cost-usd", str(extra.get("max_cost_usd", 10.0)),
    ]
    code = module.main(argv, runner_adapter_factory=lambda: None)
    if code != 0:
        return code, None
    summary = json.loads((out_dir / "prepare_summary.json").read_text(encoding="utf-8"))
    return code, summary["eval_id"]


# --- prepare ---------------------------------------------------------------


def test_cli_prepare_loads_separate_stratification_file(cli_env):
    """The stratification file is a SEPARATE input; a mismatch must fail."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    # Corrupt the separate stratification file: wrong per-case keys.
    strat = json.loads(cli_env["strat_path"].read_text(encoding="utf-8"))
    strat["per_case"] = {}
    bad = cli_env["tmp_path"] / "bad_strat.json"
    bad.write_text(json.dumps(strat, sort_keys=True), encoding="utf-8")
    exit_code = module.main(
        [
            "prepare",
            "--dataset-manifest", str(cli_env["manifest_path"]),
            "--stratification", str(bad),
            "--output-dir", str(out_dir),
            "--max-provider-calls", "100",
            "--max-cost-usd", "10.0",
        ],
        runner_adapter_factory=lambda: None,
    )
    assert exit_code == 2, "prepare must fail closed on a bad stratification file"


def test_cli_prepare_builds_real_eval_manifest_and_eval_id(cli_env):
    """prepare writes a summary containing the real EvalManifest eval_id."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    assert eval_id
    summary = json.loads((out_dir / "prepare_summary.json").read_text(encoding="utf-8"))
    assert summary["schema_version"] == "v1_1_stage1_prepare_summary_v1"
    assert summary["eval_id"] == eval_id, "prepare must build a real eval_id"
    assert summary["eval_manifest"]["evaluation_identity"]["eval_id"] == summary["eval_id"]
    assert summary["case_count"] == 12
    assert summary["execution_head_sha8"]
    assert summary["comparability"] == "NON-COMPARABLE"


# --- execute ---------------------------------------------------------------


def test_cli_execute_resumes_completed_rows_without_rerun(cli_env):
    """Identity-valid COMPLETED rows resume; the adapter is not called again."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    assert eval_id
    first_case = cli_env["case_ids"][0]
    ledger = ExecutionLedger(
        rows=(
            LedgerRow(
                eval_id=eval_id,
                case_id=first_case,
                run_manifest_id=f"manifest:{first_case}",
                run_manifest_hash=_manifest_hash(first_case),
                result_artifact_id=f"result:{first_case}",
                result_artifact_hash=_result_hash(first_case),
                terminal_status="COMPLETED",
                attempts=1,
                provider_calls=2,
                cost_usd=0.01,
                checksum="x",
                run_facts={
                    "schema_version": "v1_1_stage1_run_facts_v1",
                    "case_id": first_case,
                    "output_status": "SUFFICIENT",
                    "attribution_type": "EVIDENCE_BACKED_CAUSAL",
                    "refusal_reason": None,
                    "claims": [
                        {
                            "claim_id": f"claim:{first_case}:1",
                            "material": True,
                            "citation_ids": ["fixture-ev-001"],
                            "role": "PRIMARY",
                            "statement": "resumed claim",
                        }
                    ],
                    "sanity_tasks_completed": ["COMPANY_PRIMARY"],
                    "trajectory": {
                        "corrective_triggered": False,
                        "gap_ids": [],
                        "corrective_actions": [],
                        "stop_correct": True,
                        "new_structure_created": False,
                        "corrected": False,
                    },
                    "retrieval": {
                        "pool_id": "a" * 64,
                        "ranked_evidence_ids": ["fixture-ev-001"],
                        "reranker_contributed": False,
                        "ticker_violations": [],
                        "cutoff_violations": [],
                        "degraded": False,
                    },
                },
            ),
        )
    )
    ledger.write(out_dir / "execution_ledger.jsonl")
    factory, calls = _make_adapter_factory(cli_env)
    exit_code = module.main(
        [
            "execute",
            "--output-dir", str(out_dir),
            "--max-provider-calls", "100",
            "--max-cost-usd", "10.0",
        ],
        runner_adapter_factory=factory,
    )
    assert exit_code == 0, "execute must resume identity-valid COMPLETED rows"
    assert first_case not in calls, "resumed COMPLETED row must not rerun"
    assert len(calls) == len(cli_env["case_ids"]) - 1


def test_cli_execute_does_not_count_failed_rows_as_success(cli_env):
    """FAILED/CANCELLED/nonterminal/corrupt/identity-mismatched rows never
    count as completed success and must be re-dispatched."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    first_case = cli_env["case_ids"][0]
    ledger = ExecutionLedger(
        rows=(
            LedgerRow(
                eval_id=eval_id,
                case_id=first_case,
                run_manifest_id=f"manifest:{first_case}",
                run_manifest_hash=_manifest_hash(first_case),
                result_artifact_id=f"result:{first_case}",
                result_artifact_hash=_result_hash(first_case),
                terminal_status="FAILED",
                attempts=1,
                provider_calls=1,
                cost_usd=0.01,
                checksum="x",
            ),
        )
    )
    ledger.write(out_dir / "execution_ledger.jsonl")
    factory, calls = _make_adapter_factory(cli_env)
    exit_code = module.main(
        [
            "execute",
            "--output-dir", str(out_dir),
            "--max-provider-calls", "100",
            "--max-cost-usd", "10.0",
        ],
        runner_adapter_factory=factory,
    )
    assert exit_code == 0
    assert first_case in calls, "FAILED row is not completed success; must rerun"


def test_cli_execute_corrupt_row_is_not_reused(cli_env):
    """A corrupt ledger row (checksum mismatch) is never silently reused."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    first_case = cli_env["case_ids"][0]
    path = out_dir / "execution_ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "v1_1_stage1_execution_ledger_v1",
                "row": {
                    "eval_id": eval_id,
                    "case_id": first_case,
                    "run_manifest_id": f"manifest:{first_case}",
                    "run_manifest_hash": _manifest_hash(first_case),
                    "result_artifact_id": f"result:{first_case}",
                    "result_artifact_hash": _result_hash(first_case),
                    "terminal_status": "COMPLETED",
                    "attempts": 1,
                    "provider_calls": 2,
                    "cost_usd": 0.01,
                    "checksum": "tampered",
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    factory, calls = _make_adapter_factory(cli_env)
    exit_code = module.main(
        [
            "execute",
            "--output-dir", str(out_dir),
            "--max-provider-calls", "100",
            "--max-cost-usd", "10.0",
        ],
        runner_adapter_factory=factory,
    )
    assert exit_code == 0
    assert first_case in calls, "corrupt COMPLETED row must be re-dispatched"


def test_cli_execute_enforces_ceilings_before_dispatch(cli_env):
    """Ceilings are checked before every dispatch; exceeding stops."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, _ = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    factory, calls = _make_adapter_factory(cli_env)
    # First dispatch would exceed the zero provider-call ceiling.
    exit_code = module.main(
        [
            "execute",
            "--output-dir", str(out_dir),
            "--max-provider-calls", "0",
            "--max-cost-usd", "10.0",
        ],
        runner_adapter_factory=factory,
    )
    assert exit_code == 2, "ceiling exceeded before dispatch must stop"
    assert calls == [], "no provider dispatch may occur when the ceiling is exceeded"


# --- audit -----------------------------------------------------------------


def test_cli_audit_runs_full_validation_against_ledger(cli_env):
    """CLI audit must reject identity mismatches against ledger/run output."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    factory, _ = _make_adapter_factory(cli_env)
    assert (
        module.main(
            [
                "execute",
                "--output-dir", str(out_dir),
                "--max-provider-calls", "100",
                "--max-cost-usd", "10.0",
            ],
            runner_adapter_factory=factory,
        )
        == 0
    )
    audit_path = _valid_audit(
        cli_env, run_manifest_id="manifest:not-the-run", eval_id=eval_id
    )
    exit_code = module.main(
        [
            "audit",
            "--output-dir", str(out_dir),
            "--audit", str(audit_path),
        ]
    )
    assert exit_code == 2, "audit must fail closed on run_manifest_id mismatch"


def test_cli_audit_accepts_complete_valid_audit(cli_env):
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    factory, _ = _make_adapter_factory(cli_env)
    assert (
        module.main(
            [
                "execute",
                "--output-dir", str(out_dir),
                "--max-provider-calls", "100",
                "--max-cost-usd", "10.0",
            ],
            runner_adapter_factory=factory,
        )
        == 0
    )
    audit_path = _valid_audit(cli_env, eval_id=eval_id)
    exit_code = module.main(
        [
            "audit",
            "--output-dir", str(out_dir),
            "--audit", str(audit_path),
        ]
    )
    assert exit_code == 0


# --- report ----------------------------------------------------------------


def test_cli_report_rejects_missing_gate_evidence(cli_env):
    """report without sealed run outputs must stop, not fabricate gates."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    audit_path = _valid_audit(cli_env, eval_id=eval_id)
    json_out = cli_env["tmp_path"] / "r.json"
    exit_code = module.main(
        [
            "report",
            "--output-dir", str(out_dir),
            "--audit", str(audit_path),
            "--manifest-out", str(cli_env["tmp_path"] / "m.json"),
            "--json-out", str(json_out),
            "--markdown-out", str(cli_env["tmp_path"] / "r.md"),
        ]
    )
    assert exit_code == 2, "report must not fabricate gates from a missing ledger"
    assert not json_out.exists()


def test_cli_report_publishes_real_gates_and_non_comparable(cli_env):
    """report computes real gates and never emits empty gates/counts."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    factory, _ = _make_adapter_factory(cli_env)
    assert (
        module.main(
            [
                "execute",
                "--output-dir", str(out_dir),
                "--max-provider-calls", "100",
                "--max-cost-usd", "10.0",
            ],
            runner_adapter_factory=factory,
        )
        == 0
    )
    audit_path = _valid_audit(cli_env, eval_id=eval_id)
    json_out = cli_env["tmp_path"] / "r.json"
    exit_code = module.main(
        [
            "report",
            "--output-dir", str(out_dir),
            "--audit", str(audit_path),
            "--manifest-out", str(cli_env["tmp_path"] / "m.json"),
            "--json-out", str(json_out),
            "--markdown-out", str(cli_env["tmp_path"] / "r.md"),
        ]
    )
    assert exit_code == 0
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["hard_gates"], "report must compute real hard gates"
    assert "citation_correctness" in payload["hard_gates"]
    assert payload["coverage_limited_count"] >= 0
    assert payload["model_limited_count"] > 0
    assert payload["comparability"] == "NON-COMPARABLE"
    assert payload["eval_id"] == eval_id


def test_cli_all_orchestrates_full_pipeline(cli_env):
    """all = prepare -> execute -> audit -> report with injected adapter."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    # Prepare first to learn the real eval_id for the human audit file.
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    audit_path = _valid_audit(cli_env, eval_id=eval_id)
    factory, _ = _make_adapter_factory(cli_env)
    json_out = cli_env["tmp_path"] / "all.json"
    exit_code = module.main(
        [
            "all",
            "--dataset-manifest", str(cli_env["manifest_path"]),
            "--stratification", str(cli_env["strat_path"]),
            "--output-dir", str(out_dir),
            "--max-provider-calls", "100",
            "--max-cost-usd", "10.0",
            "--audit", str(audit_path),
            "--manifest-out", str(cli_env["tmp_path"] / "m.json"),
            "--json-out", str(json_out),
            "--markdown-out", str(cli_env["tmp_path"] / "all.md"),
        ],
        runner_adapter_factory=factory,
    )
    assert exit_code == 0
    assert json_out.is_file()
    assert (out_dir / "execution_ledger.jsonl").is_file()
    assert (out_dir / "prepare_summary.json").is_file()
