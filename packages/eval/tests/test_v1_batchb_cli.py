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

import hashlib
import importlib.util
import json
import sqlite3
from dataclasses import replace
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
    # Phase A: prepare requires a real runtime DB whose streaming SHA-256 must
    # bind to the declared Q-001 identity hash.
    runtime_db = tmp_path / "runtime.db"
    runtime_db.write_bytes(b"catalyst-runtime-db\x00" * 64)
    manifest["data_runtime_identity_ref"] = "v1:corpus:fixture"
    manifest["data_runtime_identity_hash"] = hashlib.sha256(runtime_db.read_bytes()).hexdigest()
    manifest["q001_file_sha256"] = manifest["data_runtime_identity_hash"]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return {
        "tmp_path": tmp_path,
        "dataset_path": dataset_path,
        "manifest_path": manifest_path,
        "strat_path": strat_path,
        "runtime_db": runtime_db,
        "pricing_json": _write_pricing(tmp_path),
        "cases": cases,
        "case_ids": [case.case_id for case in cases],
        "eval_manifest": build_fixture_eval_manifest(),
    }


def _write_pricing(tmp_path: Path) -> Path:
    """Non-secret operator pricing input for the shared provider budget guard."""
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "deepseek/deepseek-chat": {
                        "input_usd_per_million_tokens": 1.0,
                        "output_usd_per_million_tokens": 2.0,
                    }
                },
                "role_token_limits": {
                    "evidence_analyst": {
                        "max_input_tokens": 1000,
                        "max_output_tokens": 500,
                    },
                    "streaming_writer": {
                        "max_input_tokens": 1000,
                        "max_output_tokens": 500,
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


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

    def adapter_factory(*, db_path=None, provider_budget=None):
        from catalyst_eval.benchmark.pool_manifest import PoolArm, PoolManifest

        def _consume_budget(attempts: int) -> float | None:
            """Consume the shared guard exactly like a real provider dispatch.

            A fake adapter that reported provider calls without consuming the
            budget would be caught by the CLI's accounting reconciliation.
            """
            if provider_budget is None:
                return None
            for _ in range(attempts):
                reservation = provider_budget.reserve(
                    role="evidence_analyst",
                    provider="deepseek",
                    model_id="deepseek-chat",
                )
                provider_budget.settle(reservation)
            return provider_budget.case_snapshot()["case_used_cost_usd"]

        def _pool_for(case, candidate_ids) -> PoolManifest:
            data = dict(
                schema_version="1.0.0",
                case_id=case.case_id,
                arms=(
                    PoolArm(arm="lexical", version="1.0.0", top_k=8),
                    PoolArm(arm="dense", version="1.0.0", top_k=8),
                    PoolArm(arm="hybrid", version="1.0.0", top_k=8),
                    PoolArm(arm="reranked", version="1.0.0", top_k=8),
                ),
                chunk_inventory=tuple(sorted(set(candidate_ids))),
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
            charged_cost = _consume_budget(2)
            run_manifest_payload = {
                "run_id": f"manifest:{case.case_id}", "case_id": case.case_id,
            }
            result_artifact_payload = {"result": case.case_id}
            ranked_ids = [j.evidence_id for j in case.evidence_judgments]
            pool = _pool_for(case, ranked_ids)
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
                cost_usd=charged_cost,
                latency_ms=100,
                tokens=300,
                run_facts={
                    "schema_version": "v1_1_stage1_run_facts_v1",
                    "case_id": case.case_id,
                    "output_status": case.oracle_status,
                    "latency_ms": 100,
                    "tokens": 300,
                    "cost_usd": charged_cost,
                    "attribution_type": case.expected_attribution_type
                    or "EVIDENCE_BACKED_CAUSAL",
                    "refusal_reason": case.expected_refusal_reason,
                    "refusal_reason_available": True,
                    "model_limited": False,
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
                        "rounds_executed": 0,
                        "gap_reason_codes": [],
                        "corrective_actions": [],
                        "research_fingerprints": [],
                        "evidence_delta_ids": [],
                        "produced_structure": case.oracle_status != "ABSTAIN",
                    },
                    "retrieval": {
                        "observed": True,
                        "ranked_evidence_ids": ranked_ids,
                        "candidate_evidence_ids": ranked_ids,
                        "reranker_contributed": False,
                        "ticker_violations": [],
                        "cutoff_violations": [],
                        "degraded": False,
                        "latency_ms": 100,
                        "pool": pool.model_dump(mode="json"),
                    },
                    "provider_accounting": {
                        "provider_calls": 2,
                        "cost_usd": charged_cost,
                        "cost_method": (
                            "upper_bound_charged" if charged_cost is not None
                            else "unavailable"
                        ),
                        "tokens_in": 200,
                        "tokens_out": 100,
                    },
                },
            )

        return run_case

    return adapter_factory, calls


def test_completed_outcome_is_validated_before_ledger_persistence():
    from catalyst_eval.v1_1.runner import CaseRunOutcome

    module = _load_cli()
    outcome = CaseRunOutcome(
        case_id="case-001",
        run_manifest_id="manifest:case-001",
        run_manifest_hash="a" * 64,
        result_artifact_id="result:case-001",
        result_artifact_hash="b" * 64,
        terminal_status="COMPLETED",
        provider_calls=1,
        cost_usd=0.01,
        run_facts={"schema_version": "v1_1_stage1_run_facts_v1"},
    )
    with pytest.raises(ValueError, match="run facts"):
        module._ledger_row_from_outcome(outcome, eval_id="eval-1", attempts=1)


def _utc_datetime():
    from datetime import datetime, timezone

    return datetime(2026, 8, 19, tzinfo=timezone.utc)


def _complete_fixture_run_facts(case, *, cost_usd: float | None) -> dict:
    """Build complete, identity-bound run facts for successful fake outcomes."""
    from catalyst_eval.benchmark.pool_manifest import PoolArm, PoolManifest

    evidence_ids = [judgment.evidence_id for judgment in case.evidence_judgments]
    pool_data = {
        "schema_version": "1.0.0",
        "case_id": case.case_id,
        "arms": (
            PoolArm(arm="lexical", version="1.0.0", top_k=8),
            PoolArm(arm="dense", version="1.0.0", top_k=8),
            PoolArm(arm="hybrid", version="1.0.0", top_k=8),
            PoolArm(arm="reranked", version="1.0.0", top_k=8),
        ),
        "chunk_inventory": tuple(sorted(set(evidence_ids))),
        "corpus_manifest_id": "c" * 64,
        "index_manifest_id": "e" * 64,
        "source_artifact_id": "f" * 64,
        "created_at": _utc_datetime(),
    }
    pool_id = hashlib.sha256(
        json.dumps(
            {
                "schema_version": pool_data["schema_version"],
                "case_id": pool_data["case_id"],
                "arms": [
                    {"arm": arm.arm, "version": arm.version, "top_k": arm.top_k}
                    for arm in pool_data["arms"]
                ],
                "chunk_inventory": list(pool_data["chunk_inventory"]),
                "corpus_manifest_id": pool_data["corpus_manifest_id"],
                "index_manifest_id": pool_data["index_manifest_id"],
                "source_artifact_id": pool_data["source_artifact_id"],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    pool = PoolManifest(pool_id=pool_id, **pool_data)
    return {
        "schema_version": "v1_1_stage1_run_facts_v1",
        "case_id": case.case_id,
        "output_status": case.oracle_status,
        "latency_ms": 10,
        "tokens": 10,
        "cost_usd": cost_usd,
        "attribution_type": case.expected_attribution_type
        or "EVIDENCE_BACKED_CAUSAL",
        "refusal_reason": case.expected_refusal_reason,
        "refusal_reason_available": True,
        "model_limited": False,
        "claims": [
            {
                "claim_id": f"claim:{case.case_id}:1",
                "material": True,
                "citation_ids": list(case.expected_primary_evidence)[:1],
                "role": "PRIMARY",
                "statement": "fixture claim",
            }
        ],
        "sanity_tasks_completed": ["COMPANY_PRIMARY"],
        "trajectory": {
            "corrective_triggered": False,
            "rounds_executed": 0,
            "gap_reason_codes": [],
            "corrective_actions": [],
            "research_fingerprints": [],
            "evidence_delta_ids": [],
            "produced_structure": case.oracle_status != "ABSTAIN",
        },
        "retrieval": {
            "observed": True,
            "ranked_evidence_ids": evidence_ids,
            "candidate_evidence_ids": evidence_ids,
            "reranker_contributed": False,
            "ticker_violations": [],
            "cutoff_violations": [],
            "degraded": False,
            "latency_ms": 10,
            "pool": pool.model_dump(mode="json"),
        },
        "provider_accounting": {
            "provider_calls": 1,
            "cost_usd": cost_usd,
            "cost_method": (
                "upper_bound_charged" if cost_usd is not None else "unavailable"
            ),
            "tokens_in": 10,
            "tokens_out": 0,
        },
    }


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
        "--pricing-json", str(cli_env["pricing_json"]),
        "--runtime-db", str(cli_env["runtime_db"]),
    ]
    code = module.main(argv, runner_adapter_factory=lambda: None)
    if code != 0:
        return code, None
    summary = json.loads((out_dir / "prepare_summary.json").read_text(encoding="utf-8"))
    return code, summary["eval_id"]


def _write_report_gate_evidence(
    base: Path, *, head: str | None = None, eval_id: str | None = None
) -> None:
    """Create a self-contained, identity-bound report evidence fixture."""
    (base / "report_inputs").mkdir(parents=True, exist_ok=True)
    (base / "derived").mkdir(parents=True, exist_ok=True)
    original = base / "report_inputs" / "leakage_scan.json"
    derived = base / "derived" / "leakage_scan_after_terminal_status_exemption.json"
    secret = base / "report_inputs" / "secret_scan.json"
    runtime = base / "runtime.sqlite3"
    original_payload = {
        "n": 2,
        "findings": [
            "trace:c04/diag.terminal.result_status: hidden oracle status",
            "trace:c04/diag.terminal.status_ceiling: hidden oracle status",
        ],
    }
    original.write_text(json.dumps(original_payload, sort_keys=True), encoding="utf-8")
    diagnostics = {
        "schema_version": "v1.1_run_diagnostics_v1",
        "terminal": {"terminal_event_type": "run.completed"},
    }
    diagnostics_json = json.dumps(diagnostics, sort_keys=True, separators=(",", ":"))
    diagnostics_sha = hashlib.sha256(diagnostics_json.encode()).hexdigest()
    derived_payload = {
        "schema_version": "m7_derived_leakage_scan_v2",
        "derived": True,
        "original_scan_path": str(original.resolve()),
        "original_scan_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "original_scan_n": 2,
        "n": 0,
        "findings": [],
        "c04_rendered_messages_findings": [],
        "hidden_gold_boundary": [],
        "model_visible_prompt": False,
        "classifier": "verified_post_writer_run_diagnostics_terminal_status_exemption_v2",
        "exemption_contract": {
            "artifact_type": "run_diagnostics",
            "post_writer_persisted": True,
            "schema_version": "v1.1_run_diagnostics_v1",
            "terminal_event_type": "run.completed",
            "path_only_exemption": False,
            "generic_or_model_visible_trace_same_fields": "reported",
            "exempted_paths": ["terminal.result_status", "terminal.status_ceiling"],
        },
        "source_artifact": {
            "artifact_id": "diagnostics:fixture",
            "run_id": "run:fixture",
            "event_seq": 3,
            "artifact_type": "run_diagnostics",
            "payload_hash": diagnostics_sha,
            "schema_version": "v1.1_run_diagnostics_v1",
            "terminal_event_type": "run.completed",
            "post_writer_persisted": True,
        },
    }
    derived.write_text(json.dumps(derived_payload, sort_keys=True), encoding="utf-8")
    secret.write_text(json.dumps({"findings": []}), encoding="utf-8")
    conn = sqlite3.connect(runtime)
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            lifecycle_status TEXT NOT NULL,
            run_manifest_id TEXT NOT NULL,
            manifest_hash TEXT NOT NULL
        );
        CREATE TABLE run_events (
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            PRIMARY KEY (run_id, seq)
        );
        CREATE TABLE run_artifacts (artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_seq INTEGER NOT NULL, artifact_type TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?)",
        ("run:fixture", "COMPLETED", "manifest:fixture", "d" * 64),
    )
    conn.execute(
        "INSERT INTO run_events VALUES (?, ?, ?, ?)",
        ("run:fixture", 3, "2026-08-19T00:00:00+00:00", "run.completed"),
    )
    conn.execute(
        "INSERT INTO run_artifacts VALUES (?, ?, ?, ?, ?, ?)",
        ("diagnostics:fixture", "run:fixture", 3, "run_diagnostics", diagnostics_sha, diagnostics_json),
    )
    for index, case in enumerate(make_stage1_cases(), start=1):
        case_id = case["case_id"]
        conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?)",
            (
                f"run:{case_id}",
                "COMPLETED",
                f"manifest:{case_id}",
                _manifest_hash(case_id),
            ),
        )
        conn.execute(
            "INSERT INTO run_events VALUES (?, ?, ?, ?)",
            (
                f"run:{case_id}",
                1,
                f"2026-08-19T00:00:{index:02d}+00:00",
                "run.completed",
            ),
        )
    conn.commit()
    conn.close()
    handoff_files = []
    for relative, path in (
        ("report_inputs/leakage_scan.json", original),
        ("report_inputs/secret_scan.json", secret),
        ("runtime.sqlite3", runtime),
    ):
        handoff_files.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (base / "final_handoff_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "m7_stage1_final_handoff_manifest_v1",
                "head": head
                or json.loads((base / "prepare_summary.json").read_text(encoding="utf-8"))[
                    "execution_head_sha256"
                ],
                "eval_id": eval_id
                or json.loads((base / "prepare_summary.json").read_text(encoding="utf-8"))["eval_id"],
                "file_count": len(handoff_files),
                "files": handoff_files,
                "missing_required": [],
                "secret_scan_hits": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


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
            "--pricing-json", str(cli_env["pricing_json"]),
            "--pricing-json", str(cli_env["pricing_json"]),
            "--runtime-db", str(cli_env["runtime_db"]),
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


def test_transferred_prepare_summary_resolves_checkout_dataset_manifest(cli_env):
    """A transferred cloud summary must resolve its checkout-owned manifest."""
    module = _load_cli()
    repo = Path(__file__).resolve().parents[3]
    out_dir = repo / "data" / "eval_reports" / "v1_1_stage1_327b3eed"
    summary = json.loads((out_dir / "prepare_summary.json").read_text(encoding="utf-8"))
    summary["dataset_manifest_path"] = (
        "/root/catalyst-m7/20260915T110858Z/code/Catalyst/"
        "packages/eval/benchmarks/v1_1/stage1/manifest.json"
    )
    manifest, cases = module._load_eval_manifest_and_cases(summary, out_dir)
    assert manifest.evaluation_identity.dataset_id == "v1_1_stage1_q011"
    assert len(cases) == 12


def test_transferred_prepare_summary_resolves_checkout_stratification(cli_env):
    """A transferred cloud summary must resolve its checkout strata file."""
    module = _load_cli()
    repo = Path(__file__).resolve().parents[3]
    out_dir = repo / "data" / "eval_reports" / "v1_1_stage1_327b3eed"
    summary = json.loads((out_dir / "prepare_summary.json").read_text(encoding="utf-8"))
    summary["stratification_path"] = (
        "/root/catalyst-m7/20260915T110858Z/code/Catalyst/"
        "packages/eval/benchmarks/v1_1/stage1/stratification.json"
    )
    resolved = module._resolve_checkout_benchmark_path(
        Path(summary["stratification_path"]), "stratification.json"
    )
    assert resolved == repo / "packages" / "eval" / "benchmarks" / "v1_1" / "stage1" / "stratification.json"


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
            "--pricing-json", str(cli_env["pricing_json"]),
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
            "--pricing-json", str(cli_env["pricing_json"]),
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
            "--pricing-json", str(cli_env["pricing_json"]),
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
            "--pricing-json", str(cli_env["pricing_json"]),
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
                "--pricing-json", str(cli_env["pricing_json"]),
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
                "--pricing-json", str(cli_env["pricing_json"]),
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
                "--pricing-json", str(cli_env["pricing_json"]),
            ],
            runner_adapter_factory=factory,
        )
        == 0
    )
    _write_report_gate_evidence(out_dir)
    handoff_path = out_dir / "final_handoff_manifest.json"
    handoff_sha = hashlib.sha256(handoff_path.read_bytes()).hexdigest()
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
            "--handoff-manifest", str(handoff_path),
            "--handoff-manifest-sha256", handoff_sha,
        ]
    )
    assert exit_code == 0
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["hard_gates"], "report must compute real hard gates"
    assert "citation_correctness" in payload["hard_gates"]
    assert payload["coverage_limited_count"] >= 0
    # The offline fixture completes every case with no explicit model/runtime
    # limitation, so model_limited_count must be 0 (not 12).
    assert payload["model_limited_count"] == 0
    # Conditional NO_MATERIAL gates have no eligible case in the fixture and
    # must record exercised=false while keeping the frozen zero-violation pass.
    assert payload["attribution_metrics"]["no_material_producing_sufficient"]["eligible_count"] == 0
    assert payload["attribution_metrics"]["no_material_producing_sufficient"]["exercised"] is False
    assert payload["hard_gates"]["no_material_producing_sufficient"] is True
    assert payload["attribution_metrics"]["no_material_without_sanity"]["exercised"] is False
    assert payload["comparability"] == "NON-COMPARABLE"
    assert payload["eval_id"] == eval_id
    assert payload["gate_evidence"]["leakage_scan"]["status"] == "PASS"
    assert payload["gate_evidence"]["leakage_scan"]["original_count"] == 2
    assert payload["gate_evidence"]["leakage_scan"]["derived_count"] == 0
    assert payload["gate_evidence"]["secret_scan"]["status"] == "PASS"
    assert payload["handoff_manifest"]["head"] == json.loads(
        (out_dir / "prepare_summary.json").read_text(encoding="utf-8")
    )["execution_head_sha256"]
    assert payload["handoff_manifest"]["eval_id"] == eval_id
    assert payload["handoff_manifest"]["file_hashes"]["runtime.sqlite3"] == hashlib.sha256(
        (out_dir / "runtime.sqlite3").read_bytes()
    ).hexdigest()
    first_json = json_out.read_bytes()
    first_markdown = (cli_env["tmp_path"] / "r.md").read_bytes()
    markdown_text = first_markdown.decode("utf-8")
    assert "handoff_manifest_sha256:" in markdown_text
    assert payload["handoff_manifest"]["file_hashes"]["report_inputs/leakage_scan.json"] in markdown_text
    assert payload["handoff_manifest"]["file_hashes"]["report_inputs/secret_scan.json"] in markdown_text
    assert payload["handoff_manifest"]["file_hashes"]["runtime.sqlite3"] in markdown_text
    assert module.main(
        [
            "report",
            "--output-dir", str(out_dir),
            "--audit", str(audit_path),
            "--manifest-out", str(cli_env["tmp_path"] / "m.json"),
            "--json-out", str(json_out),
            "--markdown-out", str(cli_env["tmp_path"] / "r.md"),
            "--handoff-manifest", str(handoff_path),
            "--handoff-manifest-sha256", handoff_sha,
        ]
    ) == 0
    assert json_out.read_bytes() == first_json
    assert (cli_env["tmp_path"] / "r.md").read_bytes() == first_markdown

    assert (cli_env["tmp_path"] / "r.md").read_text(encoding="utf-8").startswith(
        "# v1_1_stage1_report_v1"
    )


