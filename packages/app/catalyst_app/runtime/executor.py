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
import inspect
import threading
import time
from typing import Any, Callable

def _anon_key() -> str:
    import uuid

    return f"__anon__:{uuid.uuid4().hex}"


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

# The failure handler terminalizes a crashed run. Handlers may declare a third
# parameter to receive the crashed exception (persisted, sanitized, as public
# diagnostic text); the two-argument form stays supported.
FailureHandler = Callable[..., None]


def _failure_handler_accepts_exception(handler: Callable[..., None] | None) -> bool:
    """True when the handler can receive the crashed exception positionally."""
    if handler is None:
        return False
    try:
        parameters = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 3 or any(
        parameter.kind is parameter.VAR_POSITIONAL for parameter in parameters
    )


class RunExecutor:
    def __init__(
        self,
        *,
        admission_slots: int = DEFAULT_ADMISSION_SLOTS,
        max_workers: int = DEFAULT_MAX_WORKERS,
        run_adapter: RunAdapter,
        failure_handler: FailureHandler | None = None,
        terminal_check: Callable[[str], bool] | None = None,
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
        self._failure_handler = failure_handler
        self._failure_handler_takes_exception = _failure_handler_accepts_exception(
            failure_handler
        )
        self._terminal_check = terminal_check
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="catalyst-run"
        )
        self._lock = threading.Lock()
        # Capacity reservations are keyed by run_id (Finding F): each run
        # releases exactly once and duplicate release cannot decrement
        # another run's reservation. Anonymous keys support legacy callers.
        self._reserved: set[str] = set()
        self._active = 0
        self._closed = False

    # -- capacity bookkeeping (RUNTIME-01) ---------------------------------

    def reconcile_slots(
        self,
        durable_capacity_bearing: int | list[str] | tuple[str, ...],
    ) -> None:
        """Reconcile the process-local reservation set from durable rows.

        Called during startup recovery before admission reopens, so a process
        restart cannot create additional capacity. Accepts the durable
        capacity-bearing run ids (preferred) or a backward-compatible count.
        """
        with self._lock:
            if isinstance(durable_capacity_bearing, int):
                reserved = set(self._reserved)
                self._reserved = set(list(reserved)[:durable_capacity_bearing])
            else:
                self._reserved = set(durable_capacity_bearing)

    def try_reserve_slot(self, run_id: str | None = None) -> bool:
        """Non-blockingly reserve one admission slot for a run.

        Keyed by ``run_id`` (Finding F); a duplicate reservation for the same
        run is idempotent. Legacy callers may omit the run id.
        """
        key = run_id or _anon_key()
        with self._lock:
            if self._closed:
                return False
            if key in self._reserved:
                return True
            if len(self._reserved) >= self.admission_slots:
                return False
            self._reserved.add(key)
            return True

    def release_slot(self, run_id: str | None = None) -> bool:
        """Release exactly one run's reservation; duplicate release is a no-op.

        Returns True when a reservation was actually released. A named release
        never decrements another run's reservation; legacy anonymous callers
        fall back to releasing one anonymous key.
        """
        with self._lock:
            if run_id is not None:
                if run_id in self._reserved:
                    self._reserved.discard(run_id)
                    return True
                return False
            anon = [key for key in self._reserved if key.startswith("__anon__")]
            if anon:
                self._reserved.discard(anon[0])
                return True
            return False

    @property
    def reserved_count(self) -> int:
        with self._lock:
            return len(self._reserved)

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
        except BaseException as exc:
            # A crashed adapter must not leave the run stranded: terminalize
            # FAILED so startup recovery is not required for ordinary faults.
            # The crashed exception is offered to the handler so the durable
            # failure record can carry its sanitized type and first line; the
            # original exception is still re-raised into the (unconsumed)
            # future for in-process callers.
            if self._failure_handler is not None:
                try:
                    if self._failure_handler_takes_exception:
                        self._failure_handler(run_id, "SYSTEM_ERROR", exc)
                    else:
                        self._failure_handler(run_id, "SYSTEM_ERROR")
                except Exception:
                    pass  # recovery/terminal race; durable state is authoritative
            raise
        finally:
            with self._lock:
                self._active -= 1
            # A slot releases only after the durable terminal commit (or a
            # pre-visibility rollback). When a terminal_check is wired, an
            # adapter that returns without terminalizing cannot release
            # capacity as if it succeeded (Finding F).
            if self._terminal_check is None or self._terminal_check(run_id):
                if not self.release_slot(run_id):
                    # Legacy anonymous reservations (callers that reserved
                    # without a run id) release one anonymous key.
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
    "FailureHandler",
    "MAX_ADMISSION_SLOTS",
    "MIN_ADMISSION_SLOTS",
    "RunAdapter",
    "RunExecutor",
]
