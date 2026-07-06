"""Phase 0 remediation entrypoint tests."""

from __future__ import annotations

import json
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path

from catalyst_data.articles import compute_article_id, ensure_articles_table
from catalyst_data.phase0_remediate import (
    fred_fit_guard,
    replace_fred_macro_from_fetcher,
    reconcile_news_checkpoints,
    materialize_cross_source_dedup,
    rederive_prose_from_bronze,
)
from catalyst_data.storage.sqlite import (
    compute_asset_id,
    ensure_clean_provenance,
    init_db,
    upsert_raw_asset,
)
from catalyst_data.quality import ensure_ingestion_quality_tables
import pytest


def _make_phase0_db(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)
    ensure_clean_provenance(conn)
    conn.execute(
        "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES "
        "('AAPL', '2026-05-01', 100.0),"
        "('MSFT', '2026-05-01', 200.0)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO index_state "
        "(chunk_id, corpus_item_id, source_kind, content_hash, content_text) "
        "VALUES ('keep::l1', 'keep', 'article', 'h', 'placeholder')"
    )
    conn.execute(
        """INSERT INTO macro_observations
           (series_id, observation_date, value, released_at)
           VALUES ('DFF', '2026-01-01', 4.5, '2026-01-02')"""
    )
    conn.commit()
    return conn


def _seed_polygon_raw(conn: sqlite3.Connection) -> str:
    raw_id = "raw-poly-aapl"
    payload = {
        "news": {
            "results": [
                {
                    "id": "same-event",
                    "title": "AAPL expands AI infrastructure",
                    "published_utc": "2026-06-15T14:00:00Z",
                    "description": "Polygon body",
                    "article_url": "https://example.com/same-event",
                    "publisher": {"name": "Polygon Publisher"},
                }
            ]
        }
    }
    upsert_raw_asset(
        conn,
        asset_id=raw_id,
        ticker="AAPL",
        source_type="polygon_news",
        reference_date="2026-05-01",
        content_raw=json.dumps(payload).encode("utf-8"),
        http_status=200,
    )
    return raw_id


def _seed_finnhub_raw(conn: sqlite3.Connection) -> str:
    published = int(datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc).timestamp())
    raw_id = compute_asset_id("AAPL", "2026-05-01", "finnhub_company_news")
    payload = [
        {
            "id": 42,
            "headline": "AAPL expands AI infrastructure",
            "summary": "Finnhub body",
            "datetime": published,
            "source": "Finnhub Publisher",
            "url": "https://example.com/same-event?utm_source=finnhub",
        }
    ]
    upsert_raw_asset(
        conn,
        asset_id=raw_id,
        ticker="AAPL",
        source_type="finnhub_company_news",
        reference_date="2026-05-01",
        content_raw=json.dumps(payload).encode("utf-8"),
        http_status=200,
    )
    return raw_id


