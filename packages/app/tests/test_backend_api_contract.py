from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from catalyst_app.main import create_app
from catalyst_app.workbench_store import WorkbenchStore


class ContractService:
    def __init__(self):
        self._run = {
            "run_id": "run-1",
            "status": "RUNNING",
            "last_completed_node": "critic",
            "predicted_next_node": "judge",
            "model_id": "model-default",
        }

    def create_run(self, *, ticker, trade_date, query=None, model=None, config="mcj_full"):
        if ticker == "BAD":
            return {
                "run_id": "run-bad",
                "status": "FAILED_REQUEST",
                "failure": {
                    "status": "FAILED_REQUEST",
                    "sub_reason": "unsupported_ticker",
                    "message": "Ticker is not available in the runtime dataset.",
                    "retryable": False,
                },
            }
        return {"run_id": "run-1", "status": "QUEUED", "failure": None}

    def run_next(self):
        return {"status": "SUCCEEDED"}

    def get_run(self, run_id):
        if run_id != "run-1":
            return None
        return self._run

    def get_events(self, run_id, *, after_seq=None):
        return [
            {
                "run_id": run_id,
                "trace_id": "t-1",
                "event_seq": 1,
                "node": "miner",
                "status_before": "RUNNING",
                "status_after": "SUFFICIENT",
                "model_id": "model-default",
            },
            {
                "run_id": run_id,
                "trace_id": "t-1",
                "event_seq": 2,
                "node": "system_error_handler",
                "status_before": "RUNNING",
                "status_after": "SYSTEM_ERROR",
                "model_id": "model-default",
            },
        ]

    def get_artifacts(self, run_id, *, event_seq=None, artifact_type=None):
        return [
            {
                "run_id": run_id,
                "event_seq": event_seq or 1,
                "node": "miner",
                "artifact_type": artifact_type or "state_snapshot",
                "payload_json": {"state": {"phase": "miner"}},
            }
        ]

    def retry_run(self, run_id, *, model=None):
        if run_id == "missing":
            return {
                "ok": False,
                "failure": {
                    "status": "FAILED_REQUEST",
                    "sub_reason": "run_not_found",
                    "message": "Run not found.",
                    "retryable": False,
                },
            }
        return {"ok": True, "run_id": "run-2", "status": "QUEUED"}


class ContractLoader:
    def health(self):
        return {
            "status": "degraded",
            "sqlite": {"status": "ready"},
            "lancedb": {"status": "ready"},
            "embedding": {"status": "ready"},
            "reranker": {"status": "degraded"},
            "default_model": {"status": "ready", "model": "model-default"},
            "errors": [],
        }


def _make_db(path):
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
        "INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("AAPL", "2025-05-02", 100.0, 101.0, 99.0, 100.5, 101010621.0, "polygon"),
            ("MSFT", "2025-05-02", 200.0, 201.0, 199.0, 200.5, 2020.0, "polygon"),
        ],
    )
    conn.commit()
    conn.close()


def _client(store):
    app = create_app(
        service_override=ContractService(),
        dependency_loader_override=ContractLoader(),
        workbench_store_override=store,
    )
    return TestClient(app)


def test_backend_api_contract_endpoints(tmp_path):
    db_path = tmp_path / "contract.db"
    _make_db(db_path)
    client = _client(WorkbenchStore(db_path=db_path))

    create_ok = client.post("/api/live-runs", json={"ticker": "AAPL", "trade_date": "2025-05-02", "query": "q"})
    create_failed = client.post("/api/live-runs", json={"ticker": "BAD", "trade_date": "2025-05-02", "query": "q"})
    run_ok = client.get("/api/live-runs/run-1")
    run_missing = client.get("/api/live-runs/unknown")
    events = client.get("/api/live-runs/run-1/events")
    artifacts = client.get("/api/live-runs/run-1/artifacts")
    retry_ok = client.post("/api/live-runs/run-1/retry", json={"model_id": "model-fast"})
    retry_missing = client.post("/api/live-runs/missing/retry", json={"model_id": "model-fast"})
    health = client.get("/api/health/runtime")
    tickers = client.get("/api/tickers")
    ohlcv = client.get("/api/ohlcv/AAPL", params={"start_date": "2025-05-02", "end_date": "2025-05-02"})
    range_local = client.get("/api/range-local")

    assert create_ok.status_code == 200
    assert create_ok.json()["status"] == "QUEUED"
    assert create_failed.status_code == 200
    assert create_failed.json()["status"] == "FAILED_REQUEST"
    assert run_ok.status_code == 200
    assert "current_node" not in run_ok.json()
    assert "last_completed_node" in run_ok.json()
    assert "predicted_next_node" in run_ok.json()
    assert run_missing.status_code == 404
    assert events.status_code == 200
    event_statuses = [item["status_after"] for item in events.json()]
    assert "SUFFICIENT" in event_statuses
    assert "SYSTEM_ERROR" in event_statuses
    assert artifacts.status_code == 200
    assert "payload" in artifacts.json()[0]
    assert "payload_json" not in artifacts.json()[0]
    assert retry_ok.status_code == 200
    assert retry_ok.json()["ok"] is True
    assert retry_missing.status_code == 200
    assert retry_missing.json()["ok"] is False
    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert tickers.status_code == 200
    assert tickers.json() == {"symbols": ["AAPL", "MSFT"], "count": 2}
    assert ohlcv.status_code == 200
    assert isinstance(ohlcv.json()["candles"][0]["volume"], (int, float))
    assert range_local.status_code == 200
    assert range_local.json()["row_count"] == 2


def test_missing_db_and_missing_ohlcv_table_return_clear_503(tmp_path):
    missing_db_client = _client(WorkbenchStore(db_path=tmp_path / "missing.db"))
    missing_db_resp = missing_db_client.get("/api/tickers")
    assert missing_db_resp.status_code == 503
    assert "SQLite DB path does not exist" in missing_db_resp.json()["detail"]

    tableless_db = tmp_path / "tableless.db"
    conn = sqlite3.connect(tableless_db)
    conn.execute("CREATE TABLE other_table (id INTEGER)")
    conn.commit()
    conn.close()

    tableless_client = _client(WorkbenchStore(db_path=tableless_db))
    missing_table_resp = tableless_client.get("/api/range-local")
    assert missing_table_resp.status_code == 503
    assert missing_table_resp.json()["detail"] == "Missing required table: ohlcv"
