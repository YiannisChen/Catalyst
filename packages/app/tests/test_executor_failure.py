"""Executor failure terminalization (M6-9 robustness)."""
from __future__ import annotations

from pathlib import Path
import threading

from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.executor import RunExecutor


def test_crashed_adapter_terminalizes_failed(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:1', 'RUNNING', NULL, 'a'*64, 'm', 'b'*64, 0, 't', 't')"
        )
        conn.commit()

    terminalized: list[tuple[str, str]] = []

    def failure_handler(run_id: str, code: str) -> None:
        terminalized.append((run_id, code))

    def crashed_adapter(run_id: str, timeout_seconds: float) -> dict:
        raise RuntimeError("graph boom")

    executor = RunExecutor(
        admission_slots=2,
        max_workers=1,
        run_adapter=crashed_adapter,
        failure_handler=failure_handler,
        shutdown_grace_seconds=0.1,
    )
    executor.try_reserve_slot()
    future = executor.submit("run:1", 60.0)
    try:
        future.result(timeout=5)
    except RuntimeError:
        pass

    import time

    deadline = time.monotonic() + 5
    while executor.active_count > 0 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert terminalized == [("run:1", "SYSTEM_ERROR")]
    assert executor.reserved_count == 0
    executor.shutdown(grace_seconds=0)


def _running_run_row(db_path: Path, run_id: str) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            f" VALUES ('{run_id}', 'RUNNING', NULL, 'a'*64, 'm', 'b'*64, 0, 't', 't')"
        )
        conn.commit()


def _failure_message(db_path: Path, run_id: str) -> tuple[str | None, str | None]:
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT failure_code, failure_message FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return row["failure_code"], row["failure_message"]


