"""No fabricated/runtime-drift values (M6 corrective).

total_latency_ms derives from real monotonic timing (never hardcoded 1);
corrective backend health derives from real runtime capabilities rather than
declaring every EvidenceNeed HEALTHY unconditionally.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
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
        CancellationController,
        CancellationTokenRegistry,
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


def test_production_capability_registry_healthy_without_lancedb(tmp_path: Path) -> None:
    """Frozen-A3 §2.1: V1.1 has no dense arm. The corrective registry is healthy
    when status, retrieval and sqlite are ready and a retriever is bound — the
    lancedb/embedding components no longer gate it."""
    from dataclasses import replace

    from test_default_runtime_composition import FakeRetriever

    loader = FakeRuntimeDependencyLoader()
    deps = replace(loader.get_dependencies(), retriever=FakeRetriever())
    health = dict(deps.health)
    health["lancedb"] = {"status": "not_served"}
    health["embedding"] = {"status": "not_served"}
    health["reranker"] = {"status": "not_served"}
    deps = replace(deps, health=health)

    registry = _production_capability_registry(deps)
    assert registry.is_recoverable(EvidenceNeed.COMPANY_PRIMARY) is True
    assert registry.is_recoverable(EvidenceNeed.COMPANY_NEWS) is True


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


def test_run_budget_guard_requires_exact_model_price_and_uses_configured_bound(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    import pytest
    from catalyst_agents.runtime.provider_budget import (
        ProviderPriceUnavailable,
        RoleTokenLimit,
    )
    from catalyst_app.persistence.connect import open_rw
    from catalyst_app.persistence.schema import init_runtime_db
    from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger

    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        now = "2026-11-02T12:00:00+00:00"
        conn.execute(
            "INSERT INTO runs (run_id,lifecycle_status,request_hash,run_manifest_id,"
            "manifest_hash,capacity_slot,created_at,updated_at,access_tier) "
            "VALUES ('public-run','ACCEPTED','hash','manifest','hash',0,?,?,'anonymous')",
            (now, now),
        )
        conn.commit()
    adapter = ProductionRunAdapter.__new__(ProductionRunAdapter)
    adapter.db_path = db_path
    adapter.provider_budget = None
    monkeypatch.delenv("CATALYST_MODEL_PRICES_JSON", raising=False)
    monkeypatch.delenv("CATALYST_PRICING_FILE", raising=False)

    with pytest.raises(RuntimeError, match="budget reservation unavailable"):
        adapter._provider_budget_for_run("public-run")

    with open_rw(db_path) as conn:
        now = "2026-11-02T12:00:00+00:00"
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id, "
            "manifest_hash, capacity_slot, created_at, updated_at, access_tier) "
            "VALUES ('reserved-run', 'ACCEPTED', 'reserved-hash', 'manifest', 'hash', 0, ?, ?, 'anonymous')",
            (now, now),
        )
        BudgetLedger(max_run_cost_usd=0.05).reserve(
            conn,
            "reserved-run",
            BudgetAdmission(
                tier="anonymous", caller_key="ip-hash", max_cost_usd=0.02,
                global_budget=False,
            ),
            now=datetime.fromisoformat(now),
        )
        conn.commit()
        byok_reservation = conn.execute(
            "SELECT reserved_cost_micros, max_run_cost_micros "
            "FROM run_budget_reservations WHERE run_id='reserved-run'"
        ).fetchone()
        assert (byok_reservation[0], byok_reservation[1]) == (0, 20_000)

    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(json.dumps({
        "models": {"provider-x/model-y": {
            "input_usd_per_million_tokens": 2.0,
            "output_usd_per_million_tokens": 4.0,
        }},
        "role_token_limits": {
            "evidence_analyst": {"max_input_tokens": 128000, "max_output_tokens": 4000},
            "streaming_writer": {"max_input_tokens": 128000, "max_output_tokens": 8000},
            "primary_verifier": {"max_input_tokens": 6000, "max_output_tokens": 400},
            "reported_reason": {"max_input_tokens": 24000, "max_output_tokens": 3000},
            "desk_extractor": {"max_input_tokens": 4000, "max_output_tokens": 600},
            "event_dater": {"max_input_tokens": 4000, "max_output_tokens": 200},
        },
    }), encoding="utf-8")
    monkeypatch.setenv("CATALYST_PRICING_FILE", str(pricing_path))
    priced = adapter._provider_budget_for_run("reserved-run")
    assert priced.max_cost_usd == pytest.approx(0.02)
    priced.register_role_limit("streaming_writer", RoleTokenLimit(1_000, 100))
    reservation = priced.reserve(
        role="streaming_writer", provider="provider-x", model_id="model-y"
    )
    assert reservation.reserved_cost_usd == pytest.approx(0.0024)
    from catalyst_app.runtime.composition import _failure_code_for_exception

    assert _failure_code_for_exception("PROVIDER_UNAVAILABLE", ProviderPriceUnavailable()) == (
        "MODEL_PRICE_UNAVAILABLE"
    )


def test_run_budget_rejects_duplicate_exact_model_price_entries(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    import pytest
    from catalyst_app.persistence.connect import open_rw
    from catalyst_app.persistence.schema import init_runtime_db

    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        now = "2026-11-02T12:00:00+00:00"
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id, "
            "manifest_hash, capacity_slot, created_at, updated_at) "
            "VALUES ('reserved-run', 'ACCEPTED', 'hash', 'manifest', 'hash', 0, ?, ?)",
            (now, now),
        )
        from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger

        BudgetLedger().reserve(
            conn,
            "reserved-run",
            BudgetAdmission(tier="anonymous", caller_key="test-caller", max_cost_usd=0.05),
            now=datetime.fromisoformat(now),
        )
        conn.commit()
    adapter = ProductionRunAdapter.__new__(ProductionRunAdapter)
    adapter.db_path = db_path
    adapter.provider_budget = None
    monkeypatch.setenv(
        "CATALYST_MODEL_PRICES_JSON",
        json.dumps([
            {
                "provider": "provider-x",
                "model_id": "model-y",
                "input_usd_per_million_tokens": 2.0,
                "output_usd_per_million_tokens": 4.0,
            },
            {
                "provider": "provider-x",
                "model_id": "model-y",
                "input_usd_per_million_tokens": 0.01,
                "output_usd_per_million_tokens": 0.01,
            },
        ]),
    )

    with pytest.raises(RuntimeError, match="CATALYST_MODEL_PRICES_JSON is invalid"):
        adapter._provider_budget_for_run("reserved-run")


def test_provider_budget_setup_failure_happens_before_run_claim(tmp_path: Path) -> None:
    import pytest
    from catalyst_app.persistence.connect import open_rw
    from catalyst_app.persistence.schema import init_runtime_db

    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        now = "2026-11-02T12:00:00+00:00"
        conn.execute(
            "INSERT INTO runs (run_id,lifecycle_status,request_hash,run_manifest_id,"
            "manifest_hash,capacity_slot,created_at,updated_at,access_tier) "
            "VALUES ('unreserved-run','ACCEPTED','public-hash','manifest','hash',0,?,?,'anonymous')",
            (now, now),
        )
        conn.commit()

    class ClaimRecorder:
        calls = 0

        def claim_run(self, *args, **kwargs):
            self.calls += 1
            return True

    adapter = ProductionRunAdapter.__new__(ProductionRunAdapter)
    adapter.db_path = db_path
    adapter._budget_local = threading.local()
    adapter.provider_budget = None
    adapter._load_manifest = lambda run_id: object()
    adapter.claimer = ClaimRecorder()

    with pytest.raises(RuntimeError, match="budget reservation unavailable"):
        adapter("unreserved-run", timeout_seconds=1)
    assert adapter.claimer.calls == 0


def test_admin_guard_uses_run_cap_without_public_budget_charge(tmp_path: Path) -> None:
    import pytest
    from catalyst_app.persistence.connect import open_rw
    from catalyst_app.persistence.schema import init_runtime_db
    from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger

    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        now = "2026-11-02T12:00:00+00:00"
        conn.execute(
            "INSERT INTO runs (run_id,lifecycle_status,request_hash,run_manifest_id,"
            "manifest_hash,capacity_slot,created_at,updated_at,access_tier) "
            "VALUES ('admin-run','ACCEPTED','admin-hash','manifest','hash',0,?,?,'admin')",
            (now, now),
        )
        BudgetLedger().reserve(
            conn,
            "admin-run",
            BudgetAdmission(tier="admin", caller_key=None, max_cost_usd=0.02),
            now=datetime.fromisoformat(now),
        )
        conn.commit()

    adapter = ProductionRunAdapter.__new__(ProductionRunAdapter)
    adapter.db_path = db_path
    adapter.provider_budget = None
    guard = adapter._provider_budget_for_run("admin-run")
    assert guard.max_cost_usd == pytest.approx(0.02)
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT quota_charged, global_budget_charged "
            "FROM run_budget_reservations WHERE run_id='admin-run'"
        ).fetchone()
    assert (row[0], row[1]) == (0, 0)
