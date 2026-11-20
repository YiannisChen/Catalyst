from __future__ import annotations

import json

from catalyst_app.access_control import install_access_control
from catalyst_app.routers import quality
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_quality_api_serves_export_from_configured_path(tmp_path, monkeypatch):
    export = tmp_path / "quality.json"
    export.write_text(json.dumps({
        "schema_version": "quality_v1", "generated_at": "2026-10-09T12:00:00+00:00",
        "code_revision": "abc123", "model": {"provider": "deepseek", "analyst_model_id": "deepseek-flash"},
        "inputs": [{"path": "score.json", "sha256": "a" * 64, "role": "v3.score"}],
        "sets": [], "comparisons": [], "calibration": {}, "adjudications": {},
    }))
    monkeypatch.setenv("CATALYST_QUALITY_JSON", str(export))
    app = FastAPI()
    app.include_router(quality.router)
    with TestClient(app) as client:
        response = client.get("/api/quality")
    assert response.status_code == 200
    assert response.json()["schema_version"] == "quality_v1"
    assert response.json()["inputs"][0]["path"] == "score.json"


def test_quality_api_returns_contract_404_when_export_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_QUALITY_JSON", str(tmp_path / "missing.json"))
    app = FastAPI()
    app.include_router(quality.router)
    with TestClient(app) as client:
        response = client.get("/api/quality")
    assert response.status_code == 404
    assert response.json() == {"detail": "quality export not generated"}


def test_quality_api_rejects_anonymous_request(monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    app = FastAPI()
    app.include_router(quality.router)
    install_access_control(app)
    with TestClient(app) as client:
        response = client.get("/api/quality")
    assert response.status_code == 401


def test_quality_path_default_is_repo_rooted_outside_checkout_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(quality, "REPO", tmp_path)
    monkeypatch.delenv("CATALYST_QUALITY_JSON", raising=False)

    assert quality._quality_path() == quality.REPO / ".local/data/ingestion/quality/quality_v1.json"
    assert not (tmp_path / ".local").exists()


def test_quality_path_relative_override_remains_repo_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CATALYST_QUALITY_JSON", "custom/quality.json")

    assert quality._quality_path() == quality.REPO / "custom/quality.json"
    assert not (tmp_path / "custom").exists()


def test_quality_path_uses_runtime_root_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CATALYST_QUALITY_JSON", raising=False)
    monkeypatch.setenv("CATALYST_RUNTIME_DATA_DIR", "runtime-state")

    assert quality._quality_path() == quality.REPO / "runtime-state/quality/quality_v1.json"
    assert not (tmp_path / "runtime-state").exists()
