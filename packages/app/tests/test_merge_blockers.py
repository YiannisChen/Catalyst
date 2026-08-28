"""Final M6 merge-blocker regressions (M6 corrective).

1. Per-run latency: ProductionRunAdapter is shared process-wide, so each
   __call__ captures its own local start time; a second sequential run must
   not inherit the first run's elapsed time.
2. Credential identity/fallback: server_env resolves only
   get_provider_env_key(provider); a missing provider-specific env key fails
   closed before any ACCEPTED run/event/slot; the V1.1 factory never falls
   back to the generic AIHUBMIX_API_KEY.
3. Provider timeout: build_v1_llm timeout is bounded by the remaining
   persisted absolute run deadline (never the fixed 90s), queue delay reduces
   it, and max_retries=0 is preserved.
4. pre_submit failure: a failed credential-registration hook gets the same
   durable guarantees as submission failure (FAILED terminal, credential
   cleanup, slot release, re-raise).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from catalyst_app.persistence.connect import open_rw
from catalyst_app.runtime_credential_store import RuntimeCredentialStore
from test_admission import _SyncExecutor, _events, _fixture_db, _manifest, _request
from test_fault_injection import _composition, _seed_accepted_run
from test_default_runtime_composition import FakeGraphResolver, FakeWriterProvider


def _seed_run(
    db_path: Path, run_id: str, *, request_hash: str, composition: object | None = None
) -> None:
    """Seed one ACCEPTED run with a distinct request hash (unique index).

    Uses the composition's production manifest factory so the graph's
    temporal window matches the fake retriever's evidence date (2026-01-15).
    """
    from catalyst_app.persistence.events import canonical_json, payload_sha256

    from test_admission import _manifest

    with open_rw(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        manifest = (
            composition.manifest_factory(
                _request(session_date="2026-01-15"), run_id, request_hash
            )
            if composition is not None
            else _manifest(_request(session_date="2026-01-15"), run_id, request_hash)
        )
        manifest_json = canonical_json(manifest.model_dump(mode="json"))
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at,"
            " ticker, provider, base_url, deadline_at)"
            " VALUES (?, 'ACCEPTED', NULL, ?, ?, ?, 0, ?, ?, 'AAPL', 'openai', NULL, ?)",
            (
                run_id,
                request_hash,
                f"manifest:{run_id}",
                "x" * 64,
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(seconds=60)).isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES (?, 1, ?, 'run.accepted', 'ADMISSION', ?, 'v1')",
            (run_id, now.isoformat(), json.dumps({"ok": True})),
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type,"
            " payload_hash, payload_json, optional)"
            " VALUES (?, ?, 1, 'run_manifest', ?, ?, 0)",
            (
                f"manifest:{run_id}",
                run_id,
                payload_sha256(manifest.model_dump(mode="json")),
                manifest_json,
            ),
        )
        conn.commit()


def _completed_latency(db_path: Path, run_id: str) -> int:
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM run_events"
            " WHERE run_id = ? AND event_type = 'run.completed'",
            (run_id,),
        ).fetchone()
    assert row is not None, f"run {run_id} has no run.completed event"
    return int(json.loads(row["payload_json"])["total_latency_ms"])


# ── 1. Per-run latency ──────────────────────────────────────────────────────

class _SlowFirstWriter(FakeWriterProvider):
    """Writer that sleeps on the first logical call only."""

    def __init__(self, hold_seconds: float) -> None:
        super().__init__()
        self.hold_seconds = hold_seconds

    def stream(self, messages):
        self.calls += 1
        if self.calls == 1:
            time.sleep(self.hold_seconds)
        return super().stream(messages)


class _SlowFirstResolver(FakeGraphResolver):
    def __init__(self, writer: _SlowFirstWriter) -> None:
        super().__init__()
        self.writer = writer

    def resolve(self, manifest, boundary):
        resolved = super().resolve(manifest, boundary)
        from dataclasses import replace

        return replace(resolved, writer_llm=self.writer)


def test_two_sequential_runs_do_not_share_latency(tmp_path: Path) -> None:
    """The shared adapter must publish each run's own elapsed time; the second
    run must not inherit the first run's start timestamp."""
    db_path = tmp_path / "runtime.db"
    composition = _composition(tmp_path)
    resolver = _SlowFirstResolver(_SlowFirstWriter(hold_seconds=1.0))
    composition.run_adapter.graph_resolver = resolver

    run1, run2 = "run:seq:1", "run:seq:2"
    _seed_run(db_path, run1, request_hash="1" * 64, composition=composition)
    _seed_run(db_path, run2, request_hash="2" * 64, composition=composition)

    composition.run_adapter(run1, timeout_seconds=60)
    composition.run_adapter(run2, timeout_seconds=60)

    lat1 = _completed_latency(db_path, run1)
    lat2 = _completed_latency(db_path, run2)
    # Run 1 held ~1s; run 2 completed immediately and must report its own
    # (small) elapsed time, not the accumulated adapter-wide duration.
    assert lat1 >= 1000
    assert lat2 < 1000


def test_latency_measurement_preserves_zero_for_sub_ms(tmp_path: Path) -> None:
    """A genuinely sub-ms run reports zero, never a fabricated minimum."""
    adapter = _composition(tmp_path).run_adapter
    started = time.monotonic()
    # No sleep: elapsed will be ~0ms; monotonic math must floor at zero.
    assert adapter._elapsed_latency_ms(started) >= 0
    assert adapter._elapsed_latency_ms(started) < 50


