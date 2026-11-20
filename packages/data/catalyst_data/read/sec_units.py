"""Read-only SEC exhibit-unit access for consumers."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from catalyst_data.storage import connect as store_connect
from catalyst_data.storage.connect import SQLiteReadTimeoutError
from catalyst_data.storage.errors import StoreReadError


def _store() -> Path | None:
    path = os.environ.get("CATALYST_NEWS_BODIES_DB")
    return Path(path).expanduser() if path else None


def _units(store: str, url: str) -> tuple[dict, ...] | None:
    conn = None
    try:
        from catalyst_data.storage.connect import (
            SQLiteReadTimeoutError,
            connect_readonly,
        )
        conn = connect_readonly(store, timeout_ms=10_000, immutable=False)
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sec_exhibit_units'"
        ).fetchone()
        if exists is None:
            cause = sqlite3.OperationalError("no such table: sec_exhibit_units")
            raise StoreReadError(store, "read sec_exhibit_units", str(cause)) from cause
        row = conn.execute("SELECT units_json FROM sec_exhibit_units WHERE url=?", (url,)).fetchone()
    except SQLiteReadTimeoutError:
        raise
    except sqlite3.Error as exc:
        raise StoreReadError(store, "read sec_exhibit_units", str(exc)) from exc
    finally:
        if conn is not None:
            conn.close()
    return tuple(json.loads(row[0])["units"]) if row else None


def units_for(url: str | None) -> tuple[dict, ...] | None:
    """The stored units of the exhibit at ``url`` (bodies store from CATALYST_NEWS_BODIES_DB), or None."""
    if filing_units_disabled():
        return None
    if not url:
        return None
    store = store_path()
    if store is None:
        return None
    if not store.is_file():
        cause = FileNotFoundError(store)
        raise StoreReadError(store, "open sec_exhibit_units", str(cause)) from cause
    return _units(str(store), url)


def title_for(url: str | None) -> str | None:
    return next((u["text"] for u in units_for(url) or () if u["kind"] == "title"), None)


def store_path() -> Path | None:
    """Configured body-store path shared by runtime reads and health checks."""
    return _store()


def filing_units_disabled() -> bool:
    """Whether an explicit evaluation waiver disables filing-unit reads."""
    return os.environ.get("CATALYST_FILING_UNITS", "").strip().lower() == "off"


def units_count(store_path: str | Path) -> int | None:
    """Return the stored exhibit-unit count; unavailable stores are errors.

    This readiness check is strictly read-only: it never initializes the
    schema, because exhibit units are created only by the SEC ingest path.
    """
    if filing_units_disabled():
        return None
    path = Path(store_path).expanduser().resolve()
    if not path.is_file():
        cause = FileNotFoundError(path)
        raise StoreReadError(path, "read sec_exhibit_units count", str(cause)) from cause
    conn = None
    try:
        conn = store_connect.connect_readonly(path, timeout_ms=10_000, immutable=False)
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sec_exhibit_units'"
        ).fetchone()
        if table is None:
            cause = sqlite3.OperationalError("no such table: sec_exhibit_units")
            raise StoreReadError(path, "read sec_exhibit_units count", str(cause)) from cause
        return int(conn.execute("SELECT COUNT(*) FROM sec_exhibit_units").fetchone()[0])
    except StoreReadError:
        raise
    except SQLiteReadTimeoutError:
        raise
    except sqlite3.Error as exc:
        raise StoreReadError(path, "read sec_exhibit_units count", str(exc)) from exc
    finally:
        if conn is not None:
            conn.close()
