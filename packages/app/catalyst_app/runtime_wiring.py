"""Default M6 app runtime wiring (M6 corrective).

The app owns one runtime composition: one EventRepository (with the SSE
after-commit notifier when a ConditionRegistry is supplied), one RunClaimer,
one CancellationTokenRegistry/CancellationController, one bounded RunExecutor,
one AdmissionController, the production RunManifest factory, and the
production run adapter that invokes the repository-owned ``run_v1_graph``
(Final Migration TSD §11/§15/§17/§18). There is no second runtime, scheduler,
or graph implementation, and no ``V1AppRuntimeNotWired`` default path.
"""
from __future__ import annotations

from pathlib import Path
import os
from typing import Any

from catalyst_app.runtime.admission import AdmissionController
from catalyst_app.runtime.cancel import CancellationController
from catalyst_app.runtime.composition import build_runtime_composition


def _db_path_from_env() -> Path:
    raw = os.getenv("CATALYST_DB_PATH")
    if raw:
        return Path(raw)
    return Path(".local/live_runtime.db")


def build_default_composition() -> Any:
    """Build the production runtime composition (no injected boundaries)."""
    return build_runtime_composition(db_path=_db_path_from_env())


def build_default_admission_controller() -> AdmissionController:
    return build_default_composition().admission


def build_default_cancellation_controller() -> CancellationController:
    return build_default_composition().cancellation


__all__ = [
    "build_default_admission_controller",
    "build_default_cancellation_controller",
    "build_default_composition",
]
