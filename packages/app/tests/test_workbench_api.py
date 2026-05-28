from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from catalyst_app.main import create_app
from catalyst_app.workbench_store import WorkbenchStore


class FakeService:
    def create_run(self, **kwargs):
        return {"run_id": "r1", "status": "QUEUED", "failure": None}

    def get_run(self, run_id):
        return {"run_id": run_id, "status": "RUNNING"}

    def get_events(self, run_id, *, after_seq=None):
        return []

    def get_artifacts(self, run_id, *, event_seq=None, artifact_type=None):
        return []

    def retry_run(self, run_id, *, model=None):
        return {"ok": True, "run_id": "r2", "status": "QUEUED"}


class FakeLoader:
    def health(self):
        return {
            "status": "ready",
            "sqlite": {"status": "ready"},
            "lancedb": {"status": "ready"},
            "embedding": {"status": "ready"},
            "reranker": {"status": "ready"},
            "default_model": {"status": "ready", "model": "model-default"},
            "errors": [],
        }


def _build_test_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ohlcv (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            source TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO ohlcv (symbol, date, open, high, low, close, volume, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("AAPL", "2026-01-15", 10.0, 12.0, 9.0, 11.0, 1000, "polygon"),
            ("AAPL", "2026-01-16", 11.0, 13.0, 10.0, 12.0, 1200, "polygon"),
            ("MSFT", "2026-01-15", 20.0, 21.0, 19.5, 20.5, 900, "polygon"),
        ],
    )
    conn.commit()
    conn.close()


def _client_with_store(store):
    app = create_app(
        service_override=FakeService(),
        dependency_loader_override=FakeLoader(),
        workbench_store_override=store,
    )
    return TestClient(app)


def test_tickers_distinct_sorted_and_count(tmp_path):
    db_path = tmp_path / "workbench.db"
    _build_test_db(db_path)
    client = _client_with_store(WorkbenchStore(db_path=db_path))

    response = client.get("/api/tickers")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"symbols": ["AAPL", "MSFT"], "count": 2}


def test_ohlcv_ticker_success_and_date_filters(tmp_path):
    db_path = tmp_path / "workbench.db"
    _build_test_db(db_path)
    client = _client_with_store(WorkbenchStore(db_path=db_path))

    all_rows = client.get("/api/ohlcv/AAPL")
    filtered = client.get("/api/ohlcv/AAPL", params={"start_date": "2026-01-16", "end_date": "2026-01-16"})

    assert all_rows.status_code == 200
    assert all_rows.json()["count"] == 2
    assert filtered.status_code == 200
    assert filtered.json()["count"] == 1
    assert filtered.json()["candles"][0]["date"] == "2026-01-16"


def test_ohlcv_unknown_ticker_or_empty_range_returns_empty_candles(tmp_path):
    db_path = tmp_path / "workbench.db"
    _build_test_db(db_path)
    client = _client_with_store(WorkbenchStore(db_path=db_path))

    unknown = client.get("/api/ohlcv/TSLA")
    empty_range = client.get("/api/ohlcv/AAPL", params={"start_date": "2027-01-01", "end_date": "2027-01-02"})

    assert unknown.status_code == 200
    assert unknown.json()["count"] == 0
    assert unknown.json()["candles"] == []
    assert empty_range.status_code == 200
    assert empty_range.json()["count"] == 0


def test_range_local_aggregate(tmp_path):
    db_path = tmp_path / "workbench.db"
    _build_test_db(db_path)
    client = _client_with_store(WorkbenchStore(db_path=db_path))

    response = client.get("/api/range-local")

    assert response.status_code == 200
    payload = response.json()
    assert payload["min_date"] == "2026-01-15"
    assert payload["max_date"] == "2026-01-16"
    assert payload["ticker_count"] == 2
    assert payload["row_count"] == 3


def test_missing_db_returns_clear_error(tmp_path):
    missing_db = tmp_path / "missing.db"
    client = _client_with_store(WorkbenchStore(db_path=missing_db))

    response = client.get("/api/tickers")

    assert response.status_code == 503
    assert "SQLite DB path does not exist" in response.json()["detail"]


def test_missing_table_returns_clear_error(tmp_path):
    db_path = tmp_path / "workbench.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE not_ohlcv (id INTEGER)")
    conn.commit()
    conn.close()
    client = _client_with_store(WorkbenchStore(db_path=db_path))

    response = client.get("/api/range-local")

    assert response.status_code == 503
    assert response.json()["detail"] == "Missing required table: ohlcv"
