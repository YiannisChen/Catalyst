"""Daily data update for the supported universe (T1).

One job brings a range of closed sessions up to the collection policy
(docs/plans/2026-10-08-t1-daily-data-update.md):

- prices: daily bars for every tracked symbol (one item per symbol);
- sec:    8-K exhibits for each universe ticker (one item per ticker: every
          advertised submissions archive and 8-K index is enumerated before
          sessions are marked complete);
- movers: per session, universe tickers with |close-to-close| >= 2.0%;
- news:   the existing per-session refresh for each mover (company headlines
          -> public bodies, Benzinga/Kitco discovery, peers, proxy, market),
          with company headlines read from the archived Finnhub index and the
          per-session Polygon feed instead of per-ticker live calls;
- verify: the session status of each mover, recorded with what is still missing;
- deferred: URLs whose host did not answer were queued (store.deferred_fetches);
          they are fetched again at the end of the job, and the job stays PARTIAL
          while any remain.

Every item is committed with its status before the next starts. A job resumes
where it stopped (RUNNING items left by a crash return to PENDING); FAILED items
are retried up to MAX_ATTEMPTS. A job is COMPLETED only when no item FAILED,
otherwise PARTIAL. Only one job runs at a time (lease in the job store).

  catalyst-daily plan --start 2025-11-03
  catalyst-daily run --start 2025-11-03 [--end ...] [--max-items N]
  catalyst-daily status [--job JOB_ID]
  catalyst-daily retry --job JOB_ID
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from catalyst_data import trading_calendar
from catalyst_data.ingestion import coverage, ledger, scope, session_news
from catalyst_data.ingestion.collectors import (
    fetch,
    headline_archive,
    headlines,
    market,
    sec,
    sec_filings,
)
from catalyst_data.ingestion.gaps import GapLedger
from catalyst_data.ingestion.ops import daily_config, daily_jobs, refresh, status
from catalyst_data.ingestion.ops.daily_contracts import (
    SEC_INTAKE_POLICY_VERSION,
    completed_sec_intake,
)
from catalyst_data.ingestion.ops.daily_polygon_feed import PolygonFeed
from catalyst_data.ingestion.storage import store
from catalyst_data.ingestion.time import timefields
from catalyst_data.read.daily_universe import (
    MOVER_MIN_ABS_RETURN_PCT,
    movers_for,
    universe,
)
from catalyst_data.retrieval.web_bodies import COMMODITY_TERMS
from catalyst_data.storage.connect import connect_readonly, make_readonly_factory
from catalyst_data.storage.runtime_paths import runtime_data_dir, runtime_store_path

# Persisted in plan_key; changing this identifier would orphan unfinished jobs.
POLICY_VERSION = "m9_daily_v1"
# Same threshold as the production observation policy's material_target_return_pct
# (packages/api/catalyst_app/runtime/manifest_factory.py); a test keeps them equal.
MAX_ATTEMPTS = 3
LEASE_SECONDS = daily_jobs.LEASE_SECONDS
BARS_LOOKBACK_DAYS = 120
PRICE_ATTEMPTS = 4
PRICE_RETRY_SLEEP_S = 2.0
# KVUE was acquired and has no price bars after 2026-10-24; avoid requesting it
# from the daily price collector while leaving target and peer manifests intact.
RETIRED_PRICE_SYMBOLS = frozenset({"KVUE"})
# ETA assumptions (seconds per item, low/high), reported with every plan.
SECONDS_PER_ITEM = {"prices": (0.5, 2.0), "sec": (1.0, 6.0), "movers": (0.1, 0.5),
                    "news": (60.0, 180.0), "verify": (0.2, 1.0)}
ESTIMATED_MOVERS_PER_SESSION = 22  # median over 273 sessions since 2025-09 (design doc)
JOB_SCHEMA = daily_jobs.JOB_SCHEMA


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Paths:
    market_db: Path
    bodies_db: Path
    peer_map: dict
    snapshot_db: Path | None
    jobs_db: Path
    archive_db: Path
    corpus_db: Path | None = None
    alpaca_news_db: Path | None = None
    corpus_fts_rebuild_enabled: bool = False
    corpus_vacuum_after_prune: bool = False
    pipeline_status_path: Path | None = None
    corpus_min_free_gb: float = 10.0
    peer_map_path: Path | None = None

    @classmethod
    def from_env(cls, repo: Path = headline_archive.REPO) -> "Paths":
        return cls(**daily_config.resolve_path_values(repo, os.environ))


def publish_paths_environment(paths: Paths, repo: Path = headline_archive.REPO) -> None:
    """Publish legacy environment settings explicitly before invoking env-based helpers."""
    daily_config.publish_paths_environment(paths, os.environ, repo)


def _optional_snapshot(repo: Path) -> Path | None:
    """The 16 GB M8 snapshot only duplicates 40 symbols the market store already holds.

    Use it when present (local parity with earlier runs); a host without it reads bars
    from the market store alone instead of failing every news item on open.
    """
    return daily_config.resolve_optional_snapshot(repo, os.environ)


def price_symbols(paths: Paths) -> list[str]:
    """Every symbol the market store already tracks, plus the universe, ETFs and peers."""
    with connect_readonly(paths.market_db, immutable=False, timeout_ms=10_000) as conn:
        tracked = {row[0] for row in conn.execute("SELECT DISTINCT symbol FROM ohlcv")}
    symbols = tracked | set(universe()) | set(market.ETFS) | set(headline_archive.subjects())
    return sorted(symbols - RETIRED_PRICE_SYMBOLS)


def sessions_between(start: str, end: str) -> list[str]:
    return trading_calendar.calendar_trading_days(start, end)


# --------------------------------------------------------------------------- plan

def _bars_present(conn, symbol: str, session_date: str) -> bool:
    return conn.execute("SELECT 1 FROM ohlcv WHERE symbol=? AND date=?", (symbol, session_date)).fetchone() is not None


def completed_intake(bodies_db: Path) -> set[tuple[str, str, str]]:
    """(subject, session, source) with a completed (OK/EMPTY) attempt after the session's close,
    the same rule as ledger.completed_attempt_after, read in one query."""
    if not Path(bodies_db).exists():
        return set()
    with connect_readonly(bodies_db, immutable=False, timeout_ms=10_000) as conn:
        try:
            rows = conn.execute(
                "SELECT subject, session_date, source, max(attempted_at) FROM intake_log "
                "WHERE policy_version=? AND status IN ('OK','EMPTY') GROUP BY 1,2,3",
                (ledger.LEDGER_POLICY_VERSION,)).fetchall()
        except sqlite3.OperationalError:
            return set()
    out = set()
    for subject, session_date, source, attempted in rows:
        try:
            if attempted > trading_calendar.session_close_utc(session_date):
                out.add((subject, session_date, source))
        except ValueError:  # not a trading session
            continue
    return out


def plan(paths: Paths, start: str, end: str | None = None, *, now: datetime | None = None) -> dict:
    """What a job over [start, end] would do. Reads the stores, writes nothing."""
    last = market.last_closed_session(now)
    end = min(end or last, last)
    sessions = sessions_between(start, end)
    tickers = universe()
    symbols = price_symbols(paths)
    with connect_readonly(paths.market_db, immutable=False, timeout_ms=10_000) as conn:
        bars_missing = [s for s in symbols if any(not _bars_present(conn, s, d) for d in sessions[-1:])]
        sessions_with_bars = [d for d in sessions if _bars_present(conn, "SPY", d)]
    done = completed_intake(paths.bodies_db)
    sec_done = completed_sec_intake(paths.bodies_db)
    sec_missing = sum(1 for ticker in tickers if any((ticker, d, "sec") not in sec_done for d in sessions))
    known_movers, news_missing = 0, 0
    for session_date in sessions_with_bars:
        movers = movers_for(paths.market_db, session_date, tickers)
        known_movers += len(movers)
        news_missing += sum(
            1 for row in movers
            if not ((row["ticker"], session_date, "finnhub+polygon") in done
                    and (row["ticker"], session_date, status.DISCOVERY_SOURCE) in done)
        )
    news_missing += ESTIMATED_MOVERS_PER_SESSION * (len(sessions) - len(sessions_with_bars))
    counts = {"prices": len(bars_missing), "sec": sec_missing, "movers": len(sessions),
              "news": news_missing, "verify": len(sessions)}
    low = sum(counts[k] * SECONDS_PER_ITEM[k][0] for k in counts)
    high = sum(counts[k] * SECONDS_PER_ITEM[k][1] for k in counts)
    key_source = {"policy": POLICY_VERSION, "start": start, "end": end,
                  "universe": hashlib.sha256(",".join(tickers).encode()).hexdigest()[:16],
                  "mover_threshold_pct": MOVER_MIN_ABS_RETURN_PCT}
    return {
        **key_source,
        "plan_key": hashlib.sha256(json.dumps(key_source, sort_keys=True).encode()).hexdigest(),
        "sessions": len(sessions),
        "first_session": sessions[0] if sessions else None,
        "last_session": sessions[-1] if sessions else None,
        "universe_size": len(tickers),
        "price_symbols": len(symbols),
        "known_movers": known_movers,
        "items_to_do": counts,
        "eta_seconds": [int(low), int(high)],
        "eta_assumptions": {"seconds_per_item": SECONDS_PER_ITEM,
                            "movers_per_unpriced_session": ESTIMATED_MOVERS_PER_SESSION},
        "planned_at": _now(),
    }


# --------------------------------------------------------------------------- job store

def open_jobs(path: Path) -> sqlite3.Connection:
    return daily_jobs.open_jobs(path)


def start(paths: Paths, the_plan: dict) -> str:
    """Create the job for a plan, or return the unfinished job with the same plan key."""
    conn = open_jobs(paths.jobs_db)
    try:
        row = conn.execute(
            "SELECT job_id FROM jobs WHERE plan_key=? AND status IN ('PENDING','RUNNING','PARTIAL') "
            "ORDER BY created_at DESC LIMIT 1", (the_plan["plan_key"],),
        ).fetchone()
        if row:
            return row[0]
        job_id = f"upd-{the_plan['last_session']}-{the_plan['plan_key'][:10]}"
        if conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone():
            job_id = f"{job_id}-{int(time.time())}"
        conn.execute("INSERT INTO jobs (job_id, plan_key, plan_json, status, created_at) VALUES (?,?,?,?,?)",
                     (job_id, the_plan["plan_key"], json.dumps(the_plan, sort_keys=True), "PENDING", _now()))
        seq = 0
        for symbol in price_symbols(paths):
            seq += 1
            _add_item(conn, job_id, seq, "prices", symbol, None)
        for ticker in universe():
            seq += 1
            _add_item(conn, job_id, seq, "sec", ticker, None)
        for session_date in sessions_between(the_plan["start"], the_plan["end"]):
            seq += 1
            _add_item(conn, job_id, seq * 1000, "movers", session_date, session_date)
        _add_item(conn, job_id, (seq + 1) * 1000, "deferred", "all", None)
        conn.commit()
        return job_id
    finally:
        conn.close()


def _add_item(conn, job_id: str, seq: int, stage: str, key: str, session_date: str | None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO job_items (job_id, seq, stage, item_key, session_date, status) "
        "VALUES (?,?,?,?,?,?)", (job_id, seq, stage, key, session_date, "PENDING"),
    )


def _owner_dead(holder: str) -> bool:
    """True only for a recognized legacy or tokenized holder on this host."""
    parts = holder.rsplit(":", 2)
    if len(parts) == 2:
        host, pid = parts
    elif len(parts) == 3 and len(parts[2]) == 32 and all(
        character in "0123456789abcdef" for character in parts[2].lower()
    ):
        host, pid, _token = parts
    else:
        return False
    if host != socket.gethostname() or not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _acquire_lease(conn, job_id: str, owner: str) -> bool:
    return daily_jobs.acquire_lease(
        conn, job_id, owner, now=_now(), lease_seconds=LEASE_SECONDS, owner_dead=_owner_dead
    )


def _execution_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"


def _renew(conn, job_id: str, owner: str) -> None:
    daily_jobs.renew_lease(conn, job_id, owner, lease_seconds=LEASE_SECONDS)


def retry_failed(paths: Paths, job_id: str) -> int:
    conn = open_jobs(paths.jobs_db)
    try:
        cursor = conn.execute(
            "UPDATE job_items SET status='PENDING', attempts=0, last_error=NULL WHERE job_id=? AND status='FAILED'",
            (job_id,),
        )
        if cursor.rowcount:
            conn.execute("UPDATE jobs SET status='PENDING', finished_at=NULL WHERE job_id=?", (job_id,))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def progress(paths: Paths, job_id: str | None = None) -> dict:
    conn = open_jobs(paths.jobs_db)
    try:
        if job_id is None:
            row = conn.execute("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
            if row is None:
                return {"job": None}
            job_id = row[0]
        job = conn.execute("SELECT job_id, status, created_at, started_at, finished_at, plan_json "
                           "FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        stages: dict[str, dict[str, int]] = {}
        for stage, item_status, count in conn.execute(
            "SELECT stage, status, count(*) FROM job_items WHERE job_id=? GROUP BY 1,2", (job_id,)
        ):
            stages.setdefault(stage, {})[item_status] = count
        failures = [
            {"stage": s, "item": k, "session": d, "attempts": a, "error": e}
            for s, k, d, a, e in conn.execute(
                "SELECT stage, item_key, session_date, attempts, last_error FROM job_items "
                "WHERE job_id=? AND status='FAILED' ORDER BY seq LIMIT 50", (job_id,))
        ]
        current = conn.execute(
            "SELECT stage, item_key, session_date FROM job_items WHERE job_id=? AND status='RUNNING' LIMIT 1",
            (job_id,)).fetchone()
        done = conn.execute(
            "SELECT max(session_date) FROM job_items WHERE job_id=? AND stage='verify' AND status IN ('OK','EMPTY')",
            (job_id,)).fetchone()[0]
        deferred = {}
        if Path(paths.bodies_db).exists():
            with connect_readonly(paths.bodies_db, immutable=False, timeout_ms=10_000) as bodies:
                try:
                    deferred = dict(bodies.execute(
                        "SELECT status, count(*) FROM deferred_fetches GROUP BY 1").fetchall())
                except sqlite3.OperationalError:
                    deferred = {}
        return {"deferred_fetches": deferred, "job_id": job[0], "status": job[1], "created_at": job[2], "started_at": job[3],
                "finished_at": job[4], "plan": json.loads(job[5]), "stages": stages,
                "current": current, "sessions_done_through": done, "failures": failures}
    finally:
        conn.close()


# --------------------------------------------------------------------------- headline sources


def archive_covers(conn: sqlite3.Connection, ticker: str, start: str, cutoff: str) -> bool:
    for day in {start[:10], cutoff[:10]}:
        row = conn.execute(
            "SELECT 1 FROM archive_windows WHERE symbol=? AND status IN ('OK','EMPTY','CAPPED') "
            "AND window_start<=? AND window_end>=? LIMIT 1", (ticker, day, day)).fetchone()
        if row is None:
            return False
    return True


def archived_selector(archive_conn: sqlite3.Connection, feed: PolygonFeed, session_date: str) -> Callable:
    """A drop-in for headlines.select_with_status that reads the archived Finnhub index and the
    session's Polygon feed; it falls back to live calls where the archive has no coverage."""

    def select(ticker: str, start: str, cutoff: str, limit: int) -> tuple[list[dict], str]:
        if archive_covers(archive_conn, ticker, start, cutoff):
            finn = [
                {"url": url, "title": title, "published": published, "provider": "finnhub"}
                for url, title, published in archive_conn.execute(
                    "SELECT url, headline, published_utc FROM finnhub_news WHERE symbol=? "
                    "AND published_utc>? AND published_utc<=?", (ticker, start, cutoff))
            ]
            finn_status = "OK" if finn else "EMPTY"
        else:  # not archived (e.g. a SIC peer): live Finnhub; Polygon still from the feed
            finn, finn_status = headlines.finnhub_headlines_with_status(ticker, start, cutoff)
        items, statuses = finn, [finn_status]
        if ticker not in COMMODITY_TERMS:
            polygon, polygon_status = feed.items(ticker, session_date)
            items, statuses = finn + polygon, statuses + [polygon_status]
        return headlines.pick(ticker, items, statuses, start, cutoff, limit)

    return select


