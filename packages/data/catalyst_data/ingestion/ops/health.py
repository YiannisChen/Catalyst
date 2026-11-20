"""Health report of the data server, read-only: one JSON document the monitor (on another host) pulls and judges.

It reads the ledgers every intake path already writes (OK / EMPTY / ERROR / DISABLED per request), the daily job
store, the latest price bars, the page-version records, failed systemd units and the disk. Nothing here decides what
is an alert; the monitor does (deploy/monitor/monitor.py), so the rules live in one place.

    python -m catalyst_data.ingestion.ops.health --out /opt/catalyst/health/health.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from catalyst_data.ingestion import sources as source_registry
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.connect import connect_readonly
from catalyst_data.storage.runtime_paths import runtime_data_dir


def resolve_data_dir(environ=None, repo: Path | None = None) -> Path:
    """Preserve health's legacy root unless a data or runtime root is configured."""
    env = os.environ if environ is None else environ
    if "CATALYST_DATA_DIR" in env:
        return Path(env["CATALYST_DATA_DIR"])
    if env.get("CATALYST_RUNTIME_DATA_DIR", "").strip():
        return runtime_data_dir(repo, env)
    return Path("/opt/catalyst/repo/data/m9")


DATA = resolve_data_dir()
ARCHIVE = Path(os.environ.get("CATALYST_ARCHIVE_DIR", "/opt/catalyst/archive"))
BACKUP = Path(os.environ.get("CATALYST_BACKUP_DIR", "/opt/catalyst/backup"))
WINDOW_H = 24
HISTORY_DAYS = 7  # earlier 24 h windows per source, for the monitor's "fewer OK than usual" rule


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        return connect_readonly(path, timeout_ms=10_000, immutable=False)
    except sqlite3.Error:
        raise


def _health_ro(path: Path) -> tuple[sqlite3.Connection | None, str | None]:
    if not path.is_file():
        return None, "missing_store"
    try:
        return connect_readonly(path, timeout_ms=10_000, immutable=False), None
    except sqlite3.Error as exc:
        reason = f"read_timeout: {exc}" if type(exc).__name__ == "SQLiteReadTimeoutError" else \
            f"{type(exc).__name__}: {str(exc)[:200]}"
        return None, reason


def _health_ok(data: dict | list) -> dict:
    return {"status": "ok", **data} if isinstance(data, dict) else {"status": "ok", "items": data}


def _health_error(reason: str) -> dict:
    return {"status": "error", "reason": reason}


def _ledger(conn, now: datetime, key_sql: str, where: str = "1=1") -> dict[str, dict]:
    """Per source key: counts by status in the window; the last OK and the last completed (OK or EMPTY) time, any
    time; the first ledger observation; and ``history``, the OK / EMPTY / ERROR counts of each of the HISTORY_DAYS
    earlier 24 h windows (index 0 is
    the window just before the current one)."""
    def blank():
        return {"OK": 0, "EMPTY": 0, "ERROR": 0, "DISABLED": 0, "first_observed_at": None,
                "last_ok_at": None, "last_done_at": None,
                "history": [{"OK": 0, "EMPTY": 0, "ERROR": 0} for _ in range(HISTORY_DAYS)]}
    out: dict[str, dict] = defaultdict(blank)
    since = _stamp(now - timedelta(hours=WINDOW_H * (HISTORY_DAYS + 1)))
    # window index: 0 = the current 24 h, 1..HISTORY_DAYS = the earlier ones
    age = f"CAST((julianday('{_stamp(now)}') - julianday(attempted_at)) * 24 / {WINDOW_H} AS INTEGER)"
    for key, status, idx, n in conn.execute(
            f"SELECT {key_sql}, status, {age}, COUNT(*) FROM intake_log WHERE attempted_at >= ? AND {where} "
            "GROUP BY 1, 2, 3", (since,)):
        if idx == 0:
            out[key][status] = out[key].get(status, 0) + n
        elif 1 <= idx <= HISTORY_DAYS and status in ("OK", "EMPTY", "ERROR"):
            out[key]["history"][idx - 1][status] += n
    for key, first_observed, last_ok, last_done in conn.execute(
            f"SELECT {key_sql}, MIN(CASE WHEN status IN ('OK', 'EMPTY', 'ERROR') THEN attempted_at END), MAX(CASE WHEN status = 'OK' THEN attempted_at END), "
            f"MAX(CASE WHEN status IN ('OK', 'EMPTY') THEN attempted_at END) FROM intake_log WHERE {where} GROUP BY 1"):
        out[key]["first_observed_at"] = first_observed
        out[key]["last_ok_at"], out[key]["last_done_at"] = last_ok, last_done  # also for a source silent in the window
    return dict(out)


