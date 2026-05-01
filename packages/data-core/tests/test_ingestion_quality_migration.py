from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from catalyst_data import config
from catalyst_data.storage.sqlite import compute_asset_id, init_db, upsert_clean_asset, upsert_raw_asset


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    init_db(conn)
    return conn


def _seed_asset(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    date: str,
    source_type: str,
    content_md: str,
) -> str:
    asset_id = compute_asset_id(ticker, date, source_type)
    upsert_raw_asset(
        conn,
        asset_id=asset_id,
        ticker=ticker,
        source_type=source_type,
        reference_date=date,
        content_raw=b"{}",
        metadata={},
    )
    upsert_clean_asset(
        conn,
        asset_id=asset_id,
        ticker=ticker,
        source_type=source_type,
        reference_date=date,
        content_md=content_md,
    )
    return asset_id


def _news_markdown(title: str, body: str, *, source: str = "The Motley Fool", published: str = "2026-04-03T18:30:00Z") -> str:
    return (
        f"## AAPL: {title}\n"
        f"*Source: {source} | {published} | Category: news*\n\n"
        f"{body}"
    )


def test_config_exposes_quality_thresholds():
    assert getattr(config, "RAG_MIN_CHAR_COUNT", None) == 200
    assert getattr(config, "TARGET_LANGUAGE", None) == "en"
    assert getattr(config, "TEMPLATE_SPAM_DUPLICATE_THRESHOLD", None) == 5


def test_migration_script_backfills_flags_and_is_idempotent(tmp_path):
    repo_root = tmp_path / "repo"
    data_dir = repo_root / "data"
    data_dir.mkdir(parents=True)

    dev = data_dir / "catalyst_dev.db"
    eval_db = data_dir / "catalyst_eval_frozen.db"
    demo = data_dir / "catalyst_demo.db"

    dev_conn = _make_db(dev)
    _seed_asset(
        dev_conn,
        ticker="AAPL",
        date="2026-04-03",
        source_type="polygon_news",
        content_md=_news_markdown("Eligible title", "A" * 240),
    )
    _seed_asset(
        dev_conn,
        ticker="AAPL",
        date="2026-04-04",
        source_type="polygon_news",
        content_md="",
    )
    _seed_asset(
        dev_conn,
        ticker="AAPL",
        date="2026-04-05",
        source_type="fmp_fundamentals",
        content_md="## AAPL Fundamentals\n\n| Field | Value |\n|---|---|\n| revenue | 1 |",
    )
    dev_conn.close()

    _make_db(eval_db).close()
    _make_db(demo).close()

    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_ingestion_quality.py"
    assert script.is_file()

    cmd = [sys.executable, str(script), "--repo-root", str(repo_root)]
    first = subprocess.run(cmd, capture_output=True, text=True, check=False)
    second = subprocess.run(cmd, capture_output=True, text=True, check=False)

    assert first.returncode == 0, first.stderr or first.stdout
    assert second.returncode == 0, second.stderr or second.stdout
    assert first.stdout == second.stdout

    conn = sqlite3.connect(dev)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"ingestion_runs", "source_checkpoints", "asset_quality_flags"} <= tables

    flagged = conn.execute(
        "SELECT asset_id, is_rag_eligible, quality_reason FROM asset_quality_flags ORDER BY asset_id"
    ).fetchall()
    assert len(flagged) == 3
    assert sum(row[1] for row in flagged) == 1
    assert {row[2] for row in flagged if not row[1]} == {"missing_fields", "short_text"}
    conn.close()


def test_migration_marks_repeated_titles_as_template_spam(tmp_path):
    repo_root = tmp_path / "repo"
    data_dir = repo_root / "data"
    data_dir.mkdir(parents=True)

    dev = data_dir / "catalyst_dev.db"
    eval_db = data_dir / "catalyst_eval_frozen.db"
    demo = data_dir / "catalyst_demo.db"

    conn = _make_db(dev)
    for day in range(1, 6):
        _seed_asset(
            conn,
            ticker="AAPL",
            date=f"2026-04-0{day}",
            source_type="polygon_news",
            content_md=_news_markdown("Repeated title", "B" * 240, published=f"2026-04-0{day}T10:00:00Z"),
        )
    conn.close()
    _make_db(eval_db).close()
    _make_db(demo).close()

    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_ingestion_quality.py"
    assert script.is_file()

    result = subprocess.run(
        [sys.executable, str(script), "--repo-root", str(repo_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    conn = sqlite3.connect(dev)
    reasons = conn.execute(
        "SELECT DISTINCT quality_reason FROM asset_quality_flags"
    ).fetchall()
    assert reasons == [("template_spam",)]
    conn.close()