# --------------------------------------------------------------------------- stages

@dataclass
class Runtime:
    paths: Paths
    job_sessions: list[str]
    now: datetime | None = None
    load_bars: Callable = market.load_bars
    sec_get: Callable | None = None
    refresh_deps: Callable[[str], refresh.Deps] | None = None
    log: Callable[[str], None] = lambda _: None
    archive_conn: sqlite3.Connection | None = field(default=None, repr=False)
    feed: PolygonFeed | None = field(default=None, repr=False)


def stage_prices(rt: Runtime, symbol: str) -> tuple[str, dict]:
    first, last = rt.job_sessions[0], rt.job_sessions[-1]
    with store.refresh_lock(rt.paths.market_db):
        conn = market.open_market_writer(rt.paths.market_db)
        try:
            since, have = conn.execute("SELECT min(date), max(date) FROM ohlcv WHERE symbol=?",
                                       (symbol,)).fetchone()
            lookback = (date.fromisoformat(first) - timedelta(days=BARS_LOOKBACK_DAYS)).isoformat()
            # History that starts after the lookback (a peer first stored for a later
            # window) is refetched from the lookback, not only extended forward.
            late_start = since is not None and since > (date.fromisoformat(lookback) + timedelta(days=7)).isoformat()
            begin = lookback if not have or have < lookback or late_start else (date.fromisoformat(have) - timedelta(days=5)).isoformat()
            if have and have >= last and not late_start:
                return "SKIPPED", {"through": have}
            count = 0
            for attempt in range(PRICE_ATTEMPTS):
                options = {"now": rt.now}
                if rt.load_bars is market.load_bars:
                    options["fallback_sessions"] = rt.job_sessions
                count = rt.load_bars(conn, [symbol], begin, last, **options).get(symbol, 0)
                if count:
                    break
                time.sleep(PRICE_RETRY_SLEEP_S * (attempt + 1))
        finally:
            conn.close()
    if not count and have:
        # A symbol with history that returns nothing is a fetch failure, not a quiet
        # symbol: fail the item so the job retries it instead of leaving a gap.
        raise RuntimeError(f"no bars returned after {PRICE_ATTEMPTS} attempts")
    return ("OK" if count else "EMPTY"), {"rows": count, "from": begin}


