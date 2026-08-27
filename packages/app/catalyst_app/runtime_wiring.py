"""Default M6 app runtime wiring.

The M6 runtime machinery (admission, executor, event repository, SSE) is
wired here. The production RunManifest factory and the agents run adapter that
invokes ``run_v1_graph`` with real temporal identity / observation provider /
persistence envelope are intentionally NOT fabricated in M6: ``V1GraphAdapter``
still raises ``V1AppRuntimeNotWired`` (M5/M6 boundary). The default wiring
fails closed with the same typed error; M6 tests inject fake adapters.
"""
from __future__ import annotations

from pathlib import Path
import os
from typing import Any

from catalyst_agents.graph import V1AppRuntimeNotWired
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.admission import AdmissionController, AdmissionRequest
from catalyst_app.runtime.cancel import CancellationController
from catalyst_app.runtime.claim import RunClaimer
from catalyst_app.runtime.executor import RunExecutor
from catalyst_agents.runtime.manifest import RunManifest


def _db_path_from_env() -> Path:
    raw = os.getenv("CATALYST_DB_PATH")
    if raw:
        return Path(raw)
    return Path(".local/live_runtime.db")


def _default_manifest_factory(
    request: AdmissionRequest, run_id: str, request_hash: str
) -> RunManifest:
    raise V1AppRuntimeNotWired(
        "production RunManifest factory wiring (temporal identity, data "
        "runtime identity, prompt hashes) lands with the V1 graph invocation"
    )


def _default_run_adapter(run_id: str, timeout_seconds: float) -> Any:
    raise V1AppRuntimeNotWired(
        "production agents run adapter wiring lands with the V1 graph "
        "invocation; M6 tests inject fake adapters"
    )


def build_default_cancellation_controller() -> CancellationController:
    db_path = _db_path_from_env()
    events = EventRepository(db_path=db_path)
    return CancellationController(
        db_path=db_path,
        events=events,
        claimer=RunClaimer(db_path=db_path, events=events),
    )


def build_default_admission_controller() -> AdmissionController:
    db_path = _db_path_from_env()
    events = EventRepository(db_path=db_path)
    executor = RunExecutor(
        admission_slots=4,
        max_workers=2,
        run_adapter=_default_run_adapter,
    )
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=events,
        manifest_factory=_default_manifest_factory,
    )
    # Ensure the runtime schema exists so health/startup work on a fresh DB.
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
    return controller


__all__ = ["build_default_admission_controller", "build_default_cancellation_controller"]
