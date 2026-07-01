from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from catalyst_data.config import (
    RAG_MIN_CHAR_COUNT,
    TARGET_LANGUAGE,
    TEMPLATE_SPAM_DUPLICATE_THRESHOLD,
)
from catalyst_data.storage.sqlite import init_db

_HEADER_RE = re.compile(r"^##\s+(?P<header>.+?)\s*$", re.MULTILINE)
_META_RE = re.compile(
    r"^\*Source:\s*(?P<source>.*?)\s*\|\s*(?P<published>.*?)\s*\|\s*Category:",
    re.MULTILINE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_STATUS_VALUES = ("pending", "success", "failed", "skipped")
_QUALITY_REASONS = (
    "short_text",
    "missing_fields",
    "non_target_language",
    "template_spam",
)

_QUALITY_TABLES_SQL = f"""
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id               TEXT PRIMARY KEY,
    started_at           TEXT NOT NULL,
    ended_at             TEXT,
    ticker_list_json     TEXT NOT NULL,
    source_list_json     TEXT NOT NULL,
    status               TEXT NOT NULL,
    success_count        INTEGER NOT NULL DEFAULT 0,
    fail_count           INTEGER NOT NULL DEFAULT 0,
    cost_usd             REAL,
    notes                TEXT
);

CREATE TABLE IF NOT EXISTS source_checkpoints (
    run_id               TEXT NOT NULL,
    source_type          TEXT NOT NULL,
    ticker               TEXT NOT NULL,
    date                 TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN { _STATUS_VALUES }),
    error_class          TEXT,
    retries              INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, source_type, ticker, date)
);

CREATE TABLE IF NOT EXISTS asset_quality_flags (
    asset_id             TEXT PRIMARY KEY,
    is_rag_eligible      INTEGER NOT NULL,
    quality_reason       TEXT CHECK (
        quality_reason IS NULL OR quality_reason IN { _QUALITY_REASONS }
    ),
    quality_score        REAL NOT NULL,
    evaluated_at         TEXT NOT NULL,
    FOREIGN KEY (asset_id) REFERENCES clean_assets(asset_id)
);
"""


@dataclass(frozen=True)
class CleanAssetRecord:
    asset_id: str
    ticker: str
    source_type: str
    reference_date: str
    content_md: str


@dataclass(frozen=True)
class AssetQualityEvaluation:
    asset_id: str
    is_rag_eligible: bool
    quality_reason: str | None
    quality_score: float
    evaluated_at: str


@dataclass(frozen=True)
class QualityAssessment:
    is_rag_eligible: bool
    quality_reason: str | None
    quality_score: float


@dataclass(frozen=True)
class MigrationReport:
    db_path: Path
    clean_assets_count: int
    asset_quality_flags_count: int
    eligible_count: int
    ingestion_runs_count: int
    source_checkpoints_count: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_header(content_md: str) -> str | None:
    match = _HEADER_RE.search(content_md or "")
    if match is None:
        return None
    return match.group("header").strip()


def extract_title(content_md: str) -> str | None:
    header = _extract_header(content_md)
    if not header:
        return None
    parts = header.split(": ", 1)
    title = parts[1] if len(parts) == 2 else header
    title = title.strip()
    return title or None


def extract_source_and_published(content_md: str) -> tuple[str | None, str | None]:
    match = _META_RE.search(content_md or "")
    if match is None:
        return None, None
    source = match.group("source").strip() or None
    published = match.group("published").strip() or None
    return source, published


def extract_body_markdown(content_md: str) -> str:
    if not content_md:
        return ""
    source_match = _META_RE.search(content_md)
    if source_match is not None:
        body = content_md[source_match.end():].strip()
    else:
        body = content_md.strip()
    body = body.split("\n## References\n", 1)[0].strip()
    return body


def detect_language(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return "unknown"
    if _CJK_RE.search(body):
        return "non_target_language"
    return "en"


def normalize_title(title: str | None) -> str | None:
    if not title:
        return None
    normalized = _NON_ALNUM_RE.sub(" ", title.lower()).strip()
    return normalized or None


def load_clean_assets(conn: sqlite3.Connection) -> list[CleanAssetRecord]:
    rows = conn.execute(
        """
        SELECT asset_id, ticker, source_type, reference_date, content_md
        FROM clean_assets
        ORDER BY asset_id
        """
    ).fetchall()
    return [CleanAssetRecord(*row) for row in rows]


def build_title_counts(records: list[CleanAssetRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        normalized = normalize_title(extract_title(record.content_md))
        if normalized is None:
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
    return counts


def assess_quality_fields(
    *,
    title: str | None,
    source: str | None,
    published_utc: str | None,
    body_md: str,
    title_counts: dict[str, int] | None = None,
) -> QualityAssessment:
    char_count = len(body_md)
    detected_language = detect_language(body_md)
    normalized_title = normalize_title(title)
    duplicate_count = (
        title_counts.get(normalized_title, 0)
        if normalized_title and title_counts is not None
        else 0
    )
    is_template_or_spam = (
        normalized_title is not None
        and duplicate_count >= TEMPLATE_SPAM_DUPLICATE_THRESHOLD
    )

    checks = {
        "min_char_count": char_count >= RAG_MIN_CHAR_COUNT,
        "title": bool(title),
        "published": bool(published_utc),
        "source": bool(source),
        "language": detected_language == TARGET_LANGUAGE,
        "template_spam": not is_template_or_spam,
    }

    if char_count == 0:
        quality_reason = "short_text"
    elif not (checks["title"] and checks["published"] and checks["source"]):
        quality_reason = "missing_fields"
    elif not checks["min_char_count"]:
        quality_reason = "short_text"
    elif not checks["language"]:
        quality_reason = "non_target_language"
    elif not checks["template_spam"]:
        quality_reason = "template_spam"
    else:
        quality_reason = None

    quality_score = round(sum(checks.values()) / len(checks), 3)
    return QualityAssessment(
        is_rag_eligible=quality_reason is None,
        quality_reason=quality_reason,
        quality_score=quality_score,
    )


def evaluate_asset_quality(
    record: CleanAssetRecord,
    title_counts: dict[str, int],
) -> AssetQualityEvaluation:
    title = extract_title(record.content_md)
    source, published = extract_source_and_published(record.content_md)
    body = extract_body_markdown(record.content_md)
    assessment = assess_quality_fields(
        title=title,
        source=source,
        published_utc=published,
        body_md=body,
        title_counts=title_counts,
    )
    return AssetQualityEvaluation(
        asset_id=record.asset_id,
        is_rag_eligible=assessment.is_rag_eligible,
        quality_reason=assessment.quality_reason,
        quality_score=assessment.quality_score,
        evaluated_at=_now_iso(),
    )


def ensure_ingestion_quality_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_QUALITY_TABLES_SQL)
    conn.commit()


def populate_asset_quality_flags(conn: sqlite3.Connection) -> int:
    records = load_clean_assets(conn)
    title_counts = build_title_counts(records)
    rows = [
        evaluate_asset_quality(record, title_counts)
        for record in records
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO asset_quality_flags
            (asset_id, is_rag_eligible, quality_reason, quality_score, evaluated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                row.asset_id,
                int(row.is_rag_eligible),
                row.quality_reason,
                row.quality_score,
                row.evaluated_at,
            )
            for row in rows
        ],
    )
    conn.commit()
    return len(rows)


def migrate_database(db_path: Path) -> MigrationReport:
    conn = sqlite3.connect(str(db_path))
    try:
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        populate_asset_quality_flags(conn)

        clean_assets_count = conn.execute(
            "SELECT COUNT(*) FROM clean_assets"
        ).fetchone()[0]
        asset_quality_flags_count = conn.execute(
            "SELECT COUNT(*) FROM asset_quality_flags"
        ).fetchone()[0]
        eligible_count = conn.execute(
            "SELECT COUNT(*) FROM asset_quality_flags WHERE is_rag_eligible = 1"
        ).fetchone()[0]
        ingestion_runs_count = conn.execute(
            "SELECT COUNT(*) FROM ingestion_runs"
        ).fetchone()[0]
        source_checkpoints_count = conn.execute(
            "SELECT COUNT(*) FROM source_checkpoints"
        ).fetchone()[0]
    finally:
        conn.close()

    return MigrationReport(
        db_path=db_path,
        clean_assets_count=clean_assets_count,
        asset_quality_flags_count=asset_quality_flags_count,
        eligible_count=eligible_count,
        ingestion_runs_count=ingestion_runs_count,
        source_checkpoints_count=source_checkpoints_count,
    )


def migrate_databases(db_paths: list[Path]) -> list[MigrationReport]:
    return [migrate_database(path) for path in db_paths]


# ---------------------------------------------------------------------------
# Ingestion Run & Checkpoint Write Helpers (Step 2)
# ---------------------------------------------------------------------------

import json
import uuid


def _run_timestamp() -> str:
    """Return a compact timestamp for run_id construction."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def open_ingestion_run(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    sources: list[str],
    mode: str = "update",
    notes: str | None = None,
) -> str:
    """Create a new ingestion_runs row with status='running'.

    Returns the generated run_id.
    """
    ts = _run_timestamp()
    short = uuid.uuid4().hex[:8]
    run_id = f"run_{ts}_{short}"

    full_notes = json.dumps({"mode": mode, **(json.loads(notes) if notes else {})})

    conn.execute(
        """INSERT INTO ingestion_runs
           (run_id, started_at, ticker_list_json, source_list_json,
            status, notes)
           VALUES (?, ?, ?, ?, 'running', ?)""",
        (
            run_id,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(tickers),
            json.dumps(sources),
            full_notes,
        ),
    )
    conn.commit()
    return run_id


def close_ingestion_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    success_count: int,
    fail_count: int,
    status: str = "completed",
) -> None:
    """Update an ingestion_runs row with final counts and ended_at."""
    conn.execute(
        """UPDATE ingestion_runs
           SET ended_at = ?, success_count = ?, fail_count = ?, status = ?
           WHERE run_id = ?""",
        (
            datetime.now(timezone.utc).isoformat(),
            success_count,
            fail_count,
            status,
            run_id,
        ),
    )
    conn.commit()


def write_source_checkpoint(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source_type: str,
    ticker: str,
    date: str,
    status: str,
    error_class: str | None = None,
    retries: int = 0,
) -> None:
    """INSERT OR REPLACE a source_checkpoints row.

    Idempotent — re-running the same cell overwrites the previous checkpoint
    (same PRIMARY KEY of run_id, source_type, ticker, date).
    """
    conn.execute(
        """INSERT OR REPLACE INTO source_checkpoints
           (run_id, source_type, ticker, date, status, error_class, retries)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (run_id, source_type, ticker, date, status, error_class, retries),
    )
    conn.commit()


def close_stale_runs(
    conn: sqlite3.Connection, *, max_age_hours: float = 24.0
) -> int:
    """Mark any ingestion_runs with status='running' older than max_age_hours
    as 'interrupted'.  Returns the count of runs marked."""
    cutoff = datetime.now(timezone.utc).isoformat()
    # Simple approach: all running runs older than max_age_hours
    # For correctness we compute cutoff as a datetime string and compare.
    from datetime import timedelta

    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    rows = conn.execute(
        """SELECT run_id FROM ingestion_runs
           WHERE status = 'running' AND started_at < ?""",
        (cutoff_dt.isoformat(),),
    ).fetchall()
    for (run_id,) in rows:
        conn.execute(
            "UPDATE ingestion_runs SET status = 'interrupted', ended_at = ? "
            "WHERE run_id = ?",
            (datetime.now(timezone.utc).isoformat(), run_id),
        )
    conn.commit()
    return len(rows)
