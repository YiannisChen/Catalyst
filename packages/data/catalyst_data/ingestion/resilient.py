"""One place for retries (2026-10-23): instrumented HTTP requests under m9/ go through ``request()``, and here
anything wait and try again (tests/ingestion/test_resilient.py fails on a retry loop anywhere else).

- **Classify.** Transient: timeout, connection error, 5xx, 429. Permanent: 401, 403, 404, 410, other 4xx, robots
  refusal, a response that does not parse. Only transient errors are retried; a permanent one is raised at once
  (``PermanentError``) and never retried.
- **Back off.** Exponential with full jitter, honouring ``Retry-After`` (seconds or an HTTP date), up to the
  provider's ``attempts`` and a total wait of ``budget_s``; then ``TransientError``.
- **Break.** Per provider: after ``breaker_k`` consecutive failed requests the provider is skipped for
  ``breaker_open_s`` (``BreakerOpen``, no request made); then one probe request decides.
- **Queue.** Work that failed transiently can be queued (``enqueue``): retried after 15 min, 1 h and 6 h by the
  ``retry`` runner (a timer), across restarts; after the last one, or past its deadline, it is final (``FAILED``)
  and reported by the health report.
- **Record.** Every logical request writes one row (provider, attempts, first try ok, final outcome, error class),
  for legacy call statistics. Task scopes separately record physical attempts, logical tasks, and discovered body versions.

State (breakers, queue, attempts) is one SQLite file, ``CATALYST_RESILIENCE_DB`` (default
/opt/catalyst/archive/resilience.sqlite3; tests point it elsewhere). The per-minute request budgets stay in
``ratelimit`` (one token per request, retries included).

    python -m catalyst_data.ingestion.resilient retry      # run due queue items (timer, every 15 min)
    python -m catalyst_data.ingestion.resilient report     # three-layer accounting and separate legacy calls, past 7 days
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

from catalyst_data.ingestion import fetch_runtime
from catalyst_data.ingestion.time import timefields

sleep: Callable[[float], None] = time.sleep
rand: Callable[[], float] = random.random
QUEUE_DELAYS = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6))
_IN_QUEUE = False  # set while run_due redoes a queued item: its requests are recorded as queue retries


@dataclass(frozen=True)
class Policy:
    attempts: int = 3            # tries per request, the first included
    base_s: float = 2.0          # back-off base: wait ~ U(0, base * 2^n), capped
    cap_s: float = 60.0
    budget_s: float = 120.0      # total waiting per request
    breaker_k: int = 5           # consecutive failed requests that open the breaker
    breaker_open_s: float = 900.0


POLICIES: dict[str, Policy] = {
    "default": Policy(),
    "finnhub": Policy(attempts=3, base_s=2.0, budget_s=90),
    "polygon": Policy(attempts=3, base_s=12.0, cap_s=60, budget_s=150),
    "alpaca": Policy(attempts=4, base_s=1.0, budget_s=60),
    "sec": Policy(attempts=3, base_s=1.0, budget_s=30),
    "yfinance": Policy(attempts=3, base_s=3.0, budget_s=60),
    "web": Policy(attempts=3, base_s=5.0, cap_s=30, budget_s=60, breaker_k=3),
    "gdelt": Policy(attempts=2, base_s=10.0, cap_s=60, budget_s=60, breaker_k=3, breaker_open_s=3600),
    "rss": Policy(attempts=2, base_s=5.0, budget_s=30),
    "marketaux": Policy(attempts=2, base_s=5.0, budget_s=30),
    "newsapi": Policy(attempts=2, base_s=5.0, budget_s=30),
    "seekingalpha": Policy(attempts=2, base_s=5.0, budget_s=30),
    "finra": Policy(attempts=3, base_s=5.0, budget_s=60),
    "regulatory": Policy(attempts=3, base_s=5.0, budget_s=60),
    "tavily": Policy(attempts=3, base_s=2.0, budget_s=30),
}
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}


class FetchError(RuntimeError):
    def __init__(self, provider: str, error_class: str, detail: str = ""):
        super().__init__(f"{provider}: {error_class} {detail}".strip())
        self.provider, self.error_class = provider, error_class


class PermanentError(FetchError):
    """Not retried: 4xx other than 408/425/429, robots refusal, unparseable response."""


class TransientError(FetchError):
    """Still failing after the provider's attempts or budget; may be queued."""


class BreakerOpen(FetchError):
    """The provider failed ``breaker_k`` times in a row; no request was made."""