def test_rederive_prose_from_bronze_regenerates_clean_assets_and_resets_dedup(
    tmp_path: Path,
):
    db = tmp_path / "phase0.db"
    conn = _make_phase0_db(db)
    polygon_raw = _seed_polygon_raw(conn)
    _seed_finnhub_raw(conn)
    conn.execute(
        """INSERT INTO clean_assets
           (asset_id, ticker, source_type, reference_date, cleaned_at,
            content_md, raw_asset_id)
           VALUES (?, 'AAPL', 'polygon_news', '2026-05-01',
                   datetime('now'), 'stale', ?)""",
        (polygon_raw, polygon_raw),
    )
    conn.commit()
    scope_before = {
        "macro": conn.execute("SELECT COUNT(*) FROM macro_observations").fetchone()[0],
        "ohlcv": conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0],
        "index": conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0],
    }
    conn.close()

    first = rederive_prose_from_bronze(db)
    second = rederive_prose_from_bronze(db)

    conn = sqlite3.connect(str(db))
    scope_after = {
        "macro": conn.execute("SELECT COUNT(*) FROM macro_observations").fetchone()[0],
        "ohlcv": conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0],
        "index": conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0],
    }
    blank_articles = conn.execute(
        """SELECT COUNT(*) FROM articles
           WHERE source_type IN ('polygon_news', 'finnhub_company_news')
             AND COALESCE(reference_date, '') = ''"""
    ).fetchone()[0]
    blank_assocs = conn.execute(
        """SELECT COUNT(*) FROM article_tickers at
           JOIN articles a ON a.article_id = at.article_id
           WHERE a.source_type IN ('polygon_news', 'finnhub_company_news')
             AND COALESCE(at.reference_date, '') = ''"""
    ).fetchone()[0]
    refs = dict(
        conn.execute(
            "SELECT article_id, reference_date FROM articles ORDER BY article_id"
        ).fetchall()
    )
    counts = {
        "articles": conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
        "assocs": conn.execute("SELECT COUNT(*) FROM article_tickers").fetchone()[0],
        "poly_clean": conn.execute(
            "SELECT COUNT(*) FROM clean_assets WHERE source_type = 'polygon_news'"
        ).fetchone()[0],
    }
    clean = conn.execute(
        """SELECT content_md, reference_date FROM clean_assets
           WHERE source_type = 'polygon_news'"""
    ).fetchone()

    conn.execute(
        "UPDATE articles SET dedup_group_id = 'stale', is_canonical = 0 "
        "WHERE source_type IN ('polygon_news', 'finnhub_company_news')"
    )
    conn.execute(
        "UPDATE article_tickers SET dedup_group_id = 'stale' "
        "WHERE article_id IN (SELECT article_id FROM articles "
        "WHERE source_type IN ('polygon_news', 'finnhub_company_news'))"
    )
    conn.commit()
    conn.close()

    reset = rederive_prose_from_bronze(db)

    conn = sqlite3.connect(str(db))
    stale_articles = conn.execute(
        """SELECT COUNT(*) FROM articles
           WHERE source_type IN ('polygon_news', 'finnhub_company_news')
             AND (dedup_group_id IS NOT NULL OR is_canonical != 1)"""
    ).fetchone()[0]
    stale_assocs = conn.execute(
        """SELECT COUNT(*) FROM article_tickers
           WHERE dedup_group_id IS NOT NULL"""
    ).fetchone()[0]
    conn.close()

    assert first["polygon"]["articles_upserted"] == 1
    assert second["after_counts"]["articles"] == first["after_counts"]["articles"]
    assert second["after_counts"]["article_tickers"] == first["after_counts"]["article_tickers"]
    assert reset["dedup_reset"]["articles_reset"] == 2
    assert reset["dedup_reset"]["article_tickers_reset"] == 2
    assert scope_after == scope_before
    assert blank_articles == 0
    assert blank_assocs == 0
    assert refs[compute_article_id("poly", "same-event")] == "2026-06-15"
    assert refs[compute_article_id("finnhub", "42")] == "2026-06-15"
    assert counts == {"articles": 2, "assocs": 2, "poly_clean": 1}
    assert clean[1] == "2026-06-15"
    assert "## AAPL: AAPL expands AI infrastructure" in clean[0]
    assert "Polygon body" in clean[0]
    assert "stale" not in clean[0]
    assert stale_articles == 0
    assert stale_assocs == 0


def test_materialize_cross_source_dedup_assigns_one_canonical_per_group(tmp_path: Path):
    db = tmp_path / "dedup.db"
    conn = _make_phase0_db(db)
    _seed_polygon_raw(conn)
    _seed_finnhub_raw(conn)
    conn.close()
    rederive_prose_from_bronze(db)

    result = materialize_cross_source_dedup(db)
    result_again = materialize_cross_source_dedup(db)

    conn = sqlite3.connect(str(db))
    groups = conn.execute(
        """SELECT at.dedup_group_id, COUNT(DISTINCT at.article_id), SUM(a.is_canonical)
           FROM article_tickers at
           JOIN articles a ON a.article_id = at.article_id
           WHERE at.dedup_group_id IS NOT NULL
           GROUP BY at.dedup_group_id"""
    ).fetchall()
    null_articles = conn.execute(
        """SELECT COUNT(*) FROM articles
           WHERE source_type IN ('polygon_news', 'finnhub_company_news')
             AND dedup_group_id IS NULL"""
    ).fetchone()[0]
    null_assocs = conn.execute(
        "SELECT COUNT(*) FROM article_tickers WHERE dedup_group_id IS NULL"
    ).fetchone()[0]
    conn.close()

    assert result["groups_resolved"] == 1
    assert result_again["after"]["dedup_null_articles"] == result["after"]["dedup_null_articles"]
    assert null_articles == 0
    assert null_assocs == 0
    assert groups == [(groups[0][0], 2, 1)]


