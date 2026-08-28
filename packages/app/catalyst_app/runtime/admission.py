"""Durable admission, idempotency, and capacity reservation (M6-3).

Final Migration TSD §11; Grok RUNTIME-01. ``AdmissionController.admit``
implements the locked order:

    validate + compute request hash
    begin immediate transaction
    resolve existing idempotency-key mapping / active request before capacity work
    non-blockingly reserve one slot only for a new run
    insert ACCEPTED run + immutable RunManifest ref + idempotency mapping +
        sequence-1 run.accepted event
    commit
    submit exactly one executor task
    return ACCEPTED only after submission succeeds

SQLite is authoritative for idempotency (unique non-NULL idempotency_key and
partial unique active request_hash indexes); process memory is never the
authority. Startup recovery re-submits valid ACCEPTED rows under reserved
capacity or fails them with a typed process-recovery code, and reconciles the
executor slot counter from durable capacity-bearing rows before admission
reopens.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import threading
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel

from catalyst_app.api_dto import compute_request_hash
from catalyst_app.events import (
    AssuranceCompletedPayload,
    ArtifactRef,
    RunAcceptedPayload,
    RunCancelledPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus, is_capacity_bearing
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import (
    ArtifactPayload,
    EventRepository,
    payload_sha256,
)
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_agents.runtime.manifest import RunManifest

_ACTIVE_SQL = "('ACCEPTED','RUNNING','CANCEL_REQUESTED')"
_CAPACITY_BEARING_STATUSES = (
    RunLifecycleStatus.ACCEPTED,
    RunLifecycleStatus.RUNNING,
    RunLifecycleStatus.CANCEL_REQUESTED,
)
PROCESS_RECOVERY_CODE = "PROCESS_RECOVERY"
SUBMISSION_FAILURE_CODE = "SUBMISSION_FAILURE"
MAX_QUERY_CHARS = 500


@dataclass(frozen=True)
class AdmissionRequest:
    ticker: str
    session_date: str
    query: str | None
    provider: str
    model_id: str
    base_url: str | None
    credential_source_identifier: str
    workflow_version: str = "v1.1"
    config_version: str = "v1.1"
    idempotency_key: str | None = None


OutcomeKind = Literal[
    "accepted", "duplicate", "conflict", "capacity_exceeded", "unavailable", "invalid"
]


@dataclass(frozen=True)
class AdmissionOutcome:
    kind: OutcomeKind
    run_id: str | None = None
    request_hash: str | None = None
    stream_url: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class RecoverySummary:
    resubmitted: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


ManifestFactory = Callable[[AdmissionRequest, str, str], RunManifest]


class AdmissionValidationError(ValueError):
    pass


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _remaining_seconds(deadline_iso: str) -> float:
    """Remaining wall seconds to an ISO-8601 UTC deadline (floor at zero)."""
    if not deadline_iso:
        return 0.0
    deadline = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())


class AdmissionController:
    def __init__(
        self,
        *,
        db_path: str | Path,
        executor: Any,
        events: EventRepository,
        manifest_factory: ManifestFactory,
        admission_lock: threading.Lock | None = None,
        cancellation: Any | None = None,
        credential_store: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.executor = executor
        self.events = events
        self._manifest_factory = manifest_factory
        self._admission_lock = admission_lock or threading.Lock()
        self._cancellation = cancellation
        self._credential_store = credential_store
        self._closed = False

    # -- admission ---------------------------------------------------------

    def admit(
        self,
        request: AdmissionRequest,
        *,
        pre_submit: Callable[[str], None] | None = None,
    ) -> AdmissionOutcome:
        """Admit one run; ``pre_submit`` (if any) runs after the ACCEPTED
        transaction commits and before the executor task is submitted so a
        volatile credential is visible to the worker before it may start.
        The hook never receives or persists the credential itself."""
        normalized = self._validate_request(request)
        request_hash = compute_request_hash(
            ticker=normalized["ticker"],
            session_date=normalized["session_date"],
            normalized_question=normalized["query"],
            provider=normalized["provider"],
            model_id=normalized["model_id"],
            normalized_base_url=normalized["base_url"] or "",
            credential_source_identifier=normalized["credential_source_identifier"],
            workflow_version=normalized["workflow_version"],
            config_version=normalized["config_version"],
        )

        self._ensure_schema()
        with self._admission_lock:
            if self._closed:
                return AdmissionOutcome(
                    kind="unavailable", failure_code="EXECUTOR_SHUTDOWN"
                )
            with open_rw(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    existing = self._resolve_existing(
                        conn,
                        request_hash=request_hash,
                        idempotency_key=request.idempotency_key,
                    )
                    if existing is not None:
                        return AdmissionOutcome(
                            kind=existing[0],
                            run_id=existing[1],
                            request_hash=request_hash,
                        )

                    if self._count_capacity_bearing(conn) >= self.executor.admission_slots:
                        return AdmissionOutcome(
                            kind="capacity_exceeded", failure_code="CAPACITY_EXCEEDED"
                        )

                    run_id = uuid4().hex
                    if not self.executor.try_reserve_slot(run_id):
                        return AdmissionOutcome(
                            kind="capacity_exceeded", failure_code="CAPACITY_EXCEEDED"
                        )

                    manifest = self._manifest_factory(request, run_id, request_hash)
                    manifest_json = manifest.model_dump(mode="json")
                    manifest_hash = payload_sha256(manifest_json)
                    now = _utc_now()
                    # One absolute deadline derived once at admission
                    # (deadline_at = created_at + run_timeout_seconds); queue
                    # delay and recovery reuse it and never reset the budget.
                    deadline_at = (
                        datetime.fromisoformat(now)
                        + timedelta(seconds=manifest.run_timeout_seconds)
                    ).isoformat()
                    conn.execute(
                        """
                        INSERT INTO runs
                            (run_id, lifecycle_status, idempotency_key, request_hash,
                             run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at,
                             ticker, provider, base_url, deadline_at)
                        VALUES (?, 'ACCEPTED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            request.idempotency_key,
                            request_hash,
                            f"manifest:{run_id}",
                            manifest_hash,
                            self.executor.reserved_count - 1,
                            now,
                            now,
                            normalized["ticker"],
                            normalized["provider"],
                            normalized["base_url"],
                            deadline_at,
                        ),
                    )
                    stream_url = f"/api/live-runs/{run_id}/stream"
                    self.events.append(
                        run_id=run_id,
                        event_type=RunEventType.RUN_ACCEPTED,
                        stage="ADMISSION",
                        payload=RunAcceptedPayload(
                            request_identity_digest=request_hash,
                            ticker=normalized["ticker"],
                            trade_date=normalized["session_date"],
                            workflow_version=normalized["workflow_version"],
                            model_provider_label=f"{normalized['provider']}/{normalized['model_id']}",
                            stream_url=stream_url,
                        ),
                        artifact_payloads=[
                            ArtifactPayload(
                                artifact_id=f"manifest:{run_id}",
                                artifact_type="run_manifest",
                                payload=manifest_json,
                            )
                        ],
                        conn=conn,
                    )
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    self.executor.release_slot(run_id)
                    raise

            # Pre-submit hook: register the volatile credential (resolved by
            # the caller, never carried on AdmissionRequest/manifest/rows) after
            # run_id allocation and before the executor task may start. A hook
            # failure gets the same durable guarantees as submission failure:
            # the committed run terminalizes FAILED, any volatile credential is
            # removed, this run's slot is released, and the error re-raises.
            if pre_submit is not None:
                try:
                    pre_submit(run_id)
                except BaseException:
                    self._terminalize_submission_failure(run_id)
                    if self._credential_store is not None:
                        self._credential_store.remove(run_id)
                    self.executor.release_slot(run_id)
                    raise

            # Submission exactly once; slot releases after terminal commit.
            # The submitted budget is the remaining time to the persisted
            # deadline so queue delay reduces it and never resets it.
            try:
                self.executor.submit(
                    run_id, _remaining_seconds(deadline_at)
                )
            except BaseException:
                self._terminalize_submission_failure(run_id)
                if self._credential_store is not None:
                    self._credential_store.remove(run_id)
                self.executor.release_slot(run_id)
                raise

        return AdmissionOutcome(
            kind="accepted",
            run_id=run_id,
            request_hash=request_hash,
            stream_url=stream_url,
        )

    # -- startup recovery / shutdown ---------------------------------------

    def startup_recovery(self) -> RecoverySummary:
        """Re-submit valid ACCEPTED rows under reserved capacity; fail stranded
        RUNNING/CANCEL_REQUESTED rows with a typed process-recovery code.
        """
        self._ensure_schema()
        with self._admission_lock:
            resubmitted: list[str] = []
            failed: list[str] = []
            with open_rw(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT run_id, lifecycle_status FROM runs ORDER BY created_at ASC"
                ).fetchall()
                accepted: list[str] = []
                for row in rows:
                    if row["lifecycle_status"] == RunLifecycleStatus.ACCEPTED.value:
                        accepted.append(row["run_id"])
                    elif row["lifecycle_status"] in (
                        RunLifecycleStatus.RUNNING.value,
                        RunLifecycleStatus.CANCEL_REQUESTED.value,
                    ):
                        self._fail_recovery(conn, row["run_id"])
                        failed.append(row["run_id"])
                conn.commit()

            durable = self._count_capacity_bearing()
            durable_ids = self._capacity_bearing_run_ids()
            self.executor.reconcile_slots(durable_ids)

            # Re-submit accepted rows only under reserved capacity; beyond it,
            # fail with the same typed process-recovery code.
            capacity = self.executor.admission_slots
            for run_id in accepted[:capacity]:
                manifest = self._load_manifest(run_id)
                if manifest is None:
                    self._fail_recovery_own_tx(run_id)
                    failed.append(run_id)
                    continue
                try:
                    # Recovery reuses the original persisted deadline; the
                    # submitted budget is the remaining time to it.
                    self.executor.submit(
                        run_id, self._remaining_seconds_for_run(run_id, manifest)
                    )
                except BaseException:
                    self._terminalize_submission_failure(run_id)
                    if self._credential_store is not None:
                        self._credential_store.remove(run_id)
                    failed.append(run_id)
                else:
                    resubmitted.append(run_id)
            for run_id in accepted[capacity:]:
                self._fail_recovery_own_tx(run_id)
                failed.append(run_id)

            # Reconcile again: terminalizations above changed the durable count.
            self.executor.reconcile_slots(self._capacity_bearing_run_ids())
            return RecoverySummary(
                resubmitted=tuple(resubmitted), failed=tuple(failed)
            )

    def shutdown(self) -> None:
        """Stop new admission, request cooperative cancellation for active
        runs, cancel queued work, and wait one bounded grace period.

        Queued ACCEPTED rows stay in a durable recoverable state that startup
        recovery terminalizes/reconciles (Final TSD §11/§15; Finding E).
        """
        with self._admission_lock:
            self._closed = True
        if self._cancellation is not None:
            # Only active (claimed) runs receive cooperative cancellation;
            # queued ACCEPTED rows stay in a durable recoverable state that
            # startup recovery terminalizes/reconciles (Finding E).
            for run_id in self._active_run_ids():
                try:
                    self._cancellation.request_cancel(run_id)
                except Exception:
                    pass  # terminal race; durable state is authoritative
        self.executor.shutdown()
        # Reconcile process-local reservations with the durable capacity rows
        # that remain explicitly recoverable after shutdown.
        self.executor.reconcile_slots(self._capacity_bearing_run_ids())

    # -- internals ---------------------------------------------------------

    def _ensure_schema(self) -> None:
        with open_rw(self.db_path) as conn:
            init_runtime_db(conn)

    def _validate_request(self, request: AdmissionRequest) -> dict[str, str | None]:
        ticker = request.ticker.strip().upper()
        if not ticker:
            raise AdmissionValidationError("ticker must be non-empty")
        try:
            date.fromisoformat(request.session_date)
        except ValueError as exc:
            raise AdmissionValidationError("session_date must be ISO-8601") from exc
        query = request.query
        if query is not None:
            query = query.strip()
            if not query:
                raise AdmissionValidationError("query must not be blank")
            if len(query) > MAX_QUERY_CHARS:
                raise AdmissionValidationError("query exceeds maximum length")
        if not request.provider.strip() or not request.model_id.strip():
            raise AdmissionValidationError("provider and model_id are required")
        return {
            "ticker": ticker,
            "session_date": request.session_date,
            "query": query,
            "provider": request.provider.strip(),
            "model_id": request.model_id.strip(),
            "base_url": request.base_url,
            "credential_source_identifier": request.credential_source_identifier,
            "workflow_version": request.workflow_version,
            "config_version": request.config_version,
        }

    def _resolve_existing(
        self, conn: Any, *, request_hash: str, idempotency_key: str | None
    ) -> tuple[Literal["duplicate", "conflict"], str] | None:
        if idempotency_key:
            row = conn.execute(
                "SELECT run_id, request_hash FROM runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["request_hash"] == request_hash:
                    return ("duplicate", row["run_id"])
                return ("conflict", row["run_id"])
            return None
        row = conn.execute(
            "SELECT run_id FROM runs WHERE request_hash = ? "
            f"AND lifecycle_status IN {_ACTIVE_SQL} ORDER BY created_at ASC LIMIT 1",
            (request_hash,),
        ).fetchone()
        if row is not None:
            return ("duplicate", row["run_id"])
        return None

    def _capacity_bearing_run_ids(self) -> list[str]:
        with open_rw(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT run_id FROM runs WHERE lifecycle_status IN {_ACTIVE_SQL}"
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def _active_run_ids(self) -> list[str]:
        with open_rw(self.db_path) as conn:
            rows = conn.execute(
                "SELECT run_id FROM runs WHERE lifecycle_status IN "
                "('RUNNING','CANCEL_REQUESTED')"
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def _count_capacity_bearing(self, conn: Any | None = None) -> int:
        if conn is not None:
            row = conn.execute(
                f"SELECT COUNT(*) FROM runs WHERE lifecycle_status IN {_ACTIVE_SQL}"
            ).fetchone()
            return int(row[0])
        with open_rw(self.db_path) as own:
            row = own.execute(
                f"SELECT COUNT(*) FROM runs WHERE lifecycle_status IN {_ACTIVE_SQL}"
            ).fetchone()
            return int(row[0])

    def _terminalize_submission_failure(self, run_id: str) -> None:
        with open_rw(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.events.append_terminal(
                run_id=run_id,
                assurance_payload=AssuranceCompletedPayload(
                    valid=False, violations=("submission_failure",)
                ),
                terminal_event_type=RunEventType.RUN_FAILED,
                terminal_payload=RunFailedPayload(
                    failure_code=SUBMISSION_FAILURE_CODE,
                    stage="ADMISSION",
                    retryable=True,
                ),
                lifecycle_update=(
                    RunLifecycleStatus.ACCEPTED,
                    RunLifecycleStatus.FAILED,
                ),
                conn=conn,
            )
            conn.commit()

    def _fail_recovery(self, conn: Any, run_id: str) -> None:
        self.events.append_terminal(
            run_id=run_id,
            assurance_payload=AssuranceCompletedPayload(
                valid=False, violations=("process_recovery",)
            ),
            terminal_event_type=RunEventType.RUN_FAILED,
            terminal_payload=RunFailedPayload(
                failure_code=PROCESS_RECOVERY_CODE,
                stage="STARTUP_RECOVERY",
                retryable=False,
            ),
            lifecycle_update=(
                RunLifecycleStatus.RUNNING
                if self._lifecycle(conn, run_id) == RunLifecycleStatus.RUNNING.value
                else RunLifecycleStatus.CANCEL_REQUESTED,
                RunLifecycleStatus.FAILED,
            ),
            conn=conn,
        )

    def _fail_recovery_own_tx(self, run_id: str) -> None:
        with open_rw(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.events.append_terminal(
                run_id=run_id,
                assurance_payload=AssuranceCompletedPayload(
                    valid=False, violations=("process_recovery",)
                ),
                terminal_event_type=RunEventType.RUN_FAILED,
                terminal_payload=RunFailedPayload(
                    failure_code=PROCESS_RECOVERY_CODE,
                    stage="STARTUP_RECOVERY",
                    retryable=False,
                ),
                lifecycle_update=(
                    RunLifecycleStatus.ACCEPTED,
                    RunLifecycleStatus.FAILED,
                ),
                conn=conn,
            )
            conn.commit()

    def _lifecycle(self, conn: Any, run_id: str) -> str | None:
        row = conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row["lifecycle_status"] if row is not None else None

    def _remaining_seconds_for_run(self, run_id: str, manifest: RunManifest) -> float:
        """Remaining seconds to the persisted absolute deadline.

        Prefers the admission-derived ``deadline_at``; legacy rows without it
        fall back to the deterministic ``created_at + run_timeout_seconds``
        formula so recovery never resets the 60-second budget.
        """
        with open_rw(self.db_path) as conn:
            row = conn.execute(
                "SELECT deadline_at, created_at FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return float(manifest.run_timeout_seconds)
        if row["deadline_at"] is not None:
            deadline_iso = row["deadline_at"]
        elif row["created_at"] is not None:
            try:
                deadline_iso = (
                    datetime.fromisoformat(row["created_at"])
                    + timedelta(seconds=manifest.run_timeout_seconds)
                ).isoformat()
            except ValueError:
                return float(manifest.run_timeout_seconds)
        else:
            return float(manifest.run_timeout_seconds)
        try:
            return _remaining_seconds(deadline_iso)
        except ValueError:
            # Legacy placeholder timestamps in tests/old rows: keep the full
            # timeout so recovery still submits exactly once.
            return float(manifest.run_timeout_seconds)

    def _load_manifest(self, run_id: str) -> RunManifest | None:
        with open_rw(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload_json FROM run_artifacts "
                "WHERE run_id = ? AND artifact_type = 'run_manifest' ORDER BY event_seq ASC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return RunManifest.model_validate_json(row["payload_json"])
        except Exception:
            return None


__all__ = [
    "AdmissionController",
    "AdmissionOutcome",
    "AdmissionRequest",
    "AdmissionValidationError",
    "ManifestFactory",
    "PROCESS_RECOVERY_CODE",
    "RecoverySummary",
    "SUBMISSION_FAILURE_CODE",
]
