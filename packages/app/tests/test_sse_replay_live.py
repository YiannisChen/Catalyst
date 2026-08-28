"""Race-safe SSE replay + live tail contract (M6-6, SSE-01).

Final Migration TSD §18: Last-Event-ID cursor rules, run-keyed asyncio
condition registry, re-query before every wait, worker-thread notification
via call_soon_threadsafe, heartbeat, reconnect-after-terminal replay, and
at-least-once delivery. SQLite is authoritative; the condition is a wakeup
optimization. No WebSocket.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time

import pytest

from catalyst_app.events import (
    AssuranceCompletedPayload,
    PublicRunEvent,
    RunAcceptedPayload,
    RunCompletedPayload,
    RunEventType,
    StageStartedPayload,
    sse_frame,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.sse import (
    ConditionRegistry,
    InvalidEventCursorError,
    StreamSnapshot,
    parse_last_event_id,
    stream_events,
)
from catalyst_data.storage.connect import open_readonly

pytestmark = pytest.mark.asyncio


def _accepted_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "request_identity_digest": "a" * 64,
        "ticker": "AAPL",
        "trade_date": "2026-01-06",
        "workflow_version": "v1.1",
        "model_provider_label": "test-provider/model",
        "stream_url": "/api/live-runs/run:1/stream",
    }
    base.update(overrides)
    return base


def _prepare_db(db_path: Path, run_id: str = "run:1", status: str = "ACCEPTED") -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES (?, ?, NULL, ?, ?, ?, 0, 't', 't')",
            (run_id, status, "d" * 64, f"manifest:{run_id}", "e" * 64),
        )
        conn.commit()


def _repo(db_path: Path, notifier=None) -> EventRepository:
    return EventRepository(db_path=db_path, notifier=notifier)


def _seed_accepted_event(db_path: Path, run_id: str = "run:1", repo: EventRepository | None = None) -> None:
    repo = repo or _repo(db_path)
    repo.append(
        run_id=run_id,
        event_type=RunEventType.RUN_ACCEPTED,
        stage="ADMISSION",
        payload=RunAcceptedPayload(**_accepted_payload()),
    )


async def _collect(agen, limit: int | None = None, timeout: float = 8.0) -> list[str]:
    frames: list[str] = []
    async def _run() -> None:
        async for frame in agen:
            frames.append(frame)
            if limit is not None and len(frames) >= limit:
                break
    await asyncio.wait_for(_run(), timeout=timeout)
    return frames


def _parse_data(frame: str) -> dict:
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
    return json.loads(data_line[len("data: "):])


# ── cursor parsing ──────────────────────────────────────────────────────────

async def test_parse_last_event_id_rules() -> None:
    assert parse_last_event_id("run:1", None) == 0
    assert parse_last_event_id("run:1", "") == 0
    assert parse_last_event_id("run:1", "run:1:7") == 7
    with pytest.raises(ValueError):
        parse_last_event_id("run:1", "run:1:-1")
    with pytest.raises(ValueError):
        parse_last_event_id("run:1", "run:1:abc")
    with pytest.raises(ValueError):
        parse_last_event_id("run:1", "run:2:3")  # cross-run
    with pytest.raises(ValueError):
        parse_last_event_id("run:1", "garbage")


# ── replay ─────────────────────────────────────────────────────────────────

async def test_stream_replays_persisted_events_from_cursor_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)
    _seed_accepted_event(db_path, repo=repo)
    repo.append(
        run_id="run:1",
        event_type=RunEventType.STAGE_STARTED,
        payload=StageStartedPayload(stage="observation_build"),
        lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
    )
    repo.append_terminal(
        run_id="run:1",
        assurance_payload=AssuranceCompletedPayload(valid=True, final_result_status="SUFFICIENT"),
        terminal_event_type=RunEventType.RUN_COMPLETED,
        terminal_payload=RunCompletedPayload(
            result_status="SUFFICIENT",
            final_output_artifact_ref="artifact:answer",
            total_latency_ms=10,
            runtime_identity_ref="runtime:1",
        ),
        lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
    )

    registry = ConditionRegistry(loop=asyncio.get_running_loop())
    frames = await _collect(
        stream_events("run:1", None, open_readonly, registry, db_path=db_path),
        limit=4,
    )

    assert len(frames) == 4
    assert frames[0].startswith("id: run:1:1\nevent: run.accepted\n")
    assert frames[1].startswith("id: run:1:2\nevent: stage.started\n")
    assert frames[2].startswith("id: run:1:3\nevent: assurance.completed\n")
    assert frames[3].startswith("id: run:1:4\nevent: run.completed\n")
    data = _parse_data(frames[0])
    assert data["sequence"] == 1
    assert data["run_id"] == "run:1"
    assert "seq" not in data  # API-01: internal names never surface
    assert "occurred_at" not in data


async def test_stream_cursor_behind_max_replays_later_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)
    _seed_accepted_event(db_path, repo=repo)
    repo.append(
        run_id="run:1",
        event_type=RunEventType.STAGE_STARTED,
        payload=StageStartedPayload(stage="observation_build"),
        lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
    )
    repo.append_terminal(
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

    registry = ConditionRegistry(loop=asyncio.get_running_loop())
    frames = await _collect(
        stream_events("run:1", "run:1:2", open_readonly, registry, db_path=db_path),
        limit=2,
    )
    assert len(frames) == 2
    assert "id: run:1:3" in frames[0]
    assert "id: run:1:4" in frames[1]


async def test_cursor_greater_than_persisted_max_is_invalid(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)
    _seed_accepted_event(db_path, repo=repo)

    registry = ConditionRegistry(loop=asyncio.get_running_loop())
    with pytest.raises(InvalidEventCursorError):
        async for _ in stream_events("run:1", "run:1:99", open_readonly, registry, db_path=db_path):
            pass


async def test_reconnect_after_terminal_replays_terminal_event_and_closes(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path, status="RUNNING")
    repo = _repo(db_path)
    _seed_accepted_event(db_path, repo=repo)
    repo.append_terminal(
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

    registry = ConditionRegistry(loop=asyncio.get_running_loop())
    # Reconnect after the terminal event: replay from cursor 2 returns the
    # terminal event and then closes.
    frames = await _collect(
        stream_events("run:1", "run:1:2", open_readonly, registry, db_path=db_path),
        limit=1,
    )
    assert len(frames) == 1
    assert "id: run:1:3" in frames[0]
    assert "event: run.completed" in frames[0]


# ── live tail races (SSE-01) ────────────────────────────────────────────────

async def test_live_tail_delivers_committed_event_without_waiting_timeout(tmp_path: Path) -> None:
    """Writer commits between the reader's empty query and wait; no hang."""
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    registry = ConditionRegistry(loop=asyncio.get_running_loop())
    repo = _repo(db_path, notifier=registry.notify_from_thread)

    async def _consume() -> list[str]:
        frames: list[str] = []
        async for frame in stream_events("run:1", None, open_readonly, registry, db_path=db_path):
            frames.append(frame)
            if len(frames) >= 1:
                break
        return frames

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0.1)  # let the reader reach its wait

    def writer() -> None:
        repo.append(
            run_id="run:1",
            event_type=RunEventType.STAGE_STARTED,
            payload=StageStartedPayload(stage="observation_build"),
            lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
        )

    started = time.monotonic()
    thread = threading.Thread(target=writer)
    thread.start()
    frames = await asyncio.wait_for(task, timeout=5.0)
    elapsed = time.monotonic() - started
    thread.join(timeout=5)

    assert len(frames) == 1
    assert "id: run:1:1" in frames[0]
    assert elapsed < 4.0  # delivered by notification, not the 10s heartbeat