from catalyst_data.ingestion.ops.daily_stage_sec import stage_sec


def stage_movers(rt: Runtime, session_date: str) -> tuple[str, dict]:
    with connect_readonly(rt.paths.market_db, immutable=False, timeout_ms=10_000) as conn:
        if not _bars_present(conn, "SPY", session_date):
            raise RuntimeError("no market bars for the session")
    movers = movers_for(rt.paths.market_db, session_date, universe())
    return ("OK" if movers else "EMPTY"), {"movers": movers}


def _deps_for(rt: Runtime, session_date: str) -> refresh.Deps:
    if rt.refresh_deps is not None:
        return rt.refresh_deps(session_date)
    if rt.archive_conn is None:
        rt.archive_conn = headline_archive.open_store(rt.paths.archive_db)
        rt.feed = PolygonFeed(rt.archive_conn)
    # The archived selector reads rt.archive_conn, opened on this thread: selection must not run on workers.
    return refresh.Deps(select_with_status=archived_selector(rt.archive_conn, rt.feed, session_date),
                        now=rt.now, select_in_workers=False)


def stage_news(rt: Runtime, ticker: str, session_date: str) -> tuple[str, dict]:
    report = refresh.refresh_session(
        ticker, session_date, snapshot_db=rt.paths.snapshot_db, market_db=rt.paths.market_db,
        bodies_db=rt.paths.bodies_db, peer_map=rt.paths.peer_map, deps=_deps_for(rt, session_date),
    )
    detail = {"before": report.before, "after": report.after,
              "actions": [{k: a[k] for k in ("step", "subject", "status", "headlines", "bodies")}
                          for a in report.actions],
              "errors": report.errors}
    if "REFRESH_BUSY" in report.errors:
        raise RuntimeError("REFRESH_BUSY")
    if report.errors:
        raise RuntimeError("; ".join(report.errors)[:300])
    # A source that answered ERROR is only in the ledger; fail the item so it is retried.
    errored = [f"{a['step']}:{a['subject']}" for a in report.actions if a["status"] == "ERROR"]
    if errored:
        raise RuntimeError("source ERROR: " + ", ".join(errored)[:300])
    bodies = sum(a["bodies"] for a in report.actions)
    return ("OK" if bodies or not report.before else "EMPTY"), detail


