from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading
from typing import Any, Callable

from catalyst_data.storage import lancedb_store


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


class RuntimeDependencyLoader:
    def __init__(
        self,
        *,
        sqlite_db_path: str | Path,
        lancedb_dir: str | Path | None = None,
        lancedb_table_name: str = "chunks",
        embedding_model: str = lancedb_store.EMBEDDING_MODEL,
        reranker_model: str = lancedb_store.RERANKER_MODEL,
        default_model: str | None = None,
        expected_embedding_dim: int | None = None,
        embedding_factory: EmbeddingFactory | None = None,
        reranker_factory: RerankerFactory | None = None,
        lancedb_connect_factory: LanceConnectFactory | None = None,
    ) -> None:
        self.sqlite_db_path = Path(sqlite_db_path)
        self.lancedb_dir = Path(lancedb_dir) if lancedb_dir is not None else None
        self.lancedb_table_name = lancedb_table_name
        self.embedding_model = embedding_model
        self.reranker_model = reranker_model
        self.default_model = default_model
        self.expected_embedding_dim = expected_embedding_dim

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

        health["lancedb"]["path"] = str(lancedb_dir)

        try:
            lancedb_db = self._lancedb_connect_factory(str(lancedb_dir))
            lancedb_table = lancedb_db.open_table(self.lancedb_table_name)
        except Exception as exc:
            return _failed_dependencies(
                health,
                component="lancedb",
                message=f"Failed to open LanceDB table '{self.lancedb_table_name}': {exc}",
            )

        try:
            embedding_fn, embedding_dim = self._embedding_factory(self.embedding_model)
        except Exception as exc:
            return _failed_dependencies(
                health,
                component="embedding",
                message=f"Failed to initialize embedding model '{self.embedding_model}': {exc}",
            )

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
        )

    def _resolve_lancedb_dir(self, health: dict[str, Any]) -> Path | None:
        if self.lancedb_dir is not None:
            return self.lancedb_dir
        raw = os.getenv("CATALYST_LANCEDB_DIR")
        if not raw:
            health["lancedb"]["status"] = "failed"
            return None
        return Path(raw)


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
    )


def _default_lancedb_connect_factory(path: str) -> Any:
    import lancedb  # type: ignore

    return lancedb.connect(path)


def _default_embedding_factory(model_name: str) -> tuple[Callable[[str], list[float]], int | None]:
    from FlagEmbedding import BGEM3FlagModel  # type: ignore

    model = BGEM3FlagModel(model_name, use_fp16=True)

    def _embedding_fn(text: str) -> list[float]:
        encoded = model.encode([text], max_length=8192)
        return encoded["dense_vecs"][0].tolist()

    sample_vector = _embedding_fn("runtime-health-check")
    return _embedding_fn, len(sample_vector)


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
    if not hasattr(table, "to_list"):
        return []
    try:
        # Last-resort fallback when no bounded read API exists.
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