def test_executor_passes_the_crashed_exception_to_a_three_argument_handler(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _running_run_row(db_path, "run:1")
    captured: list[tuple[str, str, str]] = []

    def failure_handler(run_id: str, code: str, exc: BaseException) -> None:
        captured.append((run_id, code, type(exc).__name__))

    def crashed_adapter(run_id: str, timeout_seconds: float) -> dict:
        raise RuntimeError("graph boom")

    executor = RunExecutor(
        admission_slots=2,
        max_workers=1,
        run_adapter=crashed_adapter,
        failure_handler=failure_handler,
        shutdown_grace_seconds=0.1,
    )
    executor.try_reserve_slot("run:1")
    future = executor.submit("run:1", 60.0)
    try:
        future.result(timeout=5)
    except RuntimeError:
        pass

    import time

    deadline = time.monotonic() + 5
    while executor.active_count > 0 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert captured == [("run:1", "SYSTEM_ERROR", "RuntimeError")]
    executor.shutdown(grace_seconds=0)


def test_crashed_run_persists_sanitized_exception_type_and_first_line(
    tmp_path: Path,
) -> None:
    """A crashed run must persist what failed: exception type plus the first
    line of its message, sanitized for public text."""
    from catalyst_app.persistence.events import EventRepository
    from catalyst_app.runtime.claim import RunClaimer
    from catalyst_app.runtime.composition import _default_failure_handler

    db_path = tmp_path / "runtime.db"
    _running_run_row(db_path, "run:1")
    events = EventRepository(db_path=db_path)
    claimer = RunClaimer(db_path=db_path, events=events)
    handler = _default_failure_handler(events, claimer)

    handler(
        "run:1",
        "SYSTEM_ERROR",
        RuntimeError("Error code: 400 - bad request\nsecond line ignored"),
    )

    code, message = _failure_message(db_path, "run:1")
    assert code == "SYSTEM_ERROR"
    assert message == "RuntimeError: Error code: 400 - bad request"


def test_crashed_run_with_unsafe_exception_text_persists_the_typed_code(
    tmp_path: Path,
) -> None:
    """Secret-shaped or raw-provider-shaped text never reaches the durable
    public field; the typed code is the fallback."""
    from catalyst_app.persistence.events import EventRepository
    from catalyst_app.runtime.claim import RunClaimer
    from catalyst_app.runtime.composition import _default_failure_handler

    db_path = tmp_path / "runtime.db"
    _running_run_row(db_path, "run:1")
    events = EventRepository(db_path=db_path)
    claimer = RunClaimer(db_path=db_path, events=events)
    handler = _default_failure_handler(events, claimer)

    handler(
        "run:1",
        "SYSTEM_ERROR",
        ValueError("authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345"),
    )

    code, message = _failure_message(db_path, "run:1")
    assert code == "SYSTEM_ERROR"
    assert message == "ValueError"
    assert "sk-" not in (message or "")
    assert "Bearer" not in (message or "")


def test_crashed_run_without_an_exception_keeps_a_null_message(tmp_path: Path) -> None:
    from catalyst_app.persistence.events import EventRepository
    from catalyst_app.runtime.claim import RunClaimer
    from catalyst_app.runtime.composition import _default_failure_handler

    db_path = tmp_path / "runtime.db"
    _running_run_row(db_path, "run:1")
    events = EventRepository(db_path=db_path)
    claimer = RunClaimer(db_path=db_path, events=events)

    _default_failure_handler(events, claimer)("run:1", "SYSTEM_ERROR")

    code, message = _failure_message(db_path, "run:1")
    assert code == "SYSTEM_ERROR"
    assert message is None


def test_schema_failure_parser_excerpt_persists_a_public_safe_message(
    tmp_path: Path,
) -> None:
    """The typed Analyst schema failure carries a bounded parser excerpt.

    The excerpt is produced by the agents node (``_schema_failure_excerpt``);
    whatever reaches the durable public field must still satisfy
    ``validate_safe_public_text`` — bounded, single line, and free of
    credential/raw-provider patterns.
    """
    from catalyst_agents.nodes.evidence_analyst import _schema_failure_excerpt
    from catalyst_agents.runtime.provider_capability import ModelSchemaFailure
    from catalyst_app.persistence.events import EventRepository
    from catalyst_app.public_text import validate_safe_public_text
    from catalyst_app.runtime.claim import RunClaimer
    from catalyst_app.runtime.composition import _default_failure_handler

    db_path = tmp_path / "runtime.db"
    _running_run_row(db_path, "run:1")
    events = EventRepository(db_path=db_path)
    claimer = RunClaimer(db_path=db_path, events=events)
    handler = _default_failure_handler(events, claimer)

    excerpt = _schema_failure_excerpt(
        "1 validation error for AnalystDecision\n"
        "evidence_decisions.0.disposition\n"
        "  Input should be 'SUPPORT', 'CONTRADICT' or 'WEAK' "
        "[type=enum, input_value='WEAKISH', input_type=str]\n"
        + "x" * 400
    )
    handler(
        "run:1",
        "SYSTEM_ERROR",
        ModelSchemaFailure(
            "MODEL_SCHEMA_FAILURE: role 'evidence_analyst' failed after 2 "
            f"provider attempts: ModelSchemaFailure({excerpt!r})"
        ),
    )

    code, message = _failure_message(db_path, "run:1")
    assert code == "SYSTEM_ERROR"
    assert message is not None
    validate_safe_public_text(message)
    assert "disposition" in message
    assert len(message) <= 300


def test_schema_failure_excerpt_never_leaks_secret_shaped_text(
    tmp_path: Path,
) -> None:
    """A hostile parse message must not carry a credential or raw-provider
    pattern into the durable public field."""
    from catalyst_agents.nodes.evidence_analyst import _schema_failure_excerpt
    from catalyst_agents.runtime.provider_capability import ModelSchemaFailure
    from catalyst_app.persistence.events import EventRepository
    from catalyst_app.public_text import validate_safe_public_text
    from catalyst_app.runtime.claim import RunClaimer
    from catalyst_app.runtime.composition import _default_failure_handler

    db_path = tmp_path / "runtime.db"
    _running_run_row(db_path, "run:1")
    events = EventRepository(db_path=db_path)
    claimer = RunClaimer(db_path=db_path, events=events)
    handler = _default_failure_handler(events, claimer)

    excerpt = _schema_failure_excerpt(
        'Failed to parse AnalystDecision from completion {"api_key": '
        '"sk-abcdefghijklmnopqrstuvwxyz012345"}; authorization: Bearer abcdef'
    )
    handler("run:1", "SYSTEM_ERROR", ModelSchemaFailure(f"MODEL_SCHEMA_FAILURE: {excerpt}"))

    code, message = _failure_message(db_path, "run:1")
    assert code == "SYSTEM_ERROR"
    assert message is not None
    validate_safe_public_text(message)
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in message
    assert "Bearer" not in message