def stage_verify(rt: Runtime, session_date: str, tickers: list[str]) -> tuple[str, dict]:
    gaps = {}
    for ticker in tickers:
        current = status.session_status(
            ticker, session_date, snapshot_db=rt.paths.snapshot_db, market_db=rt.paths.market_db,
            bodies_db=rt.paths.bodies_db, peer_map=rt.paths.peer_map, now=rt.now,
        )
        if current.missing:
            gaps[ticker] = list(current.missing)
    return ("OK" if not gaps else "EMPTY"), {"movers": len(tickers), "with_gaps": gaps}


def retry_deferred(paths: Paths, *, now: datetime | None = None) -> dict:
    """Fetch queued URLs whose host answers again; keep the rest queued.

    A link whose host has not answered on ``store.DEAD_AFTER_DAYS`` different days is dead: no more retries, and it
    no longer counts as pending (the daily summary lists it instead)."""
    from catalyst_data.ingestion.collectors import fetch

    done = no_body = pending = 0
    hosts_down: set[str] = set()
    relay_down: set[str] = set()  # hosts whose relay did not answer: not the site's fault, never charged a day
    newly_dead: list[dict] = []
    today = timefields.now_utc(now).strftime("%Y-%m-%d")
    with store.refresh_lock(paths.bodies_db):
        conn = store.open_writer(paths.bodies_db)
        try:
            store.ensure_deferred(conn)
            fetcher = fetch.Fetcher()
            queued = conn.execute(
                "SELECT url, ticker, session_date, title, published, host FROM deferred_fetches "
                "WHERE status='PENDING' ORDER BY host, session_date").fetchall()
            for url, ticker, session_date, title, published, host in queued:
                if host not in hosts_down:
                    item = {"url": url, "title": title, "published": published, "ticker": ticker}
                    try:
                        rows = refresh._company_rows(fetcher, [item], now)
                    except fetch.RelayUnavailable:
                        hosts_down.add(host)
                        relay_down.add(host)
                    except fetch.HostUnavailable:
                        hosts_down.add(host)
                    else:
                        store.write_assets(conn, rows)
                        conn.execute("UPDATE deferred_fetches SET status=?, attempts=attempts+1, resolved_at=? "
                                     "WHERE url=? AND ticker=?", ("DONE" if rows else "NO_BODY", _now(), url, ticker))
                        conn.commit()
                        done += bool(rows)
                        no_body += not rows
                        continue
                # The relay (our own Mac tunnel) was down: the link stays queued and costs it no day.
                if host in relay_down:
                    pending += 1
                    continue
                # The host did not answer (now, or on this run's earlier link): one more failed day for this link.
                if store.charge_failed_day(conn, url, ticker, today):
                    newly_dead.append({"host": host, "url": url, "ticker": ticker})
                else:
                    pending += 1
                conn.commit()
            dead = conn.execute("SELECT count(*) FROM deferred_fetches WHERE status='DEAD'").fetchone()[0]
        finally:
            conn.close()
    return {"fetched": done, "no_body": no_body, "still_pending": pending, "hosts_down": sorted(hosts_down),
            "relay_down": sorted(relay_down), "newly_dead": newly_dead, "dead_total": dead}


def stage_deferred(rt: Runtime) -> tuple[str, dict]:
    result = retry_deferred(rt.paths, now=rt.now)
    # A site that does not answer is charged a failed day and goes DEAD after
    # DEAD_AFTER_DAYS; only our own relay being down fails the stage.
    if result["still_pending"] and result["relay_down"]:
        raise RuntimeError(f"{result['still_pending']} deferred URL(s) still pending; "
                           f"relay unavailable for: {', '.join(result['relay_down'])}")
    return ("OK" if result["fetched"] else "EMPTY"), result


# --------------------------------------------------------------------------- runner

