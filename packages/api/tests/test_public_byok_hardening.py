from __future__ import annotations

import pytest
from catalyst_app.access_control import AccessContext
from catalyst_app.dependencies import get_credential_store
from catalyst_app.llm_factory import PUBLIC_RUN_MODEL
from catalyst_app.routers import live_runs
from catalyst_app.runtime.admission import AdmissionOutcome
from catalyst_app.runtime_credential_store import RuntimeCredentialStore
from catalyst_app.schemas import CreateRunRequest, ModelConfig
from fastapi import FastAPI
from fastapi.testclient import TestClient


class AdmissionSpy:
    def __init__(self) -> None:
        self.requests = []

    def admit(self, request, *, pre_submit=None):
        self.requests.append(request)
        run_id = "run:accepted"
        if pre_submit is not None:
            pre_submit(run_id)
        return AdmissionOutcome(
            kind="accepted",
            run_id=run_id,
            request_hash="a" * 64,
            stream_url=f"/api/live-runs/{run_id}/stream",
        )


def _client(
    monkeypatch, tier: str, *, server_key: str | None = "server-only-key"
):
    if server_key is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", server_key)
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-quota-hash-key")
    access = AccessContext(tier=tier)
    monkeypatch.setattr(live_runs, "_http_access_context", lambda _request: access)
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)
    spy = AdmissionSpy()
    monkeypatch.setattr(live_runs, "_admission_controller", lambda _request: spy)
    store = RuntimeCredentialStore()
    app = FastAPI()
    app.include_router(live_runs.router)
    app.dependency_overrides[get_credential_store] = lambda: store
    return TestClient(app), spy, store


def _body(
    *,
    api_key: str,
    provider: str = "openai",
    model_id: str = "gpt-4o-mini",
    base_url: str | None = "https://attacker.example/v1",
) -> dict:
    return {
        "ticker": "AAPL",
        "trade_date": "2026-01-06",
        "model": {
            "provider": provider,
            "model_id": model_id,
            "api_key": api_key,
            "base_url": base_url,
            "credential_source": "browser_key",
        },
    }


@pytest.mark.parametrize("api_key", ["", "   ", "\t\n"])
@pytest.mark.parametrize("tier", ["anonymous", "invite"])
def test_empty_browser_key_on_public_tier_is_pinned_and_never_registers_blank_key(
    monkeypatch, api_key: str, tier: str
) -> None:
    client, admission, store = _client(monkeypatch, tier)

    with client:
        response = client.post("/api/live-runs", json=_body(api_key=api_key))

    assert response.status_code == 200, response.text
    assert len(admission.requests) == 1
    admitted = admission.requests[0]
    assert admitted.provider == PUBLIC_RUN_MODEL.provider
    assert admitted.model_id == PUBLIC_RUN_MODEL.model_id
    assert admitted.base_url == PUBLIC_RUN_MODEL.base_url
    assert admitted.credential_source_identifier == "server_env"
    credential = store.get("run:accepted")
    assert credential is not None
    assert credential.api_key == "server-only-key"
    assert credential.api_key.strip()
    assert credential.api_key != api_key


def test_empty_browser_key_resolves_to_no_client_credential() -> None:
    request = CreateRunRequest(
        ticker="AAPL",
        trade_date="2026-01-06",
        model=ModelConfig(
            provider="openai",
            model_id="gpt-4o-mini",
            api_key="   ",
            base_url="https://attacker.example/v1",
            credential_source="browser_key",
        ),
    )

    assert live_runs._resolve_runtime_credential(
        request,
        provider="openai",
        credential_source="browser_key",
    ) is None


@pytest.mark.parametrize("tier", ["anonymous", "invite"])
def test_public_byok_rejects_non_catalog_base_url(monkeypatch, tier: str) -> None:
    client, admission, store = _client(monkeypatch, tier)

    with client:
        response = client.post(
            "/api/live-runs",
            json=_body(api_key="sk-browser-real"),
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "invalid_request: public_byok_base_url_not_allowed"
    }
    assert admission.requests == []
    assert store.get("run:accepted") is None


@pytest.mark.parametrize("tier", ["anonymous", "invite"])
def test_public_byok_allows_catalog_provider_base_url(monkeypatch, tier: str) -> None:
    client, admission, store = _client(monkeypatch, tier)

    with client:
        response = client.post(
            "/api/live-runs",
            json=_body(
                api_key="sk-browser-real",
                base_url="https://api.openai.com/v1/",
            ),
        )

    assert response.status_code == 200, response.text
    admitted = admission.requests[0]
    assert admitted.provider == "openai"
    assert admitted.model_id == "gpt-4o-mini"
    assert admitted.base_url == "https://api.openai.com/v1"
    assert admitted.credential_source_identifier == "browser_key"
    assert store.get("run:accepted").api_key == "sk-browser-real"


def test_admin_byok_arbitrary_base_url_is_unchanged(monkeypatch) -> None:
    client, admission, store = _client(monkeypatch, "admin")

    with client:
        response = client.post(
            "/api/live-runs",
            json=_body(api_key="sk-browser-real"),
        )

    assert response.status_code == 200, response.text
    admitted = admission.requests[0]
    assert admitted.provider == "openai"
    assert admitted.model_id == "gpt-4o-mini"
    assert admitted.base_url == "https://attacker.example/v1"
    assert store.get("run:accepted").api_key == "sk-browser-real"