async def test_worker_thread_notification_wakes_all_attached_streams(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    registry = ConditionRegistry(loop=asyncio.get_running_loop())
    repo = _repo(db_path, notifier=registry.notify_from_thread)

    async def _consume(name: str) -> list[str]:
        frames: list[str] = []
        async for frame in stream_events("run:1", None, open_readonly, registry, db_path=db_path):
            frames.append(frame)
            if len(frames) >= 1:
                break
        return frames

    t1 = asyncio.create_task(_consume("a"))
    t2 = asyncio.create_task(_consume("b"))
    await asyncio.sleep(0.1)

    repo.append(
        run_id="run:1",
        event_type=RunEventType.STAGE_STARTED,
        payload=StageStartedPayload(stage="observation_build"),
        lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
    )

    f1, f2 = await asyncio.gather(
        asyncio.wait_for(t1, timeout=5.0), asyncio.wait_for(t2, timeout=5.0)
    )
    assert len(f1) == 1 and len(f2) == 1
    assert "id: run:1:1" in f1[0] and "id: run:1:1" in f2[0]


async def test_notification_while_framing_does_not_strand_later_events(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    registry = ConditionRegistry(loop=asyncio.get_running_loop())
    repo = _repo(db_path, notifier=registry.notify_from_thread)

    repo.append(
        run_id="run:1",
        event_type=RunEventType.STAGE_STARTED,
        payload=StageStartedPayload(stage="first"),
        lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
    )

    async def _consume() -> list[str]:
        frames: list[str] = []
        async for frame in stream_events("run:1", None, open_readonly, registry, db_path=db_path):
            frames.append(frame)
            if len(frames) >= 2:
                break
        return frames

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)
    # The second event commits while the reader may be framing the first.
    repo.append(
        run_id="run:1",
        event_type=RunEventType.EVIDENCE_RETRIEVED,
        payload=__import__("catalyst_app.events", fromlist=["EvidenceRetrievedPayload"]).EvidenceRetrievedPayload(
            task_id="task:1", candidate_count=1
        ),
    )

    frames = await asyncio.wait_for(task, timeout=5.0)
    assert len(frames) == 2
    assert "id: run:1:1" in frames[0]
    assert "id: run:1:2" in frames[1]


async def test_idle_sse_wait_does_not_block_unrelated_async_work(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    registry = ConditionRegistry(loop=asyncio.get_running_loop())

    async def _consume() -> None:
        async for _ in stream_events("run:1", None, open_readonly, registry, db_path=db_path):
            pass

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.1)

    async def unrelated() -> str:
        await asyncio.sleep(0.1)
        return "done"

    result = await asyncio.wait_for(unrelated(), timeout=1.0)
    assert result == "done"
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass


async def test_heartbeat_emitted_on_timeout(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    registry = ConditionRegistry(loop=asyncio.get_running_loop())

    frames: list[str] = []
    async for frame in stream_events(
        "run:1", None, open_readonly, registry, db_path=db_path, wait_timeout_seconds=0.1
    ):
        frames.append(frame)
        if len(frames) >= 1:
            break

    assert frames and frames[0] == ": heartbeat\n\n"


# ── snapshot / public envelope helpers ──────────────────────────────────────

async def test_stream_snapshot_exposes_terminal_state(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path, status="RUNNING")
    repo = _repo(db_path)
    _seed_accepted_event(db_path, repo=repo)
    repo.append_terminal(
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

    snapshot = await asyncio.to_thread(
        StreamSnapshot.read, open_readonly, db_path, "run:1", 0
    )
    assert snapshot.is_terminal is True
    assert snapshot.max_seq == 3
    assert [e.event_type for e in snapshot.events] == [
        RunEventType.RUN_ACCEPTED,
        RunEventType.ASSURANCE_COMPLETED,
        RunEventType.RUN_COMPLETED,
    ]


async def test_stream_endpoint_returns_text_event_stream(tmp_path: Path) -> None:
    """GET /api/live-runs/{run_id}/stream serves SSE frames for a terminal run."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from catalyst_app.routers.stream import StreamConfig, router as stream_router

    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)
    _seed_accepted_event(db_path, repo=repo)
    repo.append(
        run_id="run:1",
        event_type=RunEventType.STAGE_STARTED,
        payload=StageStartedPayload(stage="observation_build"),
        lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
    )
    repo.append_terminal(
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

    app = FastAPI()
    app.state.stream_config = StreamConfig(db_path=db_path)
    app.include_router(stream_router)

    with TestClient(app) as client:
        response = client.get("/api/live-runs/run:1/stream")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = response.text
        assert "id: run:1:1" in body
        assert "event: run.accepted" in body
        assert "id: run:1:4" in body
        assert "event: run.completed" in body


async def test_stream_endpoint_invalid_cursor_returns_400(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from catalyst_app.routers.stream import StreamConfig, router as stream_router

    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    _seed_accepted_event(db_path)

    app = FastAPI()
    app.state.stream_config = StreamConfig(db_path=db_path)
    app.include_router(stream_router)

    with TestClient(app) as client:
        response = client.get("/api/live-runs/run:1/stream?last_event_id=run:1:99")
        assert response.status_code == 400
        assert "INVALID_EVENT_CURSOR" in response.text