def test_reconcile_news_checkpoints_demotes_phantom_successes(tmp_path: Path):
    db = tmp_path / "checkpoints.db"
    conn = _make_phase0_db(db)
    ensure_ingestion_quality_tables(conn)
    upsert_raw_asset(
        conn,
        asset_id="raw-empty",
        ticker="AAPL",
        source_type="polygon_news",
        reference_date="2026-06-29",
        content_raw=json.dumps({"results": []}).encode("utf-8"),
        http_status=200,
        metadata={"article_count": 0},
    )
    conn.execute(
        """INSERT INTO source_checkpoints
           (run_id, source_type, ticker, date, status)
           VALUES ('run-empty', 'polygon_news', 'AAPL', '2026-06-29', 'success')"""
    )
    conn.execute(
        """INSERT INTO source_checkpoints
           (run_id, source_type, ticker, date, status)
           VALUES ('run-phantom', 'polygon_news', 'TSLA', '2026-06-29', 'success')"""
    )
    conn.execute(
        """INSERT INTO source_checkpoints
           (run_id, source_type, ticker, date, status)
           VALUES ('run-failed', 'polygon_news', 'MSFT', '2026-06-29', 'failed')"""
    )
    conn.commit()
    conn.close()

    result = reconcile_news_checkpoints(db)

    conn = sqlite3.connect(str(db))
    rows = dict(
        conn.execute(
            "SELECT run_id, status || ':' || COALESCE(error_class, '') "
            "FROM source_checkpoints ORDER BY run_id"
        ).fetchall()
    )
    conn.close()

    assert result["before"]["phantom_success"] == 1
    assert result["repaired_phantom_success"] == 1
    assert result["after"].get("phantom_success", 0) == 0
    assert rows["run-empty"] == "success:"
    assert rows["run-phantom"] == "failed:PhantomSuccess"
    assert rows["run-failed"] == "failed:"


@pytest.mark.asyncio
async def test_fred_fit_guard_proves_output_type4_shape_and_params():
    calls = []

    async def fetcher(ticker, endpoint, date, **kwargs):
        calls.append((ticker, endpoint, date, kwargs))
        return type("Result", (), {
            "status": 200,
            "data": {
                "observations": [
                    {
                        "date": "2023-01-03",
                        "value": "4.33",
                        "realtime_start": "2023-01-03",
                    }
                ]
            },
            "error": None,
        })()

    result = await fred_fit_guard(
        fetcher,
        series_id="DFF",
        from_date="2023-01-01",
        to_date="2023-01-31",
    )

    assert result["fit_proven"] is True
    assert result["observation_count"] == 1
    assert calls == [
        (
            "",
            "DFF",
            "2023-01-31",
            {
                "start_date": "2023-01-01",
                "output_type": 4,
                "realtime_start": "2023-01-01",
                "realtime_end": "2023-01-31",
            },
        )
    ]


