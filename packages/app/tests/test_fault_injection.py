"""Fault-injection coverage for the terminal race boundary (M6 corrective).

_terminalize_completed catches only the exact conditional lifecycle race
(cancellation/timeout winning the conditional update); arbitrary SQLite,
artifact, validation, or hash errors re-raise so the executor failure handler
terminalizes FAILED. _acknowledge_cancelled must not return CANCELLED when
durable terminalization failed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time
from pathlib import Path

from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import payload_sha256
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.admission import AdmissionRequest
from test_admission import _request
from test_default_runtime_composition import (
    FakeGraphResolver,
    FakeRuntimeDependencyLoader,
)


def _fixture_db(db_path: Path) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()


def _seed_accepted_run(db_path: Path, run_id: str) -> None:
    with open_rw(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        from catalyst_app.persistence.events import canonical_json

        from test_admission import _manifest

        manifest = _manifest(_request(), run_id, "a" * 64)
        manifest_json = canonical_json(manifest.model_dump(mode="json"))
        now = datetime.now(timezone.utc)
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
            (f"manifest:{run_id}", run_id, payload_sha256(manifest.model_dump(mode="json")), manifest_json),
        )
        conn.commit()


def _composition(tmp_path: Path):
    from catalyst_app.runtime.composition import build_runtime_composition
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore

    return build_runtime_composition(
        db_path=tmp_path / "runtime.db",
        dependency_loader=FakeRuntimeDependencyLoader(),
        credential_store=RuntimeCredentialStore(),
        graph_resolver=FakeGraphResolver(),
        max_workers=1,
        shutdown_grace_seconds=0.1,
    )


def _wait_terminal(db_path: Path, run_id: str, timeout: float = 8.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with open_rw(db_path) as conn:
            row = conn.execute(
                "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is not None and row["lifecycle_status"] in {
            "COMPLETED", "FAILED", "CANCELLED",
        }:
            return row["lifecycle_status"]
        time.sleep(0.02)
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    return row["lifecycle_status"] if row else "missing"


def test_terminalize_sqlite_error_reraises_and_fails(tmp_path: Path) -> None:
    """A non-race SQLite error during terminalize must NOT become CANCELLED;
    the executor failure handler terminalizes FAILED."""
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    run_id = "run:fault:terminalize"
    _seed_accepted_run(db_path, run_id)
    composition = _composition(tmp_path)

    class FaultyClaimer:
        def __init__(self, inner):
            self._inner = inner

        def claim_run(self, *args, **kwargs):
            return self._inner.claim_run(*args, **kwargs)

        def current_lifecycle(self, run_id):
            return self._inner.current_lifecycle(run_id)

        def owner_of(self, run_id):
            return self._inner.owner_of(run_id)

        def terminalize(self, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def fail_stranded(self, run_id, *, failure_code):
            return self._inner.fail_stranded(run_id, failure_code=failure_code)

    composition.run_adapter.claimer = FaultyClaimer(composition.claimer)
    composition.executor.submit(run_id, timeout_seconds=60)

    assert _wait_terminal(db_path, run_id) == "FAILED"
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT failure_code FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    assert row["failure_code"] == "SYSTEM_ERROR"


def test_acknowledge_cancelled_durable_failure_propagates_to_failed(tmp_path: Path) -> None:
    """_acknowledge_cancelled must not return CANCELLED when durable
    terminalization failed; the failure propagates to FAILED."""
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    run_id = "run:fault:cancel"
    _seed_accepted_run(db_path, run_id)
    composition = _composition(tmp_path)

    class FaultyCancellation:
        def acknowledge_cancellation(self, run_id):
            raise sqlite3.OperationalError("cancel terminalize failed")

        def request_cancel(self, run_id):
            return composition.cancellation.request_cancel(run_id)

    composition.run_adapter.cancellation = FaultyCancellation()
    # Cancel while ACCEPTED would terminalize directly; instead claim first by
    # running the adapter synchronously with a pre-requested token.
    composition.run_adapter.tokens.request(run_id)
    composition.executor.submit(run_id, timeout_seconds=60)

    assert _wait_terminal(db_path, run_id) == "FAILED"
    with open_rw(db_path) as conn:
        cancelled = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id = ? AND event_type = 'run.cancelled'",
            (run_id,),
        ).fetchone()[0]
    assert cancelled == 0
