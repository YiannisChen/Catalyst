from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from catalyst_app.main import create_app
from catalyst_app.workbench_store import WorkbenchStore


class Service:
    def create_run(self, **kwargs):
        if kwargs.get("ticker") == "BAD":
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

    def run_one(self, run_id):
        return {"ok": True, "run_id": run_id, "status": "COMPLETED"}

    def get_run(self, run_id):
        if run_id != "run-1":
            return None
        return {
            "run_id": "run-1",
            "status": "RUNNING",
            "last_completed_node": "critic",
            "predicted_next_node": "judge",
        }

    def get_events(self, run_id, *, after_seq=None):
        return [
            {"run_id": run_id, "event_seq": 1, "node": "miner", "status_after": "SUFFICIENT"},
            {"run_id": run_id, "event_seq": 2, "node": "system_error_handler", "status_after": "SYSTEM_ERROR"},
        ]

    def get_artifacts(self, run_id, *, event_seq=None, artifact_type=None):
        return [{"run_id": run_id, "event_seq": 1, "node": "miner", "artifact_type": "state_snapshot", "payload_json": {"state": {}}}]

    def retry_run(self, run_id, *, model=None):
        if run_id == "missing":
            return {"ok": False, "failure": {"sub_reason": "run_not_found", "message": "Run not found."}}
        return {"ok": True, "run_id": "run-2", "status": "QUEUED"}


class Loader:
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


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "contract.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)")
        conn.execute("INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("AAPL", "2025-05-02", 1, 2, 0.5, 1.5, 101010621.0, "polygon"))
        conn.commit()
        conn.close()

        client = TestClient(create_app(service_override=Service(), dependency_loader_override=Loader(), workbench_store_override=WorkbenchStore(db_path=db)))

        assert client.post('/api/live-runs', json={"ticker":"AAPL","trade_date":"2025-05-02","query":"q"}).status_code == 200
        run = client.get('/api/live-runs/run-1').json()
        assert 'current_node' not in run and 'last_completed_node' in run and 'predicted_next_node' in run
        events = client.get('/api/live-runs/run-1/events').json()
        assert any(e['status_after'] == 'SUFFICIENT' for e in events)
        assert any(e['status_after'] == 'SYSTEM_ERROR' for e in events)
        artifacts = client.get('/api/live-runs/run-1/artifacts').json()
        assert 'payload' in artifacts[0] and 'payload_json' not in artifacts[0]
        assert client.post('/api/live-runs/run-1/retry', json={"model_id":"model-fast"}).status_code == 200
        assert client.get('/api/health/runtime').status_code == 200
        assert isinstance(client.get('/api/ohlcv/AAPL', params={"start_date":"2025-05-02","end_date":"2025-05-02"}).json()['candles'][0]['volume'], (int, float))
        assert client.get('/api/range-local').status_code == 200

    print('API contract smoke passed')


if __name__ == '__main__':
    main()