@pytest.mark.asyncio
async def test_fred_replacement_refuses_without_fit_guard(tmp_path: Path):
    db = tmp_path / "fred.db"
    conn = _make_phase0_db(db)
    before = conn.execute("SELECT COUNT(*) FROM macro_observations").fetchone()[0]
    conn.close()

    async def fetcher(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("fetcher should not be called without fit proof")

    result = await replace_fred_macro_from_fetcher(
        db,
        fetcher,
        from_date="2023-01-01",
        to_date="2023-01-31",
        series_ids=["DFF"],
        fit_guard={"fit_proven": False},
        confirm=True,
    )

    conn = sqlite3.connect(str(db))
    after = conn.execute("SELECT COUNT(*) FROM macro_observations").fetchone()[0]
    conn.close()

    assert result["status"] == "refused_fit_not_proven"
    assert after == before


@pytest.mark.asyncio
async def test_fred_replacement_clears_and_replaces_when_fit_proven(tmp_path: Path):
    db = tmp_path / "fred_replace.db"
    conn = _make_phase0_db(db)
    ensure_ingestion_quality_tables(conn)
    upsert_raw_asset(
        conn,
        asset_id="raw-old-fred",
        ticker="DFF",
        source_type="fred_macro",
        reference_date="2026-01-01",
        content_raw=b"{}",
        http_status=200,
    )
    conn.execute(
        """INSERT INTO clean_assets
           (asset_id, ticker, source_type, reference_date, cleaned_at, content_md)
           VALUES ('raw-old-fred', 'DFF', 'fred_macro', '2026-01-01',
                   datetime('now'), 'old macro text')"""
    )
    conn.execute(
        """INSERT INTO asset_quality_flags
           (asset_id, is_rag_eligible, quality_score, evaluated_at)
           VALUES ('raw-old-fred', 1, 1.0, datetime('now'))"""
    )
    conn.commit()
    conn.close()

    async def fetcher(ticker, endpoint, date, **kwargs):
        return type("Result", (), {
            "status": 200,
            "data": {
                "observations": [
                    {
                        "date": "2023-01-03",
                        "value": "4.33",
                        "realtime_start": "2023-01-03",
                    }
                ]
            },
            "error": None,
        })()

    result = await replace_fred_macro_from_fetcher(
        db,
        fetcher,
        from_date="2023-01-01",
        to_date="2023-01-31",
        series_ids=["DFF"],
        fit_guard={"fit_proven": True},
        confirm=True,
    )

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT series_id, observation_date, value, released_at "
        "FROM macro_observations"
    ).fetchall()
    raw_count = conn.execute(
        "SELECT COUNT(*) FROM raw_assets WHERE source_type = 'fred_macro'"
    ).fetchone()[0]
    clean_count = conn.execute(
        "SELECT COUNT(*) FROM clean_assets WHERE source_type = 'fred_macro'"
    ).fetchone()[0]
    quality_count = conn.execute("SELECT COUNT(*) FROM asset_quality_flags").fetchone()[0]
    conn.close()

    assert result["status"] == "replaced"
    assert result["cleared_macro_rows"] == 1
    assert result["fetched_series"] == 1
    assert raw_count == 1
    assert clean_count == 0
    assert quality_count == 0
    assert rows == [("DFF", "2023-01-03", 4.33, "2023-01-03")]


# ---------------------------------------------------------------------------
# Step 4a — S1 Per-Association Canonicality bite-tests
# ---------------------------------------------------------------------------

