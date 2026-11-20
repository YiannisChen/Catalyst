"""Cross-process exclusive locks for corpus generation changes."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


def corpus_lock_path(db_path: str | Path) -> Path:
    path = Path(db_path).expanduser().resolve()
    return path.with_name(path.name + ".fts.lock")


def acquire_corpus_lock(db_path: str | Path) -> int | None:
    """Acquire a nonblocking process lock; return its fd, or None if another process owns it."""
    path = corpus_lock_path(db_path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def release_corpus_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