# how each store's ledger is keyed in the report (the registry's ``key`` values are in these terms)
KEYS = {
    "daily": ("source", "1=1", "daily"),
    "market": ("source", "1=1", "daily"),
    "alpaca_news": ("subject", "1=1", "archive"),
}


# finer than the report key where one report row hides many units (the IR feeds are one row, "ir")
NEVER_OK_AFTER_DAYS = 3
NEVER_OK_MIN_ATTEMPTS = 5


def never_ok(now: datetime, registry: dict | None = None) -> dict:
    """Units that have been tried for NEVER_OK_AFTER_DAYS days (at least NEVER_OK_MIN_ATTEMPTS times) and have not
    had one OK, ever. The windowed rules need a week of history and see a report row, so a new source, or one feed
    among several (XOM's IR feed: 2,175 EMPTY, no OK, for a week), passed them."""
    reg = registry or source_registry.load()
    cutoff = _stamp(now - timedelta(days=NEVER_OK_AFTER_DAYS))
    out = []
    errors = {}
    for store, path in reg["stores"].items():
        if store in {"market", "daily"} or store not in KEYS:
            continue
        conn, error = _health_ro(_store_path(store, path))
        if conn is None:
            errors[store] = error or "missing_store"
            continue
        key_sql, where, _kind = KEYS[store]
        try:
            for unit, first, n in conn.execute(
                    f"SELECT {key_sql}, MIN(attempted_at), COUNT(*) FROM intake_log "
                    f"WHERE status IN ('OK', 'EMPTY', 'ERROR') AND {where} GROUP BY 1 "
                    "HAVING SUM(status = 'OK') = 0 AND COUNT(*) >= ? AND MIN(attempted_at) <= ?",
                    (NEVER_OK_MIN_ATTEMPTS, cutoff)):
                out.append({"store": store, "unit": unit, "attempts": n, "first_attempt_at": first})
        except sqlite3.Error as exc:
            errors[store] = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            conn.close()
    if errors:
        return {"status": "error", "items": sorted(out, key=lambda row: (row["store"], row["unit"])),
                "errors": errors,
                "reason": "; ".join(f"{store}: {reason}" for store, reason in errors.items())}
    return _health_ok(sorted(out, key=lambda row: (row["store"], row["unit"])))


def _store_path(store: str, configured: str) -> Path:
    """The registry names the server paths; CATALYST_DATA_DIR / CATALYST_ARCHIVE_DIR move them (tests)."""
    name = Path(configured).name
    return DATA / name if store in {"daily", "market"} else ARCHIVE / name


def sources(now: datetime, registry: dict | None = None) -> list[dict]:
    reg = registry or source_registry.load()
    groups = [(store, _store_path(store, path), *KEYS[store]) for store, path in reg["stores"].items()]
    out = []
    for store, path, key_sql, where, kind in groups:
        # market_context.sqlite3 stores price bars, not an intake ledger. prices() reports it.
        if store == "market":
            continue
        try:
            conn = _ro(path)
        except sqlite3.Error as exc:
            reason = f"read_timeout: {exc}" if type(exc).__name__ == "SQLiteReadTimeoutError" else str(exc)
            out.append({"store": store, "source": "*", "kind": kind, "error": reason[:200]})
            continue
        if conn is None:
            out.append({"store": store, "source": "*", "kind": kind, "missing_store": str(path)})
            continue
        try:
            for key, c in sorted(_ledger(conn, now, key_sql, where).items()):
                out.append({"store": store, "source": key, "kind": kind, **c,
                            "attempts": c["OK"] + c["EMPTY"] + c["ERROR"]})
        except sqlite3.Error as exc:
            out.append({"store": store, "source": "*", "kind": kind, "error": str(exc)[:200]})
        finally:
            conn.close()
    return out


