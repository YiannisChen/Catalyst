"""B2 Task 5 & 7 — Polygon pagination and connector instrumentation tests."""
from __future__ import annotations

import hashlib
import sqlite3
import pytest


class TestPolygonPagination:
    """Polygon pagination with fake transport."""

    def _make_db(self):
        from conftest import _fresh_db_at_version, _seed_ingestion_run
        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id="run-paginate")
        return db

    def _make_two_page_transport(self):
        """Return an async fake transport: page 1 returns 2 items + cursor, page 2 empty."""
        calls = [0]
        class FakeResponse:
            status_code = 200
            def __init__(self, data):
                self._data = data
            def json(self):
                return self._data
        async def transport(provider, method, url, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return FakeResponse({
                    "results": [
                        {"id": "art1", "title": "A", "description": "d", "published_utc": "2026-01-01T00:00:00Z", "article_url": "https://x.com/1", "tickers": ["AAPL"], "publisher": {"name": "Test"}},
                        {"id": "art2", "title": "B", "description": "d", "published_utc": "2026-01-01T00:00:00Z", "article_url": "https://x.com/2", "tickers": ["AAPL"], "publisher": {"name": "Test"}},
                    ],
                    "next_url": "https://api.polygon.io/v2/reference/news?cursor=page2",
                })
            return FakeResponse({"results": [], "next_url": None})
        return transport

    def test_pagination_creates_request_attempts_per_page(self):
        """Two pages → two request_attempt rows."""
        from catalyst_data.connectors.polygon import fetch_paginated_news

        db = self._make_db()

        transport = self._make_two_page_transport()

        # Use asyncio to run
        import asyncio
        result = asyncio.run(fetch_paginated_news(
            db=db, run_id="run-paginate", ticker="AAPL", date="2026-01-01",
            transport=transport,
            page_limit=3, item_limit=100,
        ))

        attempts = db.execute(
            "SELECT page_no, status FROM provider_request_attempts WHERE run_id = ? ORDER BY page_no",
            ("run-paginate",),
        ).fetchall()

        pages = [a["page_no"] for a in attempts]
        assert 1 in pages
        assert 2 in pages
        db.close()

    def test_pagination_deduplicates_articles(self):
        """Overlapping articles across pages deduplicated by native ID."""
        from catalyst_data.connectors.polygon import fetch_paginated_news
        db = self._make_db()
        import asyncio

        async def transport(*args, **kwargs):
            class FakeResponse:
                status_code = 200
                def json(self):
                    return {
                        "results": [
                            {"id": "art1", "title": "A", "description": "d", "published_utc": "2026-01-01T00:00:00Z", "article_url": "https://x.com/1", "tickers": ["AAPL"], "publisher": {"name": "T"}},
                        ],
                        "next_url": None,
                    }
            return FakeResponse()

        result = asyncio.run(fetch_paginated_news(
            db=db, run_id="run-dedup", ticker="AAPL", date="2026-01-01",
            transport=transport,
            page_limit=3, item_limit=100,
        ))
        assert result["articles_upserted"] == 1
        db.close()

    def test_pagination_page_limit_partial(self):
        """page_limit stop → partial, not success."""
        from catalyst_data.connectors.polygon import fetch_paginated_news
        db = self._make_db()
        import asyncio

        call_count = [0]

        async def transport(*args, **kwargs):
            call_count[0] += 1
            class FakeResponse:
                status_code = 200
                def json(self):
                    return {
                        "results": [{"id": f"art{call_count[0]}", "title": "X", "description": "d", "published_utc": "2026-01-01T00:00:00Z", "article_url": "https://x.com/1", "tickers": ["AAPL"], "publisher": {"name": "T"}}],
                        "next_url": "https://api.polygon.io/v2/reference/news?cursor=next" if call_count[0] < 3 else None,
                    }
            return FakeResponse()

        result = asyncio.run(fetch_paginated_news(
            db=db, run_id="run-partial", ticker="AAPL", date="2026-01-01",
            transport=transport,
            page_limit=1, item_limit=100,
        ))

        cp = db.execute(
            "SELECT status, is_complete FROM source_checkpoints WHERE run_id = ?",
            ("run-partial",),
        ).fetchone()
        assert cp is not None
        assert cp["is_complete"] == 0
        db.close()

    def test_pagination_cursor_loop_detected(self):
        """Repeating cursor → error."""
        from catalyst_data.connectors.polygon import fetch_paginated_news
        db = self._make_db()
        import asyncio

        async def transport(*args, **kwargs):
            class FakeResponse:
                status_code = 200
                def json(self):
                    return {
                        "results": [{"id": "art1", "title": "X", "description": "d", "published_utc": "2026-01-01T00:00:00Z", "article_url": "https://x.com/1", "tickers": ["AAPL"], "publisher": {"name": "T"}}],
                        "next_url": "https://api.polygon.io/v2/reference/news?cursor=SAME_CURSOR",
                    }
            return FakeResponse()

        from catalyst_data.connectors.polygon import PolygonPaginationError
        with pytest.raises(PolygonPaginationError):
            asyncio.run(fetch_paginated_news(
                db=db, run_id="run-loop", ticker="AAPL", date="2026-01-01",
                transport=transport,
                page_limit=5, item_limit=100,
            ))
        db.close()

    def test_pagination_success_empty(self):
        """Empty valid response → success_empty, is_complete=1."""
        from catalyst_data.connectors.polygon import fetch_paginated_news
        db = self._make_db()
        import asyncio

        async def transport(*args, **kwargs):
            class FakeResponse:
                status_code = 200
                def json(self):
                    return {"results": [], "next_url": None}
            return FakeResponse()

        result = asyncio.run(fetch_paginated_news(
            db=db, run_id="run-empty", ticker="AAPL", date="2026-01-01",
            transport=transport,
            page_limit=5, item_limit=100,
        ))

        cp = db.execute(
            "SELECT status, is_complete FROM source_checkpoints WHERE run_id = ?",
            ("run-empty",),
        ).fetchone()
        assert cp["is_complete"] == 1
        db.close()

    def test_pagination_upserts_canonical_articles_and_full_provenance_chain(self):
        """Two Polygon articles create exact raw/canonical/provenance rows."""
        from catalyst_data.connectors.polygon import fetch_paginated_news
        db = self._make_db()
        import asyncio

        result = asyncio.run(fetch_paginated_news(
            db=db, run_id="run-paginate", ticker="AAPL", date="2026-01-01",
            transport=self._make_two_page_transport(),
            page_limit=3, item_limit=100,
        ))

        assert result["articles_upserted"] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM raw_assets WHERE data_version='v2'"
        ).fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM article_tickers").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM normalized_provenance").fetchone()[0] == 2
        db.close()

    def test_pagination_provenance_failure_is_not_swallowed(self, monkeypatch):
        """A provenance write failure fails deterministically instead of being swallowed."""
        import catalyst_data.connectors.polygon as polygon
        db = self._make_db()
        import asyncio

        def fail_record_provenance(*args, **kwargs):
            raise sqlite3.IntegrityError("forced provenance failure")

        monkeypatch.setattr(polygon, "record_provenance", fail_record_provenance)

        with pytest.raises(sqlite3.IntegrityError, match="forced provenance failure"):
            asyncio.run(polygon.fetch_paginated_news(
                db=db, run_id="run-paginate", ticker="AAPL", date="2026-01-01",
                transport=self._make_two_page_transport(),
                page_limit=3, item_limit=100,
            ))
        db.close()