def test_cli_report_rejects_duplicate_completed_case_rows(cli_env):
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    factory, _ = _make_adapter_factory(cli_env)
    assert module.main(
        [
            "execute", "--output-dir", str(out_dir),
            "--max-provider-calls", "100", "--max-cost-usd", "10.0",
            "--pricing-json", str(cli_env["pricing_json"]),
        ],
        runner_adapter_factory=factory,
    ) == 0
    _write_report_gate_evidence(out_dir)
    handoff_path = out_dir / "final_handoff_manifest.json"
    handoff_sha = hashlib.sha256(handoff_path.read_bytes()).hexdigest()
    ledger_path = out_dir / "execution_ledger.jsonl"
    ledger = ExecutionLedger.load(ledger_path, expected_eval_id=eval_id)
    ExecutionLedger(rows=(*ledger.rows, ledger.rows[0])).write(ledger_path)
    audit_path = _valid_audit(cli_env, eval_id=eval_id)
    exit_code = module.main(
        [
            "report", "--output-dir", str(out_dir), "--audit", str(audit_path),
            "--manifest-out", str(cli_env["tmp_path"] / "m.json"),
            "--json-out", str(cli_env["tmp_path"] / "r.json"),
            "--markdown-out", str(cli_env["tmp_path"] / "r.md"),
            "--handoff-manifest", str(handoff_path),
            "--handoff-manifest-sha256", handoff_sha,
        ]
    )
    assert exit_code == 2


