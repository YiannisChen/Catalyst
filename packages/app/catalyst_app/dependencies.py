from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
from catalyst_agents.runtime.service import LiveRunService
from catalyst_app.llm_factory import build_llm
from catalyst_app.workbench_store import WorkbenchStore


def _db_path_from_env() -> Path:
    raw = os.getenv("CATALYST_DB_PATH")
    if raw:
        return Path(raw)
    return Path(".local/live_runtime.db")


def _default_model_from_env() -> str:
    return os.getenv("CATALYST_DEFAULT_MODEL", "model-default")


@lru_cache(maxsize=1)
def get_runtime_dependency_loader() -> RuntimeDependencyLoader:
    return RuntimeDependencyLoader(
        sqlite_db_path=_db_path_from_env(),
        default_model=_default_model_from_env(),
    )


def _graph_factory(model_id: str | None = None):
    """Build a fresh attribution graph with a real LLM client.

    Called once per run — the model varies per user request so the graph
    cannot be globally cached. Heavy deps (LanceDB, embedding model,
    reranker) are cached inside RuntimeDependencyLoader.
    """
    deps = get_runtime_dependency_loader().get_dependencies()
    llm = build_llm(model_id)
    return build_attribution_graph(
        use_critic=True,
        table=deps.lancedb_table,
        embedding_fn=deps.embedding_fn,
        reranker=deps.reranker,
        llm=llm,
    )


@lru_cache(maxsize=1)
def get_live_run_service() -> LiveRunService:
    return LiveRunService(
        db_path=_db_path_from_env(),
        graph_factory=_graph_factory,
    )


@lru_cache(maxsize=1)
def get_workbench_store() -> WorkbenchStore:
    return WorkbenchStore(db_path=_db_path_from_env())
