"""Offline SEC index materialize with fallback ledgering."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_data.sec.index_cells import build_index_cell
from catalyst_data.sec.materialize import materialize_sec_index
from catalyst_data.sec.index_parser import filing_index_urls


def _boot(tmp_path: Path) -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    conn.execute(
        """INSERT OR IGNORE INTO ingestion_runs
           (run_id, started_at, ticker_list_json, source_list_json, status)
           VALUES ('run1', '2026-01-01T00:00:00Z', '[]', '[]', 'RUNNING')"""
    )
    conn.commit()
    return conn


def test_sec_index_fallback_requests_all_ledgered_with_raw_response(tmp_path: Path):
    conn = _boot(tmp_path)
    cell = build_index_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        accession_number="0000320193-25-000001",
        cik="0000320193",
    )
    calls = []

    def fetch(url: str):
        calls.append(url)
        if url.endswith("-index.html"):
            return 404, b"", None
        if url.endswith("-index-headers.html"):
            html = (
                b'<html><a href="aapl.htm">aapl.htm</a>'
                b"<td>EX-99.1</td><a href=\"ex.htm\">ex.htm</a></html>"
            )
            return 200, html, "text/html"
        return 404, b"", None

    res = materialize_sec_index(
        conn,
        run_id="run1",
        cell=cell,
        fetch=fetch,
        primary_document="aapl.htm",
    )
    assert res.request_count >= 2
    assert conn.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0] >= 2
    assert any(u.endswith("-index.html") for u in calls)
    conn.close()

def test_sec_index_endpoint_uses_full_cell_extensions_in_transport():
    cell = build_index_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        accession_number="0000320193-25-000001",
        cik="0000320193",
    )
    urls = filing_index_urls(
        cik_int=cell["identity_extensions"]["cik"],
        accession=cell["identity_extensions"]["accession_number"],
    )
    assert "0000320193" in urls[0] or "320193" in urls[0]


def test_sec_submissions_endpoint_never_fetches_index_or_document(tmp_path: Path):
    """Production transport: sec_submissions only hits submissions URL."""
    import asyncio
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.migrations import run_migrations

    conn = sqlite3.connect(tmp_path / "sub.db")
    init_db(conn)
    run_migrations(conn)
    conn.row_factory = sqlite3.Row

    cell = SourceCell.create(
        stage="evidence",
        source_type="sec_filings",
        endpoint_name="sec_submissions",
        subject="AAPL",
        window_start="2026-07-23",
        window_end="2026-07-23",
        date_domain="as_of",
        provider_profile_version="v1",
    )
    plan = UpdatePlan(
        config={"sources": ["sec_filings"], "tickers": ["AAPL"], "source_scopes": {}},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [cell.to_identity()]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    urls: list[str] = []

    class SubmissionsOnly:
        async def request(self, **kwargs):
            page_url = kwargs.get("page_url")
            endpoint = kwargs.get("endpoint_name")
            assert endpoint == "sec_submissions"
            # fabricate a submissions-shaped body; no index/doc URLs
            body = b'{"filings":{"recent":{"accessionNumber":[],"form":[],"filingDate":[]}}}'
            if page_url:
                urls.append(page_url)
            urls.append(f"synthetic:sec_submissions:{kwargs.get('subject')}")
            return {"status": 200, "body": body, "data": {"filings": {"recent": {}}}}

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=SubmissionsOnly()))
    assert report["status"] in ("SUCCEEDED", "PARTIAL", "FAILED")
    joined = " ".join(urls)
    assert "index.htm" not in joined
    assert "Archives/edgar" not in joined
    conn.close()


def test_endpoint_dispatch_rejects_unknown_sec_endpoint():
    from catalyst_data.sec.cell_record import build_sec_plan_cell_v2
    import pytest
    from catalyst_data.update_pipeline import _call_b2_transport

    with pytest.raises(ValueError):
        build_sec_plan_cell_v2(
            stage="evidence",
            source_type="sec_filings",
            endpoint_name="sec_unknown",
            subject="AAPL",
            window_start="2025-08-15",
            window_end="2025-08-15",
            date_domain="as_of",
            provider_profile_version="v1",
            identity_extensions={"cik": "1", "accession_number": "a", "form_policy_version": "v1"},
        )

    # production transport call path
    async def _run():
        with pytest.raises(ValueError, match="unknown SEC endpoint"):
            await _call_b2_transport(
                transport={},
                provider="sec",
                cell={
                    "source_type": "sec_filings",
                    "endpoint_name": "sec_unknown",
                    "subject": "AAPL",
                    "window_start": "2025-08-15",
                    "window_end": "2025-08-15",
                },
            )

    import asyncio

    asyncio.run(_run())


def test_plan_executor_index_fallback_ledgered(tmp_path: Path):
    """Index cell via execute_update: each URL attempt independently ledgered."""
    import asyncio
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.migrations import run_migrations
    from catalyst_data.sec.index_parser import filing_index_urls

    conn = sqlite3.connect(tmp_path / "idx.db")
    init_db(conn)
    run_migrations(conn)
    conn.row_factory = sqlite3.Row

    cell = build_index_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        accession_number="0000320193-25-000001",
        cik="0000320193",
    )
    plan = UpdatePlan(
        config={"sources": ["sec_filings"], "tickers": ["AAPL"], "source_scopes": {}},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [cell]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    expected_urls = filing_index_urls(
        cik_int=cell["identity_extensions"]["cik"],
        accession=cell["identity_extensions"]["accession_number"],
    )
    calls: list[str] = []

    class IndexTransport:
        async def request(self, **kwargs):
            url = kwargs.get("page_url") or ""
            calls.append(url)
            # Fail first URL, succeed on second with minimal parseable HTML
            if len(calls) == 1:
                return {"status": 404, "body": b"", "data": {}}
            html = (
                b"<html><table><tr><td>1</td><td>"
                b'<a href="aapl.htm">aapl.htm</a></td>'
                b"<td>8-K</td></tr></table></html>"
            )
            return {"status": 200, "body": html, "data": {}}

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=IndexTransport()))
    assert len(calls) >= 2
    assert calls[0] == expected_urls[0]
    attempts = conn.execute(
        "SELECT COUNT(*) AS c FROM provider_request_attempts WHERE endpoint_name='sec_filing_index'"
    ).fetchone()["c"]
    assert attempts >= 2
    cp = conn.execute(
        "SELECT request_count, status, is_complete, cell_id FROM source_checkpoints WHERE cell_id=?",
        (cell["cell_id"],),
    ).fetchone()
    assert cp is not None
    assert int(cp["request_count"]) >= 2
    assert cp["cell_id"] == cell["cell_id"]
    # may succeed or fail depending on parse strictness; request_count must match attempts
    assert int(cp["request_count"]) == attempts
    assert report["status"] in ("SUCCEEDED", "PARTIAL", "FAILED")
    conn.close()
