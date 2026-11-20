"""Read-only SQLite connections for immutable snapshots and concurrently updated live stores."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from time import monotonic as _monotonic
from typing import Callable, Iterator

DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_LIVE_READ_TIMEOUT_S = 10.0
DEFAULT_LIVE_READ_RETRIES = 4


class SQLiteReadTimeoutError(sqlite3.OperationalError):
    """A live read could not acquire SQLite access within its configured time bound."""


def live_read_enabled() -> bool:
    return os.environ.get("CATALYST_SQLITE_LIVE_READ", "").strip().lower() in {"1", "true", "yes", "on"}


def _settings(timeout_ms: int) -> tuple[float, int]:
    try:
        total = float(os.environ.get("CATALYST_SQLITE_READ_TIMEOUT_S", DEFAULT_LIVE_READ_TIMEOUT_S))
        retries = int(os.environ.get("CATALYST_SQLITE_READ_RETRIES", DEFAULT_LIVE_READ_RETRIES))
    except ValueError as exc:
        raise ValueError("SQLite live read timeout/retries configuration is invalid") from exc
    if total <= 0 or retries < 0:
        raise ValueError("SQLite live read timeout must be positive and retries non-negative")
    return total, retries


def _is_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return (code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            or "database is locked" in str(exc).lower() or "database is busy" in str(exc).lower())


class _LiveReadConnection(sqlite3.Connection):
    def __init__(self, *args, read_timeout_s: float, read_retries: int, busy_timeout_ms: int, **kwargs):
        super().__init__(*args, **kwargs)
        self._read_timeout_s = read_timeout_s
        self._read_retries = read_retries
        self._busy_timeout_ms = busy_timeout_ms

    def cursor(self, factory=None):
        return super().cursor(factory or _LiveReadCursor)

    def _retry(self, operation):
        deadline = _monotonic() + self._read_timeout_s
        for attempt in range(self._read_retries + 1):
            remaining = deadline - _monotonic()
            if remaining <= 0:
                break
            sqlite3.Connection.execute(
                self, f"PRAGMA busy_timeout={max(1, min(self._busy_timeout_ms, int(remaining * 1000)))}"
            )
            try:
                return operation()
            except sqlite3.Error as exc:
                if not _is_busy(exc):
                    raise
                if attempt >= self._read_retries:
                    break
                delay = min(0.05 * (2 ** attempt), max(0, deadline - _monotonic()))
                if delay:
                    time.sleep(delay)
        raise SQLiteReadTimeoutError(f"SQLite live read remained locked (timeout {self._read_timeout_s:g}s, retries {self._read_retries})")

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        return self.cursor().executescript(sql_script)


class _LiveReadCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        return self.connection._retry(lambda: super(_LiveReadCursor, self).execute(sql, parameters))

    def executemany(self, sql, seq_of_parameters):
        return self.connection._retry(lambda: super(_LiveReadCursor, self).executemany(sql, seq_of_parameters))

    def executescript(self, sql_script):
        return self.connection._retry(lambda: super(_LiveReadCursor, self).executescript(sql_script))


def connect_readonly(
    db_path: str | Path, *, timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS, immutable: bool | None = None,
) -> sqlite3.Connection:
    """Connect read-only. Defaults to immutable snapshots unless the live-read env switch is enabled.

    Pass ``immutable=False`` to always use lock-aware live reads, independent of the environment switch.
    """
    live = live_read_enabled() if immutable is None else not immutable
    immutable_mode = not live if immutable is None else immutable
    path = Path(db_path).resolve()
    # as_uri() percent-encodes the path, so a store path with a space, "?" or "#" still opens.
    uri = f"{path.as_uri()}?mode=ro" + ("&immutable=1" if immutable_mode else "")
    if not live:
        busy_ms = timeout_ms
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=busy_ms / 1000)
    else:
        total, retries = _settings(timeout_ms)
        busy_ms = max(1, min(timeout_ms, 100, int(total * 1000)))
        factory = lambda *args, **kwargs: _LiveReadConnection(
            *args, read_timeout_s=total, read_retries=retries, busy_timeout_ms=busy_ms, **kwargs
        )
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=busy_ms / 1000, factory=factory)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    return conn


_connect_readonly = connect_readonly


@contextmanager
def open_readonly(db_path: str | Path, *, timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
                 immutable: bool | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect_readonly(db_path, timeout_ms=timeout_ms, immutable=immutable)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def make_readonly_factory(db_path: str | Path, *, timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
                          immutable: bool | None = None) -> Callable[[], sqlite3.Connection]:
    path = Path(db_path)
    return lambda: connect_readonly(path, timeout_ms=timeout_ms, immutable=immutable)


__all__ = ["DEFAULT_BUSY_TIMEOUT_MS", "DEFAULT_LIVE_READ_RETRIES", "DEFAULT_LIVE_READ_TIMEOUT_S",
           "SQLiteReadTimeoutError", "connect_readonly", "live_read_enabled", "make_readonly_factory", "open_readonly"]
