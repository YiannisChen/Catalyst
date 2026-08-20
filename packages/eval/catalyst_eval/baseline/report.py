"""Sealed V1.1 baseline report writer (M1-2).

Writes one canonical, byte-deterministic JSON baseline report using a
non-overwriting atomic publish: temp file in the same directory, fsync of
file and directory, then ``os.link`` to the final pathname. The final report
is never overwritten or truncated; a conflicting existing report raises
:class:`BaselineReportConflictError`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_eval.baseline.identity import BaselineIdentity, repository_root

_SCHEMA_VERSION = "baseline_v1"

# Data/index identity fields compared between the sealed tuple and each run row.
DATA_IDENTITY_FIELDS = (
    "snapshot_id",
    "corpus_manifest_id",
    "index_manifest_id",
    "source_bundle_id",
    "probe_report_id",
    "postbuild_readiness_id",
    "lancedb_table_name",
    "embedding_model",
    "embedding_dim",
)

_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential|authorization)")
_SECRET_VALUE = re.compile(
    r"(?i)(\bsk-[a-z0-9_-]{8,}|"
    r"(?:api[_-]?key|token|secret|password|credential|authorization)"
    r"\s*(?::|=|\bis\b)\s*\S+)"
)
_AUDITED_SUCCESS_TOKENS = frozenset({"FOUR_ARM_E2E_OK", "USER_SMOKE_OK"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _safe_evidence_token_field(key: str, value: Any) -> bool:
    if key == "success_token":
        return isinstance(value, str) and value in _AUDITED_SUCCESS_TOKENS
    if key == "success_token_sha256":
        return isinstance(value, str) and _SHA256.fullmatch(value) is not None
    return False


class BaselineReportConflictError(ValueError):
    """The sealed final report already exists with different canonical bytes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        super().__init__(f"baseline report already exists with different content: {self.path}")


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"git rev-parse HEAD failed in {repo_root}: {result.stderr.strip()}")
    return result.stdout.strip()


def _redact_secrets(payload: Any) -> Any:
    """Recursively drop secret-bearing keys; never keep their values."""
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            key_text = str(key)
            if _SECRET_KEY.search(key_text) and not _safe_evidence_token_field(
                key_text, value
            ):
                continue
            cleaned[key] = _redact_secrets(value)
        return cleaned
    if isinstance(payload, list):
        return [_redact_secrets(item) for item in payload]
    if isinstance(payload, str) and _SECRET_VALUE.search(payload):
        return None
    return payload


def _run_data_identity_comparable(run: dict, identity: BaselineIdentity) -> bool:
    for field in DATA_IDENTITY_FIELDS:
        row_value = run.get(field)
        if row_value is None:
            return False
        if row_value != getattr(identity, field):
            return False
    return True


def _data_identity_comparable(identity: BaselineIdentity, runs: list[dict]) -> bool:
    if any(getattr(identity, field) is None for field in DATA_IDENTITY_FIELDS):
        return False
    if not runs:
        return False
    return all(_run_data_identity_comparable(run, identity) for run in runs)


def _model_identity_comparable(identity: BaselineIdentity, runs: list[dict]) -> bool:
    if identity.default_model is None:
        return False
    verified = 0
    for run in runs:
        run_model = run.get("default_model") or run.get("model_id")
        if run_model is None:
            continue
        verified += 1
        if run_model != identity.default_model:
            return False
    return verified > 0


def _build_report(
    identity: BaselineIdentity,
    runs: list[dict],
    *,
    generated_at: str,
    git_revision: str,
    promoted_env_recovered: bool,
) -> dict:
    run_provenance_allows_comparison = bool(runs) and all(
        run.get("promoted_env_recovered") is True for run in runs
    )
    comparison_allowed = (
        promoted_env_recovered
        and run_provenance_allows_comparison
        and not identity.app_default_db_marked_non_comparable
    )
    return {
        "schema_version": _SCHEMA_VERSION,
        "identity": _redact_secrets(dataclasses.asdict(identity)),
        "runs": [_redact_secrets(run) for run in runs],
        "comparability": {
            "data_identity_comparable": (
                comparison_allowed and _data_identity_comparable(identity, runs)
            ),
            "model_identity_comparable": (
                comparison_allowed and _model_identity_comparable(identity, runs)
            ),
            "promoted_env_recovered": promoted_env_recovered,
            "promoted_env_reason": (
                "promoted_environment_tuple_recovered"
                if promoted_env_recovered
                else "q_002_promoted_environment_tuple_unrecovered"
            ),
            "app_default_db_non_comparable": identity.app_default_db_marked_non_comparable,
            "generated_at": generated_at,
            "git_revision": git_revision,
        },
    }


def _canonical_bytes(payload: dict) -> bytes:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (text + "\n").encode("utf-8")


def write_baseline_report(
    report_dir: str | Path,
    identity: BaselineIdentity,
    runs: list[dict],
    *,
    generated_at: str | None = None,
    promoted_env_recovered: bool = False,
    expected_git_revision: str | None = None,
    report_name: str | None = None,
) -> Path:
    """Publish the sealed baseline report at ``<report_dir>/<stem>.json``.

    Default stem is ``v1_1_baseline_<sha8>``; a corrective seal passes an
    explicit ``report_name`` (bare filename stem, no ``.json`` suffix). The
    writer fails closed before creating any file when ``expected_git_revision``
    is supplied and the live HEAD differs. Atomic create-or-idempotent-return;
    never overwrites or truncates an existing final pathname and never leaves a
    partially written final path.
    """
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    if report_name is not None:
        if (
            not report_name
            or report_name.endswith(".json")
            or Path(report_name).name != report_name
        ):
            raise ValueError(
                "report_name must be a bare filename stem without .json or path separators"
            )
        stem = report_name
    else:
        stem = f"v1_1_baseline_{identity.code_git_sha[:8]}"
    target = report_dir / f"{stem}.json"

    timestamp = generated_at or datetime.now(timezone.utc).isoformat()
    git_revision = _git_head(repository_root())
    if expected_git_revision is not None and git_revision != expected_git_revision:
        raise ValueError(
            "git HEAD no longer matches the expected execution revision"
        )
    if not isinstance(promoted_env_recovered, bool):
        raise TypeError("promoted_env_recovered must be bool")
    payload = _build_report(
        identity,
        runs,
        generated_at=timestamp,
        git_revision=git_revision,
        promoted_env_recovered=promoted_env_recovered,
    )
    canonical = _canonical_bytes(payload)

    if target.exists():
        if target.read_bytes() == canonical:
            return target
        raise BaselineReportConflictError(target)

    fd, temp_name = tempfile.mkstemp(dir=str(report_dir), prefix=f".{target.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, target)
        except FileExistsError:
            if target.read_bytes() == canonical:
                return target
            raise BaselineReportConflictError(target) from None
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        dir_fd = os.open(str(report_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


__all__ = [
    "BaselineReportConflictError",
    "DATA_IDENTITY_FIELDS",
    "write_baseline_report",
]
