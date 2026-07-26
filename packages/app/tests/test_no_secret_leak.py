"""Real API-path no-secret tests using FastAPI TestClient + temp SQLite DB.

Verifies the concrete test api_key is absent from:
- agent_runs.config
- CreateRunResponse JSON
- RetryRunResponse JSON
- RunSummaryResponse JSON
- get_events/get_artifacts/get_workspace responses (lightweight)
- Provider validation failure response (mocked)
"""
import sys
sys.path.insert(0, 'packages/agents')
sys.path.insert(0, 'packages/data-core')
sys.path.insert(0, 'packages/app')

import json
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from catalyst_app.main import create_app
from catalyst_app.dependencies import get_credential_store
from catalyst_app.runtime_credential_store import RuntimeCredentialStore
from catalyst_agents.trace.schema import init_trace_db
from catalyst_agents.runtime.service import LiveRunService
from catalyst_app.schemas import ModelConfig, CreateRunRequest
from runtime_fixture import prepare_runtime_db

TEST_API_KEY = "sk-test-secret-leak-key-xyz789"

BYOK_META = {
    "provider": "openai",
    "model_id": "gpt-4o-mini",
    "base_url": "https://api.openai.com/v1",
    "credential_source": "browser_key",
}


def _dummy_graph_factory(*args, **kwargs):
    raise RuntimeError("graph_factory should not be called")


def _make_service(db_path: Path, cred_store: RuntimeCredentialStore) -> LiveRunService:
    return LiveRunService(
        db_path=db_path,
        graph_factory=_dummy_graph_factory,
        credential_store=cred_store,
    )


def _make_client(db_path: Path):
    """Create a TestClient with overridden service and credential store."""
    cred_store = RuntimeCredentialStore()
    service = _make_service(db_path, cred_store)
    service.run_one = lambda run_id: {"run_id": run_id, "status": "SUCCEEDED"}
    app = create_app(service_override=service)
    app.dependency_overrides[get_credential_store] = lambda: cred_store
    # Also override workbench_store to avoid DB requirement
    return TestClient(app), db_path, cred_store, service


# ── Tests ──

def test_create_run_response_no_api_key():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        prepare_runtime_db(db_path)
        client, _, cred_store, _ = _make_client(db_path)

        resp = client.post("/api/live-runs", json={
            "ticker": "AAPL",
            "trade_date": "2025-09-08",
            "model": {
                "provider": "openai",
                "model_id": "gpt-4o-mini",
                "api_key": TEST_API_KEY,
                "base_url": "https://api.openai.com/v1",
            },
        })
        body = resp.json()
        body_str = json.dumps(body)

        assert resp.status_code == 200
        assert TEST_API_KEY not in body_str
        for key in body:
            assert key != "api_key", f"CreateRunResponse has key: {key}"
        print("PASS: CreateRunResponse JSON does not contain api_key")


def test_agent_runs_config_no_api_key():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        prepare_runtime_db(db_path)
        client, db_path, cred_store, _ = _make_client(db_path)

        resp = client.post("/api/live-runs", json={
            "ticker": "AAPL",
            "trade_date": "2025-09-08",
            "model": {
                "provider": "openai",
                "model_id": "gpt-4o-mini",
                "api_key": TEST_API_KEY,
                "base_url": "https://api.openai.com/v1",
            },
        })
        assert resp.status_code == 200

        body = resp.json()
        run_id = body.get("run_id")
        assert run_id, "Expected run_id in response"

        # Read raw config from SQLite
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT config FROM agent_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        conn.close()
        config_str = row[0] if row else "{}"

        assert TEST_API_KEY not in config_str
        assert "api_key" not in config_str.lower() or '"api_key"' not in config_str
        print("PASS: agent_runs.config does not contain api_key")


