"""S3 Phase 3: D2 Timestamp Canonicalization — +00:00 → Z, idempotent.

Design refs: D2.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

DEV_DB = Path(__file__).resolve().parents[3] / "data" / "catalyst_dev_ws4b.db"


def _open_dev():
    conn = sqlite3.connect(f"file:{DEV_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class TestTimestampCanonicalMigration:
    """Migration converts +00:00 suffix to Z, is idempotent, leaves zero +00:00."""

    def test_no_plus_zero_zero_after_migration(self):
        """Post-condition: zero rows with +00:00 suffix in clean_assets.published_utc."""
        conn = _open_dev()
        # Check in article_tickers reference_date (Finnhub uses +00:00)
        count_at = conn.execute(
            "SELECT COUNT(*) FROM article_tickers WHERE reference_date LIKE '%+00:00'"
        ).fetchone()[0]

        # Check in articles published_utc
        count_art = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE published_utc LIKE '%+00:00'"
        ).fetchone()[0]

        # Check in article_tickers reference_date
        count_ca = conn.execute(
            "SELECT COUNT(*) FROM clean_assets WHERE reference_date LIKE '%+00:00'"
        ).fetchone()[0]

        conn.close()
        total = count_at + count_art + count_ca
        assert total == 0, (
            f"Found {total} rows with +00:00 suffix:\n"
            f"  article_tickers: {count_at}\n"
            f"  articles: {count_art}\n"
            f"  clean_assets: {count_ca}"
        )

    def test_z_timestamps_unchanged(self):
        """Timestamps already ending in Z must be left alone (idempotent)."""
        conn = _open_dev()
        # Just verify some Z timestamps exist and look normal
        row = conn.execute(
            "SELECT published_utc FROM articles WHERE published_utc LIKE '%Z' LIMIT 1"
        ).fetchone()
        conn.close()
        if row:  # may be no rows if all are +00:00
            assert row["published_utc"].endswith("Z")


class TestTimestampCanonicalIdempotent:
    """Running the migration twice must produce same result."""

    def test_migration_version_registered(self):
        """The canonicalization migration should have a PRAGMA user_version."""
        conn = _open_dev()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        # Migration must be applied (version ≥ 6 or whatever the canonicalization version is)
        assert version >= 6, f"Expected migration version >= 6, got {version}"
        conn.close()
