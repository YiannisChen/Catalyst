"""Legacy runtime path removal contract (M6-13).

No daemon/per-run executor thread in the router, no LiveRunService.run_next
poller, and the default Workbench path cannot import or activate the polling
hook. Compatibility GET endpoints may remain until M8.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_live_runs_router_has_no_run_execution_thread() -> None:
    source = Path("packages/api/catalyst_app/routers/live_runs.py").read_text(
        encoding="utf-8"
    )
    assert "threading.Thread" not in source
    assert "threading" not in source
    assert "run_one" not in source
    assert "daemon" not in source


def test_live_run_service_has_no_run_next() -> None:
    from catalyst_agents.runtime.service import LiveRunService

    assert not hasattr(LiveRunService, "run_next")
    source = Path(
        "packages/agents/catalyst_agents/runtime/service.py"
    ).read_text(encoding="utf-8")
    assert "def run_next" not in source


@pytest.mark.skipif(
    not (REPO_ROOT / "apps" / "workbench").is_dir(),
    reason="private legacy workbench UI is not part of the public export",
)
def test_default_workbench_path_does_not_activate_polling() -> None:
    app_source = Path("apps/workbench/src/App.tsx").read_text(encoding="utf-8")
    live_source = Path(
        "apps/workbench/src/components/workbench/LiveWorkbench.tsx"
    ).read_text(encoding="utf-8")
    assert "useLiveRunPolling" not in app_source
    assert "useLiveRunPolling" not in live_source


def test_runtime_loader_has_no_cached_shared_connection(tmp_path: Path) -> None:
    import sqlite3

    from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
    from catalyst_data.retrieval.lexical_display import RETRIEVAL_PROFILE

    db_path = tmp_path / "runtime.db"
    db_path.write_text("ok")
    loader = RuntimeDependencyLoader(
        sqlite_db_path=db_path,
        retrieval_profile=RETRIEVAL_PROFILE,
    )
    deps = loader.get_dependencies()
    for attr, value in vars(deps).items():
        assert not isinstance(value, sqlite3.Connection), (
            f"RuntimeDependencies.{attr} is a cached SQLite connection"
        )


@pytest.mark.skipif(
    not (REPO_ROOT / "apps" / "workbench").is_dir(),
    reason="private legacy workbench UI is not part of the public export",
)
def test_polling_hook_is_marked_migration_only() -> None:
    source = Path("apps/workbench/src/hooks/useLiveRunPolling.ts").read_text(
        encoding="utf-8"
    )
    assert "migration" in source.lower() or "legacy" in source.lower()