class TestStep4aS1:
    """S1: per-association canonicality — singleton wins, duplicate-losers dropped."""

    @staticmethod
    def _make_db(tmp_path):
        import sqlite3
        db_path = str(tmp_path / "test_s1.db")
        conn = sqlite3.connect(db_path)
        from catalyst_data.storage.sqlite import init_db, ensure_macro_tables
        from catalyst_data.articles import ensure_articles_table
        init_db(conn)
        ensure_articles_table(conn)
        ensure_macro_tables(conn)
        return conn, db_path

    @staticmethod
    def _seed_article(conn, article_id, ticker, title, source_type="polygon_news",
                      provider="polygon", reference_date="2026-06-15",
                      dedup_group_id=None, is_canonical=1, is_rag_eligible=1,
                      description="Test description"):
        import json
        from catalyst_data.storage.sqlite import upsert_raw_asset
        from catalyst_data.articles import upsert_article, upsert_article_ticker
        raw_id = f"raw-{article_id}"
        upsert_raw_asset(
            conn, asset_id=raw_id, ticker=ticker, source_type=source_type,
            reference_date=reference_date,
            content_raw=json.dumps({"title": title}).encode("utf-8"),
        )
        upsert_article(conn, article={
            "article_id": article_id, "raw_asset_id": raw_id,
            "provider": provider, "source_type": source_type,
            "ticker": ticker, "reference_date": reference_date,
            "published_utc": f"{reference_date}T12:00:00Z",
            "title": title, "description": description,
            "publisher_name": "TestPub",
        })
        upsert_article_ticker(
            conn,
            article_id=article_id,
            ticker=ticker,
            raw_asset_id=raw_id,
            reference_date=reference_date,
        )
        if dedup_group_id is not None:
            conn.execute("UPDATE articles SET dedup_group_id = ?, is_canonical = ? WHERE article_id = ?",
                         (dedup_group_id, is_canonical, article_id))
            conn.execute("UPDATE article_tickers SET dedup_group_id = ? WHERE article_id = ? AND ticker = ?",
                         (dedup_group_id, article_id, ticker))
        if is_rag_eligible != 1:
            conn.execute("UPDATE articles SET is_rag_eligible = ? WHERE article_id = ?",
                         (is_rag_eligible, article_id))

    def test_singleton_wins_canonical_association(self, tmp_path):
        """A singleton article with one association → canonical=1 at association grain."""
        import sqlite3
        conn, db_path = self._make_db(tmp_path)
        from catalyst_data.storage.sqlite import _migrate_article_tickers_dedup
        _migrate_article_tickers_dedup(conn)
        # Add is_canonical column to article_tickers (S1 migration)
        self._add_is_canonical_column(conn)

        self._seed_article(conn, "poly:s1", "AAPL", "Singleton Title",
                           dedup_group_id="grp-singleton", is_canonical=1)

        # Run per-association canonical recompute
        from catalyst_data.dedup.cross_source import recompute_per_association_canonical
        recompute_per_association_canonical(conn)

        # Assert: article_tickers.is_canonical = 1
        at_canon = conn.execute(
            "SELECT is_canonical FROM article_tickers WHERE article_id = 'poly:s1' AND ticker = 'AAPL'"
        ).fetchone()[0]
        conn.close()
        assert at_canon == 1, f"Singleton association must be canonical=1, got {at_canon}"

    def test_duplicate_loser_not_eligible(self, tmp_path):
        """Three articles in one dedup group → one winner canonical, two losers NOT."""
        import sqlite3
        conn, db_path = self._make_db(tmp_path)
        from catalyst_data.storage.sqlite import _migrate_article_tickers_dedup
        _migrate_article_tickers_dedup(conn)
        self._add_is_canonical_column(conn)

        # Polygon has higher priority in CROSS_SOURCE_PRIORITY — wins
        self._seed_article(conn, "finnhub:loser", "AAPL", "AAPL expands AI infra",
                           source_type="finnhub_company_news", provider="finnhub",
                           dedup_group_id="grp-dup", is_canonical=1,
                           reference_date="2026-06-14")
        self._seed_article(conn, "poly:winner", "AAPL", "AAPL expands AI infra",
                           source_type="polygon_news", provider="polygon",
                           dedup_group_id="grp-dup", is_canonical=0,
                           reference_date="2026-06-15")
        self._seed_article(conn, "poly:loser2", "AAPL", "AAPL expands AI infra",
                           source_type="polygon_news", provider="polygon",
                           dedup_group_id="grp-dup", is_canonical=0,
                           reference_date="2026-06-16")

        from catalyst_data.dedup.cross_source import recompute_per_association_canonical
        recompute_per_association_canonical(conn)

        # Exactly one canonical association per ticker in this group
        canon_count = conn.execute(
            "SELECT SUM(is_canonical) FROM article_tickers WHERE dedup_group_id = 'grp-dup' AND ticker = 'AAPL'"
        ).fetchone()[0]
        loser_count = conn.execute(
            "SELECT COUNT(*) FROM article_tickers WHERE dedup_group_id = 'grp-dup' AND ticker = 'AAPL' AND is_canonical = 0"
        ).fetchone()[0]
        conn.close()
        assert canon_count == 1, f"Exactly one canonical association expected, got {canon_count}"
        assert loser_count == 2, f"Two losers expected, got {loser_count}"

    def test_eligibility_function_drops_pure_losers(self, tmp_path):
        """eligibility(conn, article_id) returns False for an article with zero canonical associations."""
        import sqlite3
        conn, db_path = self._make_db(tmp_path)
        from catalyst_data.storage.sqlite import _migrate_article_tickers_dedup
        _migrate_article_tickers_dedup(conn)
        self._add_is_canonical_column(conn)

        # Polygon wins — higher priority in CROSS_SOURCE_PRIORITY
        self._seed_article(conn, "finnhub:lose", "AAPL", "AAPL expands AI infra",
                           source_type="finnhub_company_news", provider="finnhub",
                           dedup_group_id="grp-elig", is_canonical=1,
                           reference_date="2026-06-14")
        # Polygon wins despite later date — source priority trumps
        self._seed_article(conn, "poly:win", "AAPL", "AAPL expands AI infra",
                           source_type="polygon_news", provider="polygon",
                           dedup_group_id="grp-elig", is_canonical=0,
                           reference_date="2026-06-15")

        from catalyst_data.dedup.cross_source import recompute_per_association_canonical
        recompute_per_association_canonical(conn)

        from catalyst_data.eligibility import is_article_eligible
        assert is_article_eligible(conn, "poly:win") is True
        assert is_article_eligible(conn, "finnhub:lose") is False
        conn.close()

    def test_zero_canonical_group_fails(self, tmp_path):
        """Every dedup group with >=1 member must have exactly 1 canonical per ticker."""
        import sqlite3
        conn, db_path = self._make_db(tmp_path)
        from catalyst_data.storage.sqlite import _migrate_article_tickers_dedup
        _migrate_article_tickers_dedup(conn)
        self._add_is_canonical_column(conn)

        self._seed_article(conn, "poly:z1", "AAPL", "Zero Canon", dedup_group_id="grp-zero", is_canonical=0)
        self._seed_article(conn, "poly:z2", "AAPL", "Zero Canon", dedup_group_id="grp-zero", is_canonical=0)

        from catalyst_data.dedup.cross_source import recompute_per_association_canonical
        recompute_per_association_canonical(conn)

        # After recompute, the group must have exactly one canonical
        canon = conn.execute(
            "SELECT SUM(is_canonical) FROM article_tickers WHERE dedup_group_id = 'grp-zero' AND ticker = 'AAPL'"
        ).fetchone()[0]
        conn.close()
        assert canon == 1, f"After recompute, group must have exactly 1 canonical, got {canon}"

    @staticmethod
    def _add_is_canonical_column(conn):
        cols = [c[1] for c in conn.execute("PRAGMA table_info(article_tickers)").fetchall()]
        if "is_canonical" not in cols:
            conn.execute("ALTER TABLE article_tickers ADD COLUMN is_canonical INTEGER DEFAULT 1")
            conn.commit()


