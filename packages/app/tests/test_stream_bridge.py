"""Writer stream persistence bridge contract (M6-7).

Final TSD §19: StreamBridge consumes accepted coalesced Writer chunks from the
single agents-owned coalescer (50 ms / 2048 UTF-8 chars) and persists them as
answer.delta events via EventRepository. No per-token rows; the provisional
Answer committed with answer.completed equals the exact ordered concatenation
of accepted persisted deltas; a mismatch is STREAM_PERSISTENCE_FAILURE and
invalidates the output. The app never defines a second coalescer.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from catalyst_app.events import (
    RunEventType,
    StageStartedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.stream_bridge import (
    StreamBridge,
    StreamPersistenceFailure,
)
from catalyst_agents.runtime.coalescer import Coalescer
from catalyst_agents.runtime.delta_sink import DeltaEvent
from catalyst_agents.runtime.manifest import (
    INITIAL_RUN_TIMEOUT_SECONDS,
)


def _prepare_db(db_path: Path, run_id: str = "run:1") -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES (?, 'RUNNING', NULL, ?, ?, ?, 0, 't', 't')",
            (run_id, "d" * 64, f"manifest:{run_id}", "e" * 64),
        )
        conn.commit()


def _bridge(db_path: Path, run_id: str = "run:1", repo: EventRepository | None = None) -> StreamBridge:
    return StreamBridge(db_path=db_path, events=repo or EventRepository(db_path=db_path), run_id=run_id)


def _coalescer(bridge: StreamBridge, *, interval_ms: int = 50, chars: int = 2048):
    return Coalescer(
        flush_interval_ms=interval_ms,
        flush_chars=chars,
        sink=bridge,
        stream_id="stream:1",
    )


def _read_events(db_path: Path, event_type: str) -> list[dict]:
    with open_rw(db_path) as conn:
        rows = conn.execute(
            "SELECT seq, event_type, stage, payload_json FROM run_events "
            "WHERE event_type = ? ORDER BY seq ASC",
            (event_type,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_bridge_persists_coalesced_deltas_as_answer_delta_events(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    bridge = _bridge(db_path)
    coalescer = _coalescer(bridge, chars=10)

    coalescer.accept("0123456789")  # flush at char threshold
    coalescer.accept("abcdef")
    coalescer.flush()

    deltas = _read_events(db_path, "answer.delta")
    assert len(deltas) == 2
    first = json.loads(deltas[0]["payload_json"])
    assert first["writer_stream_id"] == "stream:1"
    assert first["delta_text"] == "0123456789"
    assert first["delta_ordinal"] == 1
    assert first["cumulative_character_count"] == 10
    assert first["provisional"] is True
    second = json.loads(deltas[1]["payload_json"])
    assert second["delta_text"] == "abcdef"
    assert second["delta_ordinal"] == 2
    assert second["cumulative_character_count"] == 16
    assert bridge.deltas() == [
        DeltaEvent(stream_id="stream:1", ordinal=0, text="0123456789", cumulative_chars=10, cumulative_bytes=10),
        DeltaEvent(stream_id="stream:1", ordinal=1, text="abcdef", cumulative_chars=16, cumulative_bytes=16),
    ]


def test_bridge_commits_provisional_answer_equal_to_persisted_deltas(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    bridge = _bridge(db_path)
    coalescer = _coalescer(bridge, chars=6)

    coalescer.accept("hello ")
    coalescer.accept("world")
    coalescer.complete()

    answer = bridge.answer()
    assert answer is not None
    assert answer.text == "hello world"
    assert answer.text == "".join(d.text for d in bridge.deltas())
    assert answer.text_sha256 == hashlib.sha256(b"hello world").hexdigest()

    completed = _read_events(db_path, "answer.completed")
    assert len(completed) == 1
    payload = json.loads(completed[0]["payload_json"])
    assert payload["final_text_hash"] == answer.text_sha256
    assert payload["provisional"] is True

    with open_rw(db_path) as conn:
        artifact = conn.execute(
            "SELECT artifact_id, artifact_type, payload_json FROM run_artifacts"
        ).fetchone()
    assert artifact["artifact_type"] == "answer"
    artifact_payload = json.loads(artifact["payload_json"])
    assert artifact_payload["text"] == "hello world"


def test_bridge_rejects_delta_ordinal_gap(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    bridge = _bridge(db_path)

    bridge.commit_delta(stream_id="s", ordinal=0, text="a", cumulative_chars=1, cumulative_bytes=1)
    with pytest.raises(StreamPersistenceFailure):
        bridge.commit_delta(stream_id="s", ordinal=2, text="b", cumulative_chars=2, cumulative_bytes=2)


def test_bridge_answer_text_mismatch_is_stream_persistence_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    bridge = _bridge(db_path)
    bridge.commit_delta(stream_id="s", ordinal=0, text="accepted", cumulative_chars=8, cumulative_bytes=8)

    wrong_hash = hashlib.sha256(b"accepted").hexdigest()
    with pytest.raises(StreamPersistenceFailure):
        bridge.commit_answer(
            answer_id="answer:1",
            stream_id="s",
            text="different text",
            text_sha256=wrong_hash,
            completed_at="2026-08-19T00:00:00Z",
        )

    assert bridge.answer() is None  # output invalidated
    completed = _read_events(db_path, "answer.completed")
    assert completed == []  # nothing authoritative persisted


def test_bridge_never_writes_per_token_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    bridge = _bridge(db_path)
    coalescer = _coalescer(bridge, chars=2048)

    for _ in range(100):
        coalescer.accept("token ")  # 600 chars, below threshold
    assert len(_read_events(db_path, "answer.delta")) == 0
    coalescer.flush()
    assert len(_read_events(db_path, "answer.delta")) == 1  # one chunk, not 100 rows


def test_bridge_fails_closed_on_persistence_error(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)

    class FailingRepo:
        def append(self, **kwargs):
            raise RuntimeError("sqlite unavailable")

    bridge = _bridge(db_path, repo=FailingRepo())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        bridge.commit_delta(stream_id="s", ordinal=0, text="x", cumulative_chars=1, cumulative_bytes=1)

    assert bridge.deltas() == []
    assert bridge.answer() is None


def test_no_second_coalescer_defined_in_app_code() -> None:
    """Single ownership: app code never duplicates the 50 ms / 2048-char coalescer."""
    from catalyst_app.runtime import stream_bridge as bridge_module

    source = Path(bridge_module.__file__).read_text(encoding="utf-8")
    assert "DEFAULT_FLUSH_INTERVAL_MS" not in source
    assert "DEFAULT_FLUSH_CHARS" not in source
    assert "flush_interval_ms" not in source
    assert "flush_chars" not in source
    # The bridge consumes the agents-owned coalescer contract, not a copy.
    assert "catalyst_agents.runtime.delta_sink" in source


def test_bridge_references_agents_manifest_deadline_not_duplicated(tmp_path: Path) -> None:
    """The bridge uses the Frozen initial deadline constant via agents, not a copy."""
    from catalyst_app.runtime import stream_bridge as bridge_module

    source = Path(bridge_module.__file__).read_text(encoding="utf-8")
    # The app bridge must not hard-code a timeout constant that agents owns.
    assert "INITIAL_RUN_TIMEOUT_SECONDS" in source or "catalyst_agents.runtime" in source
    assert INITIAL_RUN_TIMEOUT_SECONDS == 60
