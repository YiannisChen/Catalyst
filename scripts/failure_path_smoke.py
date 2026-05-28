from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from catalyst_agents.runtime.service import LiveRunService
from catalyst_agents.trace.writer import TraceWriter
from catalyst_app.main import create_app
from catalyst_app.workbench_store import WorkbenchStore


class Loader:
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


class BoomGraph:
    def invoke(self, state, run_id=None):
        raise RuntimeError('graph boom')


class TimeoutGraph:
    def invoke(self, state, run_id=None):
        time.sleep(0.05)
        return {'output_status': 'SUFFICIENT'}


class StatusGraph:
    def __init__(self, db_path: Path, status: str):
        self.db_path = db_path
        self.status = status

    def invoke(self, state, run_id=None):
        with TraceWriter(db_path=self.db_path, run_id=run_id, ticker=state['ticker'], trade_date=state['trade_date'], config='mcj_full') as writer:
            writer.complete({'output_status': self.status})
        return {'output_status': self.status}


def _prepare_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)')
    conn.execute('INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)', ('AAPL', '2026-01-15', 1, 2, 0.5, 1.5, 1000.0, 'polygon'))
    conn.commit()
    conn.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / 'failure.db'
        _prepare_db(db)

        # FAILED_REQUEST
        svc_ok = LiveRunService(db_path=db, graph_factory=lambda: StatusGraph(db, 'SUFFICIENT'))
        client_ok = TestClient(create_app(service_override=svc_ok, dependency_loader_override=Loader(), workbench_store_override=WorkbenchStore(db_path=db)))
        assert client_ok.post('/api/live-runs', json={'ticker': 'TSLA', 'trade_date': '2026-01-15', 'query': 'q'}).json()['status'] == 'FAILED_REQUEST'

        # FAILED_SYSTEM from exception
        svc_boom = LiveRunService(db_path=db, graph_factory=lambda: BoomGraph())
        client_boom = TestClient(create_app(service_override=svc_boom, dependency_loader_override=Loader(), workbench_store_override=WorkbenchStore(db_path=db)))
        run_id = client_boom.post('/api/live-runs', json={'ticker': 'AAPL', 'trade_date': '2026-01-15', 'query': 'q'}).json()['run_id']
        assert client_boom.get(f'/api/live-runs/{run_id}').json()['status'] == 'FAILED_SYSTEM'

        # FAILED_SYSTEM timeout
        svc_timeout = LiveRunService(db_path=db, graph_factory=lambda: TimeoutGraph(), timeout_seconds=0.001)
        client_timeout = TestClient(create_app(service_override=svc_timeout, dependency_loader_override=Loader(), workbench_store_override=WorkbenchStore(db_path=db)))
        timeout_id = client_timeout.post('/api/live-runs', json={'ticker': 'AAPL', 'trade_date': '2026-01-15', 'query': 'q'}).json()['run_id']
        timeout_payload = client_timeout.get(f'/api/live-runs/{timeout_id}').json()
        assert timeout_payload['status'] == 'FAILED_SYSTEM'
        assert timeout_payload['failure']['sub_reason'] == 'timeout'

        # INSUFFICIENT and PARTIAL
        svc_insufficient = LiveRunService(db_path=db, graph_factory=lambda: StatusGraph(db, 'INSUFFICIENT'))
        client_insufficient = TestClient(create_app(service_override=svc_insufficient, dependency_loader_override=Loader(), workbench_store_override=WorkbenchStore(db_path=db)))
        insufficient_id = client_insufficient.post('/api/live-runs', json={'ticker': 'AAPL', 'trade_date': '2026-01-15', 'query': 'q'}).json()['run_id']
        assert client_insufficient.get(f'/api/live-runs/{insufficient_id}').json()['status'] == 'INSUFFICIENT'

        svc_partial = LiveRunService(db_path=db, graph_factory=lambda: StatusGraph(db, 'PARTIAL'))
        client_partial = TestClient(create_app(service_override=svc_partial, dependency_loader_override=Loader(), workbench_store_override=WorkbenchStore(db_path=db)))
        partial_id = client_partial.post('/api/live-runs', json={'ticker': 'AAPL', 'trade_date': '2026-01-15', 'query': 'q'}).json()['run_id']
        assert client_partial.get(f'/api/live-runs/{partial_id}').json()['status'] == 'PARTIAL'

    print('failure path smoke passed')


if __name__ == '__main__':
    main()
