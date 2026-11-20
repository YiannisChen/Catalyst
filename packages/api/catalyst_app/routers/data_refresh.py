"""M9 data coverage and persisted refresh-job API."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from catalyst_app.access_control import require_admin
from catalyst_data.ingestion.ops import refresh, status
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["data"])

_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_jobs (
    job_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    session_date TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    report_json TEXT,
    error TEXT
)
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _ensure_jobs(conn: sqlite3.Connection) -> None:
    conn.execute(_JOB_SCHEMA)


def mark_interrupted_jobs(db_path: str | Path) -> None:
    """Mark persisted jobs left active by a previous process as interrupted."""
    conn = _connect(db_path)
    try:
        _ensure_jobs(conn)
        conn.execute(
            "UPDATE refresh_jobs SET state='interrupted', finished_at=? "
            "WHERE state IN ('queued', 'running')",
            (_utc_now(),),
        )
        conn.commit()
    finally:
        conn.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _paths() -> tuple[Path | None, Path | None, Path | None, Path | None, dict[str, Any] | None]:
    market_value = os.getenv("CATALYST_MARKET_CONTEXT_DB")
    bodies_value = os.getenv("CATALYST_NEWS_BODIES_DB")
    peer_map_value = os.getenv("CATALYST_PEER_MAP")
    if not market_value or not bodies_value or not peer_map_value:
        raise HTTPException(status_code=503, detail="DATA_STORES_NOT_CONFIGURED")

    peer_map_path = Path(peer_map_value)
    try:
        peer_map = json.loads(peer_map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="DATA_STORES_NOT_CONFIGURED") from exc
    snapshot_value = os.getenv(
        "CATALYST_SNAPSHOT_DB",
        "data/snapshots/catalyst_m8a_derivative_7a004acc.db",
    )
    return (
        Path(market_value),
        Path(bodies_value),
        peer_map_path,
        Path(snapshot_value),
        peer_map,
    )


def _validate(ticker: str, session_date: str) -> None:
    if re.fullmatch(r"[A-Z.]{1,10}", ticker) is None:
        raise HTTPException(status_code=422, detail="INVALID_TICKER")
    try:
        parsed = date.fromisoformat(session_date)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="INVALID_DATE") from exc
    if parsed.isoformat() != session_date:
        raise HTTPException(status_code=422, detail="INVALID_DATE")


class RefreshRequest(BaseModel):
    ticker: str
    date: str


@router.get("/data/status")
def get_data_status(ticker: str, date: str, request: Request):
    require_admin(request)
    _validate(ticker, date)
    market_db, bodies_db, _peer_map_path, snapshot_db, peer_map = _paths()
    result = status.session_status(
        ticker,
        date,
        snapshot_db=snapshot_db,
        market_db=market_db,
        bodies_db=bodies_db,
        peer_map=peer_map,
    )
    return result.to_dict()


def _run_refresh_job(
    job_id: str,
    ticker: str,
    session_date: str,
    *,
    snapshot_db: Path,
    market_db: Path,
    bodies_db: Path,
    peer_map: dict[str, Any],
) -> None:
    try:
        conn = _connect(bodies_db)
        try:
            conn.execute(
                "UPDATE refresh_jobs SET state='running' WHERE job_id=? AND state='queued'",
                (job_id,),
            )
            conn.commit()
        finally:
            conn.close()
        report = _run_refresh(
            ticker,
            session_date,
            snapshot_db=snapshot_db,
            market_db=market_db,
            bodies_db=bodies_db,
            peer_map=peer_map,
        ).to_dict()
        state = "done"
        error = None
    except Exception as exc:
        error = f"{type(exc).__name__}: {refresh._safe_error(exc)}"
        report = {"error": error}
        state = "failed"

    conn = _connect(bodies_db)
    try:
        conn.execute(
            "UPDATE refresh_jobs SET state=?, finished_at=?, report_json=?, error=? "
            "WHERE job_id=?",
            (state, _utc_now(), json.dumps(report, sort_keys=True), error, job_id),
        )
        conn.commit()
    finally:
        conn.close()


_run_refresh = refresh.refresh_session
_start = lambda fn: threading.Thread(target=fn, daemon=True).start()


@router.post("/data/refresh", status_code=202)
def post_data_refresh(request: RefreshRequest, http_request: Request):
    require_admin(http_request)
    if os.getenv("CATALYST_DATA_REFRESH") != "1":
        raise HTTPException(status_code=403, detail="DATA_REFRESH_DISABLED")
    _validate(request.ticker, request.date)
    market_db, bodies_db, _peer_map_path, snapshot_db, peer_map = _paths()
    conn = _connect(bodies_db)
    job_id = str(uuid4())
    created_at = _utc_now()
    try:
        _ensure_jobs(conn)
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT 1 FROM refresh_jobs WHERE state IN ('queued', 'running') LIMIT 1"
        ).fetchone()
        if active is not None:
            conn.rollback()
            raise HTTPException(status_code=409, detail="REFRESH_BUSY")
        conn.execute(
            "INSERT INTO refresh_jobs "
            "(job_id, ticker, session_date, state, created_at) "
            "VALUES (?, ?, ?, 'queued', ?)",
            (job_id, request.ticker, request.date, created_at),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        _start(
            lambda: _run_refresh_job(
                job_id,
                request.ticker,
                request.date,
                snapshot_db=snapshot_db,
                market_db=market_db,
                bodies_db=bodies_db,
                peer_map=peer_map,
            )
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {refresh._safe_error(exc)}"
        conn = _connect(bodies_db)
        try:
            conn.execute(
                "UPDATE refresh_jobs SET state='failed', finished_at=?, error=?, "
                "report_json=? WHERE job_id=?",
                (_utc_now(), error, json.dumps({"error": error}), job_id),
            )
            conn.commit()
        finally:
            conn.close()
    return {"job_id": job_id}


@router.get("/data/refresh/{job_id}")
def get_data_refresh(job_id: str, request: Request):
    require_admin(request)
    _market_db, bodies_db, _peer_map_path, _snapshot_db, _peer_map = _paths()
    conn = _connect(bodies_db)
    try:
        _ensure_jobs(conn)
        row = conn.execute(
            "SELECT job_id, ticker, session_date, state, created_at, finished_at, "
            "report_json FROM refresh_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="JOB_NOT_FOUND")
    return {
        "job_id": row["job_id"],
        "ticker": row["ticker"],
        "session_date": row["session_date"],
        "state": row["state"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "report": json.loads(row["report_json"]) if row["report_json"] else None,
    }
