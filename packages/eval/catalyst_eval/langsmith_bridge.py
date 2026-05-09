from __future__ import annotations

import os
from typing import Any


def resolve_langsmith_header(env: dict[str, Any] | None = None) -> dict[str, Any]:
    active_env = env if env is not None else os.environ
    enabled = str(active_env.get("LANGCHAIN_TRACING_V2", "false")).lower() == "true"
    project = active_env.get("LANGCHAIN_PROJECT") if enabled else None
    return {"langsmith_enabled": enabled, "langsmith_project": project}
