"""SSE stream endpoint (M6-6).

GET /api/live-runs/{run_id}/stream returns ``text/event-stream`` with
persisted replay plus a live tail (Final TSD §20.1). No WebSocket.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from catalyst_app.runtime.sse import (
    ConditionRegistry,
    RunNotFoundError,
    StreamSnapshot,
    parse_last_event_id,
    stream_events,
)
from catalyst_data.storage.connect import open_readonly

router = APIRouter(prefix="/api", tags=["stream"])


@dataclass
class StreamConfig:
    db_path: Path
    open_fn: Callable[..., Any] = open_readonly
    wait_timeout_seconds: float = 10.0


def _default_db_path() -> Path:
    import os

    raw = os.getenv("CATALYST_DB_PATH")
    return Path(raw) if raw else Path(".local/live_runtime.db")


def _stream_config(request: Request) -> StreamConfig:
    config = getattr(request.app.state, "stream_config", None)
    if config is None:
        state_db = getattr(request.app.state, "db_path", None)
        config = StreamConfig(
            db_path=Path(state_db) if state_db is not None else _default_db_path()
        )
        request.app.state.stream_config = config
    return config


def _registry(request: Request) -> ConditionRegistry:
    """Resolve the single app-owned ConditionRegistry (Finding C).

    The lifespan creates the registry on the running event loop and wires its
    notifier into the production EventRepository; the stream router reuses the
    same instance so worker-thread commits wake attached streams immediately.
    """
    registry = getattr(request.app.state, "condition_registry", None)
    if registry is None:
        registry = ConditionRegistry(loop=asyncio.get_running_loop())
        request.app.state.condition_registry = registry
    return registry


@router.get("/live-runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request) -> StreamingResponse:
    config = _stream_config(request)
    registry = _registry(request)
    last_event_id = request.headers.get("last-event-id") or request.query_params.get(
        "last_event_id"
    )

    try:
        cursor = parse_last_event_id(run_id, last_event_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_cursor: {exc}") from exc

    snapshot = await asyncio.to_thread(
        StreamSnapshot.read, config.open_fn, config.db_path, run_id, cursor
    )
    if not snapshot.run_exists:
        raise HTTPException(status_code=404, detail="run_not_found")
    if cursor > snapshot.max_seq:
        raise HTTPException(status_code=400, detail="INVALID_EVENT_CURSOR")

    async def event_source():
        try:
            async for frame in stream_events(
                run_id,
                last_event_id,
                config.open_fn,
                registry,
                db_path=config.db_path,
                wait_timeout_seconds=config.wait_timeout_seconds,
            ):
                yield frame
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