def test_cli_report_rejects_unknown_cost_in_completed_ledger(cli_env):
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    factory, _ = _make_adapter_factory(cli_env)
    assert module.main(
        [
            "execute", "--output-dir", str(out_dir),
            "--max-provider-calls", "100", "--max-cost-usd", "10.0",
            "--pricing-json", str(cli_env["pricing_json"]),
        ],
        runner_adapter_factory=factory,
    ) == 0
    _write_report_gate_evidence(out_dir)
    handoff_path = out_dir / "final_handoff_manifest.json"
    handoff_sha = hashlib.sha256(handoff_path.read_bytes()).hexdigest()
    ledger = ExecutionLedger.load(out_dir / "execution_ledger.jsonl", expected_eval_id=eval_id)
    first = replace(ledger.rows[0], cost_usd=None)
    ExecutionLedger(rows=(first, *ledger.rows[1:])).write(
        out_dir / "execution_ledger.jsonl"
    )
    audit_path = _valid_audit(cli_env, eval_id=eval_id)
    assert module.main(
        [
            "report", "--output-dir", str(out_dir), "--audit", str(audit_path),
            "--manifest-out", str(cli_env["tmp_path"] / "m.json"),
            "--json-out", str(cli_env["tmp_path"] / "r.json"),
            "--markdown-out", str(cli_env["tmp_path"] / "r.md"),
            "--handoff-manifest", str(handoff_path),
            "--handoff-manifest-sha256", handoff_sha,
        ]
    ) == 2


