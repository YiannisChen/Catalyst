"""Public errors for failed reads from local data stores."""
from __future__ import annotations

from pathlib import Path


class StoreReadError(RuntimeError):
    """A local store could not be opened or queried reliably."""

    def __init__(self, store_path: str | Path, operation: str, reason: str):
        self.store_path = str(store_path)
        self.operation = operation
        self.reason = reason
        super().__init__(f"{operation} failed for {self.store_path}: {reason}")
