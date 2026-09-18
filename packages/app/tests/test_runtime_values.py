"""No fabricated/runtime-drift values (M6 corrective).

total_latency_ms derives from real monotonic timing (never hardcoded 1);
corrective backend health derives from real runtime capabilities rather than
declaring every EvidenceNeed HEALTHY unconditionally.
"""
from __future__ import annotations

import time
from pathlib import Path

from catalyst_agents.retrieval.task import EvidenceNeed
from catalyst_app.runtime.composition import (
    ProductionRunAdapter,
    _production_capability_registry,
)
from test_default_runtime_composition import (
    FakeGraphResolver,
    FakeRuntimeDependencyLoader,
)


def test_total_latency_derived_from_monotonic_timing(tmp_path: Path) -> None:
    from catalyst_app.persistence.events import EventRepository
    from catalyst_app.runtime.cancel import (
        CancellationTokenRegistry,
        CancellationController,
    )
    from catalyst_app.runtime.claim import RunClaimer
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore

    db_path = tmp_path / "runtime.db"
    adapter = ProductionRunAdapter(
        db_path=db_path,
        events=EventRepository(db_path=db_path),
        claimer=RunClaimer(db_path=db_path, events=EventRepository(db_path=db_path)),
        tokens=CancellationTokenRegistry(),
        cancellation=CancellationController(
            db_path=db_path,
            events=EventRepository(db_path=db_path),
        ),
        credential_store=RuntimeCredentialStore(),
        graph_resolver=FakeGraphResolver(),
    )
    # Simulate a run that took ~1.5s of real monotonic time from its local
    # per-run start (the adapter no longer stores a shared start timestamp).
    latency = adapter._elapsed_latency_ms(time.monotonic() - 1.5)
    assert latency >= 1500
    assert latency != 1


def test_production_capability_registry_derives_health(tmp_path: Path) -> None:
    from dataclasses import replace

    from test_default_runtime_composition import FakeRetriever

    loader = FakeRuntimeDependencyLoader()
    deps = replace(loader.get_dependencies(), retriever=FakeRetriever())
    registry = _production_capability_registry(deps)
    assert registry.is_recoverable(EvidenceNeed.COMPANY_PRIMARY) is True
    assert registry.is_recoverable(EvidenceNeed.COMPANY_NEWS) is True

    # A loader whose runtime is not ready must never report HEALTHY backends.
    broken = FakeRuntimeDependencyLoader()
    broken_health = dict(deps.health)
    broken_health["status"] = "failed"
    broken_health["retrieval"] = {"status": "failed"}
    broken._runtime_health = broken_health

    class BrokenLoader(FakeRuntimeDependencyLoader):
        def get_dependencies(self, *, force_reload: bool = False):
            deps = super().get_dependencies(force_reload=force_reload)
            from dataclasses import replace

            return replace(deps, health=broken_health, retriever=None)

    registry2 = _production_capability_registry(BrokenLoader().get_dependencies())
    assert registry2.is_recoverable(EvidenceNeed.COMPANY_PRIMARY) is False
    assert registry2.is_recoverable(EvidenceNeed.COMPANY_NEWS) is False


def test_cancelled_accounting_is_persisted_once_with_real_role_facts(tmp_path: Path):
    class Events:
        def __init__(self):
            self.calls = []

        def append(self, **kwargs):
            self.calls.append(kwargs)

    class Budget:
        def case_snapshot(self):
            return {
                "case_used_provider_calls": 2,
                "case_used_cost_usd": 0.25,
                "cost_method": "reported",
                "case_outstanding_cost_usd": 0.0,
                "case_role_accounting": {
                    "evidence_analyst": {
                        "logical_calls": 1,
                        "provider_attempts": 1,
                    },
                    "streaming_writer": {
                        "logical_calls": 1,
                        "provider_attempts": 1,
                    },
                },
            }

    events = Events()
    adapter = ProductionRunAdapter(
        db_path=tmp_path / "runtime.db",
        events=events,
        claimer=object(),
        tokens=object(),
        cancellation=object(),
        credential_store=object(),
        graph_resolver=object(),
        provider_budget=Budget(),
    )
    adapter._persist_provider_accounting("run:cancelled")
    adapter._persist_provider_accounting("run:cancelled")
    assert len(events.calls) == 1
    payload = events.calls[0]["artifact_payloads"][0].payload
    assert payload["provider_calls"] == 2
    assert payload["provider"]["analyst_logical_calls"] == 1
    assert payload["provider"]["analyst_provider_attempts"] == 1
    assert payload["provider"]["writer_logical_calls"] == 1
    assert payload["provider"]["writer_provider_attempts"] == 1
