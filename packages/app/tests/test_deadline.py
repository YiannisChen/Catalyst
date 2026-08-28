"""Absolute run deadline derived at admission (M6 corrective).

One deadline is persisted at admission (deadline_at = created_at +
run_timeout_seconds); execution reuses it so queue delay reduces the remaining
budget and recovery never resets the 60-second budget. A queued run that
starts after its deadline fails without provider dispatch, and provider/stage
timeouts can never exceed the remaining run time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import time
from pathlib import Path

from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository, payload_sha256
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.admission import (
    AdmissionController,
    AdmissionRequest,
)
from catalyst_app.runtime.executor import RunExecutor
from test_admission import _events, _manifest, _request


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fixture_db(db_path: Path) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()


def _seed_accepted_run(
    db_path: Path,
    run_id: str,
    *,
    created_at: datetime,
    deadline_at: datetime | None,
) -> None:
    with open_rw(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        manifest = _manifest(_request(), run_id, "a" * 64)
        manifest_json = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at,"
            " ticker, provider, base_url, deadline_at)"
            " VALUES (?, 'ACCEPTED', NULL, ?, ?, ?, 0, ?, ?, 'AAPL', 'openai', NULL, ?)",
            (
                run_id,
                "a" * 64,
                f"manifest:{run_id}",
                "x" * 64,
                created_at.isoformat(),
                created_at.isoformat(),
                deadline_at.isoformat() if deadline_at is not None else None,
            ),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES (?, 1, ?, 'run.accepted', 'ADMISSION', ?, 'v1')",
            (run_id, created_at.isoformat(), json.dumps({"accepted": True})),
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type,"
            " payload_hash, payload_json, optional)"
            " VALUES (?, ?, 1, 'run_manifest', ?, ?, 0)",
            (f"manifest:{run_id}", run_id, payload_sha256(manifest.model_dump(mode="json")), manifest_json),
        )
        conn.commit()


class _RecordingSyncExecutor:
    """RunExecutor-compatible synchronous executor recording submit budgets."""

    def __init__(self, adapter, *, admission_slots: int = 4) -> None:
        self.adapter = adapter
        self.admission_slots = admission_slots
        self._reserved: set[str] = set()
        self.submitted: list[tuple[str, float]] = []

    def try_reserve_slot(self, run_id: str | None = None) -> bool:
        key = run_id or "__anon__"
        if key in self._reserved:
            return True
        if len(self._reserved) >= self.admission_slots:
            return False
        self._reserved.add(key)
        return True

    @property
    def reserved_count(self) -> int:
        return len(self._reserved)

    def release_slot(self, run_id: str | None = None) -> bool:
        if run_id is not None:
            return run_id in self._reserved and self._reserved.discard(run_id) or False
        return False

    def reconcile_slots(self, durable_capacity_bearing) -> None:
        if isinstance(durable_capacity_bearing, int):
            self._reserved = set(list(self._reserved)[:durable_capacity_bearing])
        else:
            self._reserved = set(durable_capacity_bearing)

    def submit(self, run_id: str, timeout_seconds: float):
        self.submitted.append((run_id, timeout_seconds))
        return self.adapter(run_id, timeout_seconds)


def test_queued_run_past_deadline_fails_without_provider_dispatch(tmp_path: Path) -> None:
    """A run that starts after its absolute deadline fails TIMEOUT and never
    dispatches observation/retrieval/analyst/writer providers."""
    from catalyst_app.runtime.composition import (
        ProductionGraphRuntimeResolver,
        build_runtime_composition,
    )
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore
    from test_default_runtime_composition import (
        FakeGraphResolver,
        FakeRuntimeDependencyLoader,
    )

    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    now = _utc_now()
    _seed_accepted_run(
        db_path,
        "run:queued-past",
        created_at=now - timedelta(seconds=70),
        deadline_at=now - timedelta(seconds=10),
    )

    resolver = FakeGraphResolver()
    composition = build_runtime_composition(
        db_path=db_path,
        dependency_loader=FakeRuntimeDependencyLoader(),
        credential_store=RuntimeCredentialStore(),
        graph_resolver=resolver,
        max_workers=1,
        shutdown_grace_seconds=0.1,
    )
    result = composition.run_adapter("run:queued-past", timeout_seconds=0.0)

    assert result["status"] == "FAILED"
    assert result["failure_code"] == "TIMEOUT"
    assert resolver.analyst.calls == 0
    assert resolver.writer.calls == 0
    assert resolver.retriever.calls == 0
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status, failure_code FROM runs WHERE run_id = ?",
            ("run:queued-past",),
        ).fetchone()
    assert row["lifecycle_status"] == "FAILED"
    assert row["failure_code"] == "TIMEOUT"


def test_queue_delay_reduces_remaining_budget(tmp_path: Path) -> None:
    """Submit receives the remaining time to the persisted deadline, so queue
    delay reduces the budget instead of resetting it to 60 seconds."""
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    now = _utc_now()
    deadline_at = now + timedelta(seconds=3)
    _seed_accepted_run(
        db_path, "run:queued", created_at=now, deadline_at=deadline_at
    )

    repo = _events(db_path)
    executor = _RecordingSyncExecutor(adapter=lambda run_id, t: {"ok": True})
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=repo,
        manifest_factory=_manifest,
    )
    controller.startup_recovery()

    assert len(executor.submitted) == 1
    run_id, budget = executor.submitted[0]
    assert run_id == "run:queued"
    # Budget is the remaining ~3s, never the full 60s budget.
    assert 0 < budget < 60
    assert abs(budget - 3.0) < 1.5

    manifest = _manifest(_request(), run_id, "a" * 64)
    before = controller._remaining_seconds_for_run(run_id, manifest)
    time.sleep(0.6)
    after = controller._remaining_seconds_for_run(run_id, manifest)
    assert after < before


def test_recovery_does_not_reset_deadline(tmp_path: Path) -> None:
    """Startup recovery reuses the original persisted deadline; the submitted
    budget is the remaining time, not a fresh 60-second window."""
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    now = _utc_now()
    deadline_at = now + timedelta(seconds=2)
    _seed_accepted_run(
        db_path, "run:recover", created_at=now, deadline_at=deadline_at
    )

    time.sleep(1.0)

    repo = _events(db_path)
    executor = _RecordingSyncExecutor(adapter=lambda run_id, t: {"ok": True})
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=repo,
        manifest_factory=_manifest,
    )
    controller.startup_recovery()

    assert len(executor.submitted) == 1
    run_id, budget = executor.submitted[0]
    assert run_id == "run:recover"
    # ~1s remaining of the original 2s deadline, not 60 and not 2.
    assert 0 < budget < 60
    assert abs(budget - 1.0) < 1.0
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT deadline_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    # The persisted deadline was never rewritten by recovery.
    assert abs(
        (datetime.fromisoformat(row["deadline_at"]) - deadline_at).total_seconds()
    ) < 0.01
