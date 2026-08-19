"""M1-8 exit operator CLI: fresh T4 + dual gates + NON-COMPARABLE report.

Version-controlled operator entrypoint that fails closed before any gate:
validates the clean sealed HEAD, the immutable integration commit SHA, the
frozen/runtime DB derivative isolation, and DEEPSEEK_API_KEY presence. Then it
runs fresh ``production_pinned`` T4 evidence, the four-arm gate, and the
user-smoke gate (which inherits the fresh four-arm evidence dir), binds both
evidence rows, publishes exactly one NON-COMPARABLE baseline report with
``promoted_env_recovered=false`` (Q-002 unrecovered), and re-scans the
published JSON. On any preflight/gate/leak failure it returns 2 without
writing a report. It never prints, logs, or persists credential values.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_eval.baseline.identity import is_app_default_db
from catalyst_eval.baseline.leakage import scan_baseline_report
from catalyst_eval.baseline.report import _build_report, write_baseline_report
from catalyst_eval.baseline.repro import (
    _validate_runtime_derivative,
    run_baseline_repro,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PREPARE_POST_IMPORT_WAVE2 = (
    REPO_ROOT / "packages" / "eval" / "scripts" / "prepare_post_import_wave2.py"
)
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")

_LAST_PREVIEW: dict[str, Any] | None = None


class ExitGateError(RuntimeError):
    """A fail-closed M1 exit gate step failed."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--integration-commit-sha", required=True)
    parser.add_argument("--frozen-db", required=True, type=Path)
    parser.add_argument("--runtime-db", required=True, type=Path)
    parser.add_argument("--lancedb-dir", required=True, type=Path)
    parser.add_argument("--index-manifest", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--t4-run-id", required=True)
    parser.add_argument("--four-arm-run-id", required=True)
    parser.add_argument("--user-smoke-run-id", required=True)
    return parser.parse_args(argv)


def _load_script(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load runner script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("git rev-parse HEAD failed")
    return result.stdout.strip()


def _git_status_porcelain(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("git status --porcelain failed")
    return result.stdout


def _preflight_environment(args: argparse.Namespace, env: dict[str, str]) -> None:
    """Fail closed unless the sealed HEAD, identity, DB, and secret env hold.

    Only confirms ``DEEPSEEK_API_KEY`` presence; the value is never printed,
    logged, persisted, or passed as a CLI argument.
    """
    if not env.get("DEEPSEEK_API_KEY"):
        raise ValueError("DEEPSEEK_API_KEY is required in the process environment")
    if not _HEX40.fullmatch(args.expected_head):
        raise ValueError("--expected-head must be 40 lowercase hex chars")
    if _git_head(REPO_ROOT) != args.expected_head:
        raise ValueError("--expected-head does not match git HEAD")
    if _git_status_porcelain(REPO_ROOT):
        raise ValueError("worktree is not clean")
    if not _HEX40.fullmatch(args.integration_commit_sha):
        raise ValueError("--integration-commit-sha must be 40 lowercase hex chars")
    # The sealed SHA comes from the flag, never recomputed from current refs.
    env["CATALYST_INTEGRATION_COMMIT_SHA"] = args.integration_commit_sha
    for path in (
        args.frozen_db,
        args.runtime_db,
        args.lancedb_dir,
        args.index_manifest,
        args.output_root,
        args.report_dir,
    ):
        if not path.is_absolute():
            raise ValueError(f"{path} must be an absolute path")
    run_ids = (args.t4_run_id, args.four_arm_run_id, args.user_smoke_run_id)
    if any(not run_id for run_id in run_ids) or len(set(run_ids)) != 3:
        raise ValueError(
            "--t4-run-id, --four-arm-run-id, --user-smoke-run-id must be "
            "distinct non-empty strings"
        )
    if is_app_default_db(args.runtime_db, repo_root=REPO_ROOT):
        raise ValueError(
            "runtime DB must not be the app default .local/live_runtime.db"
        )
    _validate_runtime_derivative(args.frozen_db, args.runtime_db)


def _run_t4(args: argparse.Namespace) -> Path:
    """Run the pinned production T4 evidence preparation in-process.

    Absorbs the T4 CLI's stdout so the operator CLI emits exactly one JSON
    object. FAST tests monkeypatch this seam and never touch GPU/DBs.
    """
    module = _load_script(PREPARE_POST_IMPORT_WAVE2)
    t4_argv = [
        "--db", str(args.frozen_db),
        "--run-id", args.t4_run_id,
        "--output-root", str(args.output_root),
        "--lancedb-dir", str(args.lancedb_dir),
        "--index-manifest", str(args.index_manifest),
        "--embedding-mode", "production_pinned",
    ]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = module.main(t4_argv)
    evidence_dir = Path(args.output_root) / args.t4_run_id
    if (
        code != 0
        or not (evidence_dir / "meta.json").is_file()
        or not (evidence_dir / "case_pack.jsonl").is_file()
    ):
        raise ExitGateError("T4 evidence generation failed")
    return evidence_dir


def _preview_report(
    result: Any, generated_at: str, git_revision: str
) -> dict[str, Any]:
    """Build the pre-publication report dict with the sealed comparability contract."""
    return _build_report(
        result.identity,
        list(result.runs),
        generated_at=generated_at,
        git_revision=git_revision,
        promoted_env_recovered=False,
    )


def _fail(error: str) -> int:
    print(json.dumps({"ok": False, "error": error}, sort_keys=True))
    return 2


def main(argv: list[str] | None = None) -> int:
    global _LAST_PREVIEW
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ)
    try:
        _preflight_environment(args, env)

        evidence_dir = _run_t4(args)

        # Q-002 unrecovered: no promoted_identity_env, no DEEPSEEK_API_KEY copy.
        gate_env = {
            "CATALYST_INTEGRATION_COMMIT_SHA": args.integration_commit_sha,
            "CATALYST_BASELINE_FROZEN_DB": str(args.frozen_db),
            "CATALYST_DB_PATH": str(args.runtime_db),
            "CATALYST_LANCEDB_DIR": str(args.lancedb_dir),
            "CATALYST_INDEX_MANIFEST_PATH": str(args.index_manifest),
            "CATALYST_BASELINE_OUTPUT_ROOT": str(args.output_root),
            "CATALYST_BASELINE_T4_EVIDENCE_DIR": str(evidence_dir),
            "CATALYST_BASELINE_FOUR_ARM_RUN_ID": args.four_arm_run_id,
            "CATALYST_BASELINE_USER_SMOKE_RUN_ID": args.user_smoke_run_id,
            "CATALYST_BASELINE_EMBEDDING_MODE": "production_pinned",
            # Fresh T4 case pack is authoritative for both gates; never fall
            # back to the gitignored repo-default data/run_reports case pack.
            "CATALYST_BASELINE_CASE_PACK": str(evidence_dir / "case_pack.jsonl"),
        }
        result = run_baseline_repro(
            four_arm=True,
            user_smoke=True,
            env=gate_env,
        )

        if not result.ok or len(result.runs) != 2:
            return _fail("gate evidence incomplete or failed")
        rows = {row.get("gate_kind"): row for row in result.runs}
        four_row = rows.get("four_arm")
        smoke_row = rows.get("user_smoke")
        if (
            not isinstance(four_row, dict)
            or four_row.get("run_id") != args.four_arm_run_id
            or not isinstance(smoke_row, dict)
            or smoke_row.get("run_id") != args.user_smoke_run_id
        ):
            return _fail("gate run ID binding does not match requested run IDs")

        generated_at = datetime.now(timezone.utc).isoformat()
        preview = _preview_report(result, generated_at, args.expected_head)
        _LAST_PREVIEW = preview
        if scan_baseline_report(preview):
            return _fail("baseline report leakage scan failed before publication")

        report_path = write_baseline_report(
            args.report_dir,
            result.identity,
            list(result.runs),
            generated_at=generated_at,
            promoted_env_recovered=False,
        )

        published = json.loads(Path(report_path).read_text(encoding="utf-8"))
        if scan_baseline_report(published):
            return _fail("published baseline report failed post-write leakage scan")

        summary = {
            "ok": True,
            "comparable": bool(result.comparable),
            "promoted_env_recovered": False,
            "promoted_env_reason": getattr(
                result,
                "comparability_reason",
                "q_002_promoted_environment_tuple_unrecovered",
            ),
            "report_path": str(report_path),
            "git_revision": args.expected_head,
            "t4_run_id": args.t4_run_id,
            "four_arm_run_id": args.four_arm_run_id,
            "user_smoke_run_id": args.user_smoke_run_id,
        }
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (ExitGateError, ValueError) as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(f"exit gate failed: {type(exc).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
