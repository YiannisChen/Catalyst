"""Cooperative cancellation protocol contract (M6-8).

Final TSD §11: ACCEPTED cancellation is terminal; RUNNING persists
CANCEL_REQUESTED and signals the control token; CANCELLED is reached only when
work stops or late output is safely discarded; late completion loses the
conditional update; CANCEL_REQUESTED -> FAILED on integrity failure; no events
or artifacts after a terminal CANCELLED; repeated cancels are idempotent.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from catalyst_app.events import (
    AssuranceCompletedPayload,
    RunCancelledPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
    StageStartedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository, TerminalRunError
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.cancel import (
    CancelOutcome,
    CancellationController,
    CancellationTokenRegistry,
)
from catalyst_app.runtime.claim import RunClaimer


def _seed_run(db_path: Path, run_id: str = "run:1", status: str = "ACCEPTED") -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES (?, ?, NULL, ?, ?, ?, 0, 't', 't')",
            (run_id, status, "d" * 64, f"manifest:{run_id}", "e" * 64),
        )
        conn.commit()


def _controller(db_path: Path) -> CancellationController:
    events = EventRepository(db_path=db_path)
    return CancellationController(
        db_path=db_path,
        events=events,
        claimer=RunClaimer(db_path=db_path, events=events),
    )


def _seed_accepted_event(db_path: Path, run_id: str = "run:1") -> None:
    from catalyst_app.events import RunAcceptedPayload

    EventRepository(db_path=db_path).append(
        run_id=run_id,
        event_type=RunEventType.RUN_ACCEPTED,
        stage="ADMISSION",
        payload=RunAcceptedPayload(
            request_identity_digest="a" * 64,
            ticker="AAPL",
            trade_date="2026-01-06",
            workflow_version="v1.1",
            model_provider_label="test/model",
            stream_url=f"/api/live-runs/{run_id}/stream",
        ),
    )


def _lifecycle(db_path: Path, run_id: str = "run:1") -> str:
    with open_rw(db_path) as conn:
        return conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]


def test_cancel_accepted_is_terminal(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="ACCEPTED")
    _seed_accepted_event(db_path)
    controller = _controller(db_path)

    outcome = controller.request_cancel("run:1")

    assert outcome.acknowledged is True
    assert outcome.status == RunLifecycleStatus.CANCELLED
    assert _lifecycle(db_path) == "CANCELLED"
    with open_rw(db_path) as conn:
        cancelled = conn.execute(
            "SELECT event_type FROM run_events WHERE run_id='run:1' AND event_type='run.cancelled'"
        ).fetchone()
        assert cancelled is not None


def test_cancel_running_persists_cancel_requested_and_signals_token(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    controller = _controller(db_path)

    outcome = controller.request_cancel("run:1")

    assert outcome.acknowledged is True
    assert outcome.status == RunLifecycleStatus.CANCEL_REQUESTED
    assert _lifecycle(db_path) == "CANCEL_REQUESTED"
    token = controller.tokens.token("run:1")
    assert token.requested is True


def test_repeated_cancel_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    controller = _controller(db_path)

    first = controller.request_cancel("run:1")
    second = controller.request_cancel("run:1")
    third = controller.request_cancel("run:1")

    assert first.acknowledged is True
    assert second.acknowledged is True
    assert third.acknowledged is True
    assert _lifecycle(db_path) == "CANCEL_REQUESTED"


def test_acknowledge_cancellation_reaches_cancelled(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    _seed_accepted_event(db_path)
    controller = _controller(db_path)
    controller.request_cancel("run:1")

    outcome = controller.acknowledge_cancellation("run:1")

    assert outcome.status == RunLifecycleStatus.CANCELLED
    assert _lifecycle(db_path) == "CANCELLED"
    with open_rw(db_path) as conn:
        cancelled = conn.execute(
            "SELECT event_type FROM run_events WHERE run_id='run:1' AND event_type='run.cancelled'"
        ).fetchone()
        assert cancelled is not None


def test_late_completion_after_cancel_is_discarded(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    _seed_accepted_event(db_path)
    controller = _controller(db_path)
    controller.request_cancel("run:1")

    claimer = RunClaimer(db_path=db_path, events=EventRepository(db_path=db_path))
    with pytest.raises(ValueError):
        claimer.terminalize(
            run_id="run:1",
            assurance_payload=AssuranceCompletedPayload(valid=True, final_result_status="SUFFICIENT"),
            terminal_event_type=RunEventType.RUN_COMPLETED,
            terminal_payload=RunCompletedPayload(
                result_status="SUFFICIENT",
                final_output_artifact_ref="a",
                total_latency_ms=1,
                runtime_identity_ref="r",
            ),
            lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
        )

    assert _lifecycle(db_path) == "CANCEL_REQUESTED"
    controller.acknowledge_cancellation("run:1")
    with open_rw(db_path) as conn:
        completed = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id='run:1' AND event_type='run.completed'"
        ).fetchone()[0]
        assert completed == 0
        cancelled = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id='run:1' AND event_type='run.cancelled'"
        ).fetchone()[0]
        assert cancelled == 1


def test_no_events_after_cancelled_terminal(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    _seed_accepted_event(db_path)
    controller = _controller(db_path)
    controller.request_cancel("run:1")
    controller.acknowledge_cancellation("run:1")

    repo = EventRepository(db_path=db_path)
    with pytest.raises(TerminalRunError):
        repo.append(
            run_id="run:1",
            event_type=RunEventType.STAGE_STARTED,
            payload=StageStartedPayload(stage="late"),
        )


def test_cancel_requested_can_fail_on_integrity_failure(tmp_path: Path) -> None:
    """CANCEL_REQUESTED -> FAILED is the cancellation-handling integrity path."""
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    _seed_accepted_event(db_path)
    controller = _controller(db_path)
    controller.request_cancel("run:1")

    claimer = RunClaimer(db_path=db_path, events=EventRepository(db_path=db_path))
    claimer.terminalize(
        run_id="run:1",
        assurance_payload=AssuranceCompletedPayload(valid=False, violations=("cancel_integrity",)),
        terminal_event_type=RunEventType.RUN_FAILED,
        terminal_payload=RunFailedPayload(
            failure_code="CANCELLATION_INTEGRITY_FAILURE",
            stage="STREAMING_ANSWER",
            retryable=False,
        ),
        lifecycle_update=(RunLifecycleStatus.CANCEL_REQUESTED, RunLifecycleStatus.FAILED),
    )

    assert _lifecycle(db_path) == "FAILED"
    with open_rw(db_path) as conn:
        failed = conn.execute(
            "SELECT failure_code FROM runs WHERE run_id='run:1'"
        ).fetchone()[0]
        assert failed == "CANCELLATION_INTEGRITY_FAILURE"


def test_cancellation_token_is_threadsafe_boundary_check(tmp_path: Path) -> None:
    """The control token is the cooperative boundary the run adapter observes."""
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    controller = _controller(db_path)
    token = controller.tokens.token("run:1")
    assert token.requested is False

    controller.request_cancel("run:1")
    assert token.requested is True
    assert token.wait(timeout=1.0) is True



# ── M6 corrective: public cancel API over the real runtime composition ─────

def _fake_loader():
    from catalyst_agents.runtime.dependencies import RuntimeDependencies

    from test_default_runtime_composition import _runtime

    return RuntimeDependencies(
        sqlite_db_path=Path("/tmp/catalyst-test.db"),
        lancedb_dir=Path("/tmp/lancedb"),
        lancedb_table=None,
        embedding_fn=lambda _: [],
        embedding_model="BAAI/bge-m3",
        embedding_dim=1024,
        reranker=None,
        reranker_model="BAAI/bge-reranker-v2-m3",
        default_model="gemini-2.5-flash-nothink",
        health={"status": "ready", "errors": []},
        retriever=None,
        requested_manifest_id="m" * 64,
        index_manifest_id="d" * 64,
        data_runtime_identity=_runtime(),
    )


class _CancelAwareLoader:
    """Loader exposing identity-bound runtime deps for the production manifest."""

    def get_dependencies(self, *, force_reload: bool = False):
        return _fake_loader()


class _CancelAwareWriter:
    """Writer provider that blocks until the shared cancellation token fires.

    This is the deterministic external-boundary stand-in for a real
    non-cancellable provider call: it only returns after cancellation was
    requested, so the real run adapter must discard the late output and reach
    CANCELLED through its own boundary checks.
    """

    def __init__(self, token, started) -> None:
        self._token = token
        self._started = started
        self.calls = 0
        self.capability_metadata = {
            "supports_structured_output": True,
            "supports_true_streaming": True,
            "declares_token_accounting": True,
            "normalizes_timeout_errors": True,
            "capability_revision": "v1.1-capability-1",
        }

    def stream(self, messages):
        self.calls += 1
        self._started.set()
        self._token.wait(timeout=10)
        return iter(("SUMMARY\nCancelled.\nLIMITATIONS\nNone.",))


class _CancelResolver:
    """Real resolver boundary over a per-run token from the shared registry."""

    def __init__(self, tokens) -> None:
        self.tokens = tokens
        self.writer: _CancelAwareWriter | None = None
        self.writer_started = threading.Event()

    def resolve(self, manifest, boundary):
        from catalyst_agents.retrieval.corrective import (
            BackendCapability,
            BackendHealth,
            CorrectiveCapabilityRegistry,
        )
        from catalyst_agents.retrieval.task import EvidenceNeed
        from catalyst_app.runtime.composition import ResolvedGraphRuntime
        from test_default_runtime_composition import FakeObservationProvider, FakeRetriever, _runtime

        capabilities = {}
        for name in (
            "COMPANY_PRIMARY", "COMPANY_NEWS", "SECTOR_NEWS", "MACRO_EVENT",
            "MACRO_SERIES", "FUNDAMENTALS",
        ):
            capabilities[EvidenceNeed(name)] = BackendCapability(
                backend=f"backend:{name.lower()}", health=BackendHealth.HEALTHY
            )
        self.writer = _CancelAwareWriter(self.tokens.token(manifest.run_id), self.writer_started)
        from test_default_runtime_composition import FakeAnalystProvider, _runtime

        return ResolvedGraphRuntime(
            observation_provider=FakeObservationProvider(),
            retriever=FakeRetriever(),
            analyst_llm=FakeAnalystProvider(),
            writer_llm=self.writer,
            capability_registry=CorrectiveCapabilityRegistry(capabilities),
            data_runtime_identity=_runtime(),
            corrective_policy=None,
            prompt_template="You are the Evidence Analyst. Emit the strict schema.",
            query_builder=None,
            structured_provider=None,
        )


def _cancel_composition_app(tmp_path: Path):
    from fastapi.testclient import TestClient

    from catalyst_app.main import create_app
    from catalyst_app.runtime.composition import build_runtime_composition
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore

    composition = build_runtime_composition(
        db_path=tmp_path / "runtime.db",
        dependency_loader=_CancelAwareLoader(),
        credential_store=RuntimeCredentialStore(),
        graph_resolver=_CancelResolver(tokens=None),  # replaced below
        max_workers=2,
        shutdown_grace_seconds=0.2,
    )
    composition.run_adapter.graph_resolver = _CancelResolver(tokens=composition.tokens)
    app = create_app(runtime_composition=composition)
    app.state.cancellation_controller = composition.cancellation
    return app, composition


def _cancel_body(**overrides: object) -> dict:
    body = {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": "Why did AAPL move?",
        "model": {
            "provider": "openai",
            "model_id": "gpt-4.1-mini",
            "api_key": "",
            "credential_source": "server_env",
        },
    }
    body.update(overrides)
    return body


def test_cancel_api_running_reaches_cancelled_with_real_adapter_boundary(tmp_path: Path) -> None:
    """Finding D: POST /api/live-runs/{run_id}/cancel -> RUNNING ->
    CANCEL_REQUESTED -> cooperative stop -> CANCELLED through the real run
    adapter boundary sharing the composition token registry."""
    from fastapi.testclient import TestClient

    app, composition = _cancel_composition_app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=_cancel_body())
        assert created.status_code == 200
        run_id = created.json()["run_id"]

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            summary = client.get(f"/api/live-runs/{run_id}").json()
            if summary["lifecycle_status"] == "RUNNING":
                break
            time.sleep(0.02)
        assert summary["lifecycle_status"] == "RUNNING"
        # Deterministic boundary: the writer provider is now blocked inside
        # the real graph execution, so the token must reach that boundary.
        assert composition.run_adapter.graph_resolver.writer_started.wait(timeout=10)

        cancel = client.post(f"/api/live-runs/{run_id}/cancel")
        assert cancel.status_code == 200
        assert cancel.json()["acknowledged"] is True
        assert cancel.json()["status"] in {"CANCEL_REQUESTED", "CANCELLED"}

        # The writer provider saw the shared token, so the graph returned; the
        # real adapter discarded the late output and acknowledged CANCELLED.
        final = _wait_terminal_cancel(client, run_id)
        assert final["lifecycle_status"] == "CANCELLED"
        resolver = composition.run_adapter.graph_resolver
        assert resolver.writer is not None
        assert resolver.writer.calls >= 1

        # No COMPLETED transition, no terminal artifacts after CANCELLED.
        assert final.get("attribution_status") is None
        events = client.get(f"/api/live-runs/{run_id}/stream").text
        assert "event: run.completed" not in events
        assert "event: run.cancelled" in events


def test_cancel_api_accepted_is_terminal_and_idempotent(tmp_path: Path) -> None:
    """Finding D: ACCEPTED -> CANCELLED terminal; repeated cancel is idempotent."""
    from catalyst_app.runtime.composition import build_runtime_composition
    from catalyst_app.main import create_app
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore
    from fastapi.testclient import TestClient

    # A run adapter that never claims (simulates queue delay) so the run stays
    # ACCEPTED and cancellation is terminal before claim.
    gate = threading.Event()

    def never_start(run_id: str, timeout_seconds: float):
        gate.wait(timeout=10)
        return {"run_id": run_id, "status": "COMPLETED"}

    composition = build_runtime_composition(
        db_path=tmp_path / "runtime.db",
        dependency_loader=_CancelAwareLoader(),
        credential_store=RuntimeCredentialStore(),
        graph_resolver=_CancelResolver(tokens=None),
        run_adapter=never_start,
        max_workers=1,
        shutdown_grace_seconds=0.2,
    )
    app = create_app(runtime_composition=composition)

    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=_cancel_body())
        run_id = created.json()["run_id"]

        cancel1 = client.post(f"/api/live-runs/{run_id}/cancel")
        assert cancel1.status_code == 200
        assert cancel1.json()["status"] == "CANCELLED"

        cancel2 = client.post(f"/api/live-runs/{run_id}/cancel")
        assert cancel2.status_code == 200
        assert cancel2.json()["acknowledged"] is True

        summary = client.get(f"/api/live-runs/{run_id}").json()
        assert summary["lifecycle_status"] == "CANCELLED"
        stream = client.get(f"/api/live-runs/{run_id}/stream").text
        assert "event: run.cancelled" in stream
        assert "event: run.completed" not in stream
        gate.set()


def test_cancel_api_late_completion_is_discarded(tmp_path: Path) -> None:
    """Finding D: a non-cancellable provider result arriving after CANCELLED
    is discarded; no COMPLETED/artifacts may follow."""
    from fastapi.testclient import TestClient

    from catalyst_app.main import create_app
    from catalyst_app.runtime.composition import build_runtime_composition
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore

    release = threading.Event()

    class LateResolver(_CancelResolver):
        def __init__(self, tokens) -> None:
            super().__init__(tokens)
            self.late_writer = None

        def resolve(self, manifest, boundary):
            from catalyst_agents.retrieval.corrective import (
                BackendCapability,
                BackendHealth,
                CorrectiveCapabilityRegistry,
            )
            from catalyst_agents.retrieval.task import EvidenceNeed
            from catalyst_app.runtime.composition import ResolvedGraphRuntime
            from test_default_runtime_composition import FakeObservationProvider, FakeRetriever

            capabilities = {
                EvidenceNeed(name): BackendCapability(
                    backend=f"backend:{name.lower()}", health=BackendHealth.HEALTHY
                )
                for name in (
                    "COMPANY_PRIMARY", "COMPANY_NEWS", "SECTOR_NEWS",
                    "MACRO_EVENT", "MACRO_SERIES", "FUNDAMENTALS",
                )
            }

            class LateWriter:
                calls = 0
                capability_metadata = {
                    "supports_structured_output": True,
                    "supports_true_streaming": True,
                    "declares_token_accounting": True,
                    "normalizes_timeout_errors": True,
                    "capability_revision": "v1.1-capability-1",
                }

                def stream(self, messages):
                    LateWriter.calls += 1
                    release.wait(timeout=10)
                    return iter(("SUMMARY\nLate.\nLIMITATIONS\nNone.",))

            self.late_writer = LateWriter
            from test_default_runtime_composition import FakeAnalystProvider, _runtime

            return ResolvedGraphRuntime(
                observation_provider=FakeObservationProvider(),
                retriever=FakeRetriever(),
                analyst_llm=FakeAnalystProvider(),
                writer_llm=LateWriter(),
                capability_registry=CorrectiveCapabilityRegistry(capabilities),
                data_runtime_identity=_runtime(),
                corrective_policy=None,
                prompt_template="You are the Evidence Analyst. Emit the strict schema.",
                query_builder=None,
                structured_provider=None,
            )

    composition = build_runtime_composition(
        db_path=tmp_path / "runtime.db",
        dependency_loader=_CancelAwareLoader(),
        credential_store=RuntimeCredentialStore(),
        graph_resolver=None,
        max_workers=2,
        shutdown_grace_seconds=0.2,
    )
    late_resolver = LateResolver(tokens=composition.tokens)
    composition.run_adapter.graph_resolver = late_resolver
    app = create_app(runtime_composition=composition)

    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=_cancel_body())
        run_id = created.json()["run_id"]

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            summary = client.get(f"/api/live-runs/{run_id}").json()
            if summary["lifecycle_status"] == "RUNNING":
                break
            time.sleep(0.02)

        cancel = client.post(f"/api/live-runs/{run_id}/cancel")
        assert cancel.json()["status"] in {"CANCEL_REQUESTED", "CANCELLED"}
        # Let the late provider output return; the real adapter must discard it.
        release.set()
        final = _wait_terminal_cancel(client, run_id)
        assert final["lifecycle_status"] == "CANCELLED"
        assert final["attribution_status"] is None
        stream = client.get(f"/api/live-runs/{run_id}/stream").text
        assert "event: run.completed" not in stream
        # No run.completed artifact refs may be present after CANCELLED.
        page = client.get(f"/api/live-runs/{run_id}/artifacts").json()
        types = {item["ref"]["artifact_type"] for item in page["items"]}
        assert "attribution_result" not in types


def _wait_terminal_cancel(client, run_id: str, timeout_seconds: float = 12.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        summary = client.get(f"/api/live-runs/{run_id}").json()
        if summary["lifecycle_status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return summary
        time.sleep(0.02)
    return client.get(f"/api/live-runs/{run_id}").json()
