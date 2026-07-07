from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

from catalyst_data.connectors.base import FetchResult
from catalyst_data.quality import ensure_ingestion_quality_tables, write_source_checkpoint
from catalyst_data.run_report import RunConfig, RunReport
from catalyst_data.storage.sqlite import init_db
from catalyst_data.update_pipeline import request_cancel, run_update


def _make_db(path: Path, dates: list[str] | None = None) -> None:
    conn = sqlite3.connect(path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    for d in dates or ["2026-07-01"]:
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES ('AAPL', ?, 100)",
            (d,),
        )
    conn.commit()
    conn.close()


async def _sec_empty(ticker, endpoint, date):
    return FetchResult(
        status=200,
        data={"filings": {"recent": {
            "form": [], "filingDate": [], "accessionNumber": [],
            "reportDate": [], "primaryDocument": [], "items": [],
        }}},
        latency_ms=5.0,
        source_label="sec_submissions",
        items_count=0,
    )


def test_run_update_returns_report_and_persists_json(tmp_path: Path):
    db = tmp_path / "test.db"
    reports = tmp_path / "reports"
    _make_db(db)

    report = run_update(RunConfig(
        db_path=str(db),
        sources=["sec_filings"],
        from_date="2026-07-01",
        to_date="2026-07-01",
        fetch_fn={"sec_filings": _sec_empty},
        report_dir=str(reports),
        skip_doctor=True,
    ))

    assert isinstance(report, RunReport)
    persisted = Path(report.report_path)
    assert persisted.exists()
    data = json.loads(persisted.read_text())
    assert data["run_id"] == report.run_id

    conn = sqlite3.connect(db)
    stored_path = conn.execute(
        "SELECT report_path FROM ingestion_runs WHERE run_id = ?",
        (report.run_id,),
    ).fetchone()[0]
    conn.close()
    assert stored_path == report.report_path


def test_resume_reads_source_checkpoints_not_json(tmp_path: Path):
    db = tmp_path / "test.db"
    _make_db(db)
    conn = sqlite3.connect(db)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    old_run = "run_old"
    for i, status in enumerate(["failed", "skipped", "success", "success", "success"]):
        write_source_checkpoint(
            conn, run_id=old_run, source_type="sec_filings",
            ticker="AAPL", date=f"2026-07-0{i+1}", status=status,
        )
    conn.close()

    calls = []

    async def fake_sec(ticker, endpoint, date):
        calls.append((ticker, endpoint, date))
        return await _sec_empty(ticker, endpoint, date)

    report = run_update(RunConfig(
        db_path=str(db),
        resume_from=old_run,
        fetch_fn={"sec_filings": fake_sec},
        report_dir=str(tmp_path / "reports"),
        skip_doctor=True,
    ))

    assert report.run_id != old_run
    assert report.resume_from == old_run
    assert len(calls) == 2
    assert sorted(c[2] for c in calls) == ["2026-07-01", "2026-07-02"]


def test_fallback_timeout_to_finnhub_and_auth_no_fallback(tmp_path: Path):
    db = tmp_path / "test.db"
    _make_db(db, ["2026-07-01", "2026-07-02"])

    async def polygon_timeout(ticker, endpoint, date):
        raise TimeoutError("read timeout")

    async def finnhub_ok(ticker, endpoint, date):
        return FetchResult(
            status=200,
            data=[{"id": 1, "headline": "h", "summary": "s", "datetime": 1782864000,
                   "url": "https://example.com", "source": "Yahoo"}],
            latency_ms=8.0,
            source_label="finnhub_company_news",
            items_count=1,
        )

    report = run_update(RunConfig(
        db_path=str(db),
        sources=["polygon_news"],
        from_date="2026-07-01",
        to_date="2026-07-01",
        fetch_fn={"polygon_news": polygon_timeout, "finnhub_company_news": finnhub_ok},
        report_dir=str(tmp_path / "reports1"),
        skip_doctor=True,
    ))
    assert report.fallbacks["triggered"] == 1
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT source_type, status, raw_asset_id, fallback_provider, fallback_triggered "
        "FROM source_checkpoints WHERE run_id = ? ORDER BY source_type",
        (report.run_id,),
    ).fetchall()
    conn.close()
    assert any(r[0] == "polygon_news" and r[1] == "failed" and r[2] is None for r in rows)
    assert any(r[0] == "finnhub_company_news" and r[1] == "success" and r[4] == 1 for r in rows)

    async def polygon_auth(ticker, endpoint, date):
        return FetchResult(status=401, error="bad api_key=secret", source_label="polygon_news")

    report2 = run_update(RunConfig(
        db_path=str(db),
        sources=["polygon_news"],
        from_date="2026-07-02",
        to_date="2026-07-02",
        fetch_fn={"polygon_news": polygon_auth, "finnhub_company_news": finnhub_ok},
        report_dir=str(tmp_path / "reports2"),
        skip_doctor=True,
    ))
    assert report2.fallbacks["triggered"] == 0


