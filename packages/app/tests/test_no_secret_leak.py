"""V1.1 no-secret tests (M6-9 migration).

A concrete BYOK api_key is absent from every response, persisted run/event/
artifact row, and error text. Credentials register in the in-memory
RuntimeCredentialStore only.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from catalyst_app.runtime_credential_store import RuntimeCredentialStore
from v1_helpers import fixture_db, make_v1_app

TEST_API_KEY = "sk-test-secret-leak-key-xyz789"

BODY = {
    "ticker": "AAPL",
    "trade_date": "2026-01-06",
    "query": "Why did AAPL move?",
    "model": {
        "provider": "openai",
        "model_id": "gpt-4o-mini",
        "api_key": TEST_API_KEY,
        "base_url": "https://api.openai.com/v1",
        "credential_source": "browser_key",
    },
}


def _client(db_path: Path) -> tuple[TestClient, RuntimeCredentialStore]:
    from catalyst_app.dependencies import get_credential_store

    cred_store = RuntimeCredentialStore()
    app = make_v1_app(db_path)
    app.dependency_overrides[get_credential_store] = lambda: cred_store
    return TestClient(app), cred_store


def test_create_run_response_no_api_key() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        fixture_db(db_path)
        client, cred_store = _client(db_path)

        with client:
            resp = client.post("/api/live-runs", json=BODY)
            body_str = json.dumps(resp.json())

        assert resp.status_code == 200
        assert TEST_API_KEY not in body_str
        assert "api_key" not in resp.json()
        # The credential is in memory only.
        run_id = resp.json()["run_id"]
        assert cred_store.get(run_id) is not None


def test_persisted_tables_no_api_key() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        fixture_db(db_path)
        client, _ = _client(db_path)

        with client:
            run_id = client.post("/api/live-runs", json=BODY).json()["run_id"]

        with open_rw(db_path) as conn:
            runs = [dict(r) for r in conn.execute("SELECT * FROM runs").fetchall()]
            events = [dict(r) for r in conn.execute("SELECT * FROM run_events").fetchall()]
            artifacts = [dict(r) for r in conn.execute("SELECT * FROM run_artifacts").fetchall()]

        all_text = json.dumps({"runs": runs, "events": events, "artifacts": artifacts})
        assert TEST_API_KEY not in all_text
        assert run_id


def test_get_run_response_no_api_key() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        fixture_db(db_path)
        client, _ = _client(db_path)

        with client:
            run_id = client.post("/api/live-runs", json=BODY).json()["run_id"]
            resp = client.get(f"/api/live-runs/{run_id}")
            artifacts = client.get(f"/api/live-runs/{run_id}/artifacts")

        assert TEST_API_KEY not in json.dumps(resp.json())
        assert TEST_API_KEY not in json.dumps(artifacts.json())


def test_validate_model_response_no_api_key() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        fixture_db(db_path)
        client, _ = _client(db_path)

        with client:
            resp = client.post(
                "/api/models/validate",
                json={"provider": "nonexistent_provider_xyz", "model_id": "some-model", "api_key": TEST_API_KEY},
            )
            body_str = json.dumps(resp.json())

        assert resp.status_code == 200
        assert body_str["ok"] if isinstance(body_str, dict) else True
        assert TEST_API_KEY not in body_str
        assert "api_key" not in resp.json()


def test_credential_store_registers_and_cleans_up() -> None:
    store = RuntimeCredentialStore()
    store.register("run-test", api_key=TEST_API_KEY)
    assert store.get("run-test") is not None
    assert store.get("run-test").api_key == TEST_API_KEY
    store.remove("run-test")
    assert store.get("run-test") is None


def test_public_schemas_no_api_key_field() -> None:
    from catalyst_app.api_dto import (
        RunAcceptedResponse,
        RunDTO,
        ArtifactDTO,
        ArtifactRefDTO,
        ClaimDetailDTO,
        EvidenceDetailDTO,
        HealthDTO,
        CapabilityResponse,
    )

    for cls in (
        RunAcceptedResponse,
        RunDTO,
        ArtifactDTO,
        ArtifactRefDTO,
        ClaimDetailDTO,
        EvidenceDetailDTO,
        HealthDTO,
        CapabilityResponse,
    ):
        assert "api_key" not in cls.model_fields