# ---------------------------------------------------------------------------
# Step 4a — S2 index_state persistence tests
# ---------------------------------------------------------------------------

class TestStep4aS2:
    """S2: build index_state pending queue with UPSERT-by-chunk_id, eligibility filter."""

    @staticmethod
    def _make_s2_db(tmp_path):
        import sqlite3
        db_path = str(tmp_path / "test_s2.db")
        conn = sqlite3.connect(db_path)
        from catalyst_data.storage.sqlite import init_db, ensure_macro_tables
        from catalyst_data.articles import ensure_articles_table
        init_db(conn)
        ensure_articles_table(conn)
        ensure_macro_tables(conn)
        return conn, db_path

    @staticmethod
    def _seed_eligible(conn, article_id, ticker, title, dedup_group_id, description="Test desc"):
        import json
        from catalyst_data.storage.sqlite import upsert_raw_asset, _migrate_article_tickers_dedup
        from catalyst_data.articles import upsert_article, upsert_article_ticker
        _migrate_article_tickers_dedup(conn)
        raw_id = f"raw-{article_id}"
        upsert_raw_asset(conn, asset_id=raw_id, ticker=ticker, source_type="polygon_news",
            reference_date="2026-06-15",
            content_raw=json.dumps({"title": title}).encode("utf-8"))
        upsert_article(conn, article={
            "article_id": article_id, "raw_asset_id": raw_id,
            "provider": "polygon", "source_type": "polygon_news",
            "ticker": ticker, "reference_date": "2026-06-15",
            "published_utc": "2026-06-15T12:00:00Z",
            "title": title, "description": description,
            "publisher_name": "TestPub",
        })
        upsert_article_ticker(conn, article_id=article_id, ticker=ticker,
                              raw_asset_id=raw_id, reference_date="2026-06-15")
        conn.execute("UPDATE articles SET dedup_group_id = ?, is_canonical = ?, is_rag_eligible = 1 WHERE article_id = ?",
                     (dedup_group_id, 1, article_id))
        conn.execute("UPDATE article_tickers SET dedup_group_id = ?, is_canonical = 1 WHERE article_id = ? AND ticker = ?",
                     (dedup_group_id, article_id, ticker))
        conn.commit()

    def test_persist_uses_eligible_only(self, tmp_path):
        """Eligible articles enter index_state; ineligible (no canonical assoc) do not."""
        import sqlite3
        conn, db_path = self._make_s2_db(tmp_path)
        self._seed_eligible(conn, "poly:elig", "AAPL", "Eligible", "grp-a")
        # Ineligible: article_tickers.is_canonical=0
        self._seed_eligible(conn, "poly:ineligible", "AAPL", "Ineligible", "grp-b")
        conn.execute("UPDATE article_tickers SET is_canonical = 0 WHERE article_id = 'poly:ineligible'")
        conn.commit()

        from catalyst_data.index_builder import persist_index_state
        result = persist_index_state(conn)
        conn.close()

        assert result["article_l1_pending"] == 1, f"Only eligible article should enter index_state, got {result}"
        assert result["article_l2_pending"] == 0

    def test_changed_article_stales_old_row(self, tmp_path):
        """Change an article body, re-run persist → old L1 marked stale, exactly 1 new pending."""
        import sqlite3
        conn, db_path = self._make_s2_db(tmp_path)
        self._seed_eligible(conn, "poly:change", "AAPL", "Original Title", "grp-c",
                            description="Original body text for testing changes")

        from catalyst_data.index_builder import persist_index_state
        # First run
        r1 = persist_index_state(conn)
        assert r1["article_l1_pending"] == 1

        # Verify one pending L1
        pending = conn.execute(
            "SELECT chunk_id, status, content_hash FROM index_state WHERE chunk_id = 'poly:change::l1'"
        ).fetchall()
        assert len(pending) == 1 and pending[0][1] == 'pending'

        # Change the article body
        conn.execute("UPDATE articles SET description = 'Completely different body now' WHERE article_id = 'poly:change'")
        conn.commit()

        # Second run
        r2 = persist_index_state(conn)
        assert r2["article_l1_pending"] == 1  # Still exactly 1 pending

        rows = conn.execute(
            "SELECT status, content_hash FROM index_state WHERE chunk_id = 'poly:change::l1' ORDER BY status"
        ).fetchall()
        conn.close()

        statuses = [r[0] for r in rows]
        assert 'stale' in statuses, f"Old row should be stale, got {statuses}"
        assert 'pending' in statuses, f"New row should be pending, got {statuses}"
        assert len([s for s in statuses if s == 'pending']) == 1, "Exactly one pending row"
        assert rows[0][1] != rows[1][1], "Old and new content_hashes must differ"

    def test_idempotent_rerun_zero_new_rows(self, tmp_path):
        """Unchanged content → re-run inserts zero new rows."""
        import sqlite3
        conn, db_path = self._make_s2_db(tmp_path)
        self._seed_eligible(conn, "poly:idem", "AAPL", "Idempotent", "grp-d")

        from catalyst_data.index_builder import persist_index_state
        r1 = persist_index_state(conn)
        r2 = persist_index_state(conn)
        conn.close()

        assert r2["article_l1_pending"] == r1["article_l1_pending"]
        assert r2["total_new_rows"] == 0