def test_progress_and_cancellation(tmp_path: Path):
    db = tmp_path / "test.db"
    _make_db(db, ["2026-07-01", "2026-07-02"])
    seen_progress = []

    async def fake_sec(ticker, endpoint, date):
        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT run_id, current_source, current_ticker, "current_date", cells_done '
            "FROM ingestion_runs WHERE status='running' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        seen_progress.append(row)
        if len(seen_progress) == 1:
            request_cancel(row[0])
        return await _sec_empty(ticker, endpoint, date)

    report = run_update(RunConfig(
        db_path=str(db),
        sources=["sec_filings"],
        from_date="2026-07-01",
        to_date="2026-07-02",
        fetch_fn={"sec_filings": fake_sec},
        report_dir=str(tmp_path / "reports"),
        skip_doctor=True,
    ))

    assert seen_progress[0][1:4] == ("sec_filings", "AAPL", "2026-07-01")
    conn = sqlite3.connect(db)
    status, cells_done = conn.execute(
        "SELECT status, cells_done FROM ingestion_runs WHERE run_id = ?",
        (report.run_id,),
    ).fetchone()
    skipped = conn.execute(
        "SELECT COUNT(*) FROM source_checkpoints WHERE run_id = ? AND status='skipped'",
        (report.run_id,),
    ).fetchone()[0]
    conn.close()
    assert status == "canceled"
    assert cells_done == 1
    assert skipped == 1


def test_report_redacts_config_secrets(tmp_path: Path):
    db = tmp_path / "test.db"
    _make_db(db)
    report = run_update(RunConfig(
        db_path=str(db),
        sources=["sec_filings"],
        from_date="2026-07-01",
        to_date="2026-07-01",
        fetch_fn={"sec_filings": _sec_empty},
        notes="token=supersecret api_key=bad",
        report_dir=str(tmp_path / "reports"),
        skip_doctor=True,
    ))
    text = Path(report.report_path).read_text()
    assert "supersecret" not in text
    assert "api_key=bad" not in text
    assert "[REDACTED]" in text


def test_cli_update_news_wraps_run_update(tmp_path: Path, capsys):
    from catalyst_data.cli_index import cmd_update_news

    db = tmp_path / "test.db"
    _make_db(db)
    fake_report = RunReport(run_id="run_cli", mode="update", report_path="x")
    fake_report.providers = {"polygon_news": {"cells_success": 0, "cells_success_empty": 0, "cells_failed": 0}}

    with patch("catalyst_data.update_pipeline.run_update", return_value=fake_report) as mocked:
        cmd_update_news(
            str(db), from_date="2026-07-01", to_date="2026-07-01",
            tickers="AAPL", sources="polygon_news", limit=1,
            dry_run=True, live=False, confirm=False, json_output=True,
        )
    assert mocked.called
    cfg = mocked.call_args.args[0]
    assert cfg.tickers == ["AAPL"]
    assert cfg.sources == ["polygon_news"]
    assert cfg.dry_run is True
    assert '"run_id": "run_cli"' in capsys.readouterr().out