def run(paths: Paths, job_id: str, *, max_items: int | None = None, rt: Runtime | None = None,
        log: Callable[[str], None] = print, keep_lease: bool = False,
        held_lease_owner: str | None = None) -> dict:
    conn = open_jobs(paths.jobs_db)
    owner = held_lease_owner or _execution_owner()
    owns_lease = held_lease_owner is None
    stop_heartbeat = threading.Event()
    lease_lost = threading.Event()
    heartbeat = None
    heartbeat_started = False
    try:
        if keep_lease != (held_lease_owner is not None):
            raise ValueError("keep_lease requires an explicit held_lease_owner")
        if held_lease_owner is None and not _acquire_lease(conn, job_id, owner):
            raise RuntimeError("another update job holds the lease")
        if held_lease_owner is not None:
            _renew(conn, job_id, owner)
        else:
            heartbeat = threading.Thread(
                target=_lease_heartbeat,
                args=(paths, job_id, owner, stop_heartbeat, lease_lost), daemon=True,
            )
            heartbeat.start()
            heartbeat_started = True
        the_plan = json.loads(conn.execute("SELECT plan_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0])
        rt = rt or Runtime(paths=paths, job_sessions=sessions_between(the_plan["start"], the_plan["end"]), log=log)
        conn.execute("UPDATE job_items SET status='PENDING' WHERE job_id=? AND status='RUNNING'", (job_id,))
        conn.commit()
        processed = 0
        stage_timings = {}
        while max_items is None or processed < max_items:
            row = conn.execute(
                "SELECT stage, item_key, session_date, attempts FROM job_items WHERE job_id=? "
                "AND (status='PENDING' OR (status='FAILED' AND attempts<?)) "
                # failed items are retried after the pending work, not back to back
                "ORDER BY status='FAILED', seq LIMIT 1",
                (job_id, MAX_ATTEMPTS)).fetchone()
            if row is None:
                break
            stage, key, session_date, attempts = row
            conn.execute("UPDATE job_items SET status='RUNNING', attempts=?, started_at=? "
                         "WHERE job_id=? AND stage=? AND item_key=?", (attempts + 1, _now(), job_id, stage, key))
            conn.commit()
            started_at = time.monotonic()
            try:
                result, detail = _dispatch(rt, conn, job_id, stage, key, session_date)
                error = None
            except Exception as exc:  # recorded per item; the job continues
                result, detail, error = "FAILED", {}, f"{type(exc).__name__}: {exc}"[:500]
            if lease_lost.is_set():
                raise RuntimeError("daily job lease was lost")
            elapsed = time.monotonic() - started_at
            timing = stage_timings.setdefault(stage, {"count": 0, "total": 0.0, "max": 0.0, "failed": 0})
            timing["count"] += 1
            timing["total"] += elapsed
            timing["max"] = max(timing["max"], elapsed)
            timing["failed"] += result == "FAILED"
            conn.execute("UPDATE job_items SET status=?, last_error=?, finished_at=?, result_json=? "
                         "WHERE job_id=? AND stage=? AND item_key=?",
                         (result, error, _now(), json.dumps(detail, sort_keys=True), job_id, stage, key))
            _renew(conn, job_id, owner)
            conn.commit()
            processed += 1
            log(f"{stage} {key} {result}" + (f" ({error})" if error else "") + f" [{elapsed:.1f}s]")
        remaining = conn.execute(
            "SELECT count(*) FROM job_items WHERE job_id=? AND (status IN ('PENDING','RUNNING') "
            "OR (status='FAILED' AND attempts<?))", (job_id, MAX_ATTEMPTS)).fetchone()[0]
        failed = conn.execute("SELECT count(*) FROM job_items WHERE job_id=? AND status='FAILED'",
                              (job_id,)).fetchone()[0]
        final = "RUNNING" if remaining else ("PARTIAL" if failed else "COMPLETED")
        cursor = conn.execute(
            "UPDATE jobs SET status=?, finished_at=? WHERE job_id=? AND lease_owner=? "
            "AND lease_expires_at>?",
            ("PENDING" if final == "RUNNING" else final,
             None if final == "RUNNING" else _now(), job_id, owner, _now()),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise RuntimeError("daily job lease was lost")
        conn.commit()
        log("stage timing: " + " ".join(
            f"{stage}(n={timing['count']} total={timing['total']:.1f}s max={timing['max']:.1f}s failed={timing['failed']})"
            for stage, timing in stage_timings.items()
        ))
        summary = {"job_id": job_id, "status": "PENDING" if final == "RUNNING" else final,
                   "processed": processed, "remaining": remaining, "failed": failed,
                   "stage_seconds": {stage: round(timing["total"], 1) for stage, timing in stage_timings.items()}}
        if not keep_lease:
            try:
                _finalize_daily_summary(paths, summary, log)
                if lease_lost.is_set():
                    raise RuntimeError("daily job lease was lost")
            finally:
                stop_heartbeat.set()
        return summary
    finally:
        stop_heartbeat.set()
        if heartbeat_started:
            heartbeat.join(timeout=2)
        try:
            if owns_lease:
                if conn.in_transaction:
                    conn.rollback()
                conn.execute(
                    "UPDATE jobs SET lease_owner=NULL,lease_expires_at=NULL "
                    "WHERE job_id=? AND lease_owner=?",
                    (job_id, owner),
                )
                conn.commit()
        finally:
            conn.close()


def _dispatch(rt: Runtime, conn, job_id: str, stage: str, key: str, session_date: str | None):
    if stage == "prices":
        return stage_prices(rt, key)
    if stage == "sec":
        return stage_sec(rt, key, job_id)
    if stage == "movers":
        result, detail = stage_movers(rt, key)
        base = conn.execute("SELECT seq FROM job_items WHERE job_id=? AND stage='movers' AND item_key=?",
                            (job_id, key)).fetchone()[0]
        for offset, row in enumerate(detail["movers"], start=1):
            _add_item(conn, job_id, base + offset, "news", f"{key}:{row['ticker']}", key)
        _add_item(conn, job_id, base + 999, "verify", key, key)
        return result, detail
    if stage == "news":
        return stage_news(rt, key.split(":", 1)[1], session_date)
    if stage == "deferred":
        return stage_deferred(rt)
    if stage == "verify":
        movers = json.loads(conn.execute(
            "SELECT result_json FROM job_items WHERE job_id=? AND stage='movers' AND item_key=?",
            (job_id, key)).fetchone()[0])["movers"]
        return stage_verify(rt, key, [row["ticker"] for row in movers])
    raise ValueError(f"unknown stage {stage}")


def has_live_lease(paths: Paths, *, now: datetime | None = None) -> bool:
    """Read-only check for a lease another scheduled/manual invocation must respect."""
    if not paths.jobs_db.exists():
        return False
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        conn = connect_readonly(paths.jobs_db, immutable=False, timeout_ms=10_000)
        try:
            return any(not _owner_dead(owner) for owner, in conn.execute(
                "SELECT lease_owner FROM jobs WHERE lease_owner IS NOT NULL AND lease_expires_at>?",
                (stamp,)))
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return False


def _verified_sessions(paths: Paths) -> set[str]:
    if not paths.jobs_db.exists():
        return set()
    try:
        with connect_readonly(paths.jobs_db, immutable=False, timeout_ms=10_000) as conn:
            return {str(row[0]) for row in conn.execute(
                "SELECT DISTINCT session_date FROM job_items WHERE stage='verify' "
                "AND status IN ('OK','EMPTY') AND session_date IS NOT NULL")}
    except sqlite3.OperationalError:
        return set()


def _missing_sessions(paths: Paths, last_closed: str, limit: int) -> list[str]:
    first = (date.fromisoformat(last_closed) - timedelta(days=60)).isoformat()
    verified = _verified_sessions(paths)
    missing = [day for day in sessions_between(first, last_closed) if day not in verified]
    return missing[-max(1, min(limit, 5)):]


def _scheduled_log(message: str, log_path: Path) -> None:
    print(message, flush=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def _record_job_asset(jobs_db: Path, job_id: str, asset_id: str) -> None:
    conn = sqlite3.connect(jobs_db, timeout=30)
    try:
        conn.execute("INSERT OR IGNORE INTO job_assets VALUES (?,?)", (job_id, asset_id))
        conn.commit()
    finally:
        conn.close()


def _finish_one_shot_retry(paths: Paths, job_id: str, owner: str) -> None:
    """Close failed one-pass retries as PARTIAL instead of leaving a lease-free PENDING job."""
    conn = open_jobs(paths.jobs_db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _renew(conn, job_id, owner)
        conn.execute("UPDATE job_items SET attempts=? WHERE job_id=? AND status='FAILED'",
                     (MAX_ATTEMPTS, job_id))
        pending = conn.execute(
            "SELECT count(*) FROM job_items WHERE job_id=? AND status IN ('PENDING','RUNNING')",
            (job_id,)).fetchone()[0]
        failed = conn.execute("SELECT count(*) FROM job_items WHERE job_id=? AND status='FAILED'",
                              (job_id,)).fetchone()[0]
        if not pending and failed:
            cursor = conn.execute(
                "UPDATE jobs SET status='PARTIAL', finished_at=? WHERE job_id=? "
                "AND lease_owner=? AND lease_expires_at>?",
                (_now(), job_id, owner, _now()),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("daily job lease was lost")
        conn.commit()
    finally:
        conn.close()


def _lease_heartbeat(paths: Paths, job_id: str, owner: str, stop: threading.Event,
                     lost: threading.Event | None = None) -> None:
    interval = max(1.0, LEASE_SECONDS / 3)
    while not stop.wait(interval):
        conn = open_jobs(paths.jobs_db)
        try:
            try:
                _renew(conn, job_id, owner)
                conn.commit()
            except Exception:
                # Stop after lease loss or heartbeat failure; the caller fails closed.
                if lost is not None:
                    lost.set()
                return
        finally:
            conn.close()


def scheduled(paths: Paths | None = None, *, now: datetime | None = None,
              max_sessions: int = 5) -> dict:
    """Run the daily closed-session update and its bounded post-processing under one lease."""
    if paths is None:
        paths = Paths.from_env()
        publish_paths_environment(paths)
    repo = headline_archive.REPO
    log_path = runtime_data_dir(repo) / "daily_logs/scheduled.log"

    def emit(message: str) -> None:
        _scheduled_log(message, log_path)

    if has_live_lease(paths, now=now):
        summary = {"started": False, "reason": "live_lease",
                   "message": "another job holds a live lease"}
        emit(json.dumps(summary, sort_keys=True))
        return summary
    last_closed = market.last_closed_session(now)
    missing = _missing_sessions(paths, last_closed, max_sessions)
    if not missing:
        summary = {"started": False, "reason": "up_to_date", "last_closed_session": last_closed,
                   "status": "COMPLETED"}
        _finalize_daily_summary(paths, summary, emit)
        emit(json.dumps(summary, sort_keys=True))
        return summary

    the_plan = plan(paths, missing[0], missing[-1], now=now)
    job_id = start(paths, the_plan)
    owner = _execution_owner()
    conn = open_jobs(paths.jobs_db)
    lease_acquired = False
    post_run = contextlib.ExitStack()
    stop_heartbeat = threading.Event()
    lease_lost = threading.Event()
    heartbeat = None
    heartbeat_started = False
    try:
        try:
            if not _acquire_lease(conn, job_id, owner):
                summary = {"started": False, "reason": "live_lease",
                           "message": "another job holds a live lease"}
                emit(json.dumps(summary, sort_keys=True))
                return summary
            lease_acquired = True
            captured_assets = {row[0] for row in conn.execute(
                "SELECT asset_id FROM job_assets WHERE job_id=?", (job_id,))}
        finally:
            conn.close()

        results = []
        record_asset = lambda asset_id: _record_job_asset(paths.jobs_db, job_id, asset_id)
        heartbeat = threading.Thread(
            target=_lease_heartbeat,
            args=(paths, job_id, owner, stop_heartbeat, lease_lost), daemon=True,
        )
        heartbeat.start()
        heartbeat_started = True
        relay = fetch.relay_status()
        if relay["up"] is False:
            emit(f"relay unavailable ({relay['reason']}): Benzinga pages will be queued, not fetched, and no day is charged")
        market._clear_polygon_cache()
        with store.track_assets(captured_assets, record_asset):
            results.append(run(paths, job_id,
                               rt=Runtime(paths, sessions_between(the_plan["start"], the_plan["end"])),
                               log=emit, keep_lease=True, held_lease_owner=owner))
        post_run.enter_context(session_news.frozen_shared_images())
        api_key = daily_config.deepseek_api_key(os.environ)
        categorized = 0
        if not api_key:
            emit("warning: DEEPSEEK_API_KEY is unset; skipping news category classification")
        else:
            from catalyst_data.ingestion import categorize_llm
            for session in missing:
                for mover in movers_for(paths.market_db, session, universe()):
                    inputs = session_news.category_inputs(mover["ticker"], session,
                                                          bodies_db=paths.bodies_db,
                                                          archive_db=paths.archive_db)
                    categorized += len(categorize_llm.classify(inputs, api_key))

        failed = retry_failed(paths, job_id)
        if failed:
            with store.track_assets(captured_assets, record_asset):
                retry_result = run(paths, job_id,
                                   rt=Runtime(paths, sessions_between(the_plan["start"], the_plan["end"])),
                                   max_items=failed, log=emit, keep_lease=True,
                                   held_lease_owner=owner)
            _finish_one_shot_retry(paths, job_id, owner)
            if retry_result["status"] == "PENDING":
                retry_result["status"] = "PARTIAL"
            results.append(retry_result)

        from catalyst_data.ingestion import media
        if captured_assets:
            with store.refresh_lock(paths.bodies_db):
                media_conn = store.open_writer(paths.bodies_db)
                try:
                    media_result = media.backfill(media_conn, asset_ids=captured_assets)
                finally:
                    media_conn.close()
        else:
            media_result = {"checked": 0, "stored": 0, "missed": 0, "deferred": 0}
        headline_media_result = {"checked": 0, "stored": 0, "no_image": 0, "deferred": 0, "skipped": 0}
        remaining_headline_media = 400  # about 7 minutes at 1 request per second per host
        try:
            for session in missing:
                for mover in movers_for(paths.market_db, session, universe()):
                    if remaining_headline_media <= 0:
                        break
                    try:
                        result = media.headlines(
                            mover["ticker"], date=session, bodies_db=paths.bodies_db,
                            archive_db=paths.archive_db, limit=remaining_headline_media,
                        )
                    except Exception as exc:
                        emit(f"headline media fill deferred: {type(exc).__name__}")
                        continue
                    for key in headline_media_result:
                        headline_media_result[key] += result.get(key, 0)
                    remaining_headline_media -= result.get("checked", 0)
                if remaining_headline_media <= 0:
                    break
        except Exception as exc:
            # Headline images are enrichment; an image-cache issue cannot fail the daily update.
            emit(f"headline media stage skipped: {type(exc).__name__}")
        web_discovery_results = _scheduled_web_discovery(paths, missing, log=emit)
        if lease_lost.is_set():
            raise RuntimeError("daily job lease was lost")
        check_conn = open_jobs(paths.jobs_db)
        try:
            _renew(check_conn, job_id, owner)
            check_conn.commit()
        finally:
            check_conn.close()
        summary = {"started": True, "job_id": job_id, "sessions": missing,
                   "status": progress(paths, job_id)["status"], "runs": results,
                   "categorized": categorized, "media": media_result,
                   "headline_media": headline_media_result,
                   "web_discovery": web_discovery_results,
                   "relay": relay,
                   "retry_failed": failed}
        _finalize_daily_summary(paths, summary, emit)
        emit(json.dumps(summary, sort_keys=True))
        return summary
    except Exception as exc:
        summary = {"started": True, "job_id": job_id, "status": "error",
                   "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        emit(json.dumps(summary, sort_keys=True))
        raise
    finally:
        if lease_acquired:
            try:
                post_run.close()
            finally:
                try:
                    stop_heartbeat.set()
                    if heartbeat_started:
                        heartbeat.join(timeout=2)
                finally:
                    conn = open_jobs(paths.jobs_db)
                    try:
                        conn.execute(
                            "UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL "
                            "WHERE job_id=? AND lease_owner=?", (job_id, owner)
                        )
                        conn.commit()
                    finally:
                        conn.close()


def _build_ready_snapshot_best_effort(paths: Paths, log: Callable[[str], None]) -> dict:
    try:
        from catalyst_data.ingestion.ops.ready_snapshot import (
            build as build_ready_snapshot,
        )
        from catalyst_data.ingestion.ops.ready_snapshot import default_path
        output = default_path(paths.jobs_db)
        build_ready_snapshot(paths.jobs_db, output)
        result = {"status": "ok", "path": str(output)}
    except Exception as exc:
        result = {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if result["status"] == "failed":
        try:
            log(json.dumps({"ready_snapshot": result}, sort_keys=True))
        except Exception:
            pass
    return result


def _current_certified_snapshot_identity(conn: sqlite3.Connection) -> tuple[str, str]:
    from catalyst_data.ingestion.ops.daily_artifacts import (
        current_certified_snapshot_identity,
    )
    return current_certified_snapshot_identity(conn)

def _sha256_file(path: Path) -> str:
    from catalyst_data.ingestion.ops.daily_artifacts import sha256_file
    return sha256_file(path)

def _generation_payload_bytes(conn: sqlite3.Connection, build_id: str) -> int:
    from catalyst_data.ingestion.ops.daily_artifacts import generation_payload_bytes
    return generation_payload_bytes(conn, build_id)

def _build_corpus_fts_best_effort(paths: Paths, log: Callable[[str], None]) -> dict:
    from catalyst_data.ingestion.ops import daily_artifacts
    return daily_artifacts.build_corpus_fts_best_effort(
        paths,
        log,
        safe_log=_safe_daily_log,
        build_locked=_build_corpus_fts_locked,
    )

def _build_corpus_fts_locked(paths: Paths, log: Callable[[str], None]) -> dict:
    from catalyst_data.ingestion.ops import daily_artifacts
    return daily_artifacts.build_corpus_fts_locked(
        paths,
        log,
        current_identity=_current_certified_snapshot_identity,
        generation_bytes_for=_generation_payload_bytes,
        sha256=_sha256_file,
        safe_log=_safe_daily_log,
        disk_free_gb=_disk_free_gb,
        now=_now,
    )

def _safe_daily_log(log: Callable[[str], None], payload: dict) -> None:
    try:
        log(json.dumps(payload, sort_keys=True))
    except Exception:
        pass


def _disk_free_gb(path: Path) -> float | None:
    try:
        return round(shutil.disk_usage(path).free / 1_000_000_000, 3)
    except OSError:
        return None


def _write_daily_pipeline_status(paths: Paths, payload: dict) -> Path:
    target = paths.pipeline_status_path or paths.jobs_db.with_name("daily_pipeline_status.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return target


def _partial_job_has_no_pending_or_running_items(paths: Paths, job_id: str | None) -> bool:
    """Confirm a PARTIAL job is terminal before rebuilding its corpus."""
    if not job_id:
        return False
    conn = open_jobs(paths.jobs_db)
    try:
        row = conn.execute(
            "SELECT j.status, count(CASE WHEN i.status IN ('PENDING','RUNNING') THEN 1 END) "
            "FROM jobs AS j LEFT JOIN job_items AS i ON i.job_id=j.job_id "
            "WHERE j.job_id=? GROUP BY j.job_id", (job_id,)
        ).fetchone()
        return row is not None and row[0] == "PARTIAL" and row[1] == 0
    finally:
        conn.close()


def _finalize_daily_summary(paths: Paths, summary: dict, log: Callable[[str], None]) -> dict:
    try:
        ready = _build_ready_snapshot_best_effort(paths, log)
    except Exception as exc:
        ready = {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    previous_pipeline = {}
    status_path = paths.pipeline_status_path or paths.jobs_db.with_name("daily_pipeline_status.json")
    try:
        previous_pipeline = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(previous_pipeline, dict):
            previous_pipeline = {}
    except (OSError, json.JSONDecodeError):
        previous_pipeline = {}
    daily_status = summary.get("status")
    rebuild_after_partial = (
        daily_status == "PARTIAL"
        and _partial_job_has_no_pending_or_running_items(paths, summary.get("job_id"))
    )
    if daily_status == "COMPLETED" or rebuild_after_partial:
        try:
            corpus_fts_attempt = _build_corpus_fts_best_effort(paths, log)
        except Exception as exc:
            corpus_fts_attempt = {"status": "failed", "reason_code": "build_failed",
                                  "reason": f"{type(exc).__name__}: {str(exc)[:300]}"}
        corpus_fts_attempt["daily_status"] = daily_status
        if corpus_fts_attempt.get("status") == "skipped":
            corpus_fts = previous_pipeline.get("corpus_fts")
        else:
            corpus_fts = corpus_fts_attempt
    else:
        corpus_fts = previous_pipeline.get("corpus_fts")
        corpus_fts_attempt = {"status": "skipped", "reason_code": "daily_not_completed"}
    served_projection = {"status": "skipped", "reason_code": "not_configured"}
    projection_dir = os.getenv("CATALYST_SERVED_PROJECTION_DIR", "").strip()
    if corpus_fts_attempt.get("status") == "ok" and projection_dir and paths.corpus_db is not None:
        try:
            from catalyst_data.retrieval.served_projection import (
                open_or_build_served_projection,
            )

            Path(projection_dir).mkdir(parents=True, exist_ok=True)
            handle, reused = open_or_build_served_projection(
                make_readonly_factory(paths.corpus_db), projection_dir=projection_dir
            )
            served_projection = {
                "status": "ok", "build_id": handle.build_id,
                "path": str(handle.path), "reused": reused,
            }
        except Exception as exc:
            served_projection = {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    shared_images = {"status": "skipped", "reason_code": "not_attempted"}
    try:
        shared_path = runtime_data_dir() / "shared_images.json"
        session_news.write_shared_images_file(
            paths.archive_db, runtime_store_path("media_db"), shared_path
        )
        shared_images = {"status": "ok", "path": str(shared_path)}
    except Exception as exc:
        shared_images = {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    summary["ready_snapshot"] = ready
    if corpus_fts is not None:
        summary["corpus_fts"] = corpus_fts
    summary["corpus_fts_attempt"] = corpus_fts_attempt
    summary["shared_images"] = shared_images
    summary["served_projection"] = served_projection
    payload = {"updated_at": _now(), "job_id": summary.get("job_id"),
               "daily_status": summary.get("status"), "ready_snapshot": ready,
               "corpus_fts_attempt": corpus_fts_attempt}
    payload["shared_images"] = shared_images
    payload["served_projection"] = served_projection
    if corpus_fts is not None:
        payload["corpus_fts"] = corpus_fts
    try:
        _write_daily_pipeline_status(paths, payload)
    except Exception as exc:
        result = {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
        summary["pipeline_status_write"] = result
        try:
            log(json.dumps({"daily_pipeline_status": result}, sort_keys=True))
        except Exception:
            pass
    return summary


def _scheduled_web_discovery(paths: Paths, sessions: list[str], *, log: Callable[[str], None]) -> list[dict]:
    """Collect CNBC roundup pages for each session without blocking daily ingestion."""
    from catalyst_data.ingestion.collectors import cnbc_roundups

    results = []
    for session in sessions:
        try:
            results.append(cnbc_roundups.discover_day(session))
        except Exception as exc:
            # Enrichment cannot fail the daily market/news job.
            log(f"web discovery stage skipped for {session}: {type(exc).__name__}")
    return results


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalyst-daily", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--start", required=True)
        cmd.add_argument("--end")
        if name == "run":
            cmd.add_argument("--max-items", type=int)
    sub.add_parser("status").add_argument("--job")
    sub.add_parser("retry").add_argument("--job", required=True)
    sub.add_parser("deferred", help="fetch queued URLs whose host answers again")
    sub.add_parser("scheduled", help="update the latest missing closed sessions")
    args = parser.parse_args(argv)
    if args.command in ("run", "retry", "deferred"):
        import sys

        from catalyst_data.ingestion.input_manifest import require_backfill
        repo = headline_archive.REPO
        inputs, environment = daily_config.resolve_backfill_inputs(repo, os.environ)
        require_backfill(__file__, list(sys.argv[1:] if argv is None else argv), inputs=inputs,
                         environment=environment)
    from catalyst_data.ingestion.collectors import fetch

    fetch.prefer_ipv4()
    paths = Paths.from_env()
    publish_paths_environment(paths)
    if args.command == "plan":
        print(json.dumps(plan(paths, args.start, args.end), indent=1))
    elif args.command == "run":
        job_id = start(paths, plan(paths, args.start, args.end))
        print(json.dumps(run(paths, job_id, max_items=args.max_items,
                             log=lambda line: print(line, flush=True))))
    elif args.command == "status":
        print(json.dumps(progress(paths, args.job), indent=1))
    elif args.command == "deferred":
        print(json.dumps(retry_deferred(paths)))
    elif args.command == "scheduled":
        scheduled(paths)
    else:
        summary = {"reset": retry_failed(paths, args.job), "job_id": args.job,
                   "status": progress(paths, args.job)["status"]}
        _finalize_daily_summary(paths, summary, print)
        print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
