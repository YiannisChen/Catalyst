from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from catalyst_agents.graphs.graph import build_v1_graph_adapter
from catalyst_agents.runtime.dependencies import (
    EXPECTED_FTS_DIGEST_ENV,
    RETRIEVAL_PROFILE_ENV,
    RuntimeDependencyLoader,
)
from catalyst_agents.runtime.service import LiveRunService

from catalyst_app.db_paths import runtime_db_path, served_db_path
from catalyst_app.llm_factory import build_v1_llm
from catalyst_app.runtime_credential_store import RuntimeCredentialStore
from catalyst_app.workbench_store import WorkbenchStore


def _served_db_path_from_env() -> Path:
    return served_db_path()


def _runtime_db_path_from_env() -> Path:
    return runtime_db_path()


def _default_model_from_env() -> str:
    return os.getenv("CATALYST_DEFAULT_MODEL", "model-default")


def _retrieval_profile_from_env() -> str | None:
    """The V1.1 retrieval profile (Frozen-A3 §2.1). ``lexical_display_v1`` is
    the only accepted value; the loader fails closed otherwise."""
    return os.getenv(RETRIEVAL_PROFILE_ENV)


def _expected_fts_digest_from_env() -> str | None:
    """The operator-supplied served FTS digest the live tuple must match."""
    return os.getenv(EXPECTED_FTS_DIGEST_ENV)


@lru_cache(maxsize=1)
def get_runtime_dependency_loader() -> RuntimeDependencyLoader:
    return RuntimeDependencyLoader(
        sqlite_db_path=_served_db_path_from_env(),
        default_model=_default_model_from_env(),
        retrieval_profile=_retrieval_profile_from_env(),
        expected_fts_digest=_expected_fts_digest_from_env(),
        require_identity_bound_runtime=True,
    )


@lru_cache(maxsize=1)
def get_credential_store() -> RuntimeCredentialStore:
    """Singleton in-memory store for runtime API keys -- never persisted."""
    return RuntimeCredentialStore()


def _graph_factory(model: dict | str | None = None, *, api_key: str | None = None):
    """Build a fresh attribution graph with a real LLM client.

    Called once per run -- the model varies per user request so the graph
    cannot be globally cached. The bound V1.1 lexical retriever is cached
    inside RuntimeDependencyLoader.

    Args:
        model:   Dict with provider/model_id/base_url (BYOK path) or legacy
                 model_id string.
        api_key: API key from RuntimeCredentialStore. Never from persisted config.
    """
    deps = get_runtime_dependency_loader().get_dependencies()

    if isinstance(model, dict):
        llm = build_v1_llm(
            model_id=model.get("model_id"),
            provider=model.get("provider", "openai"),
            api_key=api_key,  # from RuntimeCredentialStore, NOT from persisted config
            base_url=model.get("base_url"),
        )
    else:
        llm = build_v1_llm(model, api_key=api_key)

    # M6 corrective: the production app runtime invokes run_v1_graph directly
    # through the app-owned composition; this legacy loader surface exists only
    # for LiveRunService compatibility reads and fails closed on invoke.
    return build_v1_graph_adapter(
        model=llm,
        retriever=deps.retriever,
        requested_manifest_id=deps.requested_manifest_id,
    )


@lru_cache(maxsize=1)
def get_live_run_service() -> LiveRunService:
    return LiveRunService(
        db_path=_runtime_db_path_from_env(),
        graph_factory=_graph_factory,
        credential_store=get_credential_store(),
    )


def _prices_db_path() -> Path | None:
    """The market database the data update writes, when it exists."""
    from catalyst_data.storage.runtime_paths import runtime_store_path

    try:
        path = runtime_store_path("market_db", Path(__file__).resolve().parents[3])
    except ValueError:
        return None
    return path if path.exists() else None


@lru_cache(maxsize=1)
def get_workbench_store() -> WorkbenchStore:
    return WorkbenchStore(db_path=_served_db_path_from_env(), prices_db_path=_prices_db_path())