def daily_job() -> dict:
    try:
        conn = _ro(DATA / "update_jobs.sqlite3")
    except sqlite3.Error as exc:
        return {"status": "error", "reason": str(exc)[:200]}
    if conn is None:
        return {"missing_store": True}
    try:
        row = conn.execute("SELECT job_id, status, created_at, started_at, finished_at FROM jobs "
                           "ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:
            return {}
        job = dict(zip(("job_id", "status", "created_at", "started_at", "finished_at"), row))
        stages: dict[str, dict] = defaultdict(dict)
        for stage, status, n in conn.execute(
                "SELECT stage, status, COUNT(*) FROM job_items WHERE job_id = ? GROUP BY 1, 2", (job["job_id"],)):
            stages[stage][status] = n
        job["stages"] = dict(stages)
        job["session"] = job["job_id"].split("-", 1)[1][:10] if job["job_id"].startswith("upd-") else None
        return job
    except sqlite3.Error as exc:
        return {"status": "error", "reason": str(exc)[:200]}
    finally:
        conn.close()


def daily_pipeline() -> dict:
    path = Path(os.environ.get("CATALYST_DAILY_PIPELINE_STATUS")
                or DATA / "daily_pipeline_status.json")
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return payload if isinstance(payload, dict) else {"error": "pipeline status is not a JSON object"}


def prices() -> dict:
    try:
        conn = _ro(DATA / "market_context.sqlite3")
    except sqlite3.Error as exc:
        return {"status": "error", "reason": str(exc)[:200]}
    if conn is None:
        return {"missing_store": True}
    try:
        last = conn.execute("SELECT MAX(date) FROM ohlcv WHERE symbol = 'SPY'").fetchone()[0]
        by_source = dict(conn.execute("SELECT source, COUNT(*) FROM ohlcv WHERE date = ? GROUP BY 1", (last,)))
        return {"last_spy_session": last, "bars_by_source": by_source}
    except sqlite3.Error as exc:
        return {"status": "error", "reason": str(exc)[:200]}
    finally:
        conn.close()



def sec_exhibit_units() -> dict:
    """Check the deployed body store's SEC exhibit-unit table using a read-only connection."""
    configured = os.environ.get("CATALYST_NEWS_BODIES_DB", "").strip()
    path = Path(configured).expanduser() if configured else DATA / "news_bodies_v2.sqlite3"
    if not path.is_file():
        return {"status": "error", "reason": "missing_store"}
    try:
        conn = connect_readonly(path, timeout_ms=10_000, immutable=False)
    except sqlite3.Error as exc:
        reason = f"read_timeout: {exc}" if type(exc).__name__ == "SQLiteReadTimeoutError" else str(exc)
        return {"status": "error", "reason": reason[:200]}
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sec_exhibit_units'"
        ).fetchone()
        if exists is None:
            return {"status": "error", "reason": "missing_table"}
        rows = conn.execute("SELECT COUNT(*) FROM sec_exhibit_units").fetchone()[0]
        return {"status": "ok", "rows": rows} if rows > 0 else {"status": "error", "reason": "empty"}
    except sqlite3.Error as exc:
        reason = f"read_timeout: {exc}" if type(exc).__name__ == "SQLiteReadTimeoutError" else str(exc)
        return {"status": "error", "reason": reason[:200]}
    finally:
        conn.close()


