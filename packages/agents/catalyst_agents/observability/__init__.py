"""Production-neutral runtime observability contracts (agents-owned).

These types describe facts the V1.1 runtime objectively observes during one
run. They are owned by agents and consumed by the M6 app persistence layer;
they deliberately carry no harness or benchmark concepts.
"""

from catalyst_agents.observability.aggregation import (
    build_retrieval_diagnostics,
    unavailable_retrieval_diagnostics,
)
from catalyst_agents.observability.diagnostics import (
    DIAGNOSTICS_SCHEMA_VERSION,
    CorrectiveRoundObservation,
    EvidenceDeltaObservation,
    ProviderAccountingDiagnostics,
    RetrievalArmObservation,
    RetrievalDiagnostics,
    RetrievalTaskObservation,
    RunDiagnostics,
    TerminalDiagnostics,
    TrajectoryDiagnostics,
    build_run_diagnostics,
    data_runtime_identity_object_hash,
)

__all__ = [
    "DIAGNOSTICS_SCHEMA_VERSION",
    "CorrectiveRoundObservation",
    "EvidenceDeltaObservation",
    "ProviderAccountingDiagnostics",
    "RetrievalArmObservation",
    "RetrievalDiagnostics",
    "RetrievalTaskObservation",
    "RunDiagnostics",
    "TerminalDiagnostics",
    "TrajectoryDiagnostics",
    "build_retrieval_diagnostics",
    "build_run_diagnostics",
    "data_runtime_identity_object_hash",
    "unavailable_retrieval_diagnostics",
]