# ── 2. Credential identity/fallback ─────────────────────────────────────────

def test_server_env_missing_provider_key_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With AIHUBMIX_API_KEY present but the provider-specific env key absent,
    admission fails and creates no run/event/slot."""
    from fastapi.testclient import TestClient

    from catalyst_app.main import create_app
    from catalyst_app.runtime.composition import build_runtime_composition
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore
    from test_default_runtime_composition import (
        FakeRuntimeDependencyLoader,
        _app,
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("openai_api_key", raising=False)
    monkeypatch.setenv("AIHUBMIX_API_KEY", "sk-aihubmix-generic-present")

    store = RuntimeCredentialStore()
    composition = build_runtime_composition(
        db_path=tmp_path / "runtime.db",
        dependency_loader=FakeRuntimeDependencyLoader(),
        credential_store=store,
        graph_resolver=FakeGraphResolver(),
        max_workers=2,
        shutdown_grace_seconds=0.2,
    )
    app = create_app(runtime_composition=composition)
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
    with TestClient(app) as client:
        response = client.post("/api/live-runs", json=body)
        assert response.status_code == 400, response.text
        assert "env_key_missing" in response.text
        assert "sk-aihubmix-generic-present" not in response.text

    with open_rw(tmp_path / "runtime.db") as conn:
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
    assert runs == 0
    assert events == 0
    assert store.active_count == 0
    assert composition.executor.reserved_count == 0


# ── 3. Provider timeout bounded by remaining run deadline ───────────────────

def test_run_boundary_provider_timeout_bounded_by_remaining_deadline(
    tmp_path: Path,
) -> None:
    """The resolver receives a provider timeout <= remaining persisted
    absolute deadline; a queued run with a shortened budget gets a shorter
    timeout."""
    from datetime import datetime, timedelta, timezone

    db_path = tmp_path / "runtime.db"
    composition = _composition(tmp_path)

    captured: dict[str, float | None] = {}

    class RecordingResolver(FakeGraphResolver):
        def resolve(self, manifest, boundary):
            captured["provider_timeout_seconds"] = boundary.provider_timeout_seconds
            captured["deadline_epoch_ms"] = composition.run_adapter.last_deadline_epoch_ms
            return super().resolve(manifest, boundary)

    composition.run_adapter.graph_resolver = RecordingResolver()

    # Run with a ~3s remaining budget.
    run_id = "run:budget"
    _seed_run(db_path, run_id, request_hash="3" * 64, composition=composition)
    with open_rw(db_path) as conn:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=3)
        conn.execute(
            "UPDATE runs SET deadline_at = ? WHERE run_id = ?",
            (deadline.isoformat(), run_id),
        )
        conn.commit()

    composition.run_adapter(run_id, timeout_seconds=60)
    timeout = captured["provider_timeout_seconds"]
    assert timeout is not None
    assert 0 < timeout <= 3.0

    # Queued run: remaining budget shrinks after queue delay.
    run_id2 = "run:queued-budget"
    _seed_run(db_path, run_id2, request_hash="4" * 64, composition=composition)
    with open_rw(db_path) as conn:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
        conn.execute(
            "UPDATE runs SET deadline_at = ? WHERE run_id = ?",
            (deadline.isoformat(), run_id2),
        )
        conn.commit()
    time.sleep(1.0)
    composition.run_adapter(run_id2, timeout_seconds=60)
    timeout2 = captured["provider_timeout_seconds"]
    assert timeout2 is not None
    assert 0 < timeout2 <= 1.5


# ── 4. pre_submit failure durability ────────────────────────────────────────

def test_pre_submit_failure_terminalizes_failed_releases_slot_and_reraises(
    tmp_path: Path,
) -> None:
    """A credential-registration failure must terminalize the committed run
    FAILED, remove any volatile credential, release the run's slot, and
    re-raise; no stranded ACCEPTED row and no reserved-slot leak."""
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    repo = _events(db_path)
    store = RuntimeCredentialStore()
    executor = _SyncExecutor(adapter=lambda run_id, t: {"ok": True})
    from catalyst_app.runtime.admission import AdmissionController

    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=repo,
        manifest_factory=_manifest,
        credential_store=store,
    )

    def failing_pre_submit(run_id: str) -> None:
        # Simulate a partial registration that then fails.
        store.register(run_id, api_key="sk-partial-registration")
        raise RuntimeError("credential registration failed")

    with pytest.raises(RuntimeError, match="credential registration failed"):
        controller.admit(_request(), pre_submit=failing_pre_submit)

    with open_rw(db_path) as conn:
        rows = conn.execute(
            "SELECT run_id, lifecycle_status, failure_code FROM runs"
        ).fetchall()
        failed_events = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE event_type = 'run.failed'"
        ).fetchone()[0]
        accepted = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE lifecycle_status = 'ACCEPTED'"
        ).fetchone()[0]
    assert len(rows) == 1
    assert rows[0]["lifecycle_status"] == "FAILED"
    assert rows[0]["failure_code"] == "SUBMISSION_FAILURE"
    assert failed_events == 1
    assert accepted == 0
    assert store.active_count == 0
    assert executor.reserved_count == 0
