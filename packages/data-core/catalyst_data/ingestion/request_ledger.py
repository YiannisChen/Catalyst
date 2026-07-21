"""Request-attempt ledger — append-only provider_request_attempts writer."""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any


def compute_logical_fetch_id(run_id: str, cell_id: str) -> str:
    """SHA-256(run_id + ':' + cell_id) per contract §4.1."""
    payload = f"{run_id}:{cell_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def insert_attempt(conn: sqlite3.Connection, attempt: dict[str, Any]) -> None:
    """Insert one row into provider_request_attempts.

    All required columns must be present in the dict.  Raises on duplicate request_id.
    """
    columns = [
        "request_id", "run_id", "logical_fetch_id", "source_type",
        "provider", "endpoint_name", "ticker_or_series", "window_start",
        "window_end", "attempt_no", "page_no", "parent_request_id",
        "request_fingerprint", "request_params_redacted", "cursor_fingerprint",
        "started_at", "completed_at", "status", "http_status", "latency_ms",
        "items_count", "retry_after_seconds", "rate_limit_remaining",
        "provider_request_id", "error_class", "error_message_redacted",
        "raw_asset_id", "response_sha256", "response_bytes",
    ]
    placeholders = ", ".join("?" for _ in columns)
    values = [attempt.get(col) for col in columns]
    col_names = ", ".join(columns)

    conn.execute(
        f"INSERT INTO provider_request_attempts ({col_names}) VALUES ({placeholders})",
        values,
    )


def transition_attempt(
    conn: sqlite3.Connection,
    request_id: str,
    status: str,
    **kwargs: Any,
) -> None:
    """Update a request attempt to a terminal status.

    Allowed: SUCCEEDED, HTTP_ERROR, TRANSPORT_ERROR, TIMEOUT,
    RATE_LIMITED, AUTH_ERROR, PARSE_ERROR, CANCELLED.

    Extra kwargs update additional columns (http_status, items_count, etc.).
    """
    allowed_set = {
        "http_status", "latency_ms", "items_count", "retry_after_seconds",
        "rate_limit_remaining", "provider_request_id", "error_class",
        "error_message_redacted", "raw_asset_id", "response_sha256",
        "response_bytes", "completed_at",
    }
    set_clauses = ["status = ?", "completed_at = COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%SZ','now'))"]
    values: list[Any] = [status]

    for k, v in kwargs.items():
        if k in allowed_set:
            set_clauses.append(f"{k} = ?")
            values.append(v)

    values.append(request_id)
    conn.execute(
        f"UPDATE provider_request_attempts SET {', '.join(set_clauses)} WHERE request_id = ?",
        values,
    )


def count_attempts_for(conn: sqlite3.Connection, run_id: str) -> int:
    """Return number of request attempts for a run."""
    row = conn.execute(
        "SELECT COUNT(*) FROM provider_request_attempts WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return row[0] if row else 0
