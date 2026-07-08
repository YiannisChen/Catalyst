"""Regression test: fundamentals endpoint must not return snapshots whose
fiscal period end (date field) or filing date falls after the trade_date.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from catalyst_app.main import create_app
from catalyst_app.workbench_store import WorkbenchStore


class FakeLoader:
    def health(self):
        return {"status": "ready", "sqlite": {"status": "ready"},
                "lancedb": {"status": "ready"}, "embedding": {"status": "ready"},
                "reranker": {"status": "ready"}, "default_model": {"status": "ready", "model": "m"},
                "errors": []}


def _make_fundamentals_db(path):
    """Build a test DB with fundamentals snapshots, including one whose
    content contains a fiscal date AFTER the trade_date (look-ahead leak).
    """
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ohlcv (
            symbol TEXT NOT NULL, date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT
        )
        """
    )
    conn.execute("INSERT INTO ohlcv VALUES ('AAPL','2025-09-08',100,101,99,100.5,100,'polygon')")
    conn.execute(
        """
        CREATE TABLE clean_assets (
            asset_id TEXT, ticker TEXT, source_type TEXT, reference_date TEXT,
            cleaned_at TEXT, content_md TEXT, title_hash TEXT,
            is_duplicate INTEGER, is_rag_eligible INTEGER, is_canonical INTEGER,
            published_utc TEXT
        )
        """
    )
    # Snapshot A: Q4 2025 report (filed Oct 31, period end Sep 27) — stored with
    # reference_date 2025-09-08 but the content reveals future information
    conn.execute(
        "INSERT INTO clean_assets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("a1", "AAPL", "fmp_fundamentals", "2025-09-08", "2025-10-31",
         "| Field | Value |\n| date | 2025-09-27 |\n| filingDate | 2025-10-31 |\n| acceptedDate | 2025-10-31 06:01:26 |\n| revenue | 94930000000 |",
         "h1", 0, 1, 1, "2025-10-31"),
    )
    # Snapshot B: Q3 2025 report (period end Jun 28) — truly available before Sep 8
    conn.execute(
        "INSERT INTO clean_assets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("a2", "AAPL", "fmp_fundamentals", "2025-06-28", "2025-08-01",
         "| Field | Value |\n| date | 2025-06-28 |\n| filingDate | 2025-08-01 |\n| revenue | 85777000000 |",
         "h2", 0, 1, 1, "2025-08-01"),
    )
    conn.commit()
    conn.close()


def _client(store):
    app = create_app(
        workbench_store_override=store,
        service_override=None,
        dependency_loader_override=FakeLoader(),
    )
    app.dependency_overrides = {}
    # Re-register overrides
    from catalyst_app.dependencies import get_workbench_store
    app.dependency_overrides[get_workbench_store] = lambda: store
    return TestClient(app)


def test_fundamentals_excludes_future_fiscal_period(tmp_path):
    """On 2025-09-08, only the Q3 snapshot (date <= 2025-09-08) should be
    returned. The Q4 snapshot whose fiscal date is 2025-09-27 must be skipped."""
    db_path = tmp_path / "fund.db"
    _make_fundamentals_db(db_path)
    client = _client(WorkbenchStore(db_path=db_path))

    resp = client.get("/api/fundamentals/AAPL", params={"trade_date": "2025-09-08"})

    assert resp.status_code == 200
    payload = resp.json()
    # Must NOT return the Q4 snapshot (which has revenue 94930000000)
    assert payload["metrics"].get("revenue") == "85777000000", \
        f"Expected Q3 revenue, got metrics={payload['metrics']}"
    # The reference_date should reflect the selected snapshot
    assert payload["reference_date"] == "2025-06-28", \
        f"Expected reference_date 2025-06-28, got {payload['reference_date']}"


def test_fundamentals_returns_last_available_when_no_future_leak(tmp_path):
    """When no future look-ahead exists, the most recent snapshot is returned."""
    db_path = tmp_path / "fund2.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)"
    )
    conn.execute("INSERT INTO ohlcv VALUES ('AAPL','2025-09-08',100,101,99,100.5,100,'polygon')")
    conn.execute(
        """CREATE TABLE clean_assets (
            asset_id TEXT, ticker TEXT, source_type TEXT, reference_date TEXT,
            cleaned_at TEXT, content_md TEXT, title_hash TEXT,
            is_duplicate INTEGER, is_rag_eligible INTEGER, is_canonical INTEGER,
            published_utc TEXT
        )"""
    )
    # Only Q3 snapshot, safe
    conn.execute(
        "INSERT INTO clean_assets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("a2", "AAPL", "fmp_fundamentals", "2025-06-28", "2025-08-01",
         "| Field | Value |\n| date | 2025-06-28 |\n| revenue | 85777000000 |",
         "h2", 0, 1, 1, "2025-08-01"),
    )
    conn.commit()
    conn.close()

    client = _client(WorkbenchStore(db_path=db_path))
    resp = client.get("/api/fundamentals/AAPL", params={"trade_date": "2025-09-08"})
    assert resp.status_code == 200
    assert resp.json()["metrics"].get("revenue") == "85777000000"
