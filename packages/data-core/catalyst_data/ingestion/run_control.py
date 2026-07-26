"""Durable run control — cancel, lease, resume, stage management."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta


def request_cancel(conn: sqlite3.Connection, run_id: str) -> None:
    """Persist cancel_requested flag for cooperative cancellation."""
    conn.execute(
        "UPDATE ingestion_runs SET cancel_requested = 1 WHERE run_id = ?",
        (run_id,),
    )


def is_cancelled(conn: sqlite3.Connection, run_id: str) -> bool:
    """Check if cancel has been requested for the run."""
    row = conn.execute(
        "SELECT cancel_requested FROM ingestion_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return bool(row[0]) if row else False


def acquire_lease(
    conn: sqlite3.Connection,
    run_id: str,
    holder: str,
    ttl_seconds: int = 300,
) -> bool:
    """Try to acquire a single-writer lease. Returns True on success."""
    now = datetime.now(timezone.utc).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()

    # Only acquire if no active lease exists (lease_expires_at is NULL or in the past)
    cursor = conn.execute(
        """UPDATE ingestion_runs
           SET lease_holder = ?, lease_expires_at = ?
           WHERE run_id = ?
             AND (lease_holder IS NULL OR lease_expires_at < ?)""",
        (holder, expires, run_id, now),
    )
    return cursor.rowcount > 0


def release_lease(conn: sqlite3.Connection, run_id: str, holder: str) -> None:
    """Release the lease if held by holder."""
    conn.execute(
        "UPDATE ingestion_runs SET lease_holder = NULL, lease_expires_at = NULL WHERE run_id = ? AND lease_holder = ?",
        (run_id, holder),
    )


def get_next_stage(conn: sqlite3.Connection, run_id: str) -> str | None:
    """Determine the next stage to execute based on checkpoint state.

    Returns one of: 'RUNNING_OHLCV', 'RUNNING_EVIDENCE', None (complete).
    """
    run = conn.execute(
        "SELECT status FROM ingestion_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if not run:
        return None

    status = run["status"]

    # Already in a running state
    if status in ("RUNNING_OHLCV", "RUNNING_EVIDENCE"):
        return status

    # Check if OHLCV cells have all succeeded
    ohlcv_done = conn.execute(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as succeeded
           FROM source_checkpoints
           WHERE run_id = ? AND source_type = 'ohlcv'""",
        (run_id,),
    ).fetchone()

    if ohlcv_done and ohlcv_done["total"] > 0 and ohlcv_done["succeeded"] == ohlcv_done["total"]:
        return "RUNNING_EVIDENCE"

    # Check if any OHLCV cells exist at all
    ohlcv_any = conn.execute(
        "SELECT COUNT(*) FROM source_checkpoints WHERE run_id = ? AND source_type = 'ohlcv'",
        (run_id,),
    ).fetchone()[0]

    if ohlcv_any > 0:
        # OHLCV started but not all succeeded → resume OHLCV
        return "RUNNING_OHLCV"

    # No OHLCV cells yet → start OHLCV
    if status in ("pending", "partial"):
        return "RUNNING_OHLCV"

    return None  # complete
