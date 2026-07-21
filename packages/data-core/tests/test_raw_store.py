"""B2 — append-only raw-response store tests."""
from __future__ import annotations

import hashlib
import sqlite3
import pytest


class TestRawStore:
    def test_store_raw_response_returns_asset_id(self):
        """store_raw_response returns a raw_asset_id."""
        from conftest import _fresh_db_at_version
        from catalyst_data.ingestion.raw_store import store_raw_response

        db = _fresh_db_at_version(8)
        payload = b'{"results": [{"id": "a"}]}'
        h = hashlib.sha256(payload).hexdigest()

        raw_id = store_raw_response(db, request_id="req-001", response_bytes=payload,
                                     response_sha256=h)
        assert raw_id == "raw:req-001"

        row = db.execute("SELECT * FROM raw_assets WHERE asset_id = ?", (raw_id,)).fetchone()
        assert row is not None
        assert row["response_sha256"] == h
        db.close()

    def test_idempotent_same_hash(self):
        """Same request_id + same hash → no-op, no duplicate."""
        from conftest import _fresh_db_at_version
        from catalyst_data.ingestion.raw_store import store_raw_response

        db = _fresh_db_at_version(8)
        payload = b"test payload"
        h = hashlib.sha256(payload).hexdigest()

        store_raw_response(db, request_id="req-idem", response_bytes=payload,
                           response_sha256=h)
        store_raw_response(db, request_id="req-idem", response_bytes=payload,
                           response_sha256=h)

        count = db.execute(
            "SELECT COUNT(*) FROM raw_assets WHERE request_id = 'req-idem'"
        ).fetchone()[0]
        assert count == 1
        db.close()

    def test_different_hash_integrity_error(self):
        """Same request_id + different hash → RawResponseIntegrityError."""
        from conftest import _fresh_db_at_version
        from catalyst_data.ingestion.raw_store import store_raw_response, RawResponseIntegrityError

        db = _fresh_db_at_version(8)
        payload1 = b"hello"
        payload2 = b"world"

        store_raw_response(db, request_id="req-int", response_bytes=payload1)
        with pytest.raises(RawResponseIntegrityError):
            store_raw_response(db, request_id="req-int", response_bytes=payload2)
        db.close()

    def test_content_encoding_stored(self):
        """content_encoding is recorded separately from decoded body."""
        from conftest import _fresh_db_at_version
        from catalyst_data.ingestion.raw_store import store_raw_response

        db = _fresh_db_at_version(8)
        payload = b'{"results":[]}'
        store_raw_response(db, request_id="req-ce", response_bytes=payload,
                           content_encoding="gzip")

        row = db.execute(
            "SELECT content_encoding FROM raw_assets WHERE request_id = 'req-ce'"
        ).fetchone()
        assert row["content_encoding"] == "gzip"
        db.close()

    def test_update_rejected_on_v2_row(self):
        """Trigger rejects UPDATE on v2 request-scoped raw row."""
        from conftest import _fresh_db_at_version
        from catalyst_data.ingestion.raw_store import store_raw_response

        db = _fresh_db_at_version(8)
        store_raw_response(db, request_id="req-trigger", response_bytes=b"data")

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE raw_assets SET response_sha256 = 'x' WHERE request_id = 'req-trigger'"
            )
        db.close()
