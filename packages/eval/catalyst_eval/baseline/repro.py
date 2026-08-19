"""M1-6: reproducible baseline gate wrapper.

``run_baseline_repro`` orchestrates the post-import four-arm and user-smoke
entrypoints under a gate environment, collects their success tokens and
baseline run rows, and binds them to the separately supplied promoted identity
environment. The sealed Git identity is validated before any gate runs;
unrecovered Q-002 provenance or incomplete identity produces an explicitly
NON-COMPARABLE result.
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
USER_SMOKE_SUCCESS_TOKEN = "USER_SMOKE_OK"
WAVE_TOKEN_FILENAME = "WAVE_TOKEN.txt"
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
    promoted_env_recovered: bool
    comparability_reason: str


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
    required = (
        "CATALYST_BASELINE_FROZEN_DB",
        "CATALYST_LANCEDB_DIR",
        "CATALYST_INDEX_MANIFEST_PATH",
    )
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise ValueError(f"four-arm gate requires env: {', '.join(missing)}")
    argv = [
        "--db", env["CATALYST_BASELINE_FROZEN_DB"],
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
    argv = [
        "--db", frozen_db,
        "--lancedb-dir", env["CATALYST_LANCEDB_DIR"],
        "--index-manifest", env["CATALYST_INDEX_MANIFEST_PATH"],
        "--t4-evidence-dir", env["CATALYST_BASELINE_T4_EVIDENCE_DIR"],
        "--wave2-evidence-dir", env["CATALYST_BASELINE_WAVE2_EVIDENCE_DIR"],
        "--run-id", env.get(
            "CATALYST_BASELINE_RUN_ID",
            f"baseline_repro_user_smoke_{uuid.uuid4().hex[:12]}",
        ),
        "--output-root", env.get("CATALYST_BASELINE_OUTPUT_ROOT", DEFAULT_OUTPUT_ROOT),
        "--embedding-mode", "production_pinned",
        "--provider", env.get("CATALYST_BASELINE_PROVIDER", "deepseek"),
        "--model-id", env.get("CATALYST_BASELINE_SMOKE_MODEL_ID", "deepseek-v4-flash"),
    ]
    case_pack = env.get("CATALYST_BASELINE_CASE_PACK")
    if case_pack:
        argv += ["--case-pack", case_pack]
    return argv


def _run_four_arm_gate(env: Mapping[str, str], identity: BaselineIdentity) -> dict[str, Any]:
    """EXPENSIVE default seam: run the post-import four-arm CLI in-process."""
    module = _load_script(FOUR_ARM_SCRIPT)
    with _applied_env(env):
        code, payload = _run_cli(module, _four_arm_argv(env))
    row = payload.get("baseline_run_row") if isinstance(payload.get("baseline_run_row"), dict) else {}
    meta_path = _validated_wave_token(payload, FOUR_ARM_SUCCESS_TOKEN)
    ok = code == 0 and payload.get("ok") is True and meta_path is not None
    return {
        "ok": ok,
        "exit_code": code,
        "token": FOUR_ARM_SUCCESS_TOKEN if meta_path is not None else None,
        "meta_path": str(meta_path) if meta_path is not None else None,
        "run_row": row,
        "error": payload.get("error"),
    }


def _run_user_smoke_gate(env: Mapping[str, str], identity: BaselineIdentity) -> dict[str, Any]:
    """EXPENSIVE default seam: run the post-import user-smoke CLI in-process."""
    module = _load_script(USER_SMOKE_SCRIPT)
    with _applied_env(env):
        code, payload = _run_cli(module, _user_smoke_argv(env))
    row = payload.get("baseline_run_row") if isinstance(payload.get("baseline_run_row"), dict) else {}
    meta_path = _validated_wave_token(payload, USER_SMOKE_SUCCESS_TOKEN)
    evidence_dir = str(meta_path.parent) if meta_path is not None else None
    return {
        "ok": code == 0 and payload.get("ok") is True and meta_path is not None,
        "exit_code": code,
        "token": USER_SMOKE_SUCCESS_TOKEN if meta_path is not None else None,
        "evidence_dir": evidence_dir,
        "meta_path": str(meta_path) if meta_path is not None else None,
        "run_row": row,
        "error": payload.get("error"),
    }


def _validated_wave_token(payload: Mapping[str, Any], expected: str) -> Path | None:
    """Return a real meta path only when its sibling token is exact."""
    if payload.get("token_written") is not True:
        return None
    raw_meta_path = payload.get("meta_path")
    if not isinstance(raw_meta_path, str):
        return None
    meta_path = Path(raw_meta_path)
    if not meta_path.is_file() or not meta_path.parent.is_dir():
        return None
    token_path = meta_path.parent / WAVE_TOKEN_FILENAME
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return meta_path if token == expected else None


def run_baseline_repro(
    *,
    four_arm: bool,
    user_smoke: bool,
    env: Mapping[str, str],
    promoted_identity_env: Mapping[str, str] | None = None,
) -> BaselineReproResult:
    """Run the requested baseline gates under ``env`` and bind sealed identity.

    The sealed Git identity (``CATALYST_INTEGRATION_COMMIT_SHA``) is validated
    before any gate runs. ``promoted_identity_env=None`` explicitly means the
    Q-002 tuple was not recovered, so even complete local gate pointers cannot
    produce a comparable seal.
    """
    promoted_env_recovered = promoted_identity_env is not None
    identity_env = (
        promoted_identity_env
        if promoted_identity_env is not None
        else {
            "CATALYST_INTEGRATION_COMMIT_SHA": env.get(
                "CATALYST_INTEGRATION_COMMIT_SHA", ""
            )
        }
    )
    identity = sealed_identity_tuple(env=identity_env)
    missing = _missing_ids(identity)

    runs: list[dict[str, Any]] = []
    ok = True
    four_arm_token: str | None = None
    user_smoke_evidence_dir: str | None = None

    if four_arm:
        gate = _run_four_arm_gate(env, identity)
        gate_ok = bool(gate.get("ok")) and gate.get("token") == FOUR_ARM_SUCCESS_TOKEN
        ok = ok and gate_ok
        four_arm_token = gate.get("token")
        if gate.get("run_row"):
            runs.append({**gate["run_row"], "promoted_env_recovered": promoted_env_recovered})
    if user_smoke:
        gate = _run_user_smoke_gate(env, identity)
        meta_path = gate.get("meta_path")
        gate_ok = (
            bool(gate.get("ok"))
            and gate.get("token") == USER_SMOKE_SUCCESS_TOKEN
            and isinstance(meta_path, str)
            and Path(meta_path).is_file()
        )
        ok = ok and gate_ok
        user_smoke_evidence_dir = gate.get("evidence_dir")
        if gate.get("run_row"):
            runs.append({**gate["run_row"], "promoted_env_recovered": promoted_env_recovered})

    expected_run_count = int(four_arm) + int(user_smoke)
    data_rows_match = len(runs) == expected_run_count and expected_run_count > 0 and all(
        all(
            field in row
            and row[field] is not None
            and row[field] == getattr(identity, field)
            for field in DATA_IDENTITY_FIELDS
        )
        for row in runs
    )
    run_models = [
        row.get("default_model") or row.get("model_id")
        for row in runs
        if row.get("default_model") is not None or row.get("model_id") is not None
    ]
    model_rows_match = (
        identity.default_model is not None
        and bool(run_models)
        and all(model == identity.default_model for model in run_models)
    )
    comparable = (
        promoted_env_recovered
        and not missing
        and ok
        and data_rows_match
        and model_rows_match
        and not identity.app_default_db_marked_non_comparable
    )
    comparability_reason = (
        "promoted_environment_tuple_recovered"
        if promoted_env_recovered
        else "q_002_promoted_environment_tuple_unrecovered"
    )

    return BaselineReproResult(
        identity=identity,
        missing_ids=missing,
        comparable=comparable,
        ok=ok,
        four_arm_token=four_arm_token,
        user_smoke_evidence_dir=user_smoke_evidence_dir,
        runs=tuple(runs),
        promoted_env_recovered=promoted_env_recovered,
        comparability_reason=comparability_reason,
    )


__all__ = [
    "BaselineReproResult",
    "FOUR_ARM_SUCCESS_TOKEN",
    "USER_SMOKE_SUCCESS_TOKEN",
    "run_baseline_repro",
]
