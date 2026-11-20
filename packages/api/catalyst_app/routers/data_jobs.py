"""Read-only daily ingestion status and authenticated scheduled updates."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from catalyst_app.access_control import require_admin, resolve_access_context
from catalyst_app.db_paths import served_db_path
from catalyst_data.canonical.temporal import build_temporal_identity, utc_iso_z
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.runtime_paths import (
    STORE_FILENAMES,
    runtime_data_dir,
    runtime_store_path,
)
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/api/data", tags=["data jobs"])
REPO = Path(__file__).resolve().parents[4]
_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}


_KEY_LOCKS: dict[tuple[Any, ...], threading.Lock] = {}


def _cached(key: tuple[Any, ...], ttl: int, factory):
    """Public status reads are cached and single-flight: concurrent misses on one key build it once."""
    import time

    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and time.monotonic() - entry[0] < ttl:
            return entry[1]
        key_lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with key_lock:
        with _CACHE_LOCK:
            entry = _CACHE.get(key)
            if entry and time.monotonic() - entry[0] < ttl:
                return entry[1]
        result = factory()
        with _CACHE_LOCK:
            now = time.monotonic()
            _CACHE[key] = (now, result)  # stamped after the work, so a slow build never looks fresher
            for stale in [k for k, (created, _) in _CACHE.items() if now - created > 300]:
                _CACHE.pop(stale, None)
            while len(_CACHE) > 64:
                _CACHE.pop(next(iter(_CACHE)))
        return result


class NoJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["no_job"]


class FailedJobItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    item: str
    session: str | None
    attempts: int
    error: str | None


class LatestJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    plan: dict[str, Any]
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    stages: dict[str, dict[str, int]]
    failures: list[FailedJobItem]
    deferred_by_host: dict[str, dict[str, int]]
    last_complete_session: str | None
    running: bool = False
    current_stage: str | None = None
    elapsed_seconds: int | None = None
    expected_seconds: int | None = None
    runs_on: Literal["server", "this_machine"] = "this_machine"
    plan_summary: "JobPlanSummary | None" = None
    items_done: int = 0
    items_total: int = 0
    active_seconds: int = 0
    remaining_seconds: int | None = None
    current_item: "JobItem | None" = None
    recent_items: list["JobItem"] = []


class JobPlanSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_session: str | None = None
    last_session: str | None = None
    items_to_do: dict[str, int] = {}
    eta_seconds: list[int] = []
    universe_size: int | None = None
    price_symbols: int | None = None
    mover_threshold_pct: float | None = None


class JobItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    item: str
    session: str | None
    status: str
    started_at: str | None
    finished_at: str | None
    seconds: float | None
    error: str | None


LatestJobResponse.model_rebuild()


class UpdateStartedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started: Literal[True]


def _jobs_path() -> Path:
    return runtime_store_path("jobs_db", REPO)


def _bodies_path() -> Path:
    return runtime_store_path("bodies_db", REPO)


def _readonly(path: Path) -> sqlite3.Connection:
    from catalyst_data.storage.connect import connect_readonly

    return connect_readonly(path, timeout_ms=5000, immutable=False)


def _lease_alive(owner: str | None, expires_at: str | None) -> bool:
    """One lease row: unexpired, and when it was taken on this host, its process still exists."""
    if not owner or not expires_at:
        return False
    if expires_at <= datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"):
        return False
    parts = owner.split(":")
    host, pid = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
    if host == socket.gethostname() and pid.isdigit():
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    return True


def _has_live_lease(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with _readonly(path) as conn:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows = conn.execute(
                "SELECT lease_owner, lease_expires_at FROM jobs WHERE lease_owner IS NOT NULL AND lease_expires_at>?",
                (now,)).fetchall()
            for owner, expires_at in rows:
                if _lease_alive(owner, expires_at):
                    return True
            return False
    except sqlite3.OperationalError:
        return False


def _last_complete_session(plan: dict, statuses: dict[str, str]) -> str | None:
    start, end = plan.get("start"), plan.get("end")
    if not start or not end:
        return None
    from catalyst_data import trading_calendar
    complete = None
    for session in trading_calendar.calendar_trading_days(start, end):
        if statuses.get(session) not in ("OK", "EMPTY"):
            break
        complete = session
    return complete


class StoreInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path_kind: str
    bytes: int | None
    modified_at: str | None


class SearchIndexInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    built: bool
    path_kind: Literal["served_db"]
    bytes: int | None


class SessionCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_date: str
    tickers_with_prices: list[str]
    tickers_with_news: list[str]


class DataFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prices_from: str | None = None
    prices_through: str | None = None
    price_symbols: int = 0
    headlines_through: str | None = None
    headline_count: int | None = None


class DataOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freshness: DataFreshness = DataFreshness()

    stores: list[StoreInfo]
    disk_free_bytes: int | None
    search_index: SearchIndexInfo
    coverage: list[SessionCoverage]


def _file_info(path: Path) -> tuple[int | None, str | None]:
    try:
        stat = path.stat()
    except OSError:
        return None, None
    size = stat.st_size if path.is_file() else None
    return size, datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()


@router.get("/overview", response_model=DataOverviewResponse)
def data_overview(request: Request, sessions: int = Query(default=20, ge=1, le=60)) -> dict[str, Any]:
    """Admin, read-only: store sizes, free disk, search index and per-session coverage."""
    admin = resolve_access_context(request).tier == "admin"
    if not admin:
        sessions = 20 if sessions <= 20 else 60  # visitors cannot rotate the cache key
    if not (admin and request.query_params.get("fresh") == "1"):
        return _cached(("overview", str(runtime_data_dir(REPO)), sessions, admin), 120,
                       lambda: _data_overview(request, sessions, admin))
    return _data_overview(request, sessions, admin)


def _data_overview(request: Request, sessions: int, admin: bool) -> dict[str, Any]:
    stores = []
    for name in STORE_FILENAMES:
        size, modified = _file_info(runtime_store_path(name, REPO))
        stores.append({"name": name, "path_kind": name, "bytes": size, "modified_at": modified})

    disk_path = runtime_data_dir(REPO)
    while not disk_path.exists() and disk_path != disk_path.parent:
        disk_path = disk_path.parent
    try:
        disk_free = shutil.disk_usage(disk_path).free
    except OSError:
        disk_free = None

    index_size, _ = _file_info(Path(getattr(request.app.state, "served_db_path", None) or served_db_path()))

    dates: list[str] = []
    prices: dict[str, set[str]] = {}
    freshness: dict[str, Any] = {"prices_from": None, "prices_through": None, "price_symbols": 0,
                                 "headlines_through": None, "headline_count": None}
    try:
        with closing(_readonly(runtime_store_path("market_db", REPO))) as conn:
            first, last = conn.execute("SELECT MIN(date), MAX(date) FROM ohlcv").fetchone()
            freshness["prices_from"], freshness["prices_through"] = first, last
            if last:
                freshness["price_symbols"] = int(conn.execute(
                    "SELECT COUNT(DISTINCT symbol) FROM ohlcv WHERE date=?", (last,)).fetchone()[0])
            dates = [row[0] for row in conn.execute(
                "SELECT DISTINCT date FROM ohlcv ORDER BY date DESC LIMIT ?", (sessions,))]
            if dates:
                marks = ",".join("?" for _ in dates)
                for day, symbol in conn.execute(
                        f"SELECT date,symbol FROM ohlcv WHERE date IN ({marks}) AND close IS NOT NULL", dates):
                    prices.setdefault(day, set()).add(symbol)
    except sqlite3.Error:
        pass

    try:
        with closing(_readonly(runtime_store_path("archive_db", REPO))) as conn:
            latest, count = conn.execute("SELECT MAX(published_utc), COUNT(*) FROM finnhub_news").fetchone()
            freshness["headlines_through"], freshness["headline_count"] = latest, int(count or 0)
    except sqlite3.Error:
        pass
    news: dict[str, set[str]] = {day: set() for day in dates}
    if dates:
        windows = []
        for day in dates:
            identity = build_temporal_identity(day)
            windows.append((day, utc_iso_z(identity.information_window_start_at), utc_iso_z(identity.cutoff_at)))
        lo = timefields.finnhub_read_from(min(w[1] for w in windows))
        hi = max(w[2] for w in windows)
        symbols = sorted(set().union(*prices.values())) if prices else []
        try:
            with closing(_readonly(runtime_store_path("archive_db", REPO))) as conn:
                for symbol in symbols:  # one index range scan per ticker on (symbol, published_utc)
                    for published, source in conn.execute(
                            "SELECT published_utc,source FROM finnhub_news WHERE symbol=? AND published_utc>? "
                            "AND published_utc<=?", (symbol, lo, hi)):
                        try:
                            at = timefields.unskew(published, source="finnhub", publisher=source)
                        except ValueError:
                            continue
                        for day, start, cutoff in windows:
                            if start < at <= cutoff:
                                news[day].add(symbol)
                                break
        except sqlite3.Error:
            pass
    return {
        "stores": stores,
        "disk_free_bytes": disk_free if admin else None,
        "search_index": {"built": bool(index_size), "path_kind": "served_db", "bytes": index_size},
        "freshness": freshness,
        "coverage": [{"session_date": day, "tickers_with_prices": sorted(prices.get(day, set())),
                      "tickers_with_news": sorted(news[day])} for day in dates],
    }


@router.get("/jobs/latest", response_model=NoJobResponse | LatestJobResponse)
def latest_job(request: Request, fresh: int = Query(default=0, ge=0, le=1)) -> NoJobResponse | LatestJobResponse:
    admin = resolve_access_context(request).tier == "admin"
    if not (admin and fresh):
        job = _cached(("latest", str(_jobs_path()), str(_bodies_path())), 5, lambda: _latest_job())
    else:
        job = _latest_job()
    return job if admin else _public_job(job)


_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#][^\s]*")
_SECRET = re.compile(r"(?i)\b(api[_-]?key|apikey|token|key|secret|password)=[^\s&]+")
_LOCAL_PATH = re.compile(r"(/Users|/home|/opt|/var|/root|/private|[A-Za-z]:\\)[^\s'\"]*")


def _public_error(text: str | None) -> str | None:
    """A failure message safe for visitors: no URL queries, key-like values or local paths."""
    if not text:
        return text
    text = _URL_QUERY.sub(r"\1", text)
    text = _SECRET.sub(r"\1=***", text)
    text = _LOCAL_PATH.sub("<path>", text)
    return text[:300]


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    if "failures" not in job:
        return job
    scrub = lambda entries: [{**entry, "error": _public_error(entry.get("error"))} for entry in entries]  # noqa: E731
    current = job.get("current_item")
    return {**job, "failures": scrub(job["failures"]), "recent_items": scrub(job.get("recent_items") or []),
            "current_item": scrub([current])[0] if current else None}


def _eta_plan_summary(plan: Any, stages: dict[str, dict[str, int]], timing_rows: list[Any]
                      ) -> tuple[int, float, dict[str, Any] | None, int, int]:
    per_stage: dict[str, list[int]] = {}
    for stage_name, started, finished in timing_rows:
        took = _seconds_between(started, finished)
        if took is not None:
            per_stage.setdefault(stage_name, []).append(took)
    active_seconds = sum(sum(values) for values in per_stage.values())
    assumed = ((plan.get("eta_assumptions") or {}).get("seconds_per_item") or {}) if isinstance(plan, dict) else {}
    remaining = 0.0
    for stage_name, counts in stages.items():
        left = counts.get("PENDING", 0) + counts.get("RUNNING", 0)
        if not left:
            continue
        seen = sorted(per_stage.get(stage_name, []))
        if seen:
            each = seen[len(seen) // 2]
        else:
            bounds = assumed.get(stage_name) or [1.0, 1.0]
            each = sum(bounds) / len(bounds)
        remaining += left * each
    # Steps whose items appear later (news per mover, checks per session) still count, at the plan's rate.
    planned = (plan.get("items_to_do") or {}) if isinstance(plan, dict) else {}
    for stage_name, count in planned.items():
        if stage_name not in stages and isinstance(count, (int, float)):
            bounds = assumed.get(stage_name) or [1.0, 1.0]
            remaining += count * sum(bounds) / len(bounds)
    finished_states = ("OK", "EMPTY", "FAILED", "SKIPPED")
    items_total = sum(sum(counts.values()) for counts in stages.values())
    items_done = sum(counts.get(state, 0) for counts in stages.values() for state in finished_states)
    plan_summary = {
        "first_session": plan.get("first_session") or plan.get("start"),
        "last_session": plan.get("last_session") or plan.get("end"),
        "items_to_do": {key: int(value) for key, value in (plan.get("items_to_do") or {}).items()
                        if isinstance(value, (int, float))},
        "eta_seconds": [int(value) for value in (plan.get("eta_seconds") or []) if isinstance(value, (int, float))],
        "universe_size": plan.get("universe_size"),
        "price_symbols": plan.get("price_symbols"),
        "mover_threshold_pct": plan.get("mover_threshold_pct"),
    } if isinstance(plan, dict) else None
    return active_seconds, remaining, plan_summary, items_done, items_total


def _latest_job() -> NoJobResponse | LatestJobResponse:
    jobs_path = _jobs_path()
    if not jobs_path.exists():
        return {"status": "no_job"}
    try:
        conn = _readonly(jobs_path)
    except sqlite3.OperationalError:
        return {"status": "no_job"}
    try:
        try:
            job = conn.execute(
                "SELECT job_id, plan_json, status, created_at, started_at, finished_at, lease_owner, "
                "lease_expires_at FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
            if job is None:
                return {"status": "no_job"}
            job_id, plan_json, status, created_at, started_at, finished_at, lease_owner, lease_expires = job
            plan = json.loads(plan_json)
            stages = {}
            for stage, item_status, count in conn.execute(
                    "SELECT stage,status,count(*) FROM job_items WHERE job_id=? GROUP BY stage,status",
                    (job_id,)):
                stages.setdefault(stage, {})[item_status] = count
            failures = [
                {"stage": stage, "item": item, "session": session, "attempts": attempts, "error": error}
                for stage, item, session, attempts, error in conn.execute(
                    "SELECT stage,item_key,session_date,attempts,last_error FROM job_items "
                    "WHERE job_id=? AND status='FAILED' ORDER BY seq LIMIT 50", (job_id,))]
            verify_statuses = dict(conn.execute(
                "SELECT session_date,status FROM job_items WHERE job_id=? AND stage='verify'",
                (job_id,)).fetchall())
            # Stage order = the first item seq of each stage; the current stage is the first unfinished one.
            stage_order = [row[0] for row in conn.execute(
                "SELECT stage FROM job_items WHERE job_id=? GROUP BY stage ORDER BY MIN(seq)", (job_id,))]
            item_cols = "stage,item_key,session_date,status,started_at,finished_at,last_error"
            current_row = conn.execute(
                f"SELECT {item_cols} FROM job_items WHERE job_id=? AND status='RUNNING' ORDER BY seq LIMIT 1",
                (job_id,)).fetchone()
            timing_rows = conn.execute(
                "SELECT stage, started_at, finished_at FROM job_items WHERE job_id=? AND finished_at IS NOT NULL "
                "AND started_at IS NOT NULL", (job_id,)).fetchall()
            recent_rows = conn.execute(
                f"SELECT {item_cols} FROM job_items WHERE job_id=? AND finished_at IS NOT NULL "
                "ORDER BY finished_at DESC, seq DESC LIMIT 10", (job_id,)).fetchall()
            durations = [
                _seconds_between(started, finished)
                for started, finished in conn.execute(
                    "SELECT started_at, finished_at FROM jobs WHERE status IN ('COMPLETED','PARTIAL') "
                    "AND started_at IS NOT NULL AND finished_at IS NOT NULL ORDER BY created_at DESC LIMIT 5")
            ]
        except sqlite3.OperationalError:
            return {"status": "no_job"}
    finally:
        conn.close()

    deferred: dict[str, dict[str, int]] = {}
    bodies_path = _bodies_path()
    if bodies_path.exists():
        try:
            with _readonly(bodies_path) as bodies:
                for host, item_status, count in bodies.execute(
                        "SELECT host,status,count(*) FROM deferred_fetches GROUP BY host,status"):
                    deferred.setdefault(host, {})[item_status] = count
        except sqlite3.OperationalError:
            pass
    running = status in ("RUNNING", "PENDING") and _lease_alive(lease_owner, lease_expires)
    current_stage = next(
        (stage for stage in stage_order
         if stages.get(stage, {}).get("PENDING", 0) or stages.get(stage, {}).get("RUNNING", 0)),
        None,
    ) if running else None
    now = datetime.now(timezone.utc)
    elapsed = (_seconds_between(started_at, utc_iso_z(now)) if running
               else _seconds_between(started_at, finished_at))
    def item(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        stage, key, session, item_status, started, finished, error = row
        took = _seconds_between(started, finished)
        return {"stage": stage, "item": key, "session": session, "status": item_status,
                "started_at": started, "finished_at": finished,
                "seconds": float(took) if took is not None else None, "error": error}

    # Work time and per-step estimate exclude time a resumed job sat stopped.
    active_seconds, remaining, plan_summary, items_done, items_total = _eta_plan_summary(plan, stages, timing_rows)
    known = sorted(value for value in durations if value is not None)
    expected = known[len(known) // 2] if known else None
    return {"job_id": job_id, "plan": plan, "status": status,
            "created_at": created_at, "started_at": started_at, "finished_at": finished_at,
            "stages": stages, "failures": failures, "deferred_by_host": deferred,
            "last_complete_session": _last_complete_session(plan, verify_statuses),
            "running": running, "current_stage": current_stage, "elapsed_seconds": elapsed,
            "expected_seconds": expected,
            "plan_summary": plan_summary, "items_done": items_done, "items_total": items_total,
            "active_seconds": active_seconds, "remaining_seconds": int(remaining) if running or status in ("RUNNING", "PENDING") else None,
            "current_item": item(current_row) if running else None,
            "recent_items": [item(row) for row in recent_rows],
            "runs_on": "server" if os.environ.get("CATALYST_ALLOW_NON_LOOPBACK") == "1" else "this_machine"}


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _seconds_between(start: str | None, end: str | None) -> int | None:
    a, b = _parse_utc(start), _parse_utc(end)
    return max(0, int((b - a).total_seconds())) if a and b else None


class DataJobHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    duration_seconds: int | None
    failed_items: int


class DataJobHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[DataJobHistoryItem]


@router.get("/jobs/history", response_model=DataJobHistoryResponse)
def job_history(limit: int = Query(default=7, ge=1, le=30)) -> dict[str, Any]:
    """Anyone, read-only: the most recent updates, newest first."""
    limit = 7 if limit <= 7 else 30
    return _cached(("history", str(_jobs_path()), limit), 10, lambda: _job_history(limit))


def _job_history(limit: int) -> dict[str, Any]:
    jobs_path = _jobs_path()
    if not jobs_path.exists():
        return {"items": []}
    try:
        with closing(_readonly(jobs_path)) as conn:
            rows = conn.execute(
                "SELECT j.job_id, j.status, j.created_at, j.started_at, j.finished_at, "
                "(SELECT COUNT(*) FROM job_items i WHERE i.job_id=j.job_id AND i.status='FAILED') "
                "FROM jobs j ORDER BY j.created_at DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError:
        return {"items": []}
    return {"items": [
        {"job_id": job_id, "status": status, "created_at": created, "started_at": started,
         "finished_at": finished, "duration_seconds": _seconds_between(started, finished),
         "failed_items": int(failed or 0)}
        for job_id, status, created, started, finished, failed in rows]}


class DataScheduleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_update_utc: str
    next_run_at: str


@router.get("/schedule", response_model=DataScheduleResponse)
def schedule() -> dict[str, Any]:
    """Anyone: when the scheduled daily update runs (CATALYST_DAILY_UPDATE_UTC, weekdays)."""
    raw = os.environ.get("CATALYST_DAILY_UPDATE_UTC", "11:00").strip()
    try:
        hour, minute = (int(part) for part in raw.split(":")[:2])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        hour, minute, raw = 11, 0, "11:00"
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    from zoneinfo import ZoneInfo

    new_york = ZoneInfo("America/New_York")
    # The update follows US sessions: skip runs that fall on a New York weekend.
    while candidate <= now or candidate.astimezone(new_york).weekday() >= 5:
        candidate += timedelta(days=1)
    return {"daily_update_utc": f"{hour:02d}:{minute:02d}", "next_run_at": utc_iso_z(candidate)}


@router.post("/update", status_code=202, response_model=UpdateStartedResponse)
def start_manual_update(request: Request) -> UpdateStartedResponse:
    require_admin(request)
    rebuild_flag = os.environ.get("CATALYST_CORPUS_FTS_REBUILD", "").strip().lower()
    corpus_path = os.environ.get("CATALYST_CORPUS_DB")
    if rebuild_flag in {"1", "true", "yes", "on"} and corpus_path:
        from catalyst_data.corpus.locking import (
            acquire_corpus_lock,
            release_corpus_lock,
        )

        lock_fd = acquire_corpus_lock(corpus_path)
        if lock_fd is None:
            raise HTTPException(status_code=409, detail="CORPUS_REBUILD_BUSY")
        release_corpus_lock(lock_fd)
    jobs_path = _jobs_path()
    if _has_live_lease(jobs_path):
        raise HTTPException(status_code=409, detail="UPDATE_BUSY")
    env = os.environ.copy()
    data_core = str(REPO / "packages/data")
    env["PYTHONPATH"] = data_core + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        subprocess.Popen([sys.executable, "-m", "catalyst_data.ingestion.ops.daily", "scheduled"],
                         cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="UPDATE_START_FAILED") from exc
    return UpdateStartedResponse(started=True)
