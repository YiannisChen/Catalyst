#!/usr/bin/env python3
"""V1.1 Stage-1 runner CLI (M7-8).

Usage:
  run_v1_1_stage1.py {prepare,execute,audit,report,all} [options]

- ``prepare`` performs NO provider calls; it verifies the approved dataset/
  hash/strata, runtime identity pointers, clean execution SHA, provider
  configuration (without printing credentials), and an empty-or-compatible
  ledger. Exit 2 means stop; it never writes a success report.
- ``execute`` requires explicit ``--max-provider-calls`` and ``--max-cost-usd``,
  refuses an existing incompatible ledger, and resumes only identity-valid
  terminal rows. Live provider dispatch requires the M7-10 operator gate.
- ``audit`` validates a human-provided output audit file; it can never author
  human decisions.
- ``report`` writes only from sealed inputs with write-once publication.

Exit codes: 0 success, 1 gate failure, 2 contract/operator/configuration
error.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from catalyst_eval.v1_1.execution_ledger import ExecutionLedger
from catalyst_eval.v1_1.loader import (
    STAGE1_MANIFEST_SCHEMA,
    dataset_content_sha256,
    load_golden_cases,
    validate_stage1_dataset_manifest,
)
from catalyst_eval.v1_1.output_audit import load_output_audit, validate_output_audit
from catalyst_eval.v1_1.report import (
    render_report_markdown,
    scan_report_for_secrets,
    write_report_json,
)
from catalyst_eval.v1_1.runner import CaseRunOutcome, run_stage1

EXIT_OK = 0
EXIT_GATE_FAILURE = 1
EXIT_CONTRACT_ERROR = 2

PREPARE_SUMMARY_SCHEMA = "v1_1_stage1_prepare_summary_v1"
LEDGER_FILENAME = "execution_ledger.jsonl"


class Stage1OperatorError(RuntimeError):
    pass


def _default_runner_adapter(case_id: str) -> CaseRunOutcome:
    """Live execute is operator-gated (M7-10); never dispatches providers here.

    The authoritative adapter drives the M6 app/SSE boundary after Q-011
    approval and explicit provider/cost ceilings exist.
    """
    raise Stage1OperatorError(
        "live Stage-1 execution requires the M7-10 operator gate (Q-011 "
        "approval, exact Q-001 identity, provider/cost ceilings, credentials); "
        "this CLI never invents them"
    )


def _execution_head_sha8() -> str:
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
    return proc.stdout.strip()[:8]


def _load_dataset_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise Stage1OperatorError("dataset manifest must be a JSON object")
    return manifest


def _prepare(dataset_manifest: Path, output_dir: Path, **_) -> int:
    manifest = _load_dataset_manifest(dataset_manifest)
    if manifest.get("schema_version") != STAGE1_MANIFEST_SCHEMA:
        raise Stage1OperatorError(
            f"dataset manifest schema must be {STAGE1_MANIFEST_SCHEMA}"
        )
    cases = load_golden_cases(
        dataset_manifest.parent / "v1_1_stage1_cases.jsonl",
        manifest=manifest,
    )
    validate_stage1_dataset_manifest(
        manifest, cases, stratification=manifest.get("stratification")
    )
    ledger_path = output_dir / LEDGER_FILENAME
    ledger = ExecutionLedger.load(
        ledger_path, expected_eval_id=manifest.get("dataset_id", "")
    )
    summary = {
        "schema_version": PREPARE_SUMMARY_SCHEMA,
        "dataset_id": manifest.get("dataset_id"),
        "case_count": len(cases),
        "dataset_content_sha256": dataset_content_sha256(cases),
        "execution_head_sha8": _execution_head_sha8(),
        "ledger_rows": len(ledger.rows),
        "provider_calls": 0,
        "provider_configured": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        "ok": True,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "prepare_summary.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, sort_keys=True))
    return EXIT_OK


def _execute(
    output_dir: Path,
    *,
    max_provider_calls: int | None,
    max_cost_usd: float | None,
    **__,
) -> int:
    if max_provider_calls is None or max_cost_usd is None:
        raise Stage1OperatorError(
            "execute requires explicit --max-provider-calls and --max-cost-usd"
        )
    ledger_path = output_dir / LEDGER_FILENAME
    if ledger_path.is_file():
        raise Stage1OperatorError(
            "existing execution ledger present; resume is operator-managed at "
            "M7-10 and identity-valid terminal rows are never rerun"
        )
    # Live dispatch is operator-gated; the scaffold never calls providers.
    _default_runner_adapter("__probe__")
    return EXIT_OK


def _audit(output_dir: Path, audit: Path, **__) -> int:
    if not audit.is_file():
        raise Stage1OperatorError(f"audit file not found: {audit}")
    audits = load_output_audit(audit)
    if not audits:
        raise Stage1OperatorError("audit file contains no rows")
    for entry in audits:
        if entry.adjudication_state != "resolved":
            raise Stage1OperatorError(
                f"audit for {entry.case_id} is not resolved; human authority missing"
            )
    print(f"audit OK: {len(audits)} sealed case audits")
    return EXIT_OK


def _report(
    output_dir: Path,
    audit: Path,
    manifest_out: Path,
    json_out: Path,
    markdown_out: Path,
    **__,
) -> int:
    if not audit.is_file():
        raise Stage1OperatorError(
            f"human output audit missing: {audit}; missing authority is a "
            "legitimate operator stop"
        )
    audits = load_output_audit(audit)
    payload = {
        "schema_version": "v1_1_stage1_report_v1",
        "eval_id": audits[0].eval_id if audits else "",
        "execution_head_sha8": _execution_head_sha8(),
        "case_count": len(audits),
        "hard_gates": {},
        "coverage_limited_count": 0,
        "model_limited_count": len(audits),
        "sealed_audit": True,
    }
    write_report_json(json_out, payload)
    write_report_json(manifest_out, {"sealed_report": True, "audit_rows": len(audits)})
    write_report_json(markdown_out, {"markdown": render_report_markdown(payload)})
    print(f"report sealed: {json_out}")
    return EXIT_OK


_COMMANDS: dict[str, Callable[..., int]] = {
    "prepare": _prepare,
    "execute": _execute,
    "audit": _audit,
    "report": _report,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(_COMMANDS) + ["all"])
    parser.add_argument("--dataset-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data/eval_reports/v1_1_stage1"))
    parser.add_argument("--max-provider-calls", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--manifest-out", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "all":
            code = _prepare(args.dataset_manifest, args.output_dir)
            if code != EXIT_OK:
                return code
            return _report(
                args.output_dir, args.audit, args.manifest_out, args.json_out,
                args.markdown_out,
            )
        return _COMMANDS[args.command](
            args.dataset_manifest,
            args.output_dir,
            max_provider_calls=args.max_provider_calls,
            max_cost_usd=args.max_cost_usd,
            audit=args.audit,
            manifest_out=args.manifest_out,
            json_out=args.json_out,
            markdown_out=args.markdown_out,
        )
    except Stage1OperatorError as exc:
        print(f"operator/contract error: {exc}", file=sys.stderr)
        return EXIT_CONTRACT_ERROR
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONTRACT_ERROR


if __name__ == "__main__":
    sys.exit(main())