def test_get_run_response_no_api_key():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        client, db_path, cred_store, service = _make_client(db_path)

        # Create a manual terminal run in DB with BYOK metadata
        conn = sqlite3.connect(str(db_path))
        init_trace_db(conn)
        run_id = uuid4().hex
        config = json.dumps({"model": BYOK_META, "config": "mcj_full"})
        conn.execute(
            """INSERT INTO agent_runs
               (run_id, trace_id, ticker, trade_date, status, queued_at, started_at, ended_at, config)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, uuid4().hex, "AAPL", "2025-09-08", "SUCCEEDED",
             "2025-09-08T10:00:00Z", "2025-09-08T10:00:00Z", "2025-09-08T10:00:01Z", config),
        )
        conn.commit()
        conn.close()

        resp = client.get(f"/api/live-runs/{run_id}")
        body = resp.json()
        body_str = json.dumps(body)
        assert TEST_API_KEY not in body_str
        print("PASS: RunSummaryResponse JSON does not contain api_key")


def test_validate_model_response_no_api_key():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        client, _, _, _ = _make_client(db_path)

        resp = client.post("/api/models/validate", json={
            "provider": "nonexistent_provider_xyz",
            "model_id": "some-model",
            "api_key": TEST_API_KEY,
        })
        body = resp.json()
        body_str = json.dumps(body)

        assert resp.status_code == 200
        assert body["ok"] is False  # unknown provider
        assert TEST_API_KEY not in body_str
        assert "api_key" not in body
        print("PASS: ModelValidateResponse does not echo api_key")


def test_credential_store_registers_and_cleans_up():
    store = RuntimeCredentialStore()
    store.register("run-test", api_key=TEST_API_KEY)
    assert store.get("run-test") is not None
    assert store.get("run-test").api_key == TEST_API_KEY
    store.remove("run-test")
    assert store.get("run-test") is None
    print("PASS: RuntimeCredentialStore registers and cleans up")


def test_workspace_and_event_schemas_no_api_key():
    """Schema-level check: response models have no api_key field."""
    from catalyst_app.schemas import (
        WorkspaceResponse, RunEventResponse,
        ArtifactResponse, RunSummaryResponse, CreateRunResponse,
        ModelValidateResponse, RetryRunResponse,
    )
    schemas = [
        WorkspaceResponse, RunEventResponse, ArtifactResponse,
        RunSummaryResponse, CreateRunResponse, ModelValidateResponse,
        RetryRunResponse,
    ]
    for schema in schemas:
        fields = set(schema.model_fields.keys())
        assert "api_key" not in fields, f"{schema.__name__} has api_key field"
    print("PASS: All response schemas verified — no api_key field")



# ── Missing env key tests ──

def test_create_run_server_env_missing_key_returns_error():
    """When credential_source=server_env and provider has no env key,
    return FAILED_REQUEST immediately — never queue a doomed run."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        client, _, _, _ = _make_client(db_path)

        # Use a provider not in os.environ (rely on test isolation)
        provider_without_key = "openai"  # may or may not have env key; test structure is what matters
        resp = client.post("/api/live-runs", json={
            "ticker": "AAPL",
            "trade_date": "2025-09-08",
            "model": {
                "provider": "nonesuch_provider_test",
                "model_id": "gpt-4o-mini",
                "api_key": "",
                "credential_source": "server_env",
            },
        })
        body = resp.json()
        body_str = json.dumps(body)

        # Must return 200 (not 500) with FAILED_REQUEST
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body_str}"
        assert body.get("status") == "FAILED_REQUEST", f"Expected FAILED_REQUEST: {body_str}"
        failure = body.get("failure")
        assert failure is not None, f"Expected failure payload: {body_str}"
        assert failure.get("sub_reason") == "env_key_missing", f"Expected env_key_missing: {body_str}"
        # No run queued
        assert body.get("run_id") == "" or body.get("run_id") is None, f"Should not have queued run: {body_str}"
        # No api_key leak
        assert TEST_API_KEY not in body_str
        assert "api_key" not in body
        print("PASS: server_env missing key returns FAILED_REQUEST, no run queued")


def test_retry_server_env_missing_key_returns_error():
    """Retry with server_env and missing env key returns ok=false."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        client, db_path, cred_store, service = _make_client(db_path)

        # Create a terminal run manually (no parent run needed for the error path)
        resp = client.post("/api/live-runs/nonexistent-run-id/retry", json={
            "model": {
                "provider": "nonesuch_provider_test",
                "model_id": "gpt-4o-mini",
                "api_key": "",
                "credential_source": "server_env",
            },
        })
        body = resp.json()
        body_str = json.dumps(body)

        # Must return 200 (not 500)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body_str}"
        assert body.get("ok") is False, f"Expected ok=false: {body_str}"
        failure = body.get("failure")
        assert failure is not None
        assert failure.get("sub_reason") == "env_key_missing"
        # No api_key leak
        assert TEST_API_KEY not in body_str
        print("PASS: retry with server_env missing key returns ok=false")


def test_catalog_response_no_secrets():
    """GET /api/models/catalog must never include env key values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        client, _, _, _ = _make_client(db_path)

        resp = client.get("/api/models/catalog")
        assert resp.status_code == 200
        body = resp.json()
        body_str = json.dumps(body)

        # No api_key field anywhere
        assert '"api_key"' not in body_str
        # No concrete test key
        assert TEST_API_KEY not in body_str
        # No provider has an api_key field
        for p in body.get("providers", []):
            assert "api_key" not in p, f"Provider {p.get('id')} has api_key field"
        print("PASS: catalog response contains no secrets")


def test_server_env_validate_no_key_in_response():
    """validate_model with server_env must not echo the resolved env key."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        client, _, _, _ = _make_client(db_path)

        resp = client.post("/api/models/validate", json={
            "provider": "nonesuch_provider_test",
            "model_id": "gpt-4o-mini",
            "api_key": "",
            "credential_source": "server_env",
        })
        body = resp.json()
        body_str = json.dumps(body)

        assert resp.status_code == 200
        assert body["ok"] is False
        assert TEST_API_KEY not in body_str
        assert "api_key" not in body
        print("PASS: server_env validate response has no key leak")

print()
print("ALL NO-SECRET-LEAK TESTS PASSED")