# === B2-O-X: Pipeline-level pagination loop, window defense, cursor integration ===

import json
from pathlib import Path
from dataclasses import asdict


def _pipeline_db(tmp_path: Path):
    """Create a fresh DB suitable for execute_update pipeline tests."""
    db_path = tmp_path / "test.db"
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.migrations import run_migrations
    init_db(conn)
    run_migrations(conn)
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('legacy-test-run', '', '', '[]', '[]', 'PLANNED', 0, 0, 0, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    return db_path


class TestPaginationLoopPartial:
    """Pagination loop must write partial checkpoint, not crash the run."""

    def test_loop_writes_partial_checkpoint(self, tmp_path):
        from catalyst_data.manifests.universe import SourceCell
        from catalyst_data.update_pipeline import execute_update
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        import sqlite3, asyncio

        db_path = _pipeline_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        cell = SourceCell.create(
            "evidence", "polygon_news", "news", "AAPL",
            "2025-08-01", "2025-08-07", "calendar_days", "v1",
            page_cap=3, item_cap=50,
        )
        plan = UpdatePlan(
            config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
            universe={"tickers": ["AAPL"]},
            stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash

        calls = [0]

        class LoopTransport:
            async def request(self, **kwargs):
                calls[0] += 1
                return {
                    "status": 200,
                    "body": json.dumps({
                        "results": [{"id": f"art{calls[0]}", "title": "X",
                                     "description": "d",
                                     "published_utc": "2025-08-01T12:00:00Z",
                                     "article_url": f"https://x.com/{calls[0]}",
                                     "tickers": ["AAPL"],
                                     "publisher": {"name": "Test"}}],
                        "next_url": "https://api.polygon.io/v2/reference/news?cursor=SAME_LOOP",
                    }).encode(),
                }

        report = asyncio.run(execute_update(db=conn, plan=plan, transport=LoopTransport()))
        # Must not crash — run must complete
        assert report["status"] in ("PARTIAL", "FAILED"), f"unexpected: {report['status']}"

        cp = conn.execute(
            "SELECT status, error_class, is_complete, request_count, pages_received, items_received FROM source_checkpoints WHERE cell_id=?",
            (cell.cell_id,),
        ).fetchone()
        assert cp is not None, "checkpoint not written"
        assert cp["status"] == "partial", f"expected partial, got {cp['status']}"
        assert cp["error_class"] == "pagination_loop", f"expected pagination_loop, got {cp['error_class']}"
        assert cp["is_complete"] == 0, f"expected is_complete=0, got {cp['is_complete']}"
        assert cp["request_count"] >= 2, f"request_count={cp['request_count']}"
        assert cp["items_received"] >= 2, f"items_received={cp['items_received']}"
        conn.close()


class TestWindowDefense:
    """Items with out-of-window published_utc must be rejected; cell marked partial."""

    def test_mixed_in_and_out_of_window(self, tmp_path):
        from catalyst_data.manifests.universe import SourceCell
        from catalyst_data.update_pipeline import execute_update
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        import sqlite3, asyncio

        db_path = _pipeline_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        cell = SourceCell.create(
            "evidence", "polygon_news", "news", "AAPL",
            "2025-08-04", "2025-08-04", "calendar_days", "v1",
            page_cap=1, item_cap=50,
        )
        plan = UpdatePlan(
            config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
            universe={"tickers": ["AAPL"]},
            stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash

        class OutOfWindowTransport:
            async def request(self, **kwargs):
                return {
                    "status": 200,
                    "body": json.dumps({
                        "results": [
                            {"id": "in_window", "title": "OK",
                             "description": "d", "published_utc": "2025-08-04T12:00:00Z",
                             "article_url": "https://x.com/1", "tickers": ["AAPL"],
                             "publisher": {"name": "Test"}},
                            {"id": "out_window", "title": "BAD",
                             "description": "d", "published_utc": "2025-08-10T12:00:00Z",
                             "article_url": "https://x.com/2", "tickers": ["AAPL"],
                             "publisher": {"name": "Test"}},
                        ],
                    }).encode(),
                }

        report = asyncio.run(execute_update(db=conn, plan=plan, transport=OutOfWindowTransport()))

        cp = conn.execute(
            "SELECT status, error_class, is_complete FROM source_checkpoints WHERE cell_id=?",
            (cell.cell_id,),
        ).fetchone()
        assert cp is not None
        assert cp["status"] == "partial", f"expected partial, got {cp['status']}"
        assert cp["error_class"] == "response_out_of_window", f"unexpected: {cp['error_class']}"
        assert cp["is_complete"] == 0, f"expected is_complete=0, got {cp['is_complete']}"

        # Out-of-window article must NOT be written
        bad_count = conn.execute("SELECT COUNT(*) FROM articles WHERE title='BAD'").fetchone()[0]
        assert bad_count == 0, f"out-of-window article was written: {bad_count}"

        # In-window article must be written
        good_count = conn.execute("SELECT COUNT(*) FROM articles WHERE title='OK'").fetchone()[0]
        assert good_count == 1, f"in-window article not written: {good_count}"
        conn.close()

    def test_missing_published_utc_rejected(self, tmp_path):
        """Missing published_utc must be treated as invalid."""
        from catalyst_data.manifests.universe import SourceCell
        from catalyst_data.update_pipeline import execute_update
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        import sqlite3, asyncio

        db_path = _pipeline_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        cell = SourceCell.create(
            "evidence", "polygon_news", "news", "AAPL",
            "2025-08-04", "2025-08-04", "calendar_days", "v1",
            page_cap=1, item_cap=50,
        )
        plan = UpdatePlan(
            config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
            universe={"tickers": ["AAPL"]},
            stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash

        class MissingUtcTransport:
            async def request(self, **kwargs):
                return {
                    "status": 200,
                    "body": json.dumps({
                        "results": [
                            {"id": "no_utc", "title": "Missing",
                             "description": "d",
                             "article_url": "https://x.com/1", "tickers": ["AAPL"],
                             "publisher": {"name": "Test"}},
                        ],
                    }).encode(),
                }

        report = asyncio.run(execute_update(db=conn, plan=plan, transport=MissingUtcTransport()))

        cp = conn.execute(
            "SELECT status, error_class, is_complete FROM source_checkpoints WHERE cell_id=?",
            (cell.cell_id,),
        ).fetchone()
        assert cp is not None
        assert cp["status"] == "partial", f"expected partial, got {cp['status']}"
        assert cp["error_class"] == "response_out_of_window", f"unexpected: {cp['error_class']}"
        assert cp["is_complete"] == 0
        conn.close()

    def test_window_boundary_inclusive_exclusive(self, tmp_path):
        """Items at window_start 00:00:00Z included; at window_end+1day excluded."""
        from catalyst_data.manifests.universe import SourceCell
        from catalyst_data.update_pipeline import execute_update
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        import sqlite3, asyncio

        db_path = _pipeline_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        cell = SourceCell.create(
            "evidence", "polygon_news", "news", "AAPL",
            "2025-08-04", "2025-08-04", "calendar_days", "v1",
            page_cap=1, item_cap=50,
        )
        plan = UpdatePlan(
            config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
            universe={"tickers": ["AAPL"]},
            stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash

        class BoundaryTransport:
            async def request(self, **kwargs):
                return {
                    "status": 200,
                    "body": json.dumps({
                        "results": [
                            {"id": "at_start", "title": "Start",
                             "description": "d", "published_utc": "2025-08-04T00:00:00Z",
                             "article_url": "https://x.com/1", "tickers": ["AAPL"],
                             "publisher": {"name": "Test"}},
                            {"id": "at_boundary", "title": "Boundary",
                             "description": "d", "published_utc": "2025-08-05T00:00:00Z",
                             "article_url": "https://x.com/2", "tickers": ["AAPL"],
                             "publisher": {"name": "Test"}},
                        ],
                    }).encode(),
                }

        report = asyncio.run(execute_update(db=conn, plan=plan, transport=BoundaryTransport()))

        # start-of-window included; window_end+1day excluded → partial (1 rejected)
        cp = conn.execute(
            "SELECT status, is_complete, error_class, items_received FROM source_checkpoints WHERE cell_id=?",
            (cell.cell_id,),
        ).fetchone()
        assert cp["is_complete"] == 0, f"expected is_complete=0 (1 item out of window), got {cp['is_complete']}"
        assert cp["status"] == "partial", f"expected partial, got {cp['status']}"
        assert cp["error_class"] == "response_out_of_window", f"unexpected: {cp['error_class']}"
        assert cp["items_received"] == 1, f"only in-window item should be written, got {cp['items_received']}"

        # window_end+1day 00:00:00Z excluded
        in_count = conn.execute("SELECT COUNT(*) FROM articles WHERE title='Start'").fetchone()[0]
        assert in_count == 1
        boundary_count = conn.execute("SELECT COUNT(*) FROM articles WHERE title='Boundary'").fetchone()[0]
        assert boundary_count == 0, "window_end+1day item must be rejected"
        conn.close()


class TestCursorIntegration:
    """End-to-end: page 2 must receive cursor-2 from page 1 next_url."""

    def test_page2_receives_cursor_from_page1(self, tmp_path):
        from catalyst_data.manifests.universe import SourceCell
        from catalyst_data.update_pipeline import execute_update
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        import sqlite3, asyncio

        db_path = _pipeline_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        cell = SourceCell.create(
            "evidence", "polygon_news", "news", "AAPL",
            "2025-09-01", "2025-09-07", "calendar_days", "v1",
            page_cap=2, item_cap=10,
        )
        plan = UpdatePlan(
            config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
            universe={"tickers": ["AAPL"]},
            stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash

        page_urls = []

        class CursorTransport:
            async def request(self, **kwargs):
                pu = kwargs.get("page_url")
                page_urls.append(pu)
                if pu is None:
                    return {
                        "status": 200,
                        "body": json.dumps({
                            "results": [{"id": "p1", "title": "one", "description": "d",
                                         "published_utc": "2025-09-01T12:00:00Z",
                                         "article_url": "https://x.com/1", "tickers": ["AAPL"],
                                         "publisher": {"name": "Test"}}],
                            "next_url": "https://api.polygon.io/v2/reference/news?cursor=cursor-2&limit=50",
                        }).encode(),
                    }
                return {
                    "status": 200,
                    "body": json.dumps({
                        "results": [{"id": "p2", "title": "two", "description": "d",
                                     "published_utc": "2025-09-02T12:00:00Z",
                                     "article_url": "https://x.com/2", "tickers": ["AAPL"],
                                     "publisher": {"name": "Test"}}],
                    }).encode(),
                }

        report = asyncio.run(execute_update(db=conn, plan=plan, transport=CursorTransport()))
        assert report["status"] == "SUCCEEDED", f"unexpected: {report['status']}"
        assert page_urls[0] is None
        assert page_urls[1] is not None
        assert "cursor=cursor-2" in page_urls[1], f"cursor not in page 2 URL: {page_urls[1]}"
        assert "limit=50" in page_urls[1], f"limit not in page 2 URL: {page_urls[1]}"

        cp = conn.execute(
            "SELECT status, request_count, pages_received, items_received, is_complete FROM source_checkpoints WHERE cell_id=?",
            (cell.cell_id,),
        ).fetchone()
        assert cp["is_complete"] == 1
        assert cp["request_count"] == 2
        assert cp["pages_received"] == 2
        assert cp["items_received"] == 2
        conn.close()


class TestStopAfterRejection:
    """After detecting rejected items, cell must stop immediately."""

    def test_stops_after_rejection_no_next_page(self, tmp_path):
        from catalyst_data.manifests.universe import SourceCell
        from catalyst_data.update_pipeline import execute_update
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        import sqlite3, asyncio

        db_path = _pipeline_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        cell = SourceCell.create(
            "evidence", "polygon_news", "news", "AAPL",
            "2025-08-04", "2025-08-04", "calendar_days", "v1",
            page_cap=5, item_cap=50,
        )
        plan = UpdatePlan(
            config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
            universe={"tickers": ["AAPL"]},
            stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash

        transport_calls = [0]

        class MixedTransport:
            async def request(self, **kwargs):
                transport_calls[0] += 1
                return {
                    "status": 200,
                    "body": json.dumps({
                        "results": [
                            {"id": "good", "title": "OK",
                             "description": "d", "published_utc": "2025-08-04T12:00:00Z",
                             "article_url": "https://x.com/1", "tickers": ["AAPL"],
                             "publisher": {"name": "Test"}},
                            {"id": "bad", "title": "OUT",
                             "description": "d", "published_utc": "2025-08-10T12:00:00Z",
                             "article_url": "https://x.com/2", "tickers": ["AAPL"],
                             "publisher": {"name": "Test"}},
                        ],
                        "next_url": "https://api.polygon.io/v2/reference/news?cursor=nextpage",
                    }).encode(),
                }

        report = asyncio.run(execute_update(db=conn, plan=plan, transport=MixedTransport()))

        # Transport must be called exactly once
        assert transport_calls[0] == 1, f"expected 1 transport call, got {transport_calls[0]}"

        cp = conn.execute(
            "SELECT status, error_class, is_complete, request_count, pages_received, items_received FROM source_checkpoints WHERE cell_id=?",
            (cell.cell_id,),
        ).fetchone()
        assert cp["status"] == "partial"
        assert cp["error_class"] == "response_out_of_window"
        assert cp["is_complete"] == 0
        assert cp["request_count"] == 1
        assert cp["pages_received"] == 1
        assert cp["items_received"] == 1, f"expected 1 in-window item, got {cp['items_received']}"

        # Bad article must NOT be written
        bad = conn.execute("SELECT COUNT(*) FROM articles WHERE title='OUT'").fetchone()[0]
        assert bad == 0, f"out-of-window article written: {bad}"
        # Good article must be written
        good = conn.execute("SELECT COUNT(*) FROM articles WHERE title='OK'").fetchone()[0]
        assert good == 1
        conn.close()
