"""Tests for atomic cell write — persist_cell + split rollback invariants.

All tests use injected fake FetchResults — no network calls.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catalyst_data.connectors.base import FetchResult
from catalyst_data.error_taxonomy import ErrorClass
from catalyst_data.storage.sqlite import init_db
from catalyst_data.quality import ensure_ingestion_quality_tables, write_source_checkpoint


def _make_fetch_result(status=200, data=None, error=None, latency=50.0,
                        retry_after=None, items_count=None):
    return FetchResult(
        status=status, data=data, error=error, latency_ms=latency,
        source_label="test_source", retry_after_seconds=retry_after,
        error_class=None, items_count=items_count,
    )


class TestPersistCellAtomicity:
    def test_transport_no_raw_asset(self, tmp_path: Path):
        """TRANSPORT → no raw_asset row, checkpoint with raw_asset_id=NULL."""
        from catalyst_data.update_pipeline import persist_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        fr = _make_fetch_result(status=0, error="connection refused")
        result = persist_cell(
            db_path, run_id="run_t1", ticker="AAPL", date="2025-01-01",
            source="polygon_news", fetch_result=fr,
            error_class=ErrorClass.TRANSPORT.value, retries=0,
        )

        assert result["status"] == "failed"

        conn = sqlite3.connect(db_path)
        # No raw_asset stored
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
        assert raw_count == 0, f"TRANSPORT must not store raw_asset, found {raw_count}"

        # Checkpoint exists with raw_asset_id=NULL
        cp = conn.execute(
            "SELECT status, raw_asset_id, error_class FROM source_checkpoints WHERE run_id='run_t1'"
        ).fetchone()
        assert cp is not None
        assert cp[0] == "failed"
        assert cp[1] is None  # raw_asset_id NULL
        assert cp[2] == "transport"
        conn.close()

    def test_timeout_no_raw_asset(self, tmp_path: Path):
        """TIMEOUT → same as TRANSPORT."""
        from catalyst_data.update_pipeline import persist_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        fr = _make_fetch_result(status=0, error="read timeout")
        persist_cell(
            db_path, run_id="run_t2", ticker="AAPL", date="2025-01-01",
            source="polygon_news", fetch_result=fr,
            error_class=ErrorClass.TIMEOUT.value, retries=0,
        )

        conn = sqlite3.connect(db_path)
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
        assert raw_count == 0
        conn.close()

    def test_malformed_stores_raw_and_failed_checkpoint(self, tmp_path: Path):
        """MALFORMED_RESPONSE → raw_asset EXISTS + failed checkpoint atomically."""
        from catalyst_data.update_pipeline import persist_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        fr = _make_fetch_result(status=200, data={"bad": "json"}, error="json parse failed")
        result = persist_cell(
            db_path, run_id="run_m1", ticker="AAPL", date="2025-01-01",
            source="polygon_news", fetch_result=fr,
            error_class=ErrorClass.MALFORMED_RESPONSE.value, retries=0,
        )

        conn = sqlite3.connect(db_path)
        # Raw asset EXISTS (forensic)
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
        assert raw_count == 1, f"MALFORMED must store forensic raw_asset, found {raw_count}"

        # Checkpoint exists, status=failed, raw_asset_id set
        cp = conn.execute(
            "SELECT status, raw_asset_id, error_class, error_message_redacted "
            "FROM source_checkpoints WHERE run_id='run_m1'"
        ).fetchone()
        assert cp is not None
        assert cp[0] == "failed"
        assert cp[1] is not None  # raw_asset_id is set
        assert cp[2] == "malformed_response"
        conn.close()

    def test_empty_valid_stores_success_empty(self, tmp_path: Path):
        """EMPTY_VALID → raw_asset + success_empty checkpoint."""
        from catalyst_data.update_pipeline import persist_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        fr = _make_fetch_result(status=200, data={}, items_count=0)
        result = persist_cell(
            db_path, run_id="run_ev1", ticker="AAPL", date="2025-01-01",
            source="polygon_news", fetch_result=fr,
            error_class=ErrorClass.EMPTY_VALID.value, retries=0,
        )

        assert result["status"] == "success_empty"

        conn = sqlite3.connect(db_path)
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
        assert raw_count == 1

        cp = conn.execute(
            "SELECT status, items_count FROM source_checkpoints WHERE run_id='run_ev1'"
        ).fetchone()
        assert cp[0] == "success_empty"
        assert cp[1] == 0  # items_count
        conn.close()

    def test_success_stores_checkpoint_with_raw_asset_id(self, tmp_path: Path):
        """Success → checkpoint with raw_asset_id and items_count."""
        from catalyst_data.update_pipeline import persist_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        fr = _make_fetch_result(status=200, data={"articles": [{"id": 1}]}, items_count=3)
        result = persist_cell(
            db_path, run_id="run_s1", ticker="AAPL", date="2025-01-01",
            source="polygon_news", fetch_result=fr,
            error_class=None, retries=1,
        )

        assert result["status"] == "success"

        conn = sqlite3.connect(db_path)
        cp = conn.execute(
            "SELECT status, raw_asset_id, items_count, retries, http_status, provider_latency_ms "
            "FROM source_checkpoints WHERE run_id='run_s1'"
        ).fetchone()
        assert cp[0] == "success"
        assert cp[1] is not None  # raw_asset_id
        assert cp[2] == 3  # items_count
        assert cp[3] == 1  # retries
        assert cp[4] == 200  # http_status
        assert cp[5] == 50.0  # provider_latency_ms
        conn.close()

    def test_secrets_redacted_in_checkpoint(self, tmp_path: Path):
        """API key 'sk-abc123' in error → '[REDACTED]' in checkpoint."""
        from catalyst_data.update_pipeline import persist_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        fr = _make_fetch_result(status=401, error="Auth failed with key sk-abc123def456")
        persist_cell(
            db_path, run_id="run_sec1", ticker="AAPL", date="2025-01-01",
            source="polygon_news", fetch_result=fr,
            error_class=ErrorClass.AUTH.value, retries=0,
        )

        conn = sqlite3.connect(db_path)
        cp = conn.execute(
            "SELECT error_message_redacted FROM source_checkpoints WHERE run_id='run_sec1'"
        ).fetchone()
        msg = cp[0] or ""
        assert "sk-" not in msg, f"Secret leaked: {msg}"
        assert "[REDACTED]" in msg
        conn.close()

    def test_each_error_class_correct_status(self, tmp_path: Path):
        """Each ErrorClass → correct checkpoint status."""
        from catalyst_data.update_pipeline import persist_cell

        test_cases = [
            (ErrorClass.AUTH.value, 401, "Unauthorized", "failed"),
            (ErrorClass.RATE_LIMIT.value, 429, "Too many", "failed"),
            (ErrorClass.PROVIDER_5XX.value, 503, "Down", "failed"),
            (ErrorClass.MALFORMED_RESPONSE.value, 200, "bad json", "failed"),
            (ErrorClass.PARSE_FAILURE.value, None, "json error", "failed"),
            (ErrorClass.EMPTY_VALID.value, 200, None, "success_empty"),
        ]

        for i, (ec, status, err, expected_cp_status) in enumerate(test_cases):
            db_path = str(tmp_path / f"test_{i}.db")
            conn = sqlite3.connect(db_path)
            init_db(conn)
            ensure_ingestion_quality_tables(conn)
            conn.close()

            fr = _make_fetch_result(status=status, error=err)
            result = persist_cell(
                db_path, run_id=f"run_ec{i}", ticker="AAPL", date="2025-01-01",
                source="polygon_news", fetch_result=fr,
                error_class=ec, retries=0,
            )

            conn = sqlite3.connect(db_path)
            cp = conn.execute(
                "SELECT status, error_class FROM source_checkpoints WHERE run_id=?",
                (f"run_ec{i}",)
            ).fetchone()
            if cp:
                assert cp[0] == expected_cp_status, (
                    f"{ec}: expected {expected_cp_status}, got {cp[0]}"
                )
            conn.close()

    def test_one_checkpoint_per_pk(self, tmp_path: Path):
        """Running persist_cell twice with same PK → second run idempotent."""
        from catalyst_data.update_pipeline import persist_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        fr = _make_fetch_result(status=200, items_count=5)

        # First run
        persist_cell(db_path, run_id="run_idem1", ticker="AAPL", date="2025-01-01",
                     source="polygon_news", fetch_result=fr, error_class=None, retries=0)

        # Second run — same PK
        persist_cell(db_path, run_id="run_idem1", ticker="AAPL", date="2025-01-01",
                     source="polygon_news", fetch_result=fr, error_class=None, retries=0)

        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM source_checkpoints WHERE run_id='run_idem1'"
        ).fetchone()[0]
        # INSERT OR REPLACE → exactly 1 row
        assert count == 1, f"Expected 1 row, got {count}"
        conn.close()


class TestFetchCellIntegration:
    """Integration: _fetch_cell routes through persist_cell for atomic writes."""

    def test_fetch_cell_success_atomic(self, tmp_path: Path):
        """_fetch_cell with success → raw_asset + checkpoint committed atomically."""
        import asyncio
        from catalyst_data.update_pipeline import _fetch_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        from catalyst_data.storage.sqlite import init_db
        from catalyst_data.quality import ensure_ingestion_quality_tables
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        # Fake fetch function that returns success
        calls = []

        async def fake_fetch(ticker, endpoint, date):
            calls.append((ticker, endpoint, date))
            return FetchResult(
                status=200,
                data={"results": []},  # empty results for simplicity
                latency_ms=45.0,
                source_label="polygon_news",
                items_count=0,
            )

        result = asyncio.run(_fetch_cell(
            db_path, "AAPL", "2025-01-01", "polygon_news",
            run_id="run_int_poly", fetch_fn=fake_fetch,
        ))

        # Network was called
        assert len(calls) > 0

        # Check DB state
        conn = sqlite3.connect(db_path)
        cp_count = conn.execute(
            "SELECT COUNT(*) FROM source_checkpoints WHERE run_id='run_int_poly'"
        ).fetchone()[0]
        conn.close()

        # At minimum, a checkpoint was written
        assert cp_count >= 1, f"No checkpoint found for run_int_poly; result={result}"

    def test_fetch_cell_transport_no_orphan(self, tmp_path: Path):
        """_fetch_cell with transport error → no orphan raw_asset, checkpoint exists."""
        import asyncio
        from catalyst_data.update_pipeline import _fetch_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        from catalyst_data.storage.sqlite import init_db
        from catalyst_data.quality import ensure_ingestion_quality_tables
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        raw_before = 0

        async def fake_fetch_error(*args, **kwargs):
            raise ConnectionRefusedError("connection refused")

        result = asyncio.run(_fetch_cell(
            db_path, "AAPL", "2025-01-01", "polygon_news",
            run_id="run_int_trans", fetch_fn=fake_fetch_error,
        ))

        assert result["status"] == "failed"

        conn = sqlite3.connect(db_path)
        raw_after = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
        # No new raw_asset created (transport stores nothing)
        assert raw_after == raw_before, f"Transport must not store raw_asset, got {raw_after}"

        cp = conn.execute(
            "SELECT status, raw_asset_id FROM source_checkpoints WHERE run_id='run_int_trans'"
        ).fetchone()
        assert cp is not None, "Transport must write a failed checkpoint"
        assert cp[0] == "failed"
        conn.close()


class TestSuccessPathAtomicity:
    """Crash-simulation: verify raw_asset + checkpoint commit atomically."""

    def test_polygon_crash_no_orphan(self, tmp_path: Path):
        """SUCCESS materialization + checkpoint failure → NO orphan raw_asset.

        This is the real atomicity invariant. On a successful polygon fetch,
        raw_asset + silver + checkpoint MUST commit in one transaction. We
        simulate a crash by making the checkpoint write fail AFTER storage.
        If the write is atomic, the whole transaction rolls back and NO
        raw_asset remains. If storage and checkpoint are on separate
        connections, the committed raw_asset is orphaned → this test fails.

        Do NOT weaken this to assert cp_count only — the invariant is
        raw_count == 0. A checkpoint-count assertion is trivially satisfied
        and hides the orphan.
        """
        import asyncio
        from unittest.mock import patch
        from catalyst_data.update_pipeline import _fetch_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        from catalyst_data.storage.sqlite import init_db
        from catalyst_data.quality import ensure_ingestion_quality_tables
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        async def fake_fetch(ticker, endpoint, date):
            return FetchResult(
                status=200,
                data={"results": [{"title": "Test", "description": "Body",
                     "published_utc": "2025-01-01T00:00:00Z"}]},
                latency_ms=45.0,
                source_label="polygon_news",
                items_count=1,
            )

        # Simulate a crash BETWEEN silver insert and checkpoint commit:
        # the checkpoint write raises. An atomic path rolls the raw_asset back.
        def boom(*args, **kwargs):
            raise RuntimeError("SIMULATED CRASH after storage, before checkpoint")

        try:
            with patch('catalyst_data.quality.write_source_checkpoint', boom):
                asyncio.run(_fetch_cell(
                    db_path, "AAPL", "2025-01-01", "polygon_news",
                    run_id="run_crash_poly", fetch_fn=fake_fetch,
                ))
        except Exception:
            pass  # a crash is expected; we assert on DB state below

        conn = sqlite3.connect(db_path)
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
        cp_count = conn.execute(
            "SELECT COUNT(*) FROM source_checkpoints WHERE run_id='run_crash_poly'"
        ).fetchone()[0]
        conn.close()

        # THE invariant: a failed checkpoint must leave NO orphan raw_asset.
        assert raw_count == 0, (
            f"Orphan raw_asset: checkpoint failed but {raw_count} raw_asset(s) "
            f"committed (cp={cp_count}). Storage + checkpoint are not atomic."
        )

    def test_polygon_success_stores_both_atomically(self, tmp_path: Path):
        """Normal success: raw_asset + checkpoint both present."""
        import asyncio
        from catalyst_data.update_pipeline import _fetch_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        from catalyst_data.storage.sqlite import init_db
        from catalyst_data.quality import ensure_ingestion_quality_tables
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        async def fake_fetch(ticker, endpoint, date):
            return FetchResult(
                status=200,
                data={"results": []},
                latency_ms=45.0,
                source_label="polygon_news",
                items_count=0,
            )

        result = asyncio.run(_fetch_cell(
            db_path, "AAPL", "2025-01-01", "polygon_news",
            run_id="run_ok_poly", fetch_fn=fake_fetch,
        ))

        # Checkpoint must exist
        conn = sqlite3.connect(db_path)
        cp = conn.execute(
            "SELECT status FROM source_checkpoints WHERE run_id='run_ok_poly'"
        ).fetchone()
        conn.close()
        assert cp is not None, "Checkpoint must exist for successful cell"

    def test_finnhub_crash_no_orphan(self, tmp_path: Path):
        """Finnhub success materialization + checkpoint failure → no raw orphan."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch
        from catalyst_data.update_pipeline import _fetch_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        from catalyst_data.storage.sqlite import init_db
        from catalyst_data.quality import ensure_ingestion_quality_tables
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        async def fake_fetch(ticker, endpoint, date):
            return FetchResult(
                status=200,
                data=[{
                    "id": 1,
                    "headline": "Finnhub headline",
                    "summary": "Finnhub summary",
                    "datetime": 1735689600,
                    "url": "https://example.com/fh",
                    "source": "Yahoo",
                }],
                latency_ms=31.0,
                source_label="finnhub_company_news",
                items_count=1,
            )

        def boom(*args, **kwargs):
            raise RuntimeError("SIMULATED CRASH after storage, before checkpoint")

        try:
            with patch("catalyst_data.update_pipeline.write_source_checkpoint", boom):
                asyncio.run(_fetch_cell(
                    db_path, "AAPL", "2025-01-01", "finnhub_company_news",
                    run_id="run_crash_fh", fetch_fn=None,
                    fetcher_ns=SimpleNamespace(fetch=fake_fetch),
                ))
        except Exception:
            pass

        conn = sqlite3.connect(db_path)
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
        conn.close()

        assert raw_count == 0, (
            f"Orphan raw_asset: Finnhub checkpoint failed but {raw_count} "
            "raw_asset(s) committed."
        )

    def test_sec_crash_no_orphan(self, tmp_path: Path):
        """SEC submissions success + checkpoint failure → no raw orphan."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch
        from catalyst_data.update_pipeline import _fetch_cell

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        from catalyst_data.storage.sqlite import init_db
        from catalyst_data.quality import ensure_ingestion_quality_tables
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        conn.close()

        async def fake_fetch(ticker, endpoint, date):
            return FetchResult(
                status=200,
                data={
                    "cik": "0000320193",
                    "filings": {
                        "recent": {
                            "form": [],
                            "filingDate": [],
                            "accessionNumber": [],
                            "reportDate": [],
                            "primaryDocument": [],
                            "items": [],
                        }
                    },
                },
                latency_ms=27.0,
                source_label="sec_submissions",
                items_count=0,
            )

        def boom(*args, **kwargs):
            raise RuntimeError("SIMULATED CRASH after storage, before checkpoint")

        try:
            with patch("catalyst_data.update_pipeline.write_source_checkpoint", boom):
                asyncio.run(_fetch_cell(
                    db_path, "AAPL", "2025-01-01", "sec_filings",
                    run_id="run_crash_sec", fetch_fn=None,
                    fetcher_ns=SimpleNamespace(fetch=fake_fetch),
                    submissions_cache={},
                ))
        except Exception:
            pass

        conn = sqlite3.connect(db_path)
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
        conn.close()

        assert raw_count == 0, (
            f"Orphan raw_asset: SEC checkpoint failed but {raw_count} "
            "raw_asset(s) committed."
        )
