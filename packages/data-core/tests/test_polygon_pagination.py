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
