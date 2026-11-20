"""SQLite schema and lease primitives for daily ingestion jobs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from catalyst_data.ingestion.time import timefields

LEASE_SECONDS = 15 * 60

JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    plan_key TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT
);
CREATE TABLE IF NOT EXISTS job_items (
    job_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    stage TEXT NOT NULL,
    item_key TEXT NOT NULL,
    session_date TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT,
    PRIMARY KEY (job_id, stage, item_key)
);
CREATE INDEX IF NOT EXISTS idx_job_items_order ON job_items (job_id, status, seq);
CREATE TABLE IF NOT EXISTS job_assets (
    job_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    PRIMARY KEY (job_id, asset_id)
);
"""


def open_jobs(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(JOB_SCHEMA)
    return conn


def acquire_lease(
    conn: sqlite3.Connection,
    job_id: str,
    owner: str,
    *,
    now: str,
    lease_seconds: int,
    owner_dead: Callable[[str], bool],
) -> bool:
    started_at = timefields.stored(now)
    expires = (started_at + timedelta(seconds=lease_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    # This helper commits its work; finish any caller-side setup before reserving
    # the writer so the lease scan and claim share one immediate transaction.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for job, holder in conn.execute(
            "SELECT job_id, lease_owner FROM jobs WHERE lease_owner IS NOT NULL "
            "AND lease_expires_at > ?",
            (now,),
        ).fetchall():
            if not owner_dead(holder):
                conn.rollback()
                return False
            conn.execute(
                "UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL WHERE job_id=?", (job,)
            )
        cursor = conn.execute(
            "UPDATE jobs SET lease_owner=?, lease_expires_at=?, status='RUNNING', "
            "started_at=COALESCE(started_at, ?) WHERE job_id=?",
            (owner, expires, now, job_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def renew_lease(
    conn: sqlite3.Connection, job_id: str, owner: str, *, lease_seconds: int
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cursor = conn.execute(
        "UPDATE jobs SET lease_expires_at=? WHERE job_id=? AND lease_owner=? "
        "AND lease_expires_at>?",
        (expires, job_id, owner, now),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("daily job lease was lost")
