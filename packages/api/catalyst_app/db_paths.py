"""Resolve the served data store separately from the writable runtime DB."""
from __future__ import annotations

import os
from pathlib import Path

DB_PATH_ENV = "CATALYST_DB_PATH"
RUNTIME_DB_PATH_ENV = "CATALYST_RUNTIME_DB_PATH"
DEFAULT_DB_PATH = Path(".local/live_runtime.db")


def served_db_path() -> Path:
    """The read-only served store; unchanged CATALYST_DB_PATH semantics."""
    raw = os.getenv(DB_PATH_ENV)
    return Path(raw) if raw else DEFAULT_DB_PATH


def runtime_db_path() -> Path:
    """The writable runtime DB, defaulting to the served store when unset."""
    raw = os.getenv(RUNTIME_DB_PATH_ENV)
    if raw:
        return Path(raw)
    return served_db_path()


def runtime_db_is_split() -> bool:
    return bool(os.getenv(RUNTIME_DB_PATH_ENV))


__all__ = [
    "DB_PATH_ENV",
    "DEFAULT_DB_PATH",
    "RUNTIME_DB_PATH_ENV",
    "runtime_db_is_split",
    "runtime_db_path",
    "served_db_path",
]