def page_versions(now: datetime) -> dict:
    conn, error = _health_ro(DATA / "page_versions.sqlite3")
    if conn is None:
        return _health_error(error or "missing_store")
    try:
        since = _stamp(now - timedelta(hours=WINDOW_H))
        return _health_ok({path: {"pages": n, "modified_after_cutoff": a or 0, "no_declared_modification": m or 0}
                for path, n, a, m in conn.execute(
                    "SELECT path, COUNT(*), SUM(modified_after_cutoff), SUM(declared_modified IS NULL) "
                    "FROM page_versions WHERE fetched_at >= ? GROUP BY 1", (since,))})
    except sqlite3.Error as exc:
        return _health_error(f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        conn.close()


def dead_links() -> dict:
    """Links whose host failed on several days (deferred_fetches DEAD): not retried, not counted as pending; listed
    in the daily summary by host."""
    conn, error = _health_ro(DATA / "news_bodies_v2.sqlite3")
    if conn is None:
        return _health_error(error or "missing_store")
    try:
        return _health_ok({host: {"links": n, "since": since} for host, n, since in conn.execute(
            "SELECT host, COUNT(*), MIN(resolved_at) FROM deferred_fetches WHERE status = 'DEAD' GROUP BY 1")})
    except sqlite3.Error as exc:
        return _health_error(f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        conn.close()


def time_checks(now: datetime) -> dict:
    """Bodies of the past 24 h, per publisher, whose two times were both known (body_times, page_versions): how many
    differ by a whole 4 or 5 hours without being a known, corrected error (flag suspected_tz), how many disagree
    otherwise (flag disagree: SEC's submissions JSON against the filing index page by anything but 4/5 h), how many
    were the known SEC offset, corrected from the index page (api_offset; reported, not an alert), and how many were
    dropped as imprecise (times.reconcile) or had no index time."""
    conn, error = _health_ro(DATA / "page_versions.sqlite3")
    if conn is None:
        return _health_error(error or "missing_store")
    try:
        since = _stamp(now - timedelta(hours=WINDOW_H))
        return _health_ok({pub or "?": {"compared": c or 0, "whole_hour": w or 0, "disagree": d or 0, "known_offset": k or 0,
                             "imprecise": i or 0}
                for pub, c, w, d, k, i in conn.execute(
                    "SELECT publisher, SUM(api_published IS NOT NULL AND page_published IS NOT NULL), "
                    "SUM(flag = 'suspected_tz'), SUM(flag = 'disagree'), SUM(flag = 'api_offset'), "
                    "SUM(flag IN ('imprecise', 'index_missing')) FROM body_times WHERE recorded_at >= ? GROUP BY 1",
                    (since,))})
    except sqlite3.Error as exc:
        return _health_error(f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        conn.close()


RESULTS = re.compile(r"\b(?:reports?|announces?|posts?)\b.*\b(?:results|earnings)\b|\b(?:Q[1-4]|quarter|fiscal)\b.*"
                     r"\b(?:results|earnings|EPS)\b", re.I)
CROSS_DAYS = 7
CROSS_PAIR_MAX_H = 6


def cross_checks(now: datetime) -> dict:
    """Times of one event from independent sources, past CROSS_DAYS days. ``sec_vs_wire``: each earnings exhibit
    (EX-99) of an 8-K in the daily store against the first Benzinga results headline (Alpaca archive) on the same
    ticker within CROSS_PAIR_MAX_H hours; pairs, and how many differ by a whole 4 or 5 hours (+-2 min)."""
    bodies, body_error = _health_ro(DATA / "news_bodies_v2.sqlite3")
    wire, wire_error = _health_ro(ARCHIVE / "alpaca_news.sqlite3")
    if body_error or wire_error:
        if bodies is not None:
            bodies.close()
        if wire is not None:
            wire.close()
        return _health_error("; ".join(f"{name}: {err}" for name, err in
                                        (("news_bodies_v2", body_error), ("alpaca_news", wire_error)) if err))
    try:
        since = _stamp(now - timedelta(days=CROSS_DAYS))
        pairs = whole = 0
        for title, eligible in bodies.execute(
                "SELECT DISTINCT title, eligible_at FROM web_body_chunks WHERE publisher = 'SEC filing' "
                "AND title LIKE '%EX-99%' AND eligible_at >= ?", (since,)):
            m = re.search(r"\(([A-Z][A-Z.\-]*)\) Form 8-K", title)
            if not m:
                continue
            t = timefields.stored(eligible)
            lo, hi = (_stamp(t - timedelta(hours=CROSS_PAIR_MAX_H)), _stamp(t + timedelta(hours=CROSS_PAIR_MAX_H)))
            hits = [timefields.stored(c) for c, h in wire.execute(
                "SELECT n.created_at, n.headline FROM news n JOIN news_symbols s ON s.id = n.id WHERE s.symbol = ? "
                "AND n.created_at BETWEEN ? AND ? ORDER BY n.created_at", (m.group(1).replace("-", "."), lo, hi))
                if RESULTS.search(h or "")]
            if not hits:
                continue
            pairs += 1
            diff = abs((t - hits[0]).total_seconds())
            whole += any(abs(diff - h * 3600) <= 120 for h in (4, 5))
        return _health_ok({"sec_vs_wire": {"compared": pairs, "whole_hour": whole}})
    except sqlite3.Error as exc:
        return _health_error(f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        bodies.close(); wire.close()


def fetching(now: datetime) -> dict:
    """Three independent denominators with coverage flags and separate legacy counters."""
    from catalyst_data.ingestion import resilient
    try:
        return resilient.report(7, now)
    except Exception as exc:  # noqa: BLE001
        return {"schema_version": 2, "available": False, "sources": {}, "error": type(exc).__name__}


def units() -> dict:
    def run(*args):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=20)
            if getattr(result, "returncode", 0) != 0:
                detail = (result.stderr or "").strip()[:200]
                return "", f"exit {result.returncode}" + (f": {detail}" if detail else "")
            return result.stdout, None
        except (OSError, subprocess.SubprocessError) as exc:
            return "", f"{type(exc).__name__}: {str(exc)[:200]}"
    outputs = [run("systemctl", "list-units", "--failed", "--plain", "--no-legend", "catalyst-*"),
               run("systemctl", "list-units", "--state=activating", "--plain", "--no-legend", "catalyst-*"),
               run("systemctl", "list-timers", "--all", "--plain", "--no-legend", "catalyst-*")]
    errors = {name: error for name, (_stdout, error) in zip(
        ("failed_units", "activating_units", "timers"), outputs) if error}
    commands = {name: ({"status": "error", "reason": error} if error else {"status": "ok"})
                for name, (_stdout, error) in zip(("failed_units", "activating_units", "timers"), outputs)}
    failed = [line.split()[0] for line in outputs[0][0].splitlines() if line.strip()]
    # a failed oneshot that is running again is "activating", not "failed": the monitor must not read that as recovery
    activating = [line.split()[0] for line in outputs[1][0].splitlines() if line.strip()]
    result = {"status": "error" if errors else "ok", "commands": commands,
              "failed": failed, "activating": activating}
    if "timers" not in errors:
        timers = [line.split()[-2] for line in outputs[2][0].splitlines() if line.strip()]
        timers = sorted(set(t for t in timers if t.endswith(".timer")))
        named = set(source_registry.timers())
        result.update({"timers": timers,
                       # timers on this host the registry does not name (ad hoc research jobs), and named ones not installed
                       "unregistered": [t for t in timers if t[: -len(".timer")] not in named],
                       "not_installed": sorted(n for n in named if f"{n}.timer" not in timers)})
    if errors:
        result.update({"errors": errors, "reason": "; ".join(f"{k}: {v}" for k, v in errors.items())})
    return result


def disk() -> dict:
    usage = shutil.disk_usage("/")
    return {"total_gb": round(usage.total / 1e9, 1), "free_gb": round(usage.free / 1e9, 1),
            "free_pct": round(100 * usage.free / usage.total, 1)}


def backups() -> dict:
    """The newest backup directory here and when its MANIFEST was written (the monitor alerts on a missing day)."""
    days = sorted(p for p in BACKUP.glob("20??-??-??") if (p / "MANIFEST").is_file()) if BACKUP.is_dir() else []
    if not days:
        return {"latest": None}
    manifest = days[-1] / "MANIFEST"
    return {"latest": days[-1].name, "manifest_at": timefields.external_stamp(manifest.stat().st_mtime, source="file", field="mtime"),
            "files": len(manifest.read_text().splitlines())}


def report(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"generated_at": _stamp(now), "host": socket.gethostname(), "window_hours": WINDOW_H,
            "daily_job": daily_job(), "daily_pipeline": daily_pipeline(),
            "prices": prices(), "sources": sources(now),
            "page_versions": page_versions(now), "dead_links": dead_links(), "never_ok": never_ok(now), "units": units(), "disk": disk(), "backup": backups(),
            "time_checks": time_checks(now), "cross_checks": cross_checks(now), "fetching": fetching(now),
            "registry": source_registry.load(), "sec_exhibit_units": sec_exhibit_units()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(report(), indent=1))
    tmp.replace(out)  # the monitor never reads a half-written report
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
