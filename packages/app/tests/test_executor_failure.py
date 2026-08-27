"""Executor failure terminalization (M6-9 robustness)."""
from __future__ import annotations

from pathlib import Path
import threading

from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.executor import RunExecutor


def test_crashed_adapter_terminalizes_failed(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:1', 'RUNNING', NULL, 'a'*64, 'm', 'b'*64, 0, 't', 't')"
        )
        conn.commit()

    terminalized: list[tuple[str, str]] = []

    def failure_handler(run_id: str, code: str) -> None:
        terminalized.append((run_id, code))

    def crashed_adapter(run_id: str, timeout_seconds: float) -> dict:
        raise RuntimeError("graph boom")

    executor = RunExecutor(
        admission_slots=2,
        max_workers=1,
        run_adapter=crashed_adapter,
        failure_handler=failure_handler,
        shutdown_grace_seconds=0.1,
    )
    executor.try_reserve_slot()
    future = executor.submit("run:1", 60.0)
    try:
        future.result(timeout=5)
    except RuntimeError:
        pass

    import time

    deadline = time.monotonic() + 5
    while executor.active_count > 0 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert terminalized == [("run:1", "SYSTEM_ERROR")]
    assert executor.reserved_count == 0
    executor.shutdown(grace_seconds=0)