def test_cli_all_orchestrates_full_pipeline(cli_env):
    """all = prepare -> execute -> audit -> report with injected adapter."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    # Prepare first to learn the real eval_id for the human audit file.
    code, eval_id = _run_prepare(module, cli_env, out_dir)
    assert code == 0
    audit_path = _valid_audit(cli_env, eval_id=eval_id)
    evidence_dir = cli_env["tmp_path"] / "report-evidence"
    _write_report_gate_evidence(
        evidence_dir,
        head=json.loads((out_dir / "prepare_summary.json").read_text(encoding="utf-8"))[
            "execution_head_sha256"
        ],
        eval_id=eval_id,
    )
    handoff_path = evidence_dir / "final_handoff_manifest.json"
    handoff_sha = hashlib.sha256(handoff_path.read_bytes()).hexdigest()
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
            "--pricing-json", str(cli_env["pricing_json"]),
            "--pricing-json", str(cli_env["pricing_json"]),
            "--runtime-db", str(cli_env["runtime_db"]),
            "--audit", str(audit_path),
            "--manifest-out", str(cli_env["tmp_path"] / "m.json"),
            "--json-out", str(json_out),
            "--markdown-out", str(cli_env["tmp_path"] / "all.md"),
            "--leakage-scan", str(evidence_dir / "report_inputs" / "leakage_scan.json"),
            "--derived-leakage-scan", str(evidence_dir / "derived" / "leakage_scan_after_terminal_status_exemption.json"),
            "--secret-scan", str(evidence_dir / "report_inputs" / "secret_scan.json"),
            "--runtime-evidence-db", str(evidence_dir / "runtime.sqlite3"),
            "--handoff-manifest", str(handoff_path),
            "--handoff-manifest-sha256", handoff_sha,
        ],
        runner_adapter_factory=factory,
    )
    assert exit_code == 0
    assert json_out.is_file()
    assert (out_dir / "execution_ledger.jsonl").is_file()
    assert (out_dir / "prepare_summary.json").is_file()


# ---------------------------------------------------------------------------
# supervisor blocker B: FAILED-case accounting durability
# ---------------------------------------------------------------------------

def _make_failed_case_factory(*, dispatch_log: list[str], fail_from_case: str):
    """Fake operator adapter that consumes real budget, then fails.

    The first case reserves+settles one provider attempt (real consumption is
    recorded on the shared guard); the case that would exceed the cost ceiling
    is refused before dispatch, exactly like the production invocation
    boundary. The adapter then returns the FAILED outcome the real M6 adapter
    would seal from its durable accounting artifact.
    """
    dispatched = {"count": 0}

    def adapter_factory(*, db_path=None, provider_budget=None):
        def _reserve_one(role: str):
            reservation = provider_budget.reserve(
                role=role, provider="deepseek", model_id="deepseek-chat"
            )
            provider_budget.settle(reservation)
            return provider_budget.case_snapshot()["case_used_cost_usd"]

        def run_case(case) -> object:
            from catalyst_eval.v1_1.runner import CaseRunOutcome

            dispatched["count"] += 1
            dispatch_log.append(case.case_id)
            consumed_cost = _reserve_one("evidence_analyst")
            if case.case_id == fail_from_case:
                from catalyst_agents.runtime.provider_budget import (
                    ProviderBudgetExceeded,
                )

                try:
                    _reserve_one("streaming_writer")
                except ProviderBudgetExceeded:
                    pass
                else:  # pragma: no cover - the ceiling must refuse the attempt
                    raise AssertionError(
                        "the cost ceiling must refuse the second provider attempt"
                    )
                return CaseRunOutcome(
                    case_id=case.case_id,
                    run_manifest_id=f"manifest:{case.case_id}",
                    run_manifest_hash=_manifest_hash(case.case_id),
                    result_artifact_id="",
                    result_artifact_hash="",
                    terminal_status="FAILED",
                    provider_calls=1,
                    cost_usd=consumed_cost,
                    run_facts={
                        "schema_version": "v1_1_stage1_run_facts_v1",
                        "case_id": case.case_id,
                        "terminal_status": "FAILED",
                        "provider_calls": 1,
                    },
                )
            run_manifest_payload = {
                "run_id": f"manifest:{case.case_id}", "case_id": case.case_id,
            }
            result_artifact_payload = {"result": case.case_id}
            return CaseRunOutcome(
                case_id=case.case_id,
                run_manifest_id=f"manifest:{case.case_id}",
                run_manifest_hash=_real_hash(run_manifest_payload),
                result_artifact_id=f"result:{case.case_id}",
                result_artifact_hash=_real_hash(result_artifact_payload),
                terminal_status="COMPLETED",
                run_manifest_payload=run_manifest_payload,
                result_artifact_payload=result_artifact_payload,
                provider_calls=1,
                cost_usd=consumed_cost,
                latency_ms=10,
                tokens=10,
                run_facts=_complete_fixture_run_facts(
                    case,
                    cost_usd=consumed_cost,
                ),
            )

        return run_case

    return adapter_factory, dispatched


def test_cli_failed_case_ledger_records_real_consumption_and_resume_deducts(
    cli_env,
):
    """A FAILED case keeps the calls/cost it consumed, and resume deducts them."""
    module = _load_cli()
    out_dir = cli_env["tmp_path"] / "out"
    # The fixture pricing makes one pre-call bound exactly $0.002
    # ((1000*1.0 + 500*2.0)/1e6); a $0.005 ceiling leaves room for the first
    # case's one attempt and exactly one attempt of the second case.
    code, eval_id = _run_prepare(module, cli_env, out_dir, max_provider_calls=10,
                                 max_cost_usd=0.005)
    assert code == 0
    assert eval_id

    first_case, second_case = cli_env["case_ids"][0], cli_env["case_ids"][1]
    dispatch_log: list[str] = []
    factory, dispatched = _make_failed_case_factory(
        dispatch_log=dispatch_log, fail_from_case=second_case
    )

    exit_code = module.main(
        [
            "execute",
            "--output-dir", str(out_dir),
            "--max-provider-calls", "10",
            "--max-cost-usd", "0.005",
            "--pricing-json", str(cli_env["pricing_json"]),
        ],
        runner_adapter_factory=factory,
    )
    assert exit_code == 2, "the FAILED case stops further dispatch"
    assert dispatch_log == [first_case, second_case]

    ledger = ExecutionLedger.load(
        out_dir / "execution_ledger.jsonl", expected_eval_id=eval_id
    )
    rows = {row.case_id: row for row in ledger.rows}
    assert rows[first_case].terminal_status == "COMPLETED"
    failed = rows[second_case]
    assert failed.terminal_status == "FAILED"
    # The real consumed accounting survives the FAILED terminal status.
    assert failed.provider_calls == 1
    assert failed.cost_usd == pytest.approx(0.002)
    assert failed.run_facts["terminal_status"] == "FAILED"

    # Restart/resume: the reloaded ledger deducts the FAILED row's consumption,
    # so the resumed run does not get the spent budget back and cannot refill
    # the ceiling. The re-dispatched COMPLETED rows replay their real
    # consumption on the way (1 call each), so case 2 is refused before any
    # second provider attempt can be dispatched.
    resume_dispatch: list[str] = []
    resume_factory, resumed = _make_failed_case_factory(
        dispatch_log=resume_dispatch, fail_from_case=second_case
    )
    resume_exit = module.main(
        [
            "execute",
            "--output-dir", str(out_dir),
            "--max-provider-calls", "10",
            "--max-cost-usd", "0.005",
            "--pricing-json", str(cli_env["pricing_json"]),
        ],
        runner_adapter_factory=resume_factory,
    )
    assert resume_exit == 2, "resume must not re-grant the already-spent budget"
    assert resumed["count"] == 1, (
        "resume must not re-dispatch a case once the deducted ledger ceiling "
        "is reached"
    )
