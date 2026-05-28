from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
from catalyst_app.main import create_app
from catalyst_app.workbench_store import WorkbenchStore


DEFAULT_DB_PATH = Path('/Users/yiannischen/Desktop/Catalyst/data/catalyst_eval_frozen_v2.db')
DEFAULT_LANCEDB_DIR = Path('/Users/yiannischen/Desktop/Catalyst/data/lancedb_gold/eval_frozen')
DEFAULT_ENV_FILE = Path('/Users/yiannischen/Desktop/Catalyst/packages/data-core/.env')


class NoopService:
    def create_run(self, **kwargs):
        return {"run_id": "noop", "status": "FAILED_REQUEST", "failure": {"status": "FAILED_REQUEST", "sub_reason": "not_enabled", "message": "Runtime execution is not enabled in real-data smoke.", "retryable": False}}

    def run_one(self, run_id):
        return {"ok": False, "run_id": run_id, "status": "SKIPPED"}

    def get_run(self, run_id):
        return None

    def get_events(self, run_id, *, after_seq=None):
        return []

    def get_artifacts(self, run_id, *, event_seq=None, artifact_type=None):
        return []

    def retry_run(self, run_id, *, model=None):
        return {"ok": False, "failure": {"status": "FAILED_REQUEST", "sub_reason": "not_enabled", "message": "Runtime execution is not enabled in real-data smoke.", "retryable": False}}


def _load_key_presence() -> bool:
    if os.getenv('aihubmix_api_key'):
        return True
    if not DEFAULT_ENV_FILE.exists():
        return False
    for line in DEFAULT_ENV_FILE.read_text().splitlines():
        clean = line.strip()
        if clean.startswith('aihubmix_api_key=') and clean.split('=', 1)[1].strip():
            return True
    return False


def _detect_lancedb_vector_dim(lancedb_dir: Path) -> int | None:
    try:
        import lancedb  # type: ignore
    except Exception:
        return None
    try:
        db = lancedb.connect(str(lancedb_dir))
        table = db.open_table('chunks')
        rows = table.search('health', query_type='fts').limit(1).to_list()
        if rows and isinstance(rows[0], dict) and rows[0].get('vector') is not None:
            return len(rows[0]['vector'])
    except Exception:
        return None
    return None


def main() -> None:
    db_path = Path(os.getenv('CATALYST_DB_PATH', str(DEFAULT_DB_PATH)))
    lancedb_dir = Path(os.getenv('CATALYST_LANCEDB_DIR', str(DEFAULT_LANCEDB_DIR)))

    os.environ['CATALYST_DB_PATH'] = str(db_path)
    os.environ['CATALYST_LANCEDB_DIR'] = str(lancedb_dir)

    loader = RuntimeDependencyLoader(sqlite_db_path=db_path, lancedb_dir=lancedb_dir)
    app = create_app(
        service_override=NoopService(),
        dependency_loader_override=loader,
        workbench_store_override=WorkbenchStore(db_path=db_path),
    )
    client = TestClient(app)

    tickers = client.get('/api/tickers')
    range_local = client.get('/api/range-local')
    ohlcv = client.get('/api/ohlcv/AAPL', params={'start_date': '2025-05-02', 'end_date': '2025-05-02'})
    health = client.get('/api/health/runtime')

    assert tickers.status_code == 200
    assert tickers.json()['count'] == 10
    assert range_local.status_code == 200
    assert range_local.json()['min_date'] is not None
    assert range_local.json()['max_date'] is not None
    assert range_local.json()['row_count'] > 0
    assert ohlcv.status_code == 200
    assert ohlcv.json()['count'] >= 1
    assert isinstance(ohlcv.json()['candles'][0]['volume'], (int, float))
    assert health.status_code == 200

    runtime_health = health.json()
    lancedb_status = runtime_health['lancedb']['status']
    embedding_status = runtime_health['embedding']['status']
    vector_dim_from_health = runtime_health['lancedb'].get('vector_dim')
    vector_dim = vector_dim_from_health or _detect_lancedb_vector_dim(lancedb_dir)

    assert runtime_health['status'] in {'ready', 'degraded'}, (
        f"runtime health status unexpected: {runtime_health['status']}; "
        f"lancedb={runtime_health.get('lancedb')}, embedding={runtime_health.get('embedding')}"
    )
    assert lancedb_status != 'failed', (
        f"lancedb status failed: {runtime_health.get('lancedb')}"
    )
    assert embedding_status == 'ready', (
        f"embedding status not ready: {runtime_health.get('embedding')}"
    )
    assert vector_dim is not None, (
        f"vector dim missing; lancedb={runtime_health.get('lancedb')}"
    )
    assert vector_dim == 1024, f"unexpected vector dim: {vector_dim}"
    if vector_dim_from_health is not None:
        assert int(vector_dim_from_health) == int(vector_dim), (
            f"vector dim mismatch: health={vector_dim_from_health}, detected={vector_dim}"
        )

    provider_mode = os.getenv('CATALYST_RUN_REAL_PROVIDER_SMOKE', '0') == '1'
    provider_presence = _load_key_presence()
    provider_summary = 'skipped'
    if provider_mode:
        provider_summary = 'key_present' if provider_presence else 'key_missing'

    print('DB ok:', db_path)
    print('tickers count:', tickers.json()['count'])
    print('date range:', range_local.json()['min_date'], '->', range_local.json()['max_date'])
    print('LanceDB status:', lancedb_status)
    print('embedding dim:', vector_dim)
    print('provider:', provider_summary)


if __name__ == '__main__':
    main()
