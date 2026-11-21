"""Runtime dependency assembly for the Catalyst agent runtime (WP-B B4).

V1.1 has exactly one production retrieval path (Frozen-A3 §2.1): the lexical
display retriever over the served corpus+FTS tuple. The loader therefore
resolves **no** LanceDB, embedding or reranker -- those components report
``not_served`` and are never required, and ``ProductionHybridRetriever`` stays
on disk (deletion is deferred, A3 §2.5) but is never constructed here.

Environment contract (the V1.1 serving tuple, A3 §2.1 / §2.2):

- CATALYST_RETRIEVAL_PROFILE ........ required; only ``lexical_display_v1`` is
  accepted.
- CATALYST_CORPUS_MANIFEST_ID ....... corpus manifest id (identity binding)
- CATALYST_SNAPSHOT_ID .............. data snapshot id (identity binding)
- CATALYST_SOURCE_BUNDLE_ID ......... source bundle id (identity binding)
- CATALYST_PROBE_REPORT_ID .......... probe report id (identity binding)
- CATALYST_POSTBUILD_READINESS_ID ... postbuild readiness id (identity binding)
- CATALYST_EXPECTED_FTS_DIGEST ...... operator-supplied served FTS digest,
  compared with the live served digest; a mismatch fails closed.

``CATALYST_INDEX_MANIFEST_ID`` and ``CATALYST_LANCEDB_DIR`` are not part of the
V1.1 tuple: ``index_manifest_id`` is not required (A3 §2.2) and no dense pointer
is resolved.
"""
from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.read import sec_units as sec_structure
from catalyst_data.retrieval.lexical_display import RETRIEVAL_PROFILE
from catalyst_data.retrieval.result import RetrievalContractError
from catalyst_data.retrieval.runtime_binding import (
    LexicalRuntimeBindingError,
    bind_lexical_runtime,
)
from catalyst_data.storage.connect import SQLiteReadTimeoutError
from catalyst_data.storage.errors import StoreReadError

from catalyst_agents.engine.rules import V2Rules

from .retrieval_adapter import AgentRetrieverAdapter

RETRIEVAL_PROFILE_ENV = "CATALYST_RETRIEVAL_PROFILE"
EXPECTED_FTS_DIGEST_ENV = "CATALYST_EXPECTED_FTS_DIGEST"
CORPUS_MANIFEST_ID_ENV = "CATALYST_CORPUS_MANIFEST_ID"
SNAPSHOT_ID_ENV = "CATALYST_SNAPSHOT_ID"
SOURCE_BUNDLE_ID_ENV = "CATALYST_SOURCE_BUNDLE_ID"
PROBE_REPORT_ID_ENV = "CATALYST_PROBE_REPORT_ID"
POSTBUILD_READINESS_ID_ENV = "CATALYST_POSTBUILD_READINESS_ID"
PROJECTION_DIR_ENV = "CATALYST_SERVED_PROJECTION_DIR"

# The one accepted retrieval profile (A3 §2.1). The retriever is always built
# with this constant, never with a caller-supplied string.
ACCEPTED_RETRIEVAL_PROFILES = (RETRIEVAL_PROFILE,)

NOT_SERVED = "not_served"


HealthStatus = str


@dataclass(frozen=True)
class RuntimeDependencies:
    sqlite_db_path: Path
    health: dict[str, Any] = field(default_factory=dict)
    retriever: AgentRetrieverAdapter | None = None
    # Vestigial dense fields. V1.1 has no dense arm (A3 §2.1); the loader never
    # populates them. They stay typed for the historical dependency shape.
    lancedb_dir: Path | None = None
    lancedb_table: Any = None
    embedding_fn: Callable[[str], list[float]] | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    reranker: Any | None = None
    reranker_model: str | None = None
    default_model: str | None = None
    requested_manifest_id: str | None = None
    index_manifest_id: str | None = None
    data_runtime_identity: DataRuntimeIdentity | None = None
    #: Verified served-projection identity (WP-B B7a): build_id, row_count,
    #: projection_sha256, schema_version. None when no retriever is bound.
    served_projection: dict[str, Any] | None = None


