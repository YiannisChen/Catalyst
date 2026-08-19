"""M1-6: reproducible baseline gate wrapper.

``run_baseline_repro`` orchestrates the post-import four-arm and user-smoke
entrypoints under a supplied environment, collects their success tokens and
baseline run rows, and binds them to the sealed identity tuple. The sealed
Git identity is validated before any gate runs; missing non-sealed identity
fields produce an explicitly NON-COMPARABLE result.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

from catalyst_eval.baseline.identity import BaselineIdentity, sealed_identity_tuple
from catalyst_eval.baseline.report import DATA_IDENTITY_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[4]
FOUR_ARM_SCRIPT = (
    REPO_ROOT / "packages" / "eval" / "scripts" / "run_post_import_four_arm.py"
)
USER_SMOKE_SCRIPT = (
    REPO_ROOT / "packages" / "eval" / "scripts" / "run_post_import_user_smoke.py"
)
FOUR_ARM_SUCCESS_TOKEN = "FOUR_ARM_E2E_OK"
DEFAULT_OUTPUT_ROOT = "data/run_reports/post_import"


def _missing_ids(identity: BaselineIdentity) -> tuple[str, ...]:
    return tuple(field for field in DATA_IDENTITY_FIELDS if getattr(identity, field) is None)


@dataclasses.dataclass(frozen=True)
class BaselineReproResult:
    """Collected baseline reproduction evidence plus comparability verdict."""

    identity: BaselineIdentity
    missing_ids: tuple[str, ...]
    comparable: bool
    ok: bool
    four_arm_token: str | None
    user_smoke_evidence_dir: str | None
    runs: tuple[dict[str, Any], ...]


@contextlib.contextmanager
def _applied_env(env: Mapping[str, str]):
    """Temporarily apply the supplied env over the current process env."""
    saved = dict(os.environ)
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _load_script(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load runner script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_cli(module, argv: list[str]) -> tuple[int, dict[str, Any]]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = module.main(argv)
    try:
        payload = json.loads(buffer.getvalue())
    except (json.JSONDecodeError, TypeError):
        payload = {"ok": False, "error": "runner emitted non-JSON stdout"}
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": "runner emitted non-object JSON"}
    return int(code), payload


def _four_arm_argv(env: Mapping[str, str]) -> list[str]:
    required = ("CATALYST_DB_PATH", "CATALYST_LANCEDB_DIR", "CATALYST_INDEX_MANIFEST_PATH")
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise ValueError(f"four-arm gate requires env: {', '.join(missing)}")
    argv = [
        "--db", env["CATALYST_DB_PATH"],
        "--lancedb-dir", env["CATALYST_LANCEDB_DIR"],
        "--case-pack", env.get(
            "CATALYST_BASELINE_CASE_PACK",
            str(REPO_ROOT / "data" / "run_reports" / "post_import" / "case_pack.jsonl"),
        ),
        "--run-id", env.get(
            "CATALYST_BASELINE_RUN_ID",
            f"baseline_repro_four_arm_{uuid.uuid4().hex[:12]}",
        ),
        "--output-root", env.get("CATALYST_BASELINE_OUTPUT_ROOT", DEFAULT_OUTPUT_ROOT),
        "--embedding-mode", env.get("CATALYST_BASELINE_EMBEDDING_MODE", "production_pinned"),
        "--index-manifest", env["CATALYST_INDEX_MANIFEST_PATH"],
    ]
    t4_dir = env.get("CATALYST_BASELINE_T4_EVIDENCE_DIR")
    if t4_dir:
        argv += ["--t4-evidence-dir", t4_dir]
    return argv


def _user_smoke_argv(env: Mapping[str, str]) -> list[str]:
    required = (
        "CATALYST_DB_PATH",
        "CATALYST_LANCEDB_DIR",
        "CATALYST_INDEX_MANIFEST_PATH",
        "CATALYST_BASELINE_T4_EVIDENCE_DIR",
        "CATALYST_BASELINE_WAVE2_EVIDENCE_DIR",
    )
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise ValueError(f"user-smoke gate requires env: {', '.join(missing)}")
    frozen_db = env.get("CATALYST_BASELINE_FROZEN_DB")
    if not frozen_db:
        raise ValueError("user-smoke gate requires env: CATALYST_BASELINE_FROZEN_DB")
    return [
        "--db", frozen_db,
        "--lancedb-dir", env["CATALYST_LANCEDB_DIR"],
        "--index-manifest", env["CATALYST_INDEX_MANIFEST_PATH"],
        "--t4-evidence-dir", env["CATALYST_BASELINE_T4_EVIDENCE_DIR"],
        "--wave2-evidence-dir", env["CATALYST_BASELINE_WAVE2_EVIDENCE_DIR"],
        "--case-pack", env.get("CATALYST_BASELINE_CASE_PACK", ""),
        "--run-id", env.get(
            "CATALYST_BASELINE_RUN_ID",
            f"baseline_repro_user_smoke_{uuid.uuid4().hex[:12]}",
        ),
        "--output-root", env.get("CATALYST_BASELINE_OUTPUT_ROOT", DEFAULT_OUTPUT_ROOT),
        "--embedding-mode", "production_pinned",
        "--provider", env.get("CATALYST_BASELINE_PROVIDER", "deepseek"),
        "--model-id", env.get("CATALYST_DEFAULT_MODEL", "deepseek-v4-flash"),
    ]


def _run_four_arm_gate(env: Mapping[str, str], identity: BaselineIdentity) -> dict[str, Any]:
    """EXPENSIVE default seam: run the post-import four-arm CLI in-process."""
    module = _load_script(FOUR_ARM_SCRIPT)
    with _applied_env(env):
        code, payload = _run_cli(module, _four_arm_argv(env))
    row = payload.get("baseline_run_row") if isinstance(payload.get("baseline_run_row"), dict) else {}
    token_written = bool(payload.get("token_written"))
    return {
        "ok": code == 0 and payload.get("ok") is True,
        "exit_code": code,
        "token": FOUR_ARM_SUCCESS_TOKEN if token_written else None,
        "run_row": row,
        "error": payload.get("error"),
    }


def _run_user_smoke_gate(env: Mapping[str, str], identity: BaselineIdentity) -> dict[str, Any]:
    """EXPENSIVE default seam: run the post-import user-smoke CLI in-process."""
    module = _load_script(USER_SMOKE_SCRIPT)
    with _applied_env(env):
        code, payload = _run_cli(module, _user_smoke_argv(env))
    row = payload.get("baseline_run_row") if isinstance(payload.get("baseline_run_row"), dict) else {}
    meta_path = payload.get("meta_path")
    evidence_dir = str(Path(meta_path).parent) if meta_path else None
    return {
        "ok": code == 0 and payload.get("ok") is True,
        "exit_code": code,
        "evidence_dir": evidence_dir,
        "run_row": row,
        "error": payload.get("error"),
    }


def run_baseline_repro(
    *,
    four_arm: bool,
    user_smoke: bool,
    env: Mapping[str, str],
) -> BaselineReproResult:
    """Run the requested baseline gates under ``env`` and bind sealed identity.

    The sealed Git identity (``CATALYST_INTEGRATION_COMMIT_SHA``) is validated
    before any gate runs. Gate execution results are collected as run rows;
    comparability is False whenever any non-sealed data identity field is
    missing from the tuple.
    """
    identity = sealed_identity_tuple(env=env)
    missing = _missing_ids(identity)
    comparable = not missing

    runs: list[dict[str, Any]] = []
    ok = True
    four_arm_token: str | None = None
    user_smoke_evidence_dir: str | None = None

    if four_arm:
        gate = _run_four_arm_gate(env, identity)
        ok = ok and bool(gate.get("ok"))
        four_arm_token = gate.get("token")
        if gate.get("run_row"):
            runs.append(gate["run_row"])
    if user_smoke:
        gate = _run_user_smoke_gate(env, identity)
        ok = ok and bool(gate.get("ok"))
        user_smoke_evidence_dir = gate.get("evidence_dir")
        if gate.get("run_row"):
            runs.append(gate["run_row"])

    return BaselineReproResult(
        identity=identity,
        missing_ids=missing,
        comparable=comparable,
        ok=ok,
        four_arm_token=four_arm_token,
        user_smoke_evidence_dir=user_smoke_evidence_dir,
        runs=tuple(runs),
    )


__all__ = [
    "BaselineReproResult",
    "FOUR_ARM_SUCCESS_TOKEN",
    "run_baseline_repro",
]