def classify(response=None, exc: BaseException | None = None) -> tuple[str, str]:
    """("ok" | "transient" | "permanent", error class) for a response or an exception."""
    if exc is not None:
        if isinstance(exc, PermanentError):
            return "permanent", exc.error_class
        if isinstance(exc, (requests.Timeout, TimeoutError)):
            return "transient", "timeout"
        if isinstance(exc, (requests.ConnectionError, ConnectionError, OSError)):
            return "transient", "connection"
        if isinstance(exc, (ValueError, KeyError)):
            return "permanent", "parse"
        return "transient", type(exc).__name__
    code = getattr(response, "status_code", 0)
    if code < 400:
        return "ok", str(code)
    if code in TRANSIENT_STATUS:
        return "transient", "429" if code == 429 else "5xx" if code >= 500 else str(code)
    return "permanent", str(code)


def retry_after(response, now: datetime | None = None) -> float | None:
    """Seconds the server asked us to wait (Retry-After: seconds or an HTTP date), if any."""
    value = (getattr(response, "headers", None) or {}).get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        when = timefields.external(value, source="http", field="Retry-After").utc
        if when is None:
            return None
        return max(0.0, (when - timefields.now_utc(now)).total_seconds())


def backoff_s(policy: Policy, n: int) -> float:
    """Full jitter: U(0, min(cap, base * 2^n))."""
    return rand() * min(policy.cap_s, policy.base_s * (2 ** n))


# ------------------------------------------------------------------------------------------------ state

SCHEMA = """
CREATE TABLE IF NOT EXISTS breakers (provider TEXT PRIMARY KEY, failures INTEGER NOT NULL, open_until TEXT);
CREATE TABLE IF NOT EXISTS attempts (
    provider TEXT NOT NULL, at TEXT NOT NULL, tries INTEGER NOT NULL, first_ok INTEGER NOT NULL,
    outcome TEXT NOT NULL,          -- ok | transient | permanent | breaker
    error_class TEXT, via_queue INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_attempts ON attempts(at, provider);
CREATE TABLE IF NOT EXISTS queue (
    kind TEXT NOT NULL, key TEXT NOT NULL, provider TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL,           -- PENDING | DONE | FAILED
    tries INTEGER NOT NULL, next_at TEXT NOT NULL, first_failed_at TEXT NOT NULL, deadline TEXT NOT NULL,
    last_class TEXT, resolved_at TEXT, PRIMARY KEY (kind, key)
);
"""


def db_path() -> Path:
    return Path(os.environ.get("CATALYST_RESILIENCE_DB", "/opt/catalyst/archive/resilience.sqlite3"))


def _conn() -> sqlite3.Connection | None:
    try:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Every collector writes its request accounting here, and several start in the same second (:10, :15).
        # Rollback-journal mode turned that into immediate "database is locked" errors that killed the collector;
        # WAL with a long busy timeout makes the writers queue instead.
        conn = sqlite3.connect(path, timeout=30)
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            print("resilience store is not in WAL mode: another connection holds it; writers will queue on timeout",
                  file=sys.stderr)
        conn.executescript(SCHEMA)
        fetch_runtime.ensure_schema(conn)
        conn.commit()
        return conn
    except (OSError, sqlite3.Error):
        return None  # state is best effort: a request never fails because its bookkeeping did


def _now() -> datetime:
    return timefields.now_utc()


def task(source_id, *, task_id=None, request_coverage="partial"):
    return fetch_runtime.task(source_id, task_id=task_id, request_coverage=request_coverage,
                              connect=_conn, clock=_now)


def _record(provider: str, tries: int, first_ok: bool, outcome: str, error_class: str | None, via_queue: bool = False):
    conn = _conn()
    if conn is None:
        return
    try:
        with conn:
            conn.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?)", (provider, timefields.stamp(_now()), tries,
                                                                         int(first_ok), outcome, error_class, int(via_queue)))
            if outcome == "ok" or outcome == "permanent":
                conn.execute("INSERT INTO breakers VALUES (?, 0, NULL) ON CONFLICT(provider) DO UPDATE SET failures = 0, "
                             "open_until = NULL", (provider,))
            elif outcome == "transient":
                k = POLICIES.get(provider, POLICIES["default"])
                row = conn.execute("SELECT failures FROM breakers WHERE provider = ?", (provider,)).fetchone()
                failures = (row[0] if row else 0) + 1
                until = timefields.stamp(_now() + timedelta(seconds=k.breaker_open_s)) if failures >= k.breaker_k else None
                conn.execute("INSERT INTO breakers VALUES (?,?,?) ON CONFLICT(provider) DO UPDATE SET failures = ?, "
                             "open_until = ?", (provider, failures, until, failures, until))
    finally:
        conn.close()