class RuntimeDependencyLoader:
    """Bind the single V1.1 lexical retrieval runtime (A3 §2.1/§2.2).

    Configuration is wired the same way as the historical loader options: each
    value may be supplied to the constructor, and otherwise falls back to its
    environment variable.
    """

    def __init__(
        self,
        *,
        sqlite_db_path: str | Path,
        retrieval_profile: str | None = None,
        expected_fts_digest: str | None = None,
        default_model: str | None = None,
        requested_manifest_id: str | None = None,
        source_bundle_id: str | None = None,
        snapshot_id: str | None = None,
        probe_report_id: str | None = None,
        postbuild_readiness_id: str | None = None,
        require_identity_bound_runtime: bool = False,
        # Serving is strict by default; dev_run opts out explicitly for offline evaluation.
        require_filing_units: bool = True,
        projection_dir: str | Path | None = None,
    ) -> None:
        self.sqlite_db_path = Path(sqlite_db_path)
        self.default_model = default_model
        self.retrieval_profile = retrieval_profile or os.getenv(RETRIEVAL_PROFILE_ENV)
        self.expected_fts_digest = expected_fts_digest or os.getenv(EXPECTED_FTS_DIGEST_ENV)
        self.requested_manifest_id = requested_manifest_id or os.getenv(CORPUS_MANIFEST_ID_ENV)
        self.source_bundle_id = source_bundle_id or os.getenv(SOURCE_BUNDLE_ID_ENV)
        self.snapshot_id = snapshot_id or os.getenv(SNAPSHOT_ID_ENV)
        self.probe_report_id = probe_report_id or os.getenv(PROBE_REPORT_ID_ENV)
        self.postbuild_readiness_id = (
            postbuild_readiness_id or os.getenv(POSTBUILD_READINESS_ID_ENV)
        )
        # ``index_manifest_id`` is not required by the V1.1 tuple (A3 §2.2).
        self.index_manifest_id: str | None = None
        self.require_identity_bound_runtime = require_identity_bound_runtime
        self.require_filing_units = require_filing_units
        # Where the served-build projection sidecar is written (WP-B B7a).
        # Never the repository, never data/, never the served DB's directory.
        # When unset, a fresh temporary directory outside the repository is
        # used, so the identity-bound runtime always serves the projection.
        configured_dir = projection_dir or os.getenv(PROJECTION_DIR_ENV)
        self.projection_dir = None if not configured_dir else Path(configured_dir)

        self._lock = threading.Lock()
        self._cached: RuntimeDependencies | None = None
        self._load_error: Exception | None = None

    @property
    def cached_dependencies(self) -> RuntimeDependencies | None:
        """Return the completed load, if available, without waiting for it."""
        return self._cached

    @property
    def cached_health(self) -> dict[str, Any] | None:
        if self._cached is not None:
            return self._cached.health
        if self._load_error is not None:
            exc = self._load_error
            message = str(exc)[:500]
            return {
                "status": "failed",
                "sqlite": {"status": "ready", "path": str(self.sqlite_db_path)},
                "lancedb": {"status": NOT_SERVED, "message": "V1.1 has no dense arm"},
                "embedding": {"status": NOT_SERVED, "message": "V1.1 has no dense arm"},
                "reranker": {"status": NOT_SERVED, "message": "V1.1 has no reranker"},
                "default_model": {"status": "ready", "model": self.default_model},
                "retrieval": {"status": "failed", "message": "runtime store readiness failed"},
                "sec_exhibit_units": {"status": "failed", "message": message},
                "errors": [{"component": "sec_exhibit_units", "message": message}],
            }
        return None

    def get_dependencies(self, *, force_reload: bool = False) -> RuntimeDependencies:
        with self._lock:
            if self._cached is not None and not force_reload:
                return self._cached
            try:
                self._cached = self._load_once()
                self._load_error = None
            except Exception as exc:
                # Any failed load is reported by /api/health instead of "warming up" forever.
                self._load_error = exc
                raise
            return self._cached

    def health(self) -> dict[str, Any]:
        try:
            return self.get_dependencies().health
        except (StoreReadError, SQLiteReadTimeoutError) as exc:
            message = str(exc)[:500]
            return {
                "status": "failed",
                "sqlite": {"status": "ready", "path": str(self.sqlite_db_path)},
                "lancedb": {"status": NOT_SERVED, "message": "V1.1 has no dense arm"},
                "embedding": {"status": NOT_SERVED, "message": "V1.1 has no dense arm"},
                "reranker": {"status": NOT_SERVED, "message": "V1.1 has no reranker"},
                "default_model": {"status": "ready", "model": self.default_model},
                "retrieval": {"status": "failed", "message": "runtime store readiness failed"},
                "sec_exhibit_units": {"status": "failed", "message": message},
                "errors": [{"component": "sec_exhibit_units", "message": message}],
            }

    # -- loading -----------------------------------------------------------
    def _load_once(self) -> RuntimeDependencies:
        health: dict[str, Any] = {
            "status": "ready",
            "sqlite": {"status": "ready", "path": str(self.sqlite_db_path)},
            "lancedb": {
                "status": NOT_SERVED,
                "message": "V1.1 has no dense arm; LanceDB is not served",
            },
            "embedding": {
                "status": NOT_SERVED,
                "message": "V1.1 has no dense arm; embeddings are not served",
                "model": None,
                "vector_dim": None,
            },
            "reranker": {
                "status": NOT_SERVED,
                "message": "V1.1 has no reranker; reranking is not served",
                "model": None,
            },
            "default_model": {"status": "ready", "model": self.default_model},
            "retrieval": {
                "status": "failed",
                "message": "identity-bound retrieval is not configured",
            },
            "errors": [],
        }

        if os.getenv("CATALYST_ALLOW_NON_LOOPBACK") == "1":
            disabled_readers = [
                name for name in ("CATALYST_FILING_UNITS",)
                if os.getenv(name, "").strip().lower() == "off"
            ]
            if disabled_readers:
                return _failed_dependencies(
                    health,
                    component="deployment_configuration",
                    message=(
                        "public deployment requires strict store readers; "
                        f"{', '.join(disabled_readers)}=off is forbidden"
                    ),
                )

        if not self.sqlite_db_path.exists():
            return _failed_dependencies(
                health,
                component="sqlite",
                message=f"SQLite DB path does not exist: {self.sqlite_db_path}",
            )

        try:
            filing_enabled = V2Rules.parse(os.getenv("CATALYST_V2_RULES")).filing
        except ValueError as exc:
            return _failed_dependencies(health, component="v2_rules", message=f"invalid CATALYST_V2_RULES: {exc}")
        if filing_enabled and self.require_filing_units:
            bodies_path = os.getenv("CATALYST_NEWS_BODIES_DB", "").strip()
            row_count = sec_structure.units_count(bodies_path) if bodies_path else None
            if not row_count or row_count <= 0:
                reason = (
                    "sec_exhibit_units has no rows in CATALYST_NEWS_BODIES_DB: "
                    f"{bodies_path or '<unset>'} (row_count={row_count})"
                )
                return _failed_dependencies(health, component="sec_exhibit_units", message=reason)

        profile = (self.retrieval_profile or "").strip()
        if not profile:
            return _failed_dependencies(
                health,
                component="retrieval",
                message=(
                    "retrieval_profile is required "
                    f"({RETRIEVAL_PROFILE_ENV}); V1.1 accepts only "
                    f"{RETRIEVAL_PROFILE!r}"
                ),
            )
        if profile not in ACCEPTED_RETRIEVAL_PROFILES:
            return _failed_dependencies(
                health,
                component="retrieval",
                message=(
                    f"retrieval_profile {profile!r} is not supported; V1.1 "
                    f"accepts only {RETRIEVAL_PROFILE!r}"
                ),
            )

        if not self.require_identity_bound_runtime:
            health["status"] = "degraded"
            health["retrieval"] = {
                "status": NOT_SERVED,
                "message": (
                    "identity-bound lexical retrieval is not required in this "
                    "configuration"
                ),
            }
            return RuntimeDependencies(
                sqlite_db_path=self.sqlite_db_path,
                health=health,
                retriever=None,
                data_runtime_identity=None,
            )

        manifest_is_served = self.requested_manifest_id == "served"
        required_identities = {
            "corpus_manifest_id": self.requested_manifest_id,
            "snapshot_id": self.snapshot_id,
            "source_bundle_id": self.source_bundle_id,
            "probe_report_id": self.probe_report_id,
            "postbuild_readiness_id": self.postbuild_readiness_id,
            "expected_fts_digest": self.expected_fts_digest,
        }
        missing = [name for name, value in required_identities.items() if not value]
        if missing:
            return _failed_dependencies(
                health,
                component="retrieval",
                message=(
                    "the V1.1 identity-bound runtime requires: " + ", ".join(missing)
                ),
            )

        assert self.requested_manifest_id is not None
        assert self.expected_fts_digest is not None
        assert self.snapshot_id is not None
        try:
            binding = bind_lexical_runtime(
                sqlite_db_path=self.sqlite_db_path,
                expected_manifest_id=None if manifest_is_served else self.requested_manifest_id,
                expected_fts_digest=self.expected_fts_digest,
                data_snapshot_id=self.snapshot_id,
                projection_dir_factory=self._resolve_projection_dir,
            )
        except LexicalRuntimeBindingError as exc:
            if exc.stage == "served-build resolution":
                message = f"served-build resolution failed: {exc}"
            elif exc.stage == "served projection":
                message = f"served projection failed: {exc}"
            else:
                message = str(exc)
            return _failed_dependencies(
                health,
                component="retrieval",
                message=message,
            )
        resolution = binding.resolution
        projection = binding.projection
        health["retrieval"] = {
            "status": "ready",
            "corpus_manifest_id": resolution.corpus_manifest_id,
            "served_build_id": resolution.build_id,
        }
        return RuntimeDependencies(
            sqlite_db_path=self.sqlite_db_path,
            health=health,
            retriever=AgentRetrieverAdapter(binding.retriever),
            data_runtime_identity=binding.data_runtime_identity,
            requested_manifest_id=resolution.corpus_manifest_id,
            served_projection=projection.identity(),
        )

    def _resolve_projection_dir(self) -> Path:
        """The sidecar directory; fail closed on a forbidden location."""
        if self.projection_dir is None:
            return Path(tempfile.mkdtemp(prefix="catalyst-served-projection-"))
        target = self.projection_dir.resolve()
        served_dir = self.sqlite_db_path.resolve().parent
        forbidden: list[tuple[Path, str]] = [(served_dir, "the served DB directory")]
        # The repository holding the served DB and the one holding this code.
        for anchor in (self.sqlite_db_path.resolve(), Path(__file__).resolve()):
            repo_root = _repository_root(anchor)
            if repo_root is not None:
                forbidden.append((repo_root, "the repository"))
        for root, label in forbidden:
            if target == root or (label == "the repository" and root in target.parents):
                raise RetrievalContractError(
                    "served_projection_dir_forbidden",
                    f"projection_dir {target} is inside {label}",
                )
        target.mkdir(parents=True, exist_ok=True)
        return target


def _repository_root(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        if (parent / ".git").exists() or (
            (parent / "pyproject.toml").is_file() and (parent / "packages").is_dir()
        ):
            return parent
    return None


def _failed_dependencies(
    health: dict[str, Any], *, component: str, message: str
) -> RuntimeDependencies:
    health["status"] = "failed"
    health.setdefault(component, {})["status"] = "failed"
    health[component]["message"] = message
    health["errors"].append({"component": component, "message": message})

    return RuntimeDependencies(
        sqlite_db_path=Path(health["sqlite"]["path"]),
        health=health,
        retriever=None,
    )


__all__ = [
    "ACCEPTED_RETRIEVAL_PROFILES",
    "EXPECTED_FTS_DIGEST_ENV",
    "RETRIEVAL_PROFILE_ENV",
    "RuntimeDependencies",
    "RuntimeDependencyLoader",
]
