"""Sealed V1.1 baseline identity tuple (M1-1).

Freezes the audited code baseline, the immutable integration/docs commit SHA,
installed package versions, and every recoverable data/index/model identity
into one fail-closed reconciliation structure. The immutable sealed SHA is
never recomputed from the moving milestone HEAD or ``v1.1/integration`` ref.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

AUDITED_CODE_BASELINE = "621375bc395e1dee644b335b2abe541a15d62fee"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")

_ENV_IDENTITY_KEYS = (
    "CATALYST_SNAPSHOT_ID",
    "CATALYST_CORPUS_MANIFEST_ID",
    "CATALYST_INDEX_MANIFEST_ID",
    "CATALYST_SOURCE_BUNDLE_ID",
    "CATALYST_PROBE_REPORT_ID",
    "CATALYST_POSTBUILD_READINESS_ID",
)

_PACKAGE_DISTRIBUTIONS = {
    "data_core": "catalyst-data",
    "agents": "catalyst-agents",
    "app": "catalyst-app",
    "eval": "catalyst-eval",
}


@dataclasses.dataclass(frozen=True)
class PackageVersions:
    """Frozen installed distribution versions for the four Catalyst packages."""

    data_core: str | None
    agents: str | None
    app: str | None
    eval: str | None


class BaselineIdentityConflictError(ValueError):
    """Identity sources disagree. Carries only field and source names, never values."""

    def __init__(self, field: str, sources: Sequence[str]) -> None:
        self.field = field
        self.sources = tuple(sorted(sources))
        super().__init__(
            f"baseline identity conflict for {self.field!r} across sources: "
            f"{', '.join(self.sources)}"
        )


@dataclasses.dataclass(frozen=True)
class BaselineIdentity:
    """The sealed M1 baseline identity tuple."""

    code_git_sha: str
    integration_commit_sha: str
    package_versions: PackageVersions
    snapshot_id: str | None
    corpus_manifest_id: str | None
    index_manifest_id: str | None
    source_bundle_id: str | None
    probe_report_id: str | None
    postbuild_readiness_id: str | None
    lancedb_table_name: str | None
    embedding_model: str | None
    embedding_dim: str | None
    reranker_model: str | None
    default_model: str | None
    app_default_db_marked_non_comparable: bool


def repository_root(start: Path | None = None) -> Path:
    """Discover the repository root from the module path or an injected path.

    Walks parent directories until it finds a ``.git`` entry plus
    ``packages/eval``. Never consults process CWD. Fails closed on absence.
    """
    candidate = (start or Path(__file__).resolve().parent).resolve()
    for current in (candidate, *candidate.parents):
        if (current / ".git").exists() and (current / "packages" / "eval").is_dir():
            return current
    raise ValueError(
        "cannot discover Catalyst repository root from "
        f"{start or Path(__file__).resolve()}"
    )


def reconcile_identity(field: str, sources: Mapping[str, str | None]) -> str | None:
    """Return the single reconciled identity value or raise a conflict.

    Zero present values yields None; one unique value is accepted; multiple
    equal values are accepted; distinct values raise
    :class:`BaselineIdentityConflictError`. No source has precedence.
    """
    present = {name: value for name, value in sources.items() if value is not None}
    if not present:
        return None
    unique = set(present.values())
    if len(unique) == 1:
        return next(iter(present.values()))
    raise BaselineIdentityConflictError(field, tuple(present.keys()))


def is_app_default_db(path: str | Path, *, repo_root: Path | None = None) -> bool:
    """True when ``path`` resolves to the unchecked app default runtime DB.

    Relative paths resolve against the discovered/injected repository root,
    never against process CWD.
    """
    root = repository_root(start=repo_root) if repo_root is not None else repository_root()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve() == (root / ".local" / "live_runtime.db").resolve()


def _run_git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"git {args[0]} failed in {repo_root}: {result.stderr.strip()}")
    return result.stdout.strip()


def _read_json_pointer(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _validate_integration_commit_sha(root: Path, sha: str) -> str:
    """Fail-closed validation of the immutable sealed integration/docs SHA."""
    if not _HEX40.fullmatch(sha):
        raise ValueError(
            "CATALYST_INTEGRATION_COMMIT_SHA must be a lowercase 40-char SHA"
        )
    # Commit must exist.
    try:
        _run_git(root, "cat-file", "-e", f"{sha}^{{commit}}")
    except ValueError as exc:
        raise ValueError(
            f"CATALYST_INTEGRATION_COMMIT_SHA {sha} does not resolve to a commit"
        ) from exc
    # Parent must be the audited code baseline.
    parent = _run_git(root, "rev-parse", f"{sha}^")
    if parent != AUDITED_CODE_BASELINE:
        raise ValueError(
            f"CATALYST_INTEGRATION_COMMIT_SHA {sha} parent {parent} != audited "
            f"baseline {AUDITED_CODE_BASELINE}"
        )
    # The sealed commit must carry no packages/apps/configs delta from baseline.
    diff = _run_git(
        root, "diff", "--stat", AUDITED_CODE_BASELINE, sha, "--", "packages", "apps", "configs"
    )
    if diff:
        raise ValueError(
            f"CATALYST_INTEGRATION_COMMIT_SHA {sha} has a code delta from the "
            "audited baseline; only docs-only amendments are allowed"
        )
    return sha


def _package_versions() -> PackageVersions:
    versions: dict[str, str | None] = {}
    for field, distribution in _PACKAGE_DISTRIBUTIONS.items():
        try:
            versions[field] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[field] = None
    return PackageVersions(
        data_core=versions["data_core"],
        agents=versions["agents"],
        app=versions["app"],
        eval=versions["eval"],
    )


def sealed_identity_tuple(
    *,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> BaselineIdentity:
    """Build the sealed identity tuple from env, pointers, packages, and git.

    ``integration_commit_sha`` comes only from ``CATALYST_INTEGRATION_COMMIT_SHA``
    (never from git HEAD). Missing non-sealed identity stays None; missing or
    conflicting sealed Git identity and any contradictory env/pointer/manifest
    identity fails closed.
    """
    resolved_env = dict(os.environ if env is None else env)
    root = repository_root(start=repo_root) if repo_root is not None else repository_root()

    sealed_sha = resolved_env.get("CATALYST_INTEGRATION_COMMIT_SHA")
    if not sealed_sha:
        raise ValueError("CATALYST_INTEGRATION_COMMIT_SHA is required")
    integration_commit_sha = _validate_integration_commit_sha(root, sealed_sha)

    code_git_sha = _run_git(root, "rev-parse", AUDITED_CODE_BASELINE)
    if code_git_sha != AUDITED_CODE_BASELINE:
        raise ValueError(
            f"audited code baseline resolved to {code_git_sha}, expected {AUDITED_CODE_BASELINE}"
        )

    lancedb_dir = resolved_env.get("CATALYST_LANCEDB_DIR")
    pointer: dict = {}
    if lancedb_dir:
        pointer = _read_json_pointer(Path(lancedb_dir) / "active_generation.json")

    manifest_path = resolved_env.get("CATALYST_INDEX_MANIFEST_PATH")
    manifest: dict = {}
    if manifest_path:
        manifest = _read_json_pointer(Path(manifest_path))

    def _value(payload: dict, key: str) -> str | None:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return None

    snapshot_id = reconcile_identity(
        "snapshot_id",
        {
            "env": resolved_env.get("CATALYST_SNAPSHOT_ID") or None,
            "active_generation": _value(pointer, "snapshot_id"),
            "index_manifest": _value(manifest, "snapshot_id"),
        },
    )
    corpus_manifest_id = reconcile_identity(
        "corpus_manifest_id",
        {
            "env": resolved_env.get("CATALYST_CORPUS_MANIFEST_ID") or None,
            "active_generation": _value(pointer, "corpus_manifest_id"),
            "index_manifest": _value(manifest, "corpus_manifest_id"),
        },
    )
    index_manifest_id = reconcile_identity(
        "index_manifest_id",
        {
            "env": resolved_env.get("CATALYST_INDEX_MANIFEST_ID") or None,
            "active_generation": _value(pointer, "index_manifest_id"),
            "index_manifest": _value(manifest, "index_manifest_id"),
        },
    )
    source_bundle_id = reconcile_identity(
        "source_bundle_id",
        {
            "env": resolved_env.get("CATALYST_SOURCE_BUNDLE_ID") or None,
            "active_generation": _value(pointer, "source_bundle_id"),
            "index_manifest": _value(manifest, "source_bundle_id"),
        },
    )
    probe_report_id = reconcile_identity(
        "probe_report_id",
        {
            "env": resolved_env.get("CATALYST_PROBE_REPORT_ID") or None,
            "index_manifest": _value(manifest, "probe_report_id"),
        },
    )
    postbuild_readiness_id = reconcile_identity(
        "postbuild_readiness_id",
        {
            "env": resolved_env.get("CATALYST_POSTBUILD_READINESS_ID") or None,
            "index_manifest": _value(manifest, "postbuild_readiness_id"),
        },
    )
    lancedb_table_name = reconcile_identity(
        "lancedb_table_name",
        {
            "active_generation": _value(pointer, "table_name"),
            "index_manifest": _value(manifest, "table_name"),
        },
    )
    embedding_model = reconcile_identity(
        "embedding_model",
        {"index_manifest": _value(manifest, "model_name")},
    )
    embedding_dim = reconcile_identity(
        "embedding_dim",
        {"index_manifest": _value(manifest, "dimension")},
    )

    default_model = resolved_env.get("CATALYST_DEFAULT_MODEL") or None

    raw_db = resolved_env.get("CATALYST_DB_PATH")
    app_default_db_marked_non_comparable = (
        True if not raw_db else is_app_default_db(raw_db, repo_root=root)
    )

    return BaselineIdentity(
        code_git_sha=code_git_sha,
        integration_commit_sha=integration_commit_sha,
        package_versions=_package_versions(),
        snapshot_id=snapshot_id,
        corpus_manifest_id=corpus_manifest_id,
        index_manifest_id=index_manifest_id,
        source_bundle_id=source_bundle_id,
        probe_report_id=probe_report_id,
        postbuild_readiness_id=postbuild_readiness_id,
        lancedb_table_name=lancedb_table_name,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        reranker_model=None,
        default_model=default_model,
        app_default_db_marked_non_comparable=app_default_db_marked_non_comparable,
    )


__all__ = [
    "AUDITED_CODE_BASELINE",
    "BaselineIdentity",
    "BaselineIdentityConflictError",
    "PackageVersions",
    "is_app_default_db",
    "reconcile_identity",
    "repository_root",
    "sealed_identity_tuple",
]
