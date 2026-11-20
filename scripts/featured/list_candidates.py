#!/usr/bin/env python3
"""List COMPLETED regression runs for human featured-case selection.

The lister is strictly read-only. It excludes any run whose cited/primary
answer sources include Benzinga or CNBC, and any run whose ticker/session has
an SEC 8-K acceptance correction that changes the attributed trading session.

SEC correction match rule
-------------------------
The correction CSV has no ticker column, so the lister maps the run ticker to
its CIK through the SEC company-tickers file (``--sec-tickers``). A row matches
when the row CIK equals the ticker's CIK, the original and corrected acceptance
times map to different trading sessions, and the run's session date equals
either mapped session. Session mapping uses data-core's trading calendar,
including holidays and early closes.

The lister also matches cited accession numbers exactly, but only for a
correction that changes the trading session and when the run date equals either
the original or corrected session. Without a ticker map only these cited
accessions can be matched; the CLI prints a warning in that case.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from catalyst_data.trading_calendar import (
    CALENDAR_YEARS,
    CalendarCoverageError,
    calendar_trading_days,
    is_trading_day,
    session_close_utc,
)

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CORRECTIONS = REPO / "data/corrections/sec_acceptance_v1.csv"
DEFAULT_TICKERS = REPO / "data/m9/sec_company_tickers.json"
NEW_YORK = ZoneInfo("America/New_York")
_ACCESSION = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")


class CandidateError(ValueError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iter_runtime_dbs(paths: Iterable[str | Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        candidates: Iterable[Path]
        if path.is_dir():
            candidates = sorted(path.rglob("runtime.sqlite3"))
        else:
            candidates = [path]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(resolved)
    return found


def _is_placebo_db(path: Path, explicit: set[Path]) -> bool:
    if path.resolve() in explicit:
        return True
    return any("placebo" in part.casefold() for part in path.parts)


def _transaction(conn: sqlite3.Connection, statement: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return conn.execute(statement, params).fetchone()


def _load_snapshot(conn: sqlite3.Connection, run_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    row = _transaction(
        conn,
        "SELECT payload_json FROM run_artifacts WHERE run_id=? "
        "AND artifact_type='answer_snapshot' ORDER BY event_seq DESC, artifact_id DESC LIMIT 1",
        (run_id,),
    )
    if row is not None:
        try:
            snapshot = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            snapshot = None
        if isinstance(snapshot, dict):
            return snapshot, _load_trace(conn, run_id)
    row = _transaction(
        conn,
        "SELECT payload_json FROM run_events WHERE run_id=? AND event_type='answer.ready' "
        "ORDER BY seq DESC LIMIT 1",
        (run_id,),
    )
    if row is not None:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = None
        snapshot = payload.get("answer_snapshot") if isinstance(payload, dict) else None
        if isinstance(snapshot, dict):
            return snapshot, _load_trace(conn, run_id)
    return None, _load_trace(conn, run_id)


def _load_trace(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = _transaction(
        conn,
        "SELECT payload_json FROM run_artifacts WHERE run_id=? "
        "AND artifact_type='decision_trace' ORDER BY event_seq DESC, artifact_id DESC LIMIT 1",
        (run_id,),
    )
    if row is None:
        return None
    try:
        trace = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    return trace if isinstance(trace, dict) else None


def _manifest(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = _transaction(
        conn,
        "SELECT payload_json FROM run_artifacts WHERE run_id=? "
        "AND artifact_type='run_manifest' ORDER BY event_seq ASC LIMIT 1",
        (run_id,),
    )
    if row is None:
        return None
    try:
        manifest = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _session_date(conn: sqlite3.Connection, run_id: str, manifest: dict[str, Any] | None) -> str | None:
    temporal = manifest.get("temporal_identity") if isinstance(manifest, dict) else None
    value = temporal.get("session_date") if isinstance(temporal, dict) else None
    if isinstance(value, str):
        return value
    row = _transaction(
        conn,
        "SELECT payload_json FROM run_events WHERE run_id=? AND event_type='run.accepted' "
        "ORDER BY seq ASC LIMIT 1",
        (run_id,),
    )
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    value = payload.get("trade_date") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def _terminal_payload(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = _transaction(
        conn,
        "SELECT payload_json FROM run_events WHERE run_id=? AND event_type='run.completed' "
        "ORDER BY seq DESC LIMIT 1",
        (run_id,),
    )
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _outcome(snapshot: dict[str, Any] | None, terminal: dict[str, Any] | None) -> str | None:
    value = snapshot.get("outcome") if isinstance(snapshot, dict) else None
    if value in {"ANSWERED", "ABSTAINED"}:
        return value
    status = terminal.get("result_status") if isinstance(terminal, dict) else None
    if status in {"ABSTAIN", "ABSTAINED"}:
        return "ABSTAINED"
    if status in {"ANSWERED", "SUCCEEDED", "PARTIAL"}:
        return "ANSWERED"
    return None


def _source_texts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _source_texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _source_texts(item)


def _cited_sources(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return []
    selected: list[Any] = [
        snapshot.get("key_sentence"),
        snapshot.get("also_sentence"),
        snapshot.get("event_sentence"),
        snapshot.get("sources"),
        snapshot.get("primary"),
        snapshot.get("secondary"),
    ]
    abstention = snapshot.get("abstention")
    if isinstance(abstention, dict):
        possible = abstention.get("possible_explanation")
        if isinstance(possible, dict):
            selected.append(possible.get("quotes"))
        elif isinstance(possible, list):
            selected.append(possible)
    result: list[dict[str, Any]] = []
    for value in selected:
        for item in _source_texts(value):
            if "publisher" in item or "url" in item:
                result.append(item)
    return result


def _blocked_publisher_or_host(source: dict[str, Any]) -> bool:
    labels = " ".join(
        str(source.get(field) or "").casefold()
        for field in ("publisher", "publisher_family")
    )
    if "benzinga" in labels or "cnbc" in labels:
        return True
    raw_url = source.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return False
    candidate = raw_url.strip()
    lowered = candidate.casefold()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    elif "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").casefold().rstrip(".")
        path = parts.path
    except (UnicodeError, ValueError):
        host = ""
        path = ""
    if host and (host == "benzinga.com" or host.endswith(".benzinga.com")):
        return True
    if any(segment.casefold() == "benzinga" for segment in path.split("/")):
        return True
    if host and (host == "cnbc.com" or host.endswith(".cnbc.com")):
        return True
    if any(segment.casefold() == "cnbc" for segment in path.split("/")):
        return True
    if not host:
        if "//news.cnbc.com" in lowered or "//video.cnbc.com" in lowered:
            return True
        if "//user@" in lowered and "cnbc.com" in lowered:
            return True
        if "//m.benzinga.com" in lowered:
            return True
    return False


def _source_limits_enabled() -> bool:
    """The same switch the API uses (catalyst_app.public_projection.source_limits_enabled)."""
    try:
        from catalyst_app.public_projection import source_limits_enabled
    except ImportError:  # the lister can run without the API package
        return os.getenv("CATALYST_PUBLIC_SOURCE_LIMITS") == "1"
    return source_limits_enabled()


def _blocked_source(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    for source in _cited_sources(snapshot):
        if _blocked_publisher_or_host(source):
            return source
    return None


def _abstention_reason(snapshot: dict[str, Any] | None, trace: dict[str, Any] | None) -> str | None:
    if isinstance(snapshot, dict):
        abstention = snapshot.get("abstention")
        if isinstance(abstention, dict) and isinstance(abstention.get("reason_code"), str):
            return abstention["reason_code"]
    if isinstance(trace, dict):
        recorded = trace.get("abstention_reason")
        if isinstance(recorded, dict) and isinstance(recorded.get("reason_code"), str):
            return recorded["reason_code"]
    return None


def propose_category(
    *,
    outcome: str | None,
    snapshot: dict[str, Any] | None,
    trace: dict[str, Any] | None,
    placebo: bool,
) -> tuple[str, str]:
    if placebo:
        return "PLACEBO_DAY", "placebo run"
    basis = snapshot.get("basis") if isinstance(snapshot, dict) else None
    if outcome == "ANSWERED":
        mapping = {
            "REPORTED_ATTRIBUTION": "ANSWERED_REPORTED_ATTRIBUTION",
            "GROUP_DRIVER": "ANSWERED_GROUP_DRIVER",
            "CONFIRMED_EVENT_INFERRED_LINK": "ANSWERED_UNIQUE_COMPANY_EVENT",
            "EARNINGS_FILING": "ANSWERED_EARNINGS_FALLBACK",
        }
        if basis in mapping:
            return mapping[basis], f"basis={basis}"
        return "UNCLASSIFIED", "answered run without a recognized basis"
    if outcome == "ABSTAINED":
        if isinstance(snapshot, dict) and isinstance(snapshot.get("reported_reason"), dict):
            return "REPORTED_REASON", "reason text reported_reason"
        reason = _abstention_reason(snapshot, trace)
        if reason == "VERIFIER_REJECTED":
            return "ABSTAINED_VERIFIER_REJECTED", "abstention reason=VERIFIER_REJECTED"
        return "ABSTAINED_NO_CAUSE", f"abstention reason={reason or 'not_recorded'}"
    return "UNCLASSIFIED", "run outcome is unavailable"


def _normalize_cik(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not text.isdigit():
        return None
    return text.zfill(10)


def _load_ticker_ciks(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CandidateError(f"cannot read SEC company tickers: {exc}") from exc
    result: dict[str, str] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, dict):
                ticker = value.get("ticker")
                cik = _normalize_cik(value.get("cik_str", value.get("cik")))
                if isinstance(ticker, str) and cik:
                    result[ticker.strip().upper()] = cik
            elif isinstance(value, (str, int)):
                if str(key).isdigit():
                    ticker = str(value).strip().upper()
                    cik = _normalize_cik(key)
                else:
                    ticker = str(key).strip().upper()
                    cik = _normalize_cik(value)
                if ticker and cik:
                    result[ticker] = cik
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            ticker = item.get("ticker")
            cik = _normalize_cik(item.get("cik_str", item.get("cik")))
            if isinstance(ticker, str) and cik:
                result[ticker.strip().upper()] = cik
    return result


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def attributed_session_date(accepted_utc: str) -> str:
    """Map an acceptance instant to its product trading session."""
    accepted = _parse_instant(accepted_utc)
    et_day = accepted.astimezone(NEW_YORK).date()
    if is_trading_day(et_day.isoformat()):
        close = datetime.fromisoformat(
            session_close_utc(et_day.isoformat()).replace("Z", "+00:00")
        )
        if accepted <= close:
            return et_day.isoformat()

    first = et_day + timedelta(days=1)
    last_supported = date(CALENDAR_YEARS[1], 12, 31)
    if first > last_supported:
        raise CandidateError(
            f"acceptance date {et_day.isoformat()} is beyond trading-calendar coverage"
        )
    through = min(first + timedelta(days=10), last_supported)
    try:
        sessions = calendar_trading_days(first.isoformat(), through.isoformat())
    except CalendarCoverageError as exc:
        raise CandidateError(str(exc)) from exc
    if not sessions:
        raise CandidateError(
            f"no next trading session in calendar coverage after {et_day.isoformat()}"
        )
    return sessions[0]


class CorrectionIndex:
    def __init__(self, rows: list[dict[str, str]], ticker_ciks: dict[str, str]):
        by_cik: dict[str, list[str]] = {}
        for ticker, cik in ticker_ciks.items():
            by_cik.setdefault(cik, []).append(ticker)
        self.by_session: dict[tuple[str, str], dict[str, Any]] = {}
        self.by_accession: dict[str, dict[str, Any]] = {}
        for row in rows:
            if str(row.get("form") or "8-K").strip() != "8-K":
                continue
            accession = str(row.get("accession") or "").strip()
            cik = _normalize_cik(row.get("cik"))
            original_accepted = str(row.get("json_acceptance_utc") or "").strip()
            corrected_accepted = str(row.get("edgar_accepted_utc") or "").strip()
            if not accession or not cik or not original_accepted or not corrected_accepted:
                continue
            original_session = attributed_session_date(original_accepted)
            corrected_session = attributed_session_date(corrected_accepted)
            entry = {
                "accession": accession,
                "cik": cik,
                "original_session_date": original_session,
                "corrected_session_date": corrected_session,
            }
            if original_session == corrected_session:
                continue
            self.by_accession[accession] = entry
            for ticker in by_cik.get(cik, ()):
                for session_date in (original_session, corrected_session):
                    key = (ticker, session_date)
                    self.by_session.setdefault(key, entry)

    def match_run(
        self,
        *,
        ticker: str | None,
        session_date: str | None,
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        def matches_session(entry: dict[str, Any]) -> bool:
            return bool(
                session_date
                and session_date
                in {
                    entry["original_session_date"],
                    entry["corrected_session_date"],
                }
            )

        if session_date and isinstance(ticker, str):
            hit = self.by_session.get((ticker.strip().upper(), session_date))
            if hit is not None and matches_session(hit):
                return hit
        if isinstance(snapshot, dict):
            accessions = set(_ACCESSION.findall(json.dumps(snapshot, sort_keys=True)))
            fallback = snapshot.get("earnings_fallback")
            if isinstance(fallback, dict) and isinstance(fallback.get("accession"), str):
                accessions.add(fallback["accession"])
            for accession in sorted(accessions):
                hit = self.by_accession.get(accession)
                if hit is not None and matches_session(hit):
                    return hit
        return None


def _load_corrections(path: Path) -> tuple[list[dict[str, str]], str]:
    try:
        raw = path.read_bytes()
        rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CandidateError(f"cannot read SEC correction table: {exc}") from exc
    return rows, hashlib.sha256(raw).hexdigest()


def list_candidates(
    db_paths: Iterable[str | Path],
    *,
    corrections_path: str | Path = DEFAULT_CORRECTIONS,
    sec_tickers_path: str | Path | None = None,
    placebo_dbs: Iterable[str | Path] = (),
    include_excluded: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    corrections = Path(corrections_path).expanduser()
    tickers = Path(sec_tickers_path).expanduser() if sec_tickers_path is not None else None
    ticker_ciks = _load_ticker_ciks(tickers)
    rows, corrections_sha = _load_corrections(corrections)
    index = CorrectionIndex(rows, ticker_ciks)
    explicit_placebo = {Path(path).expanduser().resolve() for path in placebo_dbs}
    database_inputs = list(db_paths) + list(placebo_dbs)
    databases = _iter_runtime_dbs(database_inputs)
    summary: dict[str, Any] = {
        "databases": [str(path) for path in databases],
        "corrections_sha256": corrections_sha,
        "ticker_map_configured": bool(ticker_ciks),
        "excluded": {
            "blocked_source": 0,
            "sec_correction_changes_session": 0,
            "unreadable_db": 0,
        },
    }
    results: list[dict[str, Any]] = []
    for db_path in databases:
        try:
            conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            summary["excluded"]["unreadable_db"] += 1
            continue
        try:
            try:
                run_rows = conn.execute(
                    "SELECT run_id, ticker, created_at, updated_at FROM runs "
                    "WHERE lifecycle_status='COMPLETED' ORDER BY created_at, run_id"
                ).fetchall()
            except sqlite3.Error:
                summary["excluded"]["unreadable_db"] += 1
                continue
            for run in run_rows:
                run_id = str(run["run_id"])
                snapshot, trace = _load_snapshot(conn, run_id)
                manifest = _manifest(conn, run_id)
                session_date = _session_date(conn, run_id, manifest)
                terminal = _terminal_payload(conn, run_id)
                outcome = _outcome(snapshot, terminal)
                blocked = _blocked_source(snapshot)
                correction = index.match_run(
                    ticker=run["ticker"],
                    session_date=session_date,
                    snapshot=snapshot,
                )
                exclusion_reason = None
                if blocked is not None and _source_limits_enabled():
                    exclusion_reason = "blocked_source"
                    summary["excluded"]["blocked_source"] += 1
                elif correction is not None:
                    exclusion_reason = "sec_correction_changes_session"
                    summary["excluded"]["sec_correction_changes_session"] += 1
                placebo = _is_placebo_db(db_path, explicit_placebo)
                category, reason = propose_category(
                    outcome=outcome,
                    snapshot=snapshot,
                    trace=trace,
                    placebo=placebo,
                )
                row = {
                    "source_db": str(db_path),
                    "run_id": run_id,
                    "ticker": run["ticker"],
                    "session_date": session_date,
                    "created_at": run["created_at"],
                    "outcome": outcome,
                    "proposed_category": category,
                    "category_reason": reason,
                    "basis": snapshot.get("basis") if isinstance(snapshot, dict) else None,
                    "cascade_stage": snapshot.get("cascade_stage") if isinstance(snapshot, dict) else None,
                    "abstention_reason": _abstention_reason(snapshot, trace),
                    "placebo": placebo,
                }
                if exclusion_reason is not None:
                    row["exclusion_reason"] = exclusion_reason
                    if correction is not None and exclusion_reason == "sec_correction_changes_session":
                        row["correction_accession"] = correction["accession"]
                    if blocked is not None and exclusion_reason == "blocked_source":
                        row["blocked_publisher"] = blocked.get("publisher")
                        row["blocked_url"] = blocked.get("url")
                    if include_excluded:
                        results.append(row)
                    continue
                results.append(row)
        finally:
            conn.close()
    summary["included"] = len(results)
    summary["ticker_map_warning"] = (
        None
        if ticker_ciks
        else "no SEC company tickers supplied; only exact cited accessions can match the correction table"
    )
    return results, summary


def _write_output(rows: list[dict[str, Any]], *, fmt: str, out: str | None) -> None:
    if fmt == "csv":
        fields = [
            "source_db",
            "run_id",
            "ticker",
            "session_date",
            "created_at",
            "outcome",
            "proposed_category",
            "category_reason",
            "basis",
            "cascade_stage",
            "abstention_reason",
            "placebo",
        ]
        if any("exclusion_reason" in row for row in rows):
            fields.append("exclusion_reason")
        stream = open(out, "w", encoding="utf-8", newline="") if out else sys.stdout
        try:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        finally:
            if out:
                stream.close()
        return
    text = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runtime_dbs", nargs="+", help="runtime.sqlite3 files or directories")
    parser.add_argument("--corrections", default=str(DEFAULT_CORRECTIONS))
    parser.add_argument(
        "--sec-tickers",
        default=os.getenv("CATALYST_SEC_COMPANY_TICKERS", str(DEFAULT_TICKERS)),
        help="SEC company_tickers.json or a ticker->CIK map",
    )
    parser.add_argument("--placebo-db", action="append", default=[], help="runtime DB whose runs are placebo days")
    parser.add_argument("--format", choices=("csv", "json"), default=None)
    parser.add_argument("--out", default=None, help="write output to this file instead of stdout")
    parser.add_argument("--include-excluded", action="store_true", help="include rows with an exclusion_reason")
    args = parser.parse_args(argv)
    fmt = args.format or ("csv" if args.out and args.out.lower().endswith(".csv") else "json")
    try:
        rows, summary = list_candidates(
            args.runtime_dbs,
            corrections_path=args.corrections,
            sec_tickers_path=args.sec_tickers if Path(args.sec_tickers).is_file() else None,
            placebo_dbs=args.placebo_db,
            include_excluded=args.include_excluded,
        )
    except CandidateError as exc:
        print(f"candidate listing failed: {exc}", file=sys.stderr)
        return 2
    _write_output(rows, fmt=fmt, out=args.out)
    if summary["ticker_map_warning"]:
        print(f"warning: {summary['ticker_map_warning']}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
