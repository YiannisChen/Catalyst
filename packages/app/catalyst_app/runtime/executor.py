"""App-owned process-wide run executor (M6-3).

Final Migration TSD §15; Grok RUNTIME-01. The app owns exactly one
ThreadPoolExecutor for run execution; the agents runner never creates another
graph executor. ``admission_slots`` is pinned in [2, 4], ``max_workers <=
admission_slots``, and the durable count of ACCEPTED + RUNNING +
CANCEL_REQUESTED never exceeds ``admission_slots``. There is no additive
pending pool: executor-queued ACCEPTED runs consume the same bound.

A slot is reserved non-blockingly at admission for a new run, held for the
entire capacity-bearing lifecycle, and released exactly once after a terminal
commit (or after pre-visibility admission rollback). Startup recovery
reconciles the process-local counter from durable capacity-bearing rows before
admission reopens.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time
from typing import Any, Callable

MIN_ADMISSION_SLOTS = 2
MAX_ADMISSION_SLOTS = 4
DEFAULT_ADMISSION_SLOTS = 4
DEFAULT_MAX_WORKERS = 2
DEFAULT_SHUTDOWN_GRACE_SECONDS = 5.0


class ExecutorClosedError(RuntimeError):
    pass


class ExecutorConfigError(ValueError):
    pass


# One agents run adapter per run. The adapter receives the run id and the
# absolute deadline (seconds); it enforces stage/provider deadlines against
# the remaining budget and terminalizes the run before returning.
RunAdapter = Callable[[str, float], Any]


class RunExecutor:
    def __init__(
        self,
        *,
        admission_slots: int = DEFAULT_ADMISSION_SLOTS,
        max_workers: int = DEFAULT_MAX_WORKERS,
        run_adapter: RunAdapter,
        shutdown_grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        if not MIN_ADMISSION_SLOTS <= admission_slots <= MAX_ADMISSION_SLOTS:
            raise ExecutorConfigError(
                f"admission_slots must be within [{MIN_ADMISSION_SLOTS}, "
                f"{MAX_ADMISSION_SLOTS}] (RUNTIME-01)"
            )
        if not 1 <= max_workers <= admission_slots:
            raise ExecutorConfigError(
                "max_workers must be at least 1 and never exceed admission_slots"
            )
        if shutdown_grace_seconds < 0:
            raise ExecutorConfigError("shutdown_grace_seconds must be non-negative")
        self.admission_slots = admission_slots
        self.max_workers = max_workers
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self._run_adapter = run_adapter
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="catalyst-run"
        )
        self._lock = threading.Lock()
        self._reserved = 0
        self._active = 0
        self._closed = False

    # -- capacity bookkeeping (RUNTIME-01) ---------------------------------

    def reconcile_slots(self, durable_capacity_bearing_count: int) -> None:
        """Reconcile the process-local reservation count from durable rows.

        Called during startup recovery before admission reopens, so a process
        restart cannot create additional capacity.
        """
        with self._lock:
            self._reserved = durable_capacity_bearing_count

    def try_reserve_slot(self) -> bool:
        """Non-blockingly reserve one admission slot for a new run."""
        with self._lock:
            if self._closed:
                return False
            if self._reserved >= self.admission_slots:
                return False
            self._reserved += 1
            return True

    def release_slot(self) -> None:
        """Release one slot; safe to call more than once but idempotent per run."""
        with self._lock:
            if self._reserved > 0:
                self._reserved -= 1

    @property
    def reserved_count(self) -> int:
        with self._lock:
            return self._reserved

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    # -- submission --------------------------------------------------------

    def submit(self, run_id: str, timeout_seconds: float) -> Future:
        """Submit exactly one agents run adapter task for ``run_id``."""
        with self._lock:
            if self._closed:
                raise ExecutorClosedError("executor is shut down")
        return self._pool.submit(self._run_task, run_id, timeout_seconds)

    def _run_task(self, run_id: str, timeout_seconds: float) -> Any:
        with self._lock:
            self._active += 1
        try:
            return self._run_adapter(run_id, timeout_seconds)
        finally:
            with self._lock:
                self._active -= 1
            # A slot releases only after the adapter terminalized the run or
            # raised; the durable count remains authoritative at admission.
            self.release_slot()

    # -- shutdown ----------------------------------------------------------

    def shutdown(self, grace_seconds: float | None = None) -> None:
        """Stop new admission, cancel pending work, wait a bounded grace period.

        Active work receives cooperative cancellation through its adapter;
        unresolved rows are left for the same startup-recovery policy.
        """
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        grace = self.shutdown_grace_seconds if grace_seconds is None else grace_seconds
        deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < deadline:
            with self._lock:
                if self._active == 0:
                    return
            time.sleep(0.05)


__all__ = [
    "DEFAULT_ADMISSION_SLOTS",
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_SHUTDOWN_GRACE_SECONDS",
    "ExecutorClosedError",
    "ExecutorConfigError",
    "MAX_ADMISSION_SLOTS",
    "MIN_ADMISSION_SLOTS",
    "RunAdapter",
    "RunExecutor",
]
