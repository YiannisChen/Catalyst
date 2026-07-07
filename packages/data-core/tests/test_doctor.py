"""Tests for doctor.py — H1 operator contract gate."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from catalyst_data.storage.sqlite import init_db
from catalyst_data.articles import ensure_articles_table, upsert_article, upsert_article_ticker


def _make_clean_db(db_path: str) -> None:
    """Create a clean DB with articles, dedup, canonical, and FRED data.

    Seeds enough state that all four doctor gates pass (P0, embed readiness,
    unknown publishers, dry-run zero-write).
    """
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_articles_table(conn)

    for i in range(5):
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES (?, 'AAPL', 'polygon_news', '2025-01-01', datetime('now'), ?)",
            (f"raw-{i}", b"{}"),
        )
    conn.commit()

    articles = [
        ("poly:a1", "raw-0", "AAPL", "Title 1", "Desc 1", "MarketWatch", 2),
        ("poly:a2", "raw-1", "TSLA", "Title 2", "Desc 2", "Benzinga", 4),
        ("poly:a3", "raw-2", "MSFT", "Title 3", "Desc 3 " + "x" * 900, "GlobeNewswire Inc.", 3),
        ("poly:a4", "raw-3", "GOOGL", "Title 4", None, "The Motley Fool", 5),
        ("poly:a5", "raw-4", "META", "Title 5", "Desc 5", "Zacks Investment Research", 5),
    ]
    for article_id, raw_id, ticker, title, desc, pub, tier in articles:
        upsert_article(conn, article={
            "article_id": article_id, "raw_asset_id": raw_id, "provider": "polygon",
            "source_type": "polygon_news", "ticker": ticker,
            "reference_date": "2025-01-01",
            "published_utc": "2025-01-01T12:00:00Z",
            "title": title, "description": desc,
            "publisher_name": pub, "source_tier": tier,
        })
        upsert_article_ticker(conn, article_id=article_id, ticker=ticker,
                              raw_asset_id=raw_id, reference_date="2025-01-01")

    # Seed dedup + canonical to pass P0 G5/G6/G7 and embed readiness E1
    from catalyst_data.storage.sqlite import _migrate_article_tickers_dedup
    _migrate_article_tickers_dedup(conn)
    for article_id in ["poly:a1", "poly:a2", "poly:a3", "poly:a4", "poly:a5"]:
        dedup_gid = f"grp-{article_id}"
        conn.execute(
            "UPDATE articles SET dedup_group_id = ?, is_rag_eligible = 1, is_canonical = 1 WHERE article_id = ?",
            (dedup_gid, article_id),
        )
        conn.execute(
            "UPDATE article_tickers SET dedup_group_id = ?, is_canonical = 1 WHERE article_id = ?",
            (dedup_gid, article_id),
        )

    # Seed FRED macro_observations to pass P0 G3/G4
    conn.execute(
        "INSERT OR REPLACE INTO macro_observations (series_id, observation_date, value, released_at, fetched_at) "
        "VALUES ('GDP', '2025-01-01', 100.0, '2025-01-02T00:00:00Z', '2025-01-03T00:00:00Z')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO macro_observations (series_id, observation_date, value, released_at, fetched_at) "
        "VALUES ('GDP', '2025-02-01', 101.0, '2025-02-02T00:00:00Z', '2025-02-03T00:00:00Z')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO macro_observations (series_id, observation_date, value, released_at, fetched_at) "
        "VALUES ('GDP', '2025-03-01', 102.0, '2025-03-02T00:00:00Z', '2025-03-03T00:00:00Z')"
    )

    # Seed index_state to pass embed readiness E2/E3
    from catalyst_data.index_builder import compute_content_hash
    for aid, title, desc in [
        ("poly:a1", "Title 1", "Desc 1"),
        ("poly:a2", "Title 2", "Desc 2"),
        ("poly:a4", "Title 4", None),
        ("poly:a5", "Title 5", "Desc 5"),
    ]:
        h = compute_content_hash(title, desc)
        conn.execute(
            "INSERT OR REPLACE INTO index_state "
            "(chunk_id, chunk_level, corpus_item_id, source_kind, content_hash, content_text, source_tier, status) "
            "VALUES (? || '::l1', 'l1', ?, 'article', ?, 'text', 5, 'pending')",
            (aid, aid, h),
        )
    # a3 has L2 due to long body
    conn.execute(
        "INSERT OR REPLACE INTO index_state "
        "(chunk_id, chunk_level, corpus_item_id, source_kind, content_hash, content_text, source_tier, status) "
        "VALUES ('poly:a3::l1', 'l1', 'poly:a3', 'article', 'h3', 'text', 3, 'pending')"
    )

    conn.commit()
    conn.close()


class TestDoctor:
    def test_doctor_all_gates_pass_on_clean_db(self, tmp_path: Path):
        """Clean DB with known publishers → exit 0, all gates passed."""
        db_path = str(tmp_path / "test.db")
        _make_clean_db(db_path)

        from catalyst_data.doctor import doctor

        f = io.StringIO()
        with redirect_stdout(f):
            result = doctor(db_path, json_output=False)
        output = f.getvalue()

        assert result["all_gates_passed"] is True
        assert "[PASS]" in output or "PASS" in output

    def test_doctor_exits_nonzero_on_seeded_violation(self, tmp_path: Path):
        """Inject unknown publisher → exit 1."""
        db_path = str(tmp_path / "test.db")
        _make_clean_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES ('ra_unk', 'AAPL', 'polygon_news', '2025-01-01', datetime('now'), x'7b7d')"
        )
        conn.commit()
        conn.execute(
            "INSERT OR REPLACE INTO articles "
            "(article_id, raw_asset_id, provider, source_type, ticker, "
            " reference_date, published_utc, title, publisher_name, source_tier) "
            "VALUES ('poly:unk1', 'ra_unk', 'polygon', 'polygon_news', 'AAPL', "
            " '2025-01-01', '2025-01-01T12:00:00Z', 'Unknown Title', 'UnknownPublisherLLC', 4)"
        )
        conn.commit()
        conn.close()

        from catalyst_data.doctor import doctor

        f = io.StringIO()
        with redirect_stdout(f):
            result = doctor(db_path, json_output=False)

        assert result["all_gates_passed"] is False
        assert result["exit_code"] == 1

    def test_doctor_json_output_is_valid(self, tmp_path: Path):
        """--json produces parseable JSON with expected keys."""
        db_path = str(tmp_path / "test.db")
        _make_clean_db(db_path)

        from catalyst_data.doctor import doctor

        f = io.StringIO()
        with redirect_stdout(f):
            result = doctor(db_path, json_output=True)
        output = f.getvalue()

        parsed = json.loads(output)
        assert "all_gates_passed" in parsed
        assert "gates" in parsed
        assert "doctor_version" in parsed

    def test_doctor_json_redacts_secrets(self, tmp_path: Path):
        """JSON output must not contain raw API key patterns."""
        db_path = str(tmp_path / "test.db")
        _make_clean_db(db_path)

        from catalyst_data.doctor import doctor

        f = io.StringIO()
        with redirect_stdout(f):
            doctor(db_path, json_output=True)
        output = f.getvalue()

        # No raw API key patterns
        assert "sk-" not in output.lower() or "[REDACTED]" in output

    def test_doctor_unknown_publisher_detected(self, tmp_path: Path):
        """Unknown publisher → unknown_publishers gate fails."""
        db_path = str(tmp_path / "test.db")
        _make_clean_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES ('ra_unk', 'AAPL', 'polygon_news', '2025-01-01', datetime('now'), x'7b7d')"
        )
        conn.commit()
        conn.execute(
            "INSERT OR REPLACE INTO articles "
            "(article_id, raw_asset_id, provider, source_type, ticker, "
            " reference_date, published_utc, title, publisher_name, source_tier) "
            "VALUES ('poly:unk2', 'ra_unk', 'polygon', 'polygon_news', 'AAPL', "
            " '2025-01-01', '2025-01-01T12:00:00Z', 'T', 'WeirdPublisherXYZ', 4)"
        )
        conn.commit()
        conn.close()

        from catalyst_data.doctor import doctor

        f = io.StringIO()
        with redirect_stdout(f):
            result = doctor(db_path, json_output=False)

        gates = result.get("gates", {})
        up_gate = gates.get("unknown_publishers", {})
        assert up_gate.get("gate_passed") is False

    def test_doctor_output_contains_all_four_gates(self, tmp_path: Path):
        """All four gate sections present in output."""
        db_path = str(tmp_path / "test.db")
        _make_clean_db(db_path)

        from catalyst_data.doctor import doctor

        f = io.StringIO()
        with redirect_stdout(f):
            result = doctor(db_path, json_output=True)
        output = f.getvalue()

        parsed = json.loads(output)
        gates = parsed.get("gates", {})
        for gate_name in ("gate_p0", "embed_readiness", "unknown_publishers", "dry_run_zero_write"):
            assert gate_name in gates, f"Missing gate: {gate_name}"

    def test_doctor_handles_missing_tables(self, tmp_path: Path):
        """Graceful handling of DB without index_state table."""
        db_path = str(tmp_path / "test.db")

        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.execute("DROP TABLE IF EXISTS index_state")
        conn.execute("DROP TABLE IF EXISTS index_manifests")
        conn.commit()
        conn.close()

        from catalyst_data.doctor import doctor

        # Should not crash
        f = io.StringIO()
        with redirect_stdout(f):
            result = doctor(db_path, json_output=False)
        assert result is not None
        assert "all_gates_passed" in result
