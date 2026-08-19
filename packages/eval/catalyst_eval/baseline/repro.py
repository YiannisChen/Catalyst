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
import hashlib
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
APPROVED_FROZEN_DB_SHA256 = (
    "bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40"
)


def _missing_ids(identity: BaselineIdentity) -> tuple[str, ...]:
    return tuple(field for field in DATA_IDENTITY_FIELDS if getattr(identity, field) is None)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_runtime_derivative(
    frozen_db: str | Path,
    runtime_db: str | Path,
    *,
    expected_frozen_sha: str = APPROVED_FROZEN_DB_SHA256,
) -> None:
    """Require a byte-equivalent, writable, independently stored DB copy."""
    frozen = Path(frozen_db)
    runtime = Path(runtime_db)
    if frozen.is_symlink() or runtime.is_symlink():
        raise ValueError("frozen/runtime DB paths must not be symlinks")
    if not frozen.is_file() or not runtime.is_file():
        raise ValueError("frozen and runtime derivative DB files are required")
    if frozen.resolve() == runtime.resolve():
        raise ValueError("frozen and runtime derivative DB paths must be distinct")
    frozen_stat = frozen.stat()
    runtime_stat = runtime.stat()
    if (frozen_stat.st_dev, frozen_stat.st_ino) == (
        runtime_stat.st_dev,
        runtime_stat.st_ino,
    ):
        raise ValueError("runtime derivative must not share frozen DB inode/hardlink")
    if frozen_stat.st_size <= 0 or runtime_stat.st_size <= 0:
        raise ValueError("frozen and runtime derivative DBs must be non-empty")
    frozen_sha = _sha256_file(frozen)
    if frozen_sha != expected_frozen_sha:
        raise ValueError("frozen DB sha mismatch")
    if _sha256_file(runtime) != frozen_sha:
        raise ValueError("runtime derivative initial sha must match frozen DB sha")
    if not os.access(runtime, os.W_OK):
        raise ValueError("runtime derivative DB must be writable")


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
            "CATALYST_BASELINE_FOUR_ARM_RUN_ID",
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
    _validate_runtime_derivative(frozen_db, env["CATALYST_DB_PATH"])
    argv = [
        "--db", frozen_db,
        "--lancedb-dir", env["CATALYST_LANCEDB_DIR"],
        "--index-manifest", env["CATALYST_INDEX_MANIFEST_PATH"],
        "--t4-evidence-dir", env["CATALYST_BASELINE_T4_EVIDENCE_DIR"],
        "--wave2-evidence-dir", env["CATALYST_BASELINE_WAVE2_EVIDENCE_DIR"],
        "--run-id", env.get(
            "CATALYST_BASELINE_USER_SMOKE_RUN_ID",
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


def _bind_gate_run_row(
    gate_kind: str,
    gate: Mapping[str, Any],
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Attach immutable gate/T4 evidence hashes to a baseline run row."""
    row = gate.get("run_row")
    if not isinstance(row, dict) or not row.get("run_id"):
        raise ValueError(f"{gate_kind} gate did not emit a baseline run row")
    meta_path_raw = gate.get("meta_path")
    token = gate.get("token")
    if not isinstance(meta_path_raw, str) or not isinstance(token, str):
        raise ValueError(f"{gate_kind} gate evidence binding is incomplete")
    meta_path = Path(meta_path_raw)
    token_path = meta_path.parent / WAVE_TOKEN_FILENAME
    t4_dir_raw = env.get("CATALYST_BASELINE_T4_EVIDENCE_DIR")
    if not t4_dir_raw:
        raise ValueError("CATALYST_BASELINE_T4_EVIDENCE_DIR is required for evidence binding")
    t4_meta_path = Path(t4_dir_raw) / "meta.json"
    for path, label in (
        (meta_path, "gate meta"),
        (token_path, "gate token"),
        (t4_meta_path, "T4 meta"),
    ):
        if not path.is_file():
            raise ValueError(f"{label} file missing for evidence binding")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError(
            f"{gate_kind} gate run ID mismatch: meta is unreadable or malformed"
        ) from None
    if not isinstance(meta, dict):
        raise ValueError(
            f"{gate_kind} gate run ID mismatch: meta must be a JSON object"
        )
    run_id_env_key = {
        "four_arm": "CATALYST_BASELINE_FOUR_ARM_RUN_ID",
        "user_smoke": "CATALYST_BASELINE_USER_SMOKE_RUN_ID",
    }.get(gate_kind)
    if run_id_env_key is None:
        raise ValueError("unsupported baseline gate kind")
    requested_run_id = env.get(run_id_env_key)
    if (
        not requested_run_id
        or row.get("run_id") != requested_run_id
        or meta.get("run_id") != requested_run_id
    ):
        raise ValueError(f"{gate_kind} gate run ID mismatch across request/row/meta")
    return {
        **row,
        "gate_kind": gate_kind,
        "evidence_ref": str(meta_path.parent.resolve()),
        "evidence_meta_ref": str(meta_path.resolve()),
        "evidence_meta_sha256": _sha256_file(meta_path),
        "success_token": token,
        "success_token_sha256": _sha256_file(token_path),
        "t4_evidence_ref": str(Path(t4_dir_raw).resolve()),
        "t4_meta_sha256": _sha256_file(t4_meta_path),
    }


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

    gate_env = dict(env)
    if four_arm:
        gate_env.setdefault(
            "CATALYST_BASELINE_FOUR_ARM_RUN_ID",
            f"baseline_repro_four_arm_{uuid.uuid4().hex[:12]}",
        )
    if user_smoke:
        gate_env.setdefault(
            "CATALYST_BASELINE_USER_SMOKE_RUN_ID",
            f"baseline_repro_user_smoke_{uuid.uuid4().hex[:12]}",
        )
    if four_arm and user_smoke:
        four_run_id = gate_env.get("CATALYST_BASELINE_FOUR_ARM_RUN_ID")
        smoke_run_id = gate_env.get("CATALYST_BASELINE_USER_SMOKE_RUN_ID")
        if four_run_id and smoke_run_id and four_run_id == smoke_run_id:
            raise ValueError("four-arm and user-smoke run IDs must be distinct")
    if user_smoke and not four_arm and not gate_env.get(
        "CATALYST_BASELINE_WAVE2_EVIDENCE_DIR"
    ):
        raise ValueError(
            "user-smoke-only requires CATALYST_BASELINE_WAVE2_EVIDENCE_DIR"
        )

    runs: list[dict[str, Any]] = []
    ok = True
    four_arm_token: str | None = None
    user_smoke_evidence_dir: str | None = None

    if four_arm:
        gate = _run_four_arm_gate(gate_env, identity)
        gate_ok = bool(gate.get("ok")) and gate.get("token") == FOUR_ARM_SUCCESS_TOKEN
        ok = ok and gate_ok
        four_arm_token = gate.get("token")
        if gate_ok:
            bound_row = _bind_gate_run_row("four_arm", gate, gate_env)
            runs.append({**bound_row, "promoted_env_recovered": promoted_env_recovered})
            if user_smoke:
                gate_env["CATALYST_BASELINE_WAVE2_EVIDENCE_DIR"] = str(
                    Path(str(gate["meta_path"])).parent.resolve()
                )
    if user_smoke and (not four_arm or ok):
        gate = _run_user_smoke_gate(gate_env, identity)
        meta_path = gate.get("meta_path")
        gate_ok = (
            bool(gate.get("ok"))
            and gate.get("token") == USER_SMOKE_SUCCESS_TOKEN
            and isinstance(meta_path, str)
            and Path(meta_path).is_file()
        )
        ok = ok and gate_ok
        user_smoke_evidence_dir = gate.get("evidence_dir")
        if gate_ok:
            bound_row = _bind_gate_run_row("user_smoke", gate, gate_env)
            if four_arm and runs and Path(bound_row["evidence_ref"]).resolve() == Path(
                runs[0]["evidence_ref"]
            ).resolve():
                gate_ok = False
                ok = False
            else:
                runs.append(
                    {**bound_row, "promoted_env_recovered": promoted_env_recovered}
                )

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
