"""Which (ticker, trading day) pairs the data layer has prepared: the first version's whole query scope.

The daily job verifies one closed session at a time and records that session's movers. A pair is prepared when the
session passed the job's ``verify`` stage (OK or EMPTY, as ``daily._verified_sessions``) and the ticker is one of that
session's movers. Everything else is outside the first version: nothing is fetched on demand, so a consumer must not
offer it. Read-only: the job store is opened ``mode=ro`` and nothing here writes.

Prepared does not mean complete. The gap table (``gaps.GapLedger.public_rows``) says which categories of data are
missing for a prepared pair; this module only answers whether the pair is in scope.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from catalyst_data.storage.connect import connect_readonly


@dataclass(frozen=True)
class PreparedPair:
    ticker: str
    session_date: str
    return_pct: float


def _open(jobs_db: str | Path) -> sqlite3.Connection:
    path = Path(jobs_db)
    if not path.is_file():
        raise FileNotFoundError(f"update jobs store not found: {path}")
    return connect_readonly(path, timeout_ms=10_000, immutable=False)


def prepared_pairs(jobs_db: str | Path) -> list[PreparedPair]:
    """Every prepared pair, oldest session first, largest move first within a session."""
    conn = _open(jobs_db)
    try:
        return prepared_pairs_from_connection(conn)
    finally:
        conn.close()


def prepared_pairs_from_connection(conn: sqlite3.Connection) -> list[PreparedPair]:
    """Every prepared pair from an already-open jobs connection."""
    verified = {row[0] for row in conn.execute(
        "SELECT DISTINCT session_date FROM job_items WHERE stage='verify' AND status IN ('OK','EMPTY') "
        "AND session_date IS NOT NULL")}
    # A session can have been run by several jobs; the latest finished OK movers record is the one in force.
    latest: dict[str, str] = {}
    for session, result_json in conn.execute(
            "SELECT session_date, result_json FROM job_items WHERE stage='movers' AND status='OK' "
            "AND session_date IS NOT NULL AND result_json IS NOT NULL ORDER BY finished_at"):
        latest[session] = result_json
    pairs: list[PreparedPair] = []
    for session in sorted(verified & set(latest)):
        for mover in json.loads(latest[session]).get("movers", []):
            pairs.append(PreparedPair(str(mover["ticker"]), session, float(mover["return_pct"])))
    return sorted(pairs, key=lambda p: (p.session_date, -abs(p.return_pct), p.ticker))


def prepared_sessions(jobs_db: str | Path) -> list[str]:
    """The dates a date picker may offer: sessions with at least one prepared ticker."""
    return sorted({p.session_date for p in prepared_pairs(jobs_db)})


def prepared_tickers(jobs_db: str | Path, session_date: str) -> list[str]:
    return [p.ticker for p in prepared_pairs(jobs_db) if p.session_date == session_date]


def is_prepared(jobs_db: str | Path, ticker: str, session_date: str) -> bool:
    return any(p.ticker == ticker.upper() and p.session_date == session_date for p in prepared_pairs(jobs_db))