def breaker_open(provider: str) -> bool:
    """True while the provider's breaker is open. Once the time is up, one request is let through (half-open): its
    outcome closes the breaker or opens it again."""
    conn = _conn()
    if conn is None:
        return False
    try:
        row = conn.execute("SELECT open_until FROM breakers WHERE provider = ?", (provider,)).fetchone()
        if not row or not row[0]:
            return False
        if timefields.stored(row[0]) > _now():
            return True
        with conn:  # half-open: let this one through; a failure re-opens it at once (failures stays >= k)
            conn.execute("UPDATE breakers SET open_until = NULL WHERE provider = ?", (provider,))
        return False
    finally:
        conn.close()


# ----------------------------------------------------------------------------------------------- request

def request(provider: str, method: str, url: str, *, http: Callable[..., Any] | None = None,
            bucket=None, ok_status: tuple[int, ...] = (), via_queue: bool = False, **kwargs) -> Any:
    """``http(url, **kwargs)`` (default requests.get / requests.post) with the provider's retry policy. Returns the
    response; a status in ``ok_status`` (e.g. 404 meaning "no data") is returned, not raised. Raises
    PermanentError, TransientError or BreakerOpen."""
    policy = POLICIES.get(provider, POLICIES["default"])
    via_queue = via_queue or _IN_QUEUE
    accounted_request = fetch_runtime.begin_request()
    if breaker_open(provider):
        fetch_runtime.record_request(accounted_request, "skipped_breaker")
        _record(provider, 0, False, "breaker", "breaker_open", via_queue)
        raise BreakerOpen(provider, "breaker_open")
    call = http or (requests.post if method.upper() == "POST" else requests.get)
    waited, last = 0.0, ("transient", "unknown")
    for n in range(policy.attempts):
        if bucket is not None:
            bucket.acquire()
        response, exc = None, None
        try:
            response = call(url, **kwargs)
        except Exception as e:  # noqa: BLE001  (classified below)
            exc = e
        kind, cls = classify(response, exc)
        physical_ok = exc is None and response is not None and 200 <= getattr(response, "status_code", 0) < 400
        fetch_runtime.record_request(accounted_request, "success" if physical_ok else "failure")
        if kind == "ok" or (response is not None and getattr(response, "status_code", 0) in ok_status):
            _record(provider, n + 1, n == 0, "ok", None if n == 0 else last[1], via_queue)
            return response
        last = (kind, cls)
        if kind == "permanent":
            _record(provider, n + 1, False, "permanent", cls, via_queue)
            raise PermanentError(provider, cls, str(exc or getattr(response, "status_code", ""))[:120])
        if n + 1 >= policy.attempts:
            break
        wait = retry_after(response) if response is not None else None
        wait = backoff_s(policy, n) if wait is None else min(wait, policy.cap_s)
        if waited + wait > policy.budget_s:
            break
        if bucket is not None and cls == "429" and hasattr(bucket, "drain"):
            bucket.drain()
        sleep(wait)
        waited += wait
    _record(provider, n + 1, False, "transient", last[1], via_queue)
    raise TransientError(provider, last[1])


def get(provider: str, url: str, **kwargs) -> Any:
    return request(provider, "GET", url, **kwargs)


def post(provider: str, url: str, **kwargs) -> Any:
    return request(provider, "POST", url, **kwargs)


# ------------------------------------------------------------------------------------------------- queue

HANDLERS: dict[str, Callable[[dict], None]] = {}


def handler(kind: str):
    """Register the function that redoes one queued item of ``kind`` (raise to fail again)."""
    def wrap(fn):
        HANDLERS[kind] = fn
        return fn
    return wrap


def enqueue(kind: str, key: str, provider: str, payload: dict, *, error_class: str = "",
            deadline: datetime | None = None) -> None:
    """Queue work that failed transiently; retried after 15 min, 1 h and 6 h, or until ``deadline``."""
    conn = _conn()
    if conn is None:
        return
    now = _now()
    deadline = deadline or now + sum(QUEUE_DELAYS, timedelta()) + timedelta(hours=1)
    try:
        with conn:
            previous = conn.execute("SELECT first_failed_at FROM queue WHERE kind=? AND key=?", (kind, key)).fetchone()
            conn.execute("INSERT INTO queue VALUES (?,?,?,?, 'PENDING', 0, ?, ?, ?, ?, NULL) ON CONFLICT(kind, key) DO "
                         "UPDATE SET last_class = excluded.last_class WHERE status = 'PENDING'",
                         (kind, key, provider, json.dumps(payload, sort_keys=True), timefields.stamp(now + QUEUE_DELAYS[0]),
                          timefields.stamp(now), timefields.stamp(deadline), error_class))
            fetch_runtime.link_queue(conn, kind, key, previous[0] if previous else timefields.stamp(now),
                                     is_new=previous is None)
    finally:
        conn.close()


