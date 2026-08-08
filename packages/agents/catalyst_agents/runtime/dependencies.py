"""Runtime dependency assembly for the Catalyst agent runtime.

Environment contract (Wave 1, post-import index wiring):
- CATALYST_LANCEDB_DIR ............. path to the gold LanceDB directory
- CATALYST_CORPUS_MANIFEST_ID ...... corpus manifest id (identity binding)
- CATALYST_INDEX_MANIFEST_ID ....... index manifest id (identity binding)
- CATALYST_SOURCE_BUNDLE_ID ........ source bundle id (identity binding)
- CATALYST_SNAPSHOT_ID ............. data snapshot id (identity binding)
- CATALYST_PROBE_REPORT_ID ......... probe report id (identity binding)
- CATALYST_POSTBUILD_READINESS_ID .. postbuild readiness id (identity binding)

Active table resolution: unless an explicit ``lancedb_table_name`` is
supplied, the table name is read from ``active_generation.json`` (schema
``active_generation_v1``) under the resolved lancedb dir. Missing or invalid
pointer fails closed; the runtime never silently falls back to the legacy
table name ``"chunks"``.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable

from catalyst_data.storage import lancedb_store
from catalyst_data.retrieval.hybrid import ProductionHybridRetriever
from catalyst_data.retrieval.index_manifest import IndexManifest

from .query_embedding import QueryEmbeddingFactory
from .retrieval_adapter import AgentRetrieverAdapter


ACTIVE_GENERATION_FILENAME = "active_generation.json"
ACTIVE_GENERATION_SCHEMA_VERSION = "active_generation_v1"


HealthStatus = str
EmbeddingFactory = Callable[[str], tuple[Callable[[str], list[float]], int | None]]
RerankerFactory = Callable[[str], Any | None]
LanceConnectFactory = Callable[[str], Any]


@dataclass(frozen=True)
class RuntimeDependencies:
    sqlite_db_path: Path
    lancedb_dir: Path
    lancedb_table: Any
    embedding_fn: Callable[[str], list[float]]
    embedding_model: str
    embedding_dim: int | None
    reranker: Any | None
    reranker_model: str
    default_model: str | None
    health: dict[str, Any]
    retriever: AgentRetrieverAdapter | None = None
    requested_manifest_id: str | None = None
    index_manifest_id: str | None = None


class RuntimeDependencyLoader:
    def __init__(
        self,
        *,
        sqlite_db_path: str | Path,
        lancedb_dir: str | Path | None = None,
        lancedb_table_name: str | None = None,
        embedding_model: str = lancedb_store.EMBEDDING_MODEL,
        reranker_model: str = lancedb_store.RERANKER_MODEL,
        default_model: str | None = None,
        expected_embedding_dim: int | None = None,
        embedding_factory: EmbeddingFactory | None = None,
        reranker_factory: RerankerFactory | None = None,
        lancedb_connect_factory: LanceConnectFactory | None = None,
        requested_manifest_id: str | None = None,
        index_manifest_id: str | None = None,
        reranker_timeout_seconds: float = 2.0,
        query_embedding_factory: QueryEmbeddingFactory | None = None,
        index_manifest_path: str | Path | None = None,
        require_identity_bound_runtime: bool = False,
    ) -> None:
        self.sqlite_db_path = Path(sqlite_db_path)
        self.lancedb_dir = Path(lancedb_dir) if lancedb_dir is not None else None
        self.lancedb_table_name = lancedb_table_name
        self.embedding_model = embedding_model
        self.reranker_model = reranker_model
        self.default_model = default_model
        self.expected_embedding_dim = expected_embedding_dim
        self.requested_manifest_id = requested_manifest_id or os.getenv("CATALYST_CORPUS_MANIFEST_ID")
        self.index_manifest_id = index_manifest_id or os.getenv("CATALYST_INDEX_MANIFEST_ID")
        self.reranker_timeout_seconds = reranker_timeout_seconds
        self.index_manifest_path = Path(index_manifest_path) if index_manifest_path is not None else None
        self.require_identity_bound_runtime = require_identity_bound_runtime
        self._query_embedding_factory = query_embedding_factory
        self.source_bundle_id = os.getenv("CATALYST_SOURCE_BUNDLE_ID")
        self.snapshot_id = os.getenv("CATALYST_SNAPSHOT_ID")
        self.probe_report_id = os.getenv("CATALYST_PROBE_REPORT_ID")
        self.postbuild_readiness_id = os.getenv("CATALYST_POSTBUILD_READINESS_ID")

        self._embedding_factory = embedding_factory or _default_embedding_factory
        self._reranker_factory = reranker_factory or lancedb_store.load_reranker
        self._lancedb_connect_factory = lancedb_connect_factory or _default_lancedb_connect_factory

        self._lock = threading.Lock()
        self._cached: RuntimeDependencies | None = None

    def get_dependencies(self, *, force_reload: bool = False) -> RuntimeDependencies:
        with self._lock:
            if self._cached is not None and not force_reload:
                return self._cached
            self._cached = self._load_once()
            return self._cached

    def health(self) -> dict[str, Any]:
        return self.get_dependencies().health

    def _load_once(self) -> RuntimeDependencies:
        health: dict[str, Any] = {
            "status": "ready",
            "sqlite": {"status": "ready", "path": str(self.sqlite_db_path)},
            "lancedb": {"status": "ready", "path": None, "table": self.lancedb_table_name},
            "embedding": {"status": "ready", "model": self.embedding_model, "vector_dim": None},
            "reranker": {"status": "ready", "model": self.reranker_model},
            "default_model": {"status": "ready", "model": self.default_model},
            "retrieval": {
                "status": "failed",
                "message": "identity-bound retrieval is not configured",
            },
            "errors": [],
        }

        if not self.sqlite_db_path.exists():
            return _failed_dependencies(
                health,
                component="sqlite",
                message=f"SQLite DB path does not exist: {self.sqlite_db_path}",
            )

        lancedb_dir = self._resolve_lancedb_dir(health)
        if lancedb_dir is None:
            return _failed_dependencies(
                health,
                component="lancedb",
                message="CATALYST_LANCEDB_DIR is not set and lancedb_dir was not provided.",
            )

        if self.require_identity_bound_runtime:
            required_ids = {
                "corpus_manifest_id": self.requested_manifest_id,
                "index_manifest_id": self.index_manifest_id,
                "source_bundle_id": self.source_bundle_id,
                "snapshot_id": self.snapshot_id,
                "probe_report_id": self.probe_report_id,
                "postbuild_readiness_id": self.postbuild_readiness_id,
            }
            if any(not value for value in required_ids.values()):
                return _failed_dependencies(
                    health,
                    component="retrieval",
                    message="all manager-approved retrieval identities are required",
                )

        health["lancedb"]["path"] = str(lancedb_dir)

        lancedb_table_name = self._resolve_lancedb_table_name(lancedb_dir, health)
        if lancedb_table_name is None:
            return _failed_dependencies(
                health,
                component="lancedb",
                message=health["lancedb"]["message"],
            )

        if self.require_identity_bound_runtime:
            manifest_path = self.index_manifest_path or (lancedb_dir / "index_manifest.json")
            try:
                raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                index_manifest = IndexManifest.from_dict(raw_manifest)
                if index_manifest.index_manifest_id != self.index_manifest_id:
                    raise ValueError("IndexManifest ID mismatch")
                index_manifest.assert_approved_identities(
                    source_bundle_id=self.source_bundle_id,
                    snapshot_id=self.snapshot_id,
                    corpus_manifest_id=self.requested_manifest_id,
                    probe_report_id=self.probe_report_id,
                    postbuild_readiness_id=self.postbuild_readiness_id,
                )
            except Exception as exc:
                return _failed_dependencies(
                    health,
                    component="retrieval",
                    message=f"identity-bound IndexManifest validation failed: {exc}",
                )

        try:
            lancedb_db = self._lancedb_connect_factory(str(lancedb_dir))
            lancedb_table = lancedb_db.open_table(lancedb_table_name)
        except Exception as exc:
            return _failed_dependencies(
                health,
                component="lancedb",
                message=f"Failed to open LanceDB table '{lancedb_table_name}': {exc}",
            )

        try:
            if self._query_embedding_factory is not None:
                query_embedder = self._query_embedding_factory.create(model_name=self.embedding_model)
                embedding_fn = query_embedder.embed_query
                embedding_dim = query_embedder.dimension
            else:
                embedding_fn, embedding_dim = self._embedding_factory(self.embedding_model)
        except Exception as exc:
            return _failed_dependencies(
                health,
                component="embedding",
                message=f"Failed to initialize embedding model '{self.embedding_model}': {exc}",
            )

        health["lancedb"]["table"] = lancedb_table_name
        health["embedding"]["vector_dim"] = embedding_dim
        index_dim = _detect_index_vector_dim(lancedb_table)
        if index_dim is not None:
            health["lancedb"]["vector_dim"] = index_dim

        expected_dim = self.expected_embedding_dim if self.expected_embedding_dim is not None else index_dim
        if expected_dim is not None and embedding_dim is not None and expected_dim != embedding_dim:
            return _failed_dependencies(
                health,
                component="embedding",
                message=(
                    f"Embedding dimension mismatch: expected {expected_dim}, got {embedding_dim}."
                ),
            )

        try:
            reranker = self._reranker_factory(self.reranker_model)
        except Exception as exc:
            reranker = None
            health["status"] = "degraded"
            health["reranker"]["status"] = "degraded"
            health["reranker"]["message"] = (
                f"Reranker unavailable; retrieval fallback enabled. ({exc})"
            )
        if reranker is None and health["reranker"]["status"] == "ready":
            health["status"] = "degraded"
            health["reranker"]["status"] = "degraded"
            health["reranker"]["message"] = "Reranker unavailable; retrieval fallback enabled."

        retriever = None
        readonly_db = None
        if self.requested_manifest_id and self.index_manifest_id:
            readonly_db = sqlite3.connect(f"file:{self.sqlite_db_path}?mode=ro", uri=True)
            production_retriever = ProductionHybridRetriever(
                db=readonly_db,
                lancedb_table=lancedb_table,
                embedding_fn=embedding_fn,
                reranker=reranker,
                index_manifest_id=self.index_manifest_id,
                reranker_timeout_seconds=self.reranker_timeout_seconds,
            )
            retriever = AgentRetrieverAdapter(production_retriever)
            health["retrieval"] = {
                "status": "ready",
                "corpus_manifest_id": self.requested_manifest_id,
                "index_manifest_id": self.index_manifest_id,
            }

        return RuntimeDependencies(
            sqlite_db_path=self.sqlite_db_path,
            lancedb_dir=lancedb_dir,
            lancedb_table=lancedb_table,
            embedding_fn=embedding_fn,
            embedding_model=self.embedding_model,
            embedding_dim=embedding_dim,
            reranker=reranker,
            reranker_model=self.reranker_model,
            default_model=self.default_model,
            health=health,
            retriever=retriever,
            requested_manifest_id=self.requested_manifest_id,
            index_manifest_id=self.index_manifest_id,
        )

    def _resolve_lancedb_dir(self, health: dict[str, Any]) -> Path | None:
        if self.lancedb_dir is not None:
            return self.lancedb_dir
        raw = os.getenv("CATALYST_LANCEDB_DIR")
        if not raw:
            health["lancedb"]["status"] = "failed"
            return None
        return Path(raw)

    def _resolve_lancedb_table_name(self, lancedb_dir: Path, health: dict[str, Any]) -> str | None:
        """Resolve the active LanceDB table name.

        Priority: an explicit ``lancedb_table_name`` argument wins; otherwise
        read ``active_generation.json`` (schema ``active_generation_v1``) from
        the lancedb dir. Missing/invalid pointer fails closed and never falls
        back to the legacy table name ``"chunks"``.
        """
        if self.lancedb_table_name is not None:
            return self.lancedb_table_name

        active_path = lancedb_dir / ACTIVE_GENERATION_FILENAME
        if not active_path.is_file():
            health["lancedb"]["status"] = "failed"
            health["lancedb"]["message"] = (
                f"active_generation.json not found in {lancedb_dir}; "
                "refusing to fall back to table 'chunks'"
            )
            return None

        try:
            payload = json.loads(active_path.read_text(encoding="utf-8"))
            schema_version = payload.get("schema_version")
            if schema_version != ACTIVE_GENERATION_SCHEMA_VERSION:
                raise ValueError(f"unexpected schema_version {schema_version!r}")
            table_name = payload.get("table_name")
            if not isinstance(table_name, str) or not table_name:
                raise ValueError("active_generation.json missing table_name")
        except Exception as exc:
            health["lancedb"]["status"] = "failed"
            health["lancedb"]["message"] = f"failed to read {active_path}: {exc}"
            return None

        health["lancedb"]["active_generation"] = str(active_path)
        health["lancedb"]["table"] = table_name
        return table_name


def _failed_dependencies(health: dict[str, Any], *, component: str, message: str) -> RuntimeDependencies:
    health["status"] = "failed"
    health.setdefault(component, {})["status"] = "failed"
    health[component]["message"] = message
    health["errors"].append({"component": component, "message": message})

    def _unavailable_embedding(_: str) -> list[float]:
        raise RuntimeError(message)

    return RuntimeDependencies(
        sqlite_db_path=Path(health["sqlite"]["path"]),
        lancedb_dir=Path(health["lancedb"].get("path") or "."),
        lancedb_table=None,
        embedding_fn=_unavailable_embedding,
        embedding_model=health["embedding"]["model"],
        embedding_dim=health["embedding"].get("vector_dim"),
        reranker=None,
        reranker_model=health["reranker"]["model"],
        default_model=health["default_model"]["model"],
        health=health,
        retriever=None,
        requested_manifest_id=None,
        index_manifest_id=None,
    )


def _default_lancedb_connect_factory(path: str) -> Any:
    import lancedb  # type: ignore

    return lancedb.connect(path)


def _default_embedding_factory(model_name: str) -> tuple[Callable[[str], list[float]], int | None]:
    del model_name
    raise RuntimeError(
        "default CPU/fp16 embedding loader is disabled; initialize the "
        "identity-bound B6-L CUDA embedder through the manager-approved preflight"
    )


def _detect_index_vector_dim(table: Any) -> int | None:
    for getter in (_sample_rows_from_fts_search, _sample_rows_from_limit_api, _sample_rows_from_to_list):
        rows = getter(table)
        if not rows:
            continue
        vector = rows[0].get("vector")
        if vector is not None:
            try:
                return len(vector)
            except TypeError:
                return None
    return None


def _sample_rows_from_to_list(table: Any) -> list[dict[str, Any]]:
    """Last-resort dimension probe preferring bounded reads.

    Real LanceDB tables expose ``take_offsets``/``head``/``limit``, so the
    whole-table ``to_list()`` path is only used for table objects that have no
    bounded read API at all (fixture doubles and hypothetical stores).
    """
    bounded_names = ("take_offsets", "head", "limit")
    if any(hasattr(table, name) for name in bounded_names):
        for api_name in bounded_names:
            try:
                if api_name == "take_offsets":
                    rows = table.take_offsets([0])
                else:
                    rows = getattr(table, api_name)(1)
            except Exception:
                continue
            if hasattr(rows, "to_list"):
                try:
                    rows = rows.to_list()
                except Exception:
                    continue
            elif hasattr(rows, "to_pylist"):
                try:
                    rows = rows.to_pylist()
                except Exception:
                    continue
            result: list[dict[str, Any]] = []
            for row in rows:
                if isinstance(row, dict):
                    result.append(dict(row))
                    break
            if result:
                return result
        return []
    if not hasattr(table, "to_list"):
        return []
    try:
        # Absolute last resort: the table object exposes no bounded read API.
        rows = table.to_list()
    except Exception:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            result.append(dict(row))
            break
    return result


def _sample_rows_from_limit_api(table: Any) -> list[dict[str, Any]]:
    for api_name in ("limit", "head"):
        if not hasattr(table, api_name):
            continue
        try:
            rows = getattr(table, api_name)(1).to_list()
        except Exception:
            continue
        return [dict(row) for row in rows if isinstance(row, dict)][:1]
    return []


def _sample_rows_from_fts_search(table: Any) -> list[dict[str, Any]]:
    if not hasattr(table, "search"):
        return []
    try:
        query = table.search("health", query_type="fts").limit(1)
        rows = query.to_list()
    except Exception:
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]
