"""In-memory credential store for BYOK API keys.

Keys are held by run_id in a plain dict with a threading.Lock.
Never persisted to disk, SQLite, artifacts, logs, or API responses.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeCredential:
    run_id: str
    api_key: str


class RuntimeCredentialStore:
    """Thread-safe in-memory store for runtime API keys."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, RuntimeCredential] = {}

    def register(self, run_id: str, *, api_key: str) -> None:
        """Store a credential for a run. Overwrites if re-registered."""
        with self._lock:
            self._store[run_id] = RuntimeCredential(run_id=run_id, api_key=api_key)

    def get(self, run_id: str) -> RuntimeCredential | None:
        """Retrieve a credential by run_id. Returns None if not found."""
        with self._lock:
            return self._store.get(run_id)

    def remove(self, run_id: str) -> None:
        """Remove a credential (called on terminal/cancel/failure/cleanup)."""
        with self._lock:
            self._store.pop(run_id, None)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._store)
