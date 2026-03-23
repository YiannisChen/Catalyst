import sqlite3
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone

from data_core.models import DataAsset
from data_core.sqlite_cache import (
    compute_asset_id,
    get_cached_asset,
    init_db,
    upsert_asset,
)


def _make_asset(
    is_final=True,
    source_type="fmp_fundamentals",
    last_updated=None,
    content_clean="|col1|col2|\n|---|---|\n|1|2|",
):
    if last_updated is None:
        last_updated = datetime.now(timezone.utc)

    return DataAsset(
        asset_id=compute_asset_id("NVDA", "2026-03-20", source_type, "v1"),
        ticker="NVDA",
        source_type=source_type,
        reference_date_utc=datetime(2026, 3, 20, 20, 0, 0, tzinfo=timezone.utc),
        reference_date_et="2026-03-20 16:00 ET",
        last_updated=last_updated,
        data_version="v1",
        is_final=is_final,
        content_raw=b'{"k":"v"}',
        content_clean=content_clean,
        metadata={"endpoint_statuses": {"income_statement": 200}},
    )


class TestInitDb(unittest.TestCase):
    def test_wal_mode_on_file_backed_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            conn = sqlite3.connect(tmp.name)
            init_db(conn)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            sync = conn.execute("PRAGMA synchronous").fetchone()[0]
            self.assertEqual(sync, 1)
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(timeout, 5000)
            conn.close()

    def test_init_db_on_memory_db_does_not_raise(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        conn.close()


class TestUpsertAndRead(unittest.TestCase):
    def test_roundtrip_compress_decompress(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        asset = _make_asset()

        upsert_asset(conn, asset)
        cached = get_cached_asset(
            conn=conn,
            ticker="NVDA",
            date="2026-03-20",
            source_type="fmp_fundamentals",
            data_version="v1",
            force_refresh=False,
            now_utc=datetime.now(timezone.utc),
        )

        self.assertIsNotNone(cached)
        self.assertEqual(cached.content_raw, b'{"k":"v"}')
        self.assertTrue(cached.content_clean.startswith("|col1|col2|"))
        conn.close()

    def test_upsert_overwrites_existing_record(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        asset_v1 = _make_asset(content_clean="OLD CONTENT")
        upsert_asset(conn, asset_v1)

        asset_v2 = _make_asset(content_clean="NEW CONTENT")
        upsert_asset(conn, asset_v2)

        cached = get_cached_asset(
            conn=conn,
            ticker="NVDA",
            date="2026-03-20",
            source_type="fmp_fundamentals",
            data_version="v1",
            force_refresh=False,
            now_utc=datetime.now(timezone.utc),
        )
        self.assertIsNotNone(cached)
        self.assertEqual(cached.content_clean, "NEW CONTENT")
        conn.close()

    def test_content_raw_none_roundtrip(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        asset = DataAsset(
            asset_id=compute_asset_id("NVDA", "2026-03-20", "gdelt_news", "v1"),
            ticker="NVDA",
            source_type="gdelt_news",
            reference_date_utc=datetime(2026, 3, 20, 20, 0, 0, tzinfo=timezone.utc),
            reference_date_et="2026-03-20 16:00 ET",
            last_updated=datetime.now(timezone.utc),
            content_raw=None,
            content_clean="some text",
        )
        upsert_asset(conn, asset)
        cached = get_cached_asset(
            conn=conn,
            ticker="NVDA",
            date="2026-03-20",
            source_type="gdelt_news",
            data_version="v1",
            force_refresh=False,
            now_utc=datetime.now(timezone.utc),
        )
        self.assertIsNotNone(cached)
        self.assertIsNone(cached.content_raw)
        conn.close()

    def test_corrupted_zlib_blob_returns_none(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        upsert_asset(conn, _make_asset())

        aid = compute_asset_id("NVDA", "2026-03-20", "fmp_fundamentals", "v1")
        conn.execute(
            "UPDATE data_assets SET content_raw = ? WHERE asset_id = ?",
            (b"not-valid-zlib-data", aid),
        )
        conn.commit()

        cached = get_cached_asset(
            conn=conn,
            ticker="NVDA",
            date="2026-03-20",
            source_type="fmp_fundamentals",
            data_version="v1",
            force_refresh=False,
            now_utc=datetime.now(timezone.utc),
        )
        self.assertIsNone(cached)
        conn.close()


class TestForceRefresh(unittest.TestCase):
    def test_force_refresh_bypasses_cache_read(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        upsert_asset(conn, _make_asset())

        cached = get_cached_asset(
            conn=conn,
            ticker="NVDA",
            date="2026-03-20",
            source_type="fmp_fundamentals",
            data_version="v1",
            force_refresh=True,
            now_utc=datetime.now(timezone.utc),
        )
        self.assertIsNone(cached)
        conn.close()


class TestTTL(unittest.TestCase):
    def test_non_final_fundamentals_expires_after_24h(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        old_time = datetime.now(timezone.utc) - timedelta(hours=25)
        upsert_asset(conn, _make_asset(is_final=False, last_updated=old_time))

        expired = get_cached_asset(
            conn=conn,
            ticker="NVDA",
            date="2026-03-20",
            source_type="fmp_fundamentals",
            data_version="v1",
            force_refresh=False,
            now_utc=datetime.now(timezone.utc),
        )
        self.assertIsNone(expired)
        conn.close()

    def test_final_fundamentals_has_infinite_ttl(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        old_time = datetime.now(timezone.utc) - timedelta(days=3650)
        upsert_asset(conn, _make_asset(is_final=True, last_updated=old_time))

        cached = get_cached_asset(
            conn=conn,
            ticker="NVDA",
            date="2026-03-20",
            source_type="fmp_fundamentals",
            data_version="v1",
            force_refresh=False,
            now_utc=datetime.now(timezone.utc),
        )
        self.assertIsNotNone(cached)
        conn.close()

    def test_news_has_infinite_ttl(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        old_time = datetime.now(timezone.utc) - timedelta(days=3650)
        source = "gdelt_news"
        upsert_asset(conn, _make_asset(source_type=source, last_updated=old_time))

        cached = get_cached_asset(
            conn=conn,
            ticker="NVDA",
            date="2026-03-20",
            source_type=source,
            data_version="v1",
            force_refresh=False,
            now_utc=datetime.now(timezone.utc),
        )
        self.assertIsNotNone(cached)
        conn.close()


if __name__ == "__main__":
    unittest.main()
