"""SEC intake stage for the daily update."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

import requests

from catalyst_data import trading_calendar
from catalyst_data.ingestion import coverage, ledger
from catalyst_data.ingestion.collectors import headlines, sec, sec_filings
from catalyst_data.ingestion.gaps import GapLedger
from catalyst_data.ingestion.ops import refresh
from catalyst_data.ingestion.ops.daily_contracts import (
    SEC_INTAKE_POLICY_VERSION,
    completed_sec_intake,
)
from catalyst_data.ingestion.storage import store

if TYPE_CHECKING:
    from catalyst_data.ingestion.ops.daily import Runtime


def stage_sec(rt: Runtime, ticker: str, job_id: str) -> tuple[str, dict]:
    user_agent = os.environ.get("SEC_USER_AGENT")
    done = completed_sec_intake(rt.paths.bodies_db)
    sessions = list(rt.job_sessions)
    todo = [d for d in sessions if (ticker, d, "sec") not in done]
    logical_task_id = f"{job_id}:sec:{ticker}"
    windows = {d: headlines.session_window(d) for d in sessions}

    def target_session(accepted_at: str | None) -> str | None:
        if not accepted_at:
            return None
        return next((session for session in sessions
                     if windows[session][0] < accepted_at <= windows[session][1]), None)

    if not todo:
        with store.refresh_lock(rt.paths.bodies_db):
            conn = store.open_writer(rt.paths.bodies_db)
            try:
                ledger.ensure_ledger(conn)
                gaps = GapLedger(conn)
                statuses = {}
                for session_date in sessions:
                    cutoff = trading_calendar.session_close_utc(session_date)
                    prior = conn.execute(
                        "SELECT status FROM intake_log WHERE subject=? AND session_date=? AND source='sec' "
                        "AND policy_version=? AND status IN ('OK','EMPTY') AND attempted_at>? "
                        "ORDER BY attempted_at DESC, attempt_id DESC LIMIT 1",
                        (ticker, session_date, SEC_INTAKE_POLICY_VERSION, cutoff),
                    ).fetchone()
                    if prior is None:
                        raise RuntimeError(f"completed SEC intake has no successful ledger row for {session_date}")
                    statuses[session_date] = prior[0]
                completed_count = sum(status in ("OK", "EMPTY") for status in statuses.values())
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                for session_date in sessions:
                    window_start, window_end = windows[session_date]
                    gaps.record(
                        session_date=session_date,
                        ticker=ticker,
                        source_id="daily:sec",
                        scope_kind="target",
                        scope_id=ticker,
                        status=statuses[session_date],
                        requested_from=window_start,
                        requested_until=window_end,
                        coverage_from=window_start,
                        coverage_until=window_end,
                        pagination_complete=True,
                        logical_task_id=logical_task_id,
                        progress_state="completed",
                        target_count=len(sessions),
                        completed_count=completed_count,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return "SKIPPED", {}
    http_get = rt.sec_get or requests.Session().get
    last_request = [0.0]

    def paced_get(url, **kwargs):
        for attempt in range(3):  # SEC drops some TLS connections; retry those briefly
            wait = sec.SEC_INTERVAL_S - (time.monotonic() - last_request[0])
            if wait > 0:
                time.sleep(wait)
            last_request[0] = time.monotonic()
            try:
                return http_get(url, **kwargs)
            except requests.ConnectionError:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))

    filings = []
    enumeration_complete = False
    disabled = not user_agent
    request_error = "SEC_USER_AGENT not set" if disabled else None
    cik = None
    if not disabled:
        try:
            cik = sec.cik_for(ticker)
        except Exception:
            request_error = "SEC CIK lookup failed"
        if cik is None and request_error is None:
            request_error = "SEC CIK mapping unavailable"
    if not request_error and cik is not None:
        first_start = min(window[0] for window in windows.values())
        last_cutoff = max(window[1] for window in windows.values())
        try:
            filings = sec_filings.candidates_for_window(
                cik, first_start, last_cutoff, http_get=paced_get, user_agent=user_agent
            )
            enumeration_complete = True
        except Exception:
            request_error = "SEC submissions pagination failed"

    by_session: dict[str, list[dict]] = {d: [] for d in sessions}
    candidate_by_session = {d: False for d in sessions}
    failed_by_session = {d: False for d in sessions}
    unknown_candidate_failure = False
    for filing in filings:
        json_accepted_at = filing.get("json_accepted_at") or filing["accepted_at"]
        json_session = target_session(json_accepted_at)
        try:
            rows, failed = sec.rows_for_filing(
                cik, ticker, filing, http_get=paced_get, user_agent=user_agent, now=rt.now,
                window=(first_start, last_cutoff), cache_dir=sec_filings.cache_dir(),
            )
        except Exception:
            rows, failed = [], True
        accepted_session = target_session(filing.get("accepted_at"))
        if failed and "time_flag" not in filing:
            # Index lookup did not establish the official Accepted time.
            # The provisional JSON time cannot identify the affected session.
            for session_date in sessions:
                candidate_by_session[session_date] = True
                failed_by_session[session_date] = True
        elif accepted_session is not None:
            candidate_by_session[accepted_session] = True
            by_session[accepted_session].extend(rows)
            failed_by_session[accepted_session] |= failed
            if failed and json_session is not None and json_session != accepted_session:
                # Until the index-time candidate is fetched, do not turn its
                # provisional JSON bucket into a permanent empty result.
                candidate_by_session[json_session] = True
                failed_by_session[json_session] = True
        elif failed:
            # Without an index-page time there is no proven skew bound, so the
            # provisional JSON time cannot safely select a single session.
            targets = sessions
            if not targets:
                unknown_candidate_failure = True
            for session_date in targets:
                candidate_by_session[session_date] = True
                failed_by_session[session_date] = True

    if any((ticker, session_date, "sec") in done and candidate_by_session[session_date]
           for session_date in sessions):
        # Reopening a terminal progress row needs a fresh task identity. Attempts
        # themselves stay append-only in intake_log.
        logical_task_id = f"{logical_task_id}:reopen:{uuid4().hex}"

    statuses: dict[str, str] = {}
    attempted_sessions: set[str] = set(todo)
    completed_before: dict[str, str] = {}
    with store.refresh_lock(rt.paths.bodies_db):
        conn = store.open_writer(rt.paths.bodies_db)
        try:
            ledger.ensure_ledger(conn)
            gaps = GapLedger(conn)
            coverage.ensure_schema(conn)
            conn.commit()

            for session_date in sessions:
                if (ticker, session_date, "sec") not in done:
                    continue
                cutoff = trading_calendar.session_close_utc(session_date)
                prior = conn.execute(
                    "SELECT status FROM intake_log WHERE subject=? AND session_date=? AND source='sec' "
                    "AND policy_version=? AND status IN ('OK','EMPTY') AND attempted_at>? "
                    "ORDER BY attempted_at DESC, attempt_id DESC LIMIT 1",
                    (ticker, session_date, SEC_INTAKE_POLICY_VERSION, cutoff),
                ).fetchone()
                if prior is None:
                    raise RuntimeError(f"completed SEC intake has no successful ledger row for {session_date}")
                completed_before[session_date] = prior[0]

            failed_sessions = []
            stored = 0
            store_reports = []
            conn.execute("BEGIN IMMEDIATE")
            for session_date in sessions:
                rows = by_session[session_date]
                if rows:
                    store_reports.append(store.write_assets(conn, rows, commit=False))
                    stored += len(rows)

                if (ticker, session_date, "sec") in done:
                    status = completed_before[session_date]
                    if candidate_by_session[session_date]:
                        attempted_sessions.add(session_date)
                        if failed_by_session[session_date]:
                            status = "ERROR"
                        elif rows:
                            status = "OK"
                    statuses[session_date] = status
                    if failed_by_session[session_date]:
                        failed_sessions.append(session_date)
                    continue

                status = "NOT_FETCHED" if disabled else (
                    "ERROR" if request_error or failed_by_session[session_date] or unknown_candidate_failure
                    else ("OK" if rows else "EMPTY")
                )
                statuses[session_date] = status
                if status in ("ERROR", "NOT_FETCHED"):
                    failed_sessions.append(session_date)

            completed_count = sum(status in ("OK", "EMPTY") for status in statuses.values())
            for session_date in sorted(attempted_sessions, key=sessions.index):
                status = statuses[session_date]
                ledger.record_attempt(
                    conn, subject=ticker, session_date=session_date, source="sec",
                    status="DISABLED" if status == "NOT_FETCHED" else status,
                    headlines=len(by_session[session_date]), bodies=len(by_session[session_date]),
                    attempted_at=refresh._attempt_time(rt.now),
                    policy_version=SEC_INTAKE_POLICY_VERSION, commit=False,
                )
                window_start, window_end = windows[session_date]
                attempt = coverage.start_attempt(
                    conn, logical_task_id=logical_task_id, attempt_id=uuid4().hex,
                    source_id="daily:sec", product="full_text", subject_kind="ticker",
                    subject_id=ticker, query_spec={}, coverage_policy_version=SEC_INTAKE_POLICY_VERSION,
                    requested_from=window_start, requested_until=window_end,
                )
                # A complete empty session has no in-window filings. For a
                # nonempty session, extracted URLs are only a lower bound on
                # the selected attachment set until that set is recorded
                # independently of successful extraction.
                is_complete = enumeration_complete and status == "EMPTY" and not candidate_by_session[session_date]
                targets = ([row["url"] for row in by_session[session_date]]
                           if status in ("OK", "EMPTY") else None)
                coverage.record_coverage(
                    conn, coverage_id=uuid4().hex, attempt_id=attempt.attempt_id,
                    status=status, reason=("DISABLED" if status == "NOT_FETCHED" else
                                           "ERROR" if status == "ERROR" else None),
                    recorded_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    coverage_from=window_start if is_complete else None,
                    coverage_until=window_end if is_complete else None,
                    enumeration_complete=is_complete,
                    expected_targets=targets, acquired_targets=targets,
                )
            for session_date in sessions:
                status = statuses[session_date]
                window_start, window_end = windows[session_date]
                is_error = status in ("ERROR", "NOT_FETCHED")
                gaps.record(
                    session_date=session_date,
                    ticker=ticker,
                    source_id="daily:sec",
                    scope_kind="target",
                    scope_id=ticker,
                    status=status,
                    reason="DISABLED" if status == "NOT_FETCHED" else ("ERROR" if status == "ERROR" else None),
                    requested_from=window_start,
                    requested_until=window_end,
                    # Claim only the window fully enumerated, after every 8-K
                    # candidate's official index and required documents succeed.
                    coverage_from=(window_start if enumeration_complete and not is_error else None),
                    coverage_until=(window_end if enumeration_complete and not is_error else None),
                    pagination_complete=bool(enumeration_complete and not is_error),
                    logical_task_id=logical_task_id,
                    progress_state="retrying" if is_error else "completed",
                    target_count=len(sessions),
                    completed_count=completed_count,
                )
            conn.commit()
            for report in store_reports:
                store.notify_assets_stored(report)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    detail = {"sessions": len(todo), "filings": len(filings), "rows": stored, "failed_sessions": failed_sessions}
    if request_error:
        raise RuntimeError(request_error)
    if failed_sessions:
        raise RuntimeError(f"exhibit fetch failed for {len(failed_sessions)} session(s)")
    return ("OK" if stored else "EMPTY"), detail
