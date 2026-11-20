"""Visitor requests for an administrator to refresh public data."""
from __future__ import annotations

import logging
import os
import smtplib
import sqlite3
import ssl
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Literal
from zoneinfo import ZoneInfo

from catalyst_app.access_control import resolve_access_context
from catalyst_app.db_paths import runtime_db_path
from catalyst_app.routers.public_access import _caller_key
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/api/data", tags=["data jobs"])
logger = logging.getLogger(__name__)


class NotifyAdminRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["data_stale", "update_failed", "other"]


def _smtp_config() -> dict[str, str] | None:
    host, user, password, admin = (os.getenv(k, "").strip() for k in (
        "CATALYST_SMTP_HOST", "CATALYST_SMTP_USER", "CATALYST_SMTP_PASSWORD", "CATALYST_ADMIN_NOTIFY_EMAIL"))
    if not all((host, user, password, admin)):
        return None
    return {"host": host, "port": os.getenv("CATALYST_SMTP_PORT", "465"), "user": user,
            "password": password, "sender": os.getenv("CATALYST_NOTIFY_FROM", user), "admin": admin,
            "starttls": os.getenv("CATALYST_SMTP_STARTTLS") == "1"}


def _send(config: dict[str, str], subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["sender"]
    message["To"] = config["admin"]
    message.set_content(body)
    if config["starttls"]:
        with smtplib.SMTP(config["host"], int(config["port"]), timeout=15) as client:
            client.starttls(context=ssl.create_default_context())
            client.login(config["user"], config["password"])
            client.send_message(message)
    else:
        with smtplib.SMTP_SSL(config["host"], int(config["port"]), timeout=15, context=ssl.create_default_context()) as client:
            client.login(config["user"], config["password"])
            client.send_message(message)


class NotifyAdminStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available: bool
    next_allowed_at: str | None


class NotifyAdminResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queued: Literal[True]
    next_allowed_at: str | None


# Process-local gate: while the global limit is known to be closed, refuse without touching the database.
_GATE_LOCK = threading.Lock()
_GLOBAL_CLOSED_UNTIL: list[datetime | None] = [None]


def _rate_limited(next_at: datetime) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "notify_rate_limited", "next_allowed_at": _iso(next_at)})


def _global_next_allowed(conn: sqlite3.Connection, now: datetime) -> datetime | None:
    return _sent_next_allowed(conn, now)


PER_CALLER_HOURS = 24
GLOBAL_PER_DAY = 3
GLOBAL_GAP_HOURS = 2
_TABLE = ("CREATE TABLE IF NOT EXISTS admin_notifications (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, "
          "caller_key TEXT NOT NULL, reason TEXT NOT NULL, sent INTEGER NOT NULL)")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _caller(request: Request) -> str:
    access = resolve_access_context(request)
    if access.tier == "invite":
        return "invite:" + str(access.token_id)
    if access.tier == "admin":
        return "admin"
    return "ip:" + _caller_key(request)


def _next_allowed(conn: sqlite3.Connection, caller: str, now: datetime) -> datetime | None:
    """When this caller may ask next; None when now. Limits: 1 per caller per 24 h, 3 sent a day, 2 h apart."""
    day_ago = _iso(now - timedelta(hours=24))
    waits: list[datetime] = []
    last_caller = conn.execute(
        "SELECT created_at FROM admin_notifications WHERE caller_key=? AND created_at>? "
        "ORDER BY created_at DESC LIMIT 1", (caller, day_ago)).fetchone()
    if last_caller:
        waits.append(_when(last_caller[0]) + timedelta(hours=PER_CALLER_HOURS))
    waits.extend(_sent_waits(conn, now))
    latest = max(waits) if waits else None
    return latest if latest is not None and latest > now else None


def _sent_waits(conn: sqlite3.Connection, now: datetime) -> list[datetime]:
    day_ago = _iso(now - timedelta(hours=24))
    sent = [_when(row[0]) for row in conn.execute(
        "SELECT created_at FROM admin_notifications WHERE sent=1 AND created_at>? ORDER BY created_at DESC",
        (day_ago,))]
    waits = []
    if sent:
        waits.append(sent[0] + timedelta(hours=GLOBAL_GAP_HOURS))
    if len(sent) >= GLOBAL_PER_DAY:
        waits.append(sent[GLOBAL_PER_DAY - 1] + timedelta(hours=24))
    return waits


