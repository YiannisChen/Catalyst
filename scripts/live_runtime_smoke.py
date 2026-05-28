from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from catalyst_agents.runtime.service import LiveRunService
from catalyst_agents.trace.artifacts import write_node_artifact
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


class Graph:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def invoke(self, state, run_id=None):
        with TraceWriter(db_path=self.db_path, run_id=run_id, ticker=state['ticker'], trade_date=state['trade_date'], config='mcj_full') as writer:
            seq = writer.event(
                node='critic',
                started_at='2026-01-15T00:00:00Z',
                ended_at='2026-01-15T00:00:01Z',
                latency_ms=10,
                model_id='model-default',
                input_tokens=1,
                output_tokens=2,
                cost_usd=0.01,
                decision=None,
                error_type=None,
                error_message=None,
                status_before='RUNNING',
                status_after='SUFFICIENT',
            )
            write_node_artifact(writer.conn, run_id=writer.run_id, event_seq=seq, node='critic', artifact_type='raw_llm_response', payload={'text': 'mock raw response'})
            writer.complete({'output_status': 'SUFFICIENT', 'summary_md': 'done', 'total_cost_usd': 0.01})
        return {'output_status': 'SUFFICIENT', 'summary_md': 'done'}


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / 'runtime.db'
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)')
        conn.execute('INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)', ('AAPL', '2026-01-15', 1, 2, 0.5, 1.5, 1000.0, 'polygon'))
        conn.commit()
        conn.close()

        service = LiveRunService(db_path=db, graph_factory=lambda: Graph(db))
        client = TestClient(create_app(service_override=service, dependency_loader_override=Loader(), workbench_store_override=WorkbenchStore(db_path=db)))

        created = client.post('/api/live-runs', json={'ticker': 'AAPL', 'trade_date': '2026-01-15', 'query': 'explain', 'model_id': 'model-default'})
        run_id = created.json()['run_id']
        assert created.status_code == 200
        assert client.get(f'/api/live-runs/{run_id}').json()['status'] != 'QUEUED'
        assert len(client.get(f'/api/live-runs/{run_id}/events').json()) >= 1
        arts = client.get(f'/api/live-runs/{run_id}/artifacts', params={'artifact_type': 'raw_llm_response'}).json()
        assert len(arts) >= 1 and 'payload' in arts[0]
        retry = client.post(f'/api/live-runs/{run_id}/retry', json={'model_id': 'model-fast'}).json()
        assert retry['ok'] is True and retry['run_id'] != run_id
        assert client.get(f"/api/live-runs/{retry['run_id']}").json()['status'] != 'QUEUED'

    print('live runtime smoke passed')


if __name__ == '__main__':
    main()