def run_due(now: datetime | None = None, log=print) -> dict:
    """Run every due PENDING item through its handler; reschedule, finish, or fail it for good."""
    global _IN_QUEUE
    now = timefields.now_utc(now)
    conn = _conn()
    if conn is None:
        return {"error": "no state"}
    counts = {"DONE": 0, "PENDING": 0, "FAILED": 0, "no_handler": 0}
    try:
        due = conn.execute("SELECT kind, key, provider, payload, tries, deadline, first_failed_at FROM queue WHERE status = 'PENDING' "
                           "AND next_at <= ? ORDER BY next_at", (timefields.stamp(now),)).fetchall()
        for kind, key, provider, payload, tries, deadline, first_failed_at in due:
            fn = HANDLERS.get(kind)
            if fn is None:
                counts["no_handler"] += 1
                continue
            owner = fetch_runtime.queue_owner(conn, kind, key, first_failed_at)
            conn.commit()
            context = task(owner[1], task_id=owner[0], request_coverage="complete") if owner else fetch_runtime.legacy_queue()
            with context as work:
                _IN_QUEUE = True
                try:
                    fn(json.loads(payload))
                    status, cls, nxt = "DONE", None, None
                except PermanentError as exc:
                    status, cls, nxt = "FAILED", exc.error_class, None
                except Exception as exc:  # noqa: BLE001
                    cls = getattr(exc, "error_class", type(exc).__name__)
                    n = tries + 1
                    nxt = now + QUEUE_DELAYS[n] if n < len(QUEUE_DELAYS) else None
                    status = "PENDING" if nxt is not None and nxt <= timefields.stored(deadline) else "FAILED"
                finally:
                    _IN_QUEUE = False
                with conn:
                    conn.execute("UPDATE queue SET status = ?, tries = tries + 1, next_at = COALESCE(?, next_at), "
                                 "last_class = COALESCE(?, last_class), resolved_at = ? WHERE kind = ? AND key = ?",
                                 (status, timefields.stamp(nxt) if nxt else None, cls,
                                  timefields.stamp(now) if status != "PENDING" else None, kind, key))
                    if work is not None:
                        work.finish("OK" if status == "DONE" else "ERROR")
                        work.persist_final_state(conn)
                if work is not None:
                    work.finalized = True
            counts[status] += 1
            if status == "FAILED":
                log(f"queue {kind} {key}: final failure ({cls})")
    finally:
        conn.close()
    return counts


def _legacy_report(days: int = 7, now: datetime | None = None) -> dict:
    """Legacy logical-call counters and separate queue state; no mixed completion ratio."""
    conn = _conn()
    if conn is None:
        return {}
    since = timefields.stamp(timefields.now_utc(now) - timedelta(days=days))
    try:
        out: dict[str, dict] = {}
        for provider, n, first, ok, breaker in conn.execute(
                "SELECT provider, COUNT(*), SUM(first_ok), SUM(outcome = 'ok'), SUM(outcome = 'breaker') FROM attempts "
                "WHERE at >= ? AND via_queue = 0 GROUP BY 1", (since,)):
            out[provider] = {"requests": n, "first_try_ok": round(first / n, 4), "ok_after_retries": round(ok / n, 4),
                             "skipped_by_breaker": breaker, "classes": {}}
        for provider, cls, k in conn.execute(
                "SELECT provider, error_class, COUNT(*) FROM attempts WHERE at >= ? AND error_class IS NOT NULL GROUP BY 1, 2",
                (since,)):
            out.setdefault(provider, {"classes": {}})["classes"][cls] = k
        for provider, status, k in conn.execute(
                "SELECT provider, status, COUNT(*) FROM queue WHERE first_failed_at >= ? GROUP BY 1, 2", (since,)):
            q = out.setdefault(provider, {"classes": {}}).setdefault("queue", {})
            q[status] = k
        return out
    finally:
        conn.close()


def report(days: int = 7, now: datetime | None = None) -> dict:
    """Independent request/task/body-target denominators; old counters are not comparable."""
    end = timefields.now_utc(now)
    if days <= 0:
        raise ValueError("report days must be positive")
    from catalyst_data.ingestion import sources
    expected = [s["id"] for s in sources.load()["sources"] if s["store"] != "daily"
                and s["status"] in sources.ACTIVE and s.get("timers")]
    conn = _conn()
    if conn is None:
        return {"schema_version": 2, "available": False, "error": "accounting_store_unavailable",
                "sources": {}, "legacy_comparable_to_v2": False}
    try:
        result = fetch_runtime.report(conn, start=end - timedelta(days=days), end=end,
                                      as_of=end, expected_sources=expected)
    finally:
        conn.close()
    result.update(schema_version=2, legacy=_legacy_report(days, now), legacy_comparable_to_v2=False)
    return result


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "report"
    if cmd == "retry":
        print(json.dumps(run_due()))
    else:
        print(json.dumps(report(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