def _sent_next_allowed(conn: sqlite3.Connection, now: datetime) -> datetime | None:
    waits = _sent_waits(conn, now)
    latest = max(waits) if waits else None
    return latest if latest is not None and latest > now else None


def _data_facts() -> list[str]:
    """Server-side facts for the email body; never visitor text."""
    lines: list[str] = []
    try:
        from catalyst_app.routers.data_jobs import _latest_job

        job = _latest_job()
        if isinstance(job, dict) and job.get("job_id"):
            lines.append(f"Latest update: {job['job_id']} - {job.get('status')} - finished {job.get('finished_at') or 'not yet'}")
            if job.get("last_complete_session"):
                lines.append(f"Data complete through: {job['last_complete_session']}")
    except Exception:  # noqa: BLE001 - facts are best effort
        pass
    return lines


@router.get("/notify-admin", response_model=NotifyAdminStatusResponse)
def notify_status(request: Request):
    if _smtp_config() is None:
        return {"available": False, "next_allowed_at": None}
    now = datetime.now(timezone.utc)
    try:
        caller = _caller(request)
        with sqlite3.connect(runtime_db_path(), timeout=10) as conn:
            conn.execute(_TABLE)
            next_at = _next_allowed(conn, caller, now)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        next_at = None
    return {"available": True, "next_allowed_at": _iso(next_at) if next_at else None}


@router.post("/notify-admin", status_code=202, response_model=NotifyAdminResponse)
def notify_admin(payload: NotifyAdminRequest, request: Request):
    config = _smtp_config()
    if config is None:
        raise HTTPException(status_code=503, detail="notify_unavailable")
    caller = _caller(request)
    now = datetime.now(timezone.utc)
    now_text = _iso(now)
    with _GATE_LOCK:
        closed = _GLOBAL_CLOSED_UNTIL[0]
        if closed is not None and closed > now:
            return _rate_limited(closed)
    conn = sqlite3.connect(runtime_db_path(), timeout=2, isolation_level=None)
    try:
        conn.execute(_TABLE)
        conn.execute("BEGIN IMMEDIATE")
        next_at = _next_allowed(conn, caller, now)
        if next_at is not None:
            conn.rollback()
            with _GATE_LOCK:
                _GLOBAL_CLOSED_UNTIL[0] = _global_next_allowed(conn, now)
            return _rate_limited(next_at)
        # Recorded as sent before sending, so a failing mail server still counts toward the limits.
        conn.execute("INSERT INTO admin_notifications(created_at,caller_key,reason,sent) VALUES(?,?,?,1)",
                     (now_text, caller, payload.reason))
        conn.commit()
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        after = _next_allowed(conn, caller, now)
        with _GATE_LOCK:
            _GLOBAL_CLOSED_UNTIL[0] = _global_next_allowed(conn, now)
    finally:
        conn.close()
    reasons = {"data_stale": "A visitor reports that the data may be stale.", "update_failed": "A visitor reports that a data update failed.", "other": "A visitor asked the administrator to check the data."}
    def send():
        try:
            body = [
                f"Reason: {reasons[payload.reason]}",
                f"Time (UTC): {now_text}",
                f"Time (New York): {now.astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M %Z')}",
                *_data_facts(),
            ]
            if os.getenv("CATALYST_PUBLIC_URL", "").strip():
                body.append(f"Public site: {os.getenv('CATALYST_PUBLIC_URL', '').strip()}")
            _send(config, "Catalyst: a visitor asked for a data update", "\n".join(body) + "\n")
        except Exception:
            logger.warning("admin notification email failed")
            # A mail failure gives the global slot back (the caller's own 24 h slot stays used).
            try:
                with sqlite3.connect(runtime_db_path(), timeout=2) as failed:
                    failed.execute("UPDATE admin_notifications SET sent=0 WHERE id=?", (row_id,))
                with _GATE_LOCK:
                    _GLOBAL_CLOSED_UNTIL[0] = None
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=send, daemon=True, name="catalyst-admin-notify").start()
    return {"queued": True, "next_allowed_at": _iso(after) if after else None}
