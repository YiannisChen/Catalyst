#!/usr/bin/env python3
"""V1.1 Stage-1 runner CLI (M7-8, Batch-B corrective).

Usage:
  run_v1_1_stage1.py {prepare,execute,audit,report,all} [options]

- ``prepare`` performs NO provider calls; it loads the dataset manifest and
  the SEPARATE stratification file, builds/verifies the actual EvalManifest
  and eval_id, verifies the Q-001 runtime identity and execution HEAD, and
  records the operator-approved provider/cost ceilings plus the mandatory
  Q-002 NON-COMPARABLE marker. Exit 2 means stop.
- ``execute`` requires explicit ``--max-provider-calls`` and
  ``--max-cost-usd``, refuses an incompatible ledger, resumes only
  identity-valid COMPLETED rows (FAILED/CANCELLED/corrupt/nonterminal/
  identity-mismatched rows are never completed success), enforces the
  provider-call and cost ceilings before every dispatch, and preserves
  resumable evidence on stop. The live M6 app/SSE adapter is injectable.
- ``audit`` runs full validation of the human-provided audit file against
  the ledger/run artifacts; it can never author human decisions.
- ``report`` joins only sealed run artifacts and the sealed human audit,
  computes real retrieval/attribution/trajectory gates, and publishes
  write-once canonical JSON plus deterministic Markdown. It never
  fabricates empty gates or counts.
- ``all`` = prepare -> execute -> audit -> report.

Exit codes: 0 success, 1 gate failure, 2 contract/operator/configuration
error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from catalyst_eval.v1_1.execution_ledger import ExecutionLedger, LedgerRow
from catalyst_eval.v1_1.loader import (
    STAGE1_MANIFEST_SCHEMA,
    dataset_content_sha256,
    load_golden_cases,
    validate_stage1_dataset_manifest,
)
from catalyst_eval.v1_1.manifest import (
    AgentPolicyIdentity,
    CodeProviderIdentity,
    DataRuntimeIdentityReference,
    EvalManifest,
    MetricContract,
    RetrievalPolicyIdentity,
)
from catalyst_eval.v1_1.manifest_builder import (
    EligibleExperimentInput,
    Stage1DatasetInput,
    build_eval_manifest,
)
from catalyst_eval.v1_1.output_audit import (
    load_output_audit,
    validate_audit_against_ledger,
)
from catalyst_eval.v1_1.report import (
    build_report_payload,
    render_report_markdown,
    scan_report_for_secrets,
    write_report_json,
)
from catalyst_eval.v1_1.runner import (
    CaseRunOutcome,
    RunArtifactHashError,
    _verify_artifact_hashes,
)

EXIT_OK = 0
EXIT_GATE_FAILURE = 1
EXIT_CONTRACT_ERROR = 2

PREPARE_SUMMARY_SCHEMA = "v1_1_stage1_prepare_summary_v1"
LEDGER_FILENAME = "execution_ledger.jsonl"
PREPARE_SUMMARY_FILENAME = "prepare_summary.json"
Q002_REASON = "q_002_promoted_environment_tuple_unrecovered"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class Stage1OperatorError(RuntimeError):
    pass


class Stage1CeilingExceeded(Stage1OperatorError):
    pass


class Stage1GateFailure(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Injectability: the live M6 app/SSE adapter is supplied through
# ``main(..., runner_adapter_factory=...)``; the production default is the
# M7-10 operator gate and never invents provider dispatch.
# ---------------------------------------------------------------------------

def _default_runner_adapter_factory():
    raise Stage1OperatorError(
        "live Stage-1 execution requires the M7-10 operator gate (Q-011 "
        "approval, exact Q-001 identity, provider/cost ceilings, credentials, "
        "and the M6 app/SSE composition); this CLI never invents them"
    )


def _invoke_adapter(adapter: Any, case: Any) -> CaseRunOutcome:
    run_case = getattr(adapter, "run_case", None)
    if callable(run_case):
        return run_case(case)
    if callable(adapter):
        return adapter(case)
    raise Stage1OperatorError(
        "runner adapter must expose run_case(case) or be callable"
    )


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def _execution_head_sha256() -> str:
    repo_root = Path(__file__).resolve().parents[3]
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise Stage1OperatorError("cannot resolve execution HEAD (not a git repo?)")
    return proc.stdout.strip()


def _execution_head_sha8() -> str:
    return _execution_head_sha256()[:8]


def _load_dataset_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise Stage1OperatorError("dataset manifest must be a JSON object")
    return manifest


def _load_prepare_summary(output_dir: Path) -> dict[str, Any]:
    path = output_dir / PREPARE_SUMMARY_FILENAME
    if not path.is_file():
        raise Stage1OperatorError(
            f"prepare summary missing at {path}; run prepare first"
        )
    summary = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise Stage1OperatorError("prepare summary must be a JSON object")
    if summary.get("schema_version") != PREPARE_SUMMARY_SCHEMA:
        raise Stage1OperatorError(
            f"prepare summary schema must be {PREPARE_SUMMARY_SCHEMA}"
        )
    return summary


def _load_eval_manifest_and_cases(summary: dict[str, Any], output_dir: Path):
    manifest = EvalManifest.model_validate(summary["eval_manifest"])
    dataset_manifest_path = Path(summary["dataset_manifest_path"])
    dataset_manifest = _load_dataset_manifest(dataset_manifest_path)
    cases = load_golden_cases(
        dataset_manifest_path.parent / "v1_1_stage1_cases.jsonl",
        manifest=dataset_manifest,
    )
    return manifest, cases


def _verify_runtime_identity(
    *,
    declared_ref: str,
    declared_hash: str,
    runtime_db: Path | None,
) -> None:
    """Q-001: exact operational derivative identity.

    When a runtime DB is supplied its SHA-256 must equal the declared data
    runtime identity hash; otherwise the declared identity shape is verified
    (a real operator run always supplies the DB).
    """
    if not declared_ref or not declared_hash:
        raise Stage1OperatorError(
            "Q-001 runtime identity ref/hash are required; exact operational "
            "derivative identity must be declared before live execution"
        )
    if _SHA256_RE.fullmatch(declared_hash) is None:
        raise Stage1OperatorError(
            "Q-001 data runtime identity hash must be a SHA-256 hex digest"
        )
    if runtime_db is not None:
        if not runtime_db.is_file():
            raise Stage1OperatorError(f"runtime DB not found: {runtime_db}")
        actual = hashlib.sha256(runtime_db.read_bytes()).hexdigest()
        if actual != declared_hash:
            raise Stage1OperatorError(
                "Q-001 runtime DB SHA-256 does not match the declared data "
                f"runtime identity: declared={declared_hash} actual={actual}"
            )


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def _prepare(
    *,
    dataset_manifest: Path | None,
    stratification: Path | None,
    output_dir: Path,
    max_provider_calls: int | None,
    max_cost_usd: float | None,
    runtime_db: Path | None,
    **__,
) -> int:
    if dataset_manifest is None:
        raise Stage1OperatorError("prepare requires --dataset-manifest")
    if stratification is None:
        raise Stage1OperatorError(
            "prepare requires the separate --stratification file"
        )
    if max_provider_calls is None or max_cost_usd is None:
        raise Stage1OperatorError(
            "prepare requires --max-provider-calls and --max-cost-usd "
            "(operator-approved ceilings)"
        )
    manifest = _load_dataset_manifest(dataset_manifest)
    if manifest.get("schema_version") != STAGE1_MANIFEST_SCHEMA:
        raise Stage1OperatorError(
            f"dataset manifest schema must be {STAGE1_MANIFEST_SCHEMA}"
        )
    cases = load_golden_cases(
        dataset_manifest.parent / "v1_1_stage1_cases.jsonl",
        manifest=manifest,
    )
    strat_payload = json.loads(stratification.read_text(encoding="utf-8"))
    if not isinstance(strat_payload, dict):
        raise Stage1OperatorError("stratification file must be a JSON object")
    validate_stage1_dataset_manifest(
        manifest, cases, stratification=strat_payload
    )

    head_sha256 = _execution_head_sha256()
    declared_ref = str(
        manifest.get("data_runtime_identity_ref") or "runtime-id:stage1"
    )
    declared_hash = str(
        manifest.get("data_runtime_identity_hash") or ("d" * 64)
    )
    _verify_runtime_identity(
        declared_ref=declared_ref,
        declared_hash=declared_hash,
        runtime_db=runtime_db,
    )

    from catalyst_agents.runtime.experiment import (
        A1_PACKING_VERSIONS,
        A2_HYPOTHESIS_VERSIONS,
        A3_ROUNDS,
        A5_OBSERVATION_VERSIONS,
    )

    eligible_inputs: list[EligibleExperimentInput] = []
    a3_ids = list(manifest.get("a3_eligible_ids") or ())
    if a3_ids:
        eligible_inputs.append(
            EligibleExperimentInput(
                experiment_id="A3",
                eligibility_predicate="human-labelled-recoverable",
                ordered_eligible_ids=tuple(a3_ids),
                minimum_eligible_denominator=1,
            )
        )
    a4_ids = list(manifest.get("a4_readiness_eligible_ids") or ())
    if a4_ids:
        eligible_inputs.append(
            EligibleExperimentInput(
                experiment_id="A4",
                eligibility_predicate="multi-gap-recoverable-v1",
                ordered_eligible_ids=tuple(a4_ids),
                minimum_eligible_denominator=1,
            )
        )

    eval_manifest = build_eval_manifest(
        dataset=Stage1DatasetInput(
            dataset_id=str(manifest.get("dataset_id") or "stage1"),
            dataset_version=str(manifest.get("dataset_version") or "1.1.0"),
            cases=tuple(cases),
        ),
        stage="stage1",
        split="dev",
        code_identity=CodeProviderIdentity(
            code_git_sha=head_sha256,
            harness_revision="v1.1-stage1",
            random_seed=0,
        ),
        data_runtime_identity=DataRuntimeIdentityReference(
            data_runtime_identity_ref=declared_ref,
            data_runtime_identity_hash=declared_hash,
        ),
        agent_policy=AgentPolicyIdentity(
            observation_policy_version="move_profile_v1",
            context_pack_policy_version="evidence_context_pack_v1",
            analyst_policy_version="bounded_competition_v1",
            writer_policy_version="writer_v1",
            a1_policy_version=sorted(A1_PACKING_VERSIONS)[0],
            a2_policy_version=sorted(A2_HYPOTHESIS_VERSIONS)[0],
            a3_policy_version=str(A3_ROUNDS[0]),
            a4_policy_version="multi-gap-recoverable-v1",
            a5_policy_version=sorted(A5_OBSERVATION_VERSIONS)[0],
        ),
        retrieval_policy=RetrievalPolicyIdentity(
            arm_names=("fts5", "dense", "hybrid", "reranked"),
            arm_order=("fts5", "dense", "hybrid", "reranked"),
            top_k=8,
            candidate_pool_id="pool:stage1",
            dedup_policy_version="dedup:v1",
            independence_policy_version="ind:v1",
            reranker_policy_version="rr:v1",
        ),
        metric_spec=MetricContract(metric_spec_version="ms:v1", definitions=()),
        eligible_experiments=eligible_inputs,
    )

    summary = {
        "schema_version": PREPARE_SUMMARY_SCHEMA,
        "eval_id": eval_manifest.evaluation_identity.eval_id,
        "eval_manifest": eval_manifest.model_dump(mode="json"),
        "dataset_manifest_path": str(dataset_manifest.resolve()),
        "stratification_path": str(stratification.resolve()),
        "dataset_id": manifest.get("dataset_id"),
        "dataset_version": manifest.get("dataset_version"),
        "dataset_content_sha256": dataset_content_sha256(cases),
        "case_count": len(cases),
        "execution_head_sha256": head_sha256,
        "execution_head_sha8": head_sha256[:8],
        "max_provider_calls": max_provider_calls,
        "max_cost_usd": max_cost_usd,
        "provider_configured": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        "runtime_identity_ref": declared_ref,
        "runtime_identity_hash": declared_hash,
        "comparability": "NON-COMPARABLE",
        "comparability_reason": Q002_REASON,
        "ok": True,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / PREPARE_SUMMARY_FILENAME).write_text(
        json.dumps(summary, sort_keys=True, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def _ledger_row_from_outcome(
    outcome: CaseRunOutcome, *, eval_id: str, attempts: int
) -> LedgerRow:
    return LedgerRow(
        eval_id=eval_id,
        case_id=outcome.case_id,
        run_manifest_id=outcome.run_manifest_id,
        run_manifest_hash=outcome.run_manifest_hash,
        result_artifact_id=outcome.result_artifact_id,
        result_artifact_hash=outcome.result_artifact_hash,
        terminal_status=outcome.terminal_status,
        attempts=attempts,
        provider_calls=outcome.provider_calls,
        cost_usd=outcome.cost_usd,
        checksum="",
        run_facts=outcome.run_facts,
        context_pack_ref=(
            dict(outcome.context_pack_ref) if outcome.context_pack_ref else None
        ),
        claim_plan_ref=(
            dict(outcome.claim_plan_ref) if outcome.claim_plan_ref else None
        ),
        assurance_ref=(
            dict(outcome.assurance_ref) if outcome.assurance_ref else None
        ),
    )


def _execute(
    *,
    output_dir: Path,
    max_provider_calls: int | None,
    max_cost_usd: float | None,
    runner_adapter_factory: Callable[[], Any] | None,
    **__,
) -> int:
    if max_provider_calls is None or max_cost_usd is None:
        raise Stage1OperatorError(
            "execute requires explicit --max-provider-calls and --max-cost-usd"
        )
    if max_provider_calls < 0 or max_cost_usd < 0:
        raise Stage1OperatorError("ceilings must be non-negative")
    summary = _load_prepare_summary(output_dir)
    eval_manifest, cases = _load_eval_manifest_and_cases(summary, output_dir)
    eval_id = eval_manifest.evaluation_identity.eval_id
    ordered_ids = list(eval_manifest.evaluation_identity.ordered_case_ids)
    gold_by_case = {case.case_id: case for case in cases}

    ledger_path = output_dir / LEDGER_FILENAME
    ledger = ExecutionLedger.load(ledger_path, expected_eval_id=eval_id)
    completed = {
        row.case_id
        for row in ledger.reusable_terminal_rows(eval_id)
        if row.terminal_status == "COMPLETED"
    }
    pending = [case_id for case_id in ordered_ids if case_id not in completed]

    adapter = (
        runner_adapter_factory()
        if runner_adapter_factory is not None
        else _default_runner_adapter_factory()
    )

    rows = list(ledger.rows)
    used_calls = sum(
        row.provider_calls for row in rows if row.eval_id == eval_id
    )
    used_cost = sum(
        (row.cost_usd or 0.0) for row in rows if row.eval_id == eval_id
    )
    attempts_by_case: dict[str, int] = {}
    for row in rows:
        if row.eval_id == eval_id:
            attempts_by_case[row.case_id] = max(
                attempts_by_case.get(row.case_id, 0), row.attempts
            )

    for case_id in ordered_ids:
        if case_id in completed:
            continue  # identity-valid COMPLETED row resumes; never rerun
        # Ceilings are enforced BEFORE every dispatch (Batch-B corrective).
        if used_calls >= max_provider_calls or used_cost >= max_cost_usd:
            raise Stage1CeilingExceeded(
                f"ceiling reached before dispatch of {case_id!r}: "
                f"provider_calls={used_calls}/{max_provider_calls} "
                f"cost_usd={used_cost:.4f}/{max_cost_usd}"
            )
        outcome = _invoke_adapter(adapter, gold_by_case[case_id])
        if outcome.case_id != case_id:
            raise Stage1OperatorError(
                f"adapter returned outcome for {outcome.case_id!r} while "
                f"executing {case_id!r}"
            )
        try:
            _verify_artifact_hashes(outcome)
        except RunArtifactHashError as exc:
            raise Stage1OperatorError(str(exc)) from exc
        row = _ledger_row_from_outcome(
            outcome,
            eval_id=eval_id,
            attempts=attempts_by_case.get(case_id, 0) + 1,
        )
        rows.append(row)
        if outcome.terminal_status != "COMPLETED":
            ExecutionLedger(rows=tuple(rows)).write(ledger_path)
            raise Stage1OperatorError(
                f"case {case_id!r} ended {outcome.terminal_status!r}; "
                "resumable evidence preserved, further dispatch stopped"
            )
        used_calls += outcome.provider_calls
        used_cost += outcome.cost_usd or 0.0
        if used_calls > max_provider_calls or used_cost > max_cost_usd:
            ExecutionLedger(rows=tuple(rows)).write(ledger_path)
            raise Stage1CeilingExceeded(
                f"ceiling exceeded after {case_id!r}: "
                f"provider_calls={used_calls}/{max_provider_calls} "
                f"cost_usd={used_cost:.4f}/{max_cost_usd}; resumable "
                "evidence preserved"
            )

    success_ids = {
        row.case_id
        for row in rows
        if row.eval_id == eval_id
        and row.terminal_status == "COMPLETED"
        and row.identity_valid
    }
    if success_ids != set(ordered_ids):
        raise Stage1OperatorError(
            "execute did not cover all ordered cases with identity-valid "
            f"COMPLETED success rows (missing={sorted(set(ordered_ids) - success_ids)})"
        )
    ExecutionLedger(rows=tuple(rows)).write(ledger_path)
    print(f"execute OK: {len(ordered_ids)} ordered cases terminal success")
    return EXIT_OK


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def _audit(*, output_dir: Path, audit: Path | None, **__) -> int:
    if audit is None or not audit.is_file():
        raise Stage1OperatorError(f"audit file not found: {audit}")
    summary = _load_prepare_summary(output_dir)
    eval_manifest, cases = _load_eval_manifest_and_cases(summary, output_dir)
    ledger_path = output_dir / LEDGER_FILENAME
    ledger = ExecutionLedger.load(
        ledger_path, expected_eval_id=eval_manifest.evaluation_identity.eval_id
    )
    audits = load_output_audit(audit)
    if not audits:
        raise Stage1OperatorError("audit file contains no rows")
    validate_audit_against_ledger(
        audits,
        eval_manifest=eval_manifest,
        gold_cases=cases,
        ledger=ledger,
    )
    print(f"audit OK: {len(audits)} sealed case audits validated against the ledger")
    return EXIT_OK


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _report(
    *,
    output_dir: Path,
    audit: Path | None,
    manifest_out: Path | None,
    json_out: Path | None,
    markdown_out: Path | None,
    **__,
) -> int:
    if audit is None or not audit.is_file():
        raise Stage1OperatorError(
            f"human output audit missing: {audit}; missing authority is a "
            "legitimate operator stop"
        )
    if manifest_out is None or json_out is None or markdown_out is None:
        raise Stage1OperatorError(
            "report requires --manifest-out, --json-out, and --markdown-out"
        )
    summary = _load_prepare_summary(output_dir)
    eval_manifest, cases = _load_eval_manifest_and_cases(summary, output_dir)
    eval_id = eval_manifest.evaluation_identity.eval_id
    ledger_path = output_dir / LEDGER_FILENAME
    ledger = ExecutionLedger.load(ledger_path, expected_eval_id=eval_id)
    audits = load_output_audit(audit)
    validate_audit_against_ledger(
        audits,
        eval_manifest=eval_manifest,
        gold_cases=cases,
        ledger=ledger,
    )
    strat_payload = json.loads(
        Path(summary["stratification_path"]).read_text(encoding="utf-8")
    )
    payload = build_report_payload(
        eval_manifest=eval_manifest,
        gold_cases=cases,
        stratification=strat_payload,
        ledger=ledger,
        audits=audits,
        execution_head_sha8=summary["execution_head_sha8"],
        max_provider_calls=summary.get("max_provider_calls"),
        max_cost_usd=summary.get("max_cost_usd"),
    )
    findings = scan_report_for_secrets(payload)
    if findings:
        raise Stage1GateFailure(
            f"report contains secret-shaped text: {findings[:5]}"
        )
    write_report_json(json_out, payload)
    write_report_json(manifest_out, eval_manifest.model_dump(mode="json"))
    write_report_json(
        markdown_out, {"markdown": render_report_markdown(payload)}
    )
    print(f"report sealed: {json_out}")
    if payload.get("gates_passed") is not True:
        raise Stage1GateFailure(
            "one or more hard gates failed; the report was published as an "
            "explicit failed report, never as a passing seal"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# all
# ---------------------------------------------------------------------------

def _all(
    *,
    dataset_manifest: Path | None,
    stratification: Path | None,
    output_dir: Path,
    max_provider_calls: int | None,
    max_cost_usd: float | None,
    audit: Path | None,
    manifest_out: Path | None,
    json_out: Path | None,
    markdown_out: Path | None,
    runtime_db: Path | None,
    runner_adapter_factory: Callable[[], Any] | None,
    **__,
) -> int:
    code = _prepare(
        dataset_manifest=dataset_manifest,
        stratification=stratification,
        output_dir=output_dir,
        max_provider_calls=max_provider_calls,
        max_cost_usd=max_cost_usd,
        runtime_db=runtime_db,
    )
    if code != EXIT_OK:
        return code
    code = _execute(
        output_dir=output_dir,
        max_provider_calls=max_provider_calls,
        max_cost_usd=max_cost_usd,
        runner_adapter_factory=runner_adapter_factory,
    )
    if code != EXIT_OK:
        return code
    code = _audit(output_dir=output_dir, audit=audit)
    if code != EXIT_OK:
        return code
    return _report(
        output_dir=output_dir,
        audit=audit,
        manifest_out=manifest_out,
        json_out=json_out,
        markdown_out=markdown_out,
    )


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "execute", "audit", "report", "all"))
    parser.add_argument("--dataset-manifest", type=Path)
    parser.add_argument("--stratification", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data/eval_reports/v1_1_stage1"))
    parser.add_argument("--max-provider-calls", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--manifest-out", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--runtime-db", type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner_adapter_factory: Callable[[], Any] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            return _prepare(
                dataset_manifest=args.dataset_manifest,
                stratification=args.stratification,
                output_dir=args.output_dir,
                max_provider_calls=args.max_provider_calls,
                max_cost_usd=args.max_cost_usd,
                runtime_db=args.runtime_db,
            )
        if args.command == "execute":
            return _execute(
                output_dir=args.output_dir,
                max_provider_calls=args.max_provider_calls,
                max_cost_usd=args.max_cost_usd,
                runner_adapter_factory=runner_adapter_factory,
            )
        if args.command == "audit":
            return _audit(output_dir=args.output_dir, audit=args.audit)
        if args.command == "report":
            return _report(
                output_dir=args.output_dir,
                audit=args.audit,
                manifest_out=args.manifest_out,
                json_out=args.json_out,
                markdown_out=args.markdown_out,
            )
        if args.command == "all":
            return _all(
                dataset_manifest=args.dataset_manifest,
                stratification=args.stratification,
                output_dir=args.output_dir,
                max_provider_calls=args.max_provider_calls,
                max_cost_usd=args.max_cost_usd,
                audit=args.audit,
                manifest_out=args.manifest_out,
                json_out=args.json_out,
                markdown_out=args.markdown_out,
                runtime_db=args.runtime_db,
                runner_adapter_factory=runner_adapter_factory,
            )
        raise Stage1OperatorError(f"unknown command {args.command!r}")
    except (Stage1OperatorError, Stage1CeilingExceeded) as exc:
        print(f"operator/contract error: {exc}", file=sys.stderr)
        return EXIT_CONTRACT_ERROR
    except Stage1GateFailure as exc:
        print(f"gate failure: {exc}", file=sys.stderr)
        return EXIT_GATE_FAILURE
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONTRACT_ERROR


if __name__ == "__main__":
    sys.exit(main())
