from __future__ import annotations

import json
from urllib.parse import parse_qs

from catalyst_app import access_control
from catalyst_app.access_control import AccessContext, install_access_control
from catalyst_app.routers import live_runs
from catalyst_app.runtime.admission import AdmissionOutcome
from fastapi import FastAPI
from fastapi.testclient import TestClient


class AdmissionSpy:
    def __init__(self) -> None:
        self.calls = []

    def admit(self, request, *, pre_submit=None):
        self.calls.append(request)
        return AdmissionOutcome(
            kind="accepted",
            run_id="run:accepted",
            request_hash="a" * 64,
            stream_url="/api/live-runs/run:accepted/stream",
        )


def _client(monkeypatch, *, tier: str = "anonymous") -> tuple[TestClient, AdmissionSpy]:
    monkeypatch.delenv("CATALYST_ALLOW_NON_LOOPBACK", raising=False)
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-quota-hash-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-server-key")
    context = AccessContext(tier=tier)
    monkeypatch.setattr(access_control, "resolve_access_context", lambda _request: context)
    monkeypatch.setattr(
        live_runs,
        "resolve_access_context",
        lambda _request: context,
    )
    admission = AdmissionSpy()
    monkeypatch.setattr(live_runs, "_admission_controller", lambda _request: admission)
    app = FastAPI()
    app.include_router(live_runs.router)
    install_access_control(app)
    return TestClient(app), admission


def _body() -> dict[str, object]:
    return {
        "ticker": "AAPL",
        "trade_date": "2026-01-06",
        "model": {
            "provider": "deepseek",
            "model_id": "deepseek-v4-flash",
            "credential_source": "server_env",
        },
    }


def _siteverify_response(
    success: object,
    *,
    hostname: str | None = "APP.EXAMPLE.COM",
    action: str | None = "live_run",
    error_codes: list[str] | None = None,
):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit: int | None = None) -> bytes:
            payload = {"success": success}
            if hostname is not None:
                payload["hostname"] = hostname
            if action is not None:
                payload["action"] = action
            if error_codes is not None:
                payload["error-codes"] = error_codes
            return json.dumps(payload).encode()[:limit]

    return Response()


def test_valid_turnstile_token_reaches_admission(monkeypatch) -> None:
    from catalyst_app import turnstile

    client, admission = _client(monkeypatch)
    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com,other.example.com")
    monkeypatch.setenv("CATALYST_TURNSTILE_EXPECTED_ACTION", "live_run")
    requests = []

    def siteverify(request, *, timeout):
        requests.append((request, timeout))
        return _siteverify_response(True)

    monkeypatch.setattr(turnstile, "urlopen", siteverify)

    with client:
        response = client.post(
            "/api/live-runs",
            json=_body(),
            headers={"X-Turnstile-Token": "valid+test/token=="},
        )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run:accepted"
    assert len(admission.calls) == 1
    assert len(requests) == 1
    assert requests[0][1] == turnstile.TURNSTILE_TIMEOUT_SECONDS
    request = requests[0][0]
    assert request.full_url == turnstile.TURNSTILE_SITEVERIFY_URL
    assert parse_qs(request.data.decode("ascii")) == {
        "secret": ["test-only-secret"],
        "response": ["valid+test/token=="],
    }


def test_missing_or_rejected_turnstile_token_never_reaches_admission(
    monkeypatch,
) -> None:
    from catalyst_app import turnstile

    client, admission = _client(monkeypatch)
    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")
    monkeypatch.setattr(
        turnstile,
        "urlopen",
        lambda *_args, **_kwargs: _siteverify_response(
            False, error_codes=["invalid-input-response"]
        ),
    )

    with client:
        missing = client.post("/api/live-runs", json=_body())
        rejected = client.post(
            "/api/live-runs",
            json=_body(),
            headers={"X-Turnstile-Token": "invalid-test-token"},
        )

    assert missing.status_code == 401
    assert missing.json() == {"detail": "turnstile_failed"}
    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "turnstile_failed"}
    assert admission.calls == []


def test_turnstile_timeout_fails_closed_before_admission(monkeypatch) -> None:
    from catalyst_app import turnstile

    client, admission = _client(monkeypatch)
    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")

    def timeout(*_args, **_kwargs):
        raise TimeoutError("siteverify timeout")

    monkeypatch.setattr(turnstile, "urlopen", timeout)

    with client:
        response = client.post(
            "/api/live-runs",
            json=_body(),
            headers={"X-Turnstile-Token": "test-token"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "runtime_unavailable: turnstile_verification_unavailable"
    }
    assert admission.calls == []


def test_siteverify_http_and_url_errors_fail_closed(monkeypatch) -> None:
    from http.client import HTTPException
    from urllib.error import HTTPError, URLError

    from catalyst_app import turnstile

    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")
    failures = (
        HTTPException("protocol error"),
        URLError("network error"),
        HTTPError("https://siteverify.invalid", 500, "server error", {}, None),
    )
    for failure in failures:
        client, admission = _client(monkeypatch)

        def fail(*_args, _failure=failure, **_kwargs):
            raise _failure

        monkeypatch.setattr(turnstile, "urlopen", fail)
        with client:
            response = client.post(
                "/api/live-runs", json=_body(), headers={"X-Turnstile-Token": "ok"}
            )
        assert response.status_code == 503
        assert admission.calls == []


def test_unconfigured_turnstile_fails_closed_before_admission(monkeypatch) -> None:
    from catalyst_app import turnstile

    client, admission = _client(monkeypatch)
    monkeypatch.delenv("CATALYST_TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")
    calls = []
    monkeypatch.setattr(
        turnstile, "urlopen", lambda *_args, **_kwargs: calls.append("request")
    )

    with client:
        response = client.post(
            "/api/live-runs",
            json=_body(),
            headers={"X-Turnstile-Token": "test-token"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "runtime_unavailable: turnstile_not_configured"
    }
    assert calls == []
    assert admission.calls == []


def test_admin_live_run_does_not_require_turnstile(monkeypatch) -> None:
    client, admission = _client(monkeypatch, tier="admin")
    monkeypatch.delenv("CATALYST_TURNSTILE_SECRET_KEY", raising=False)

    with client:
        response = client.post("/api/live-runs", json=_body())

    assert response.status_code == 200
    assert len(admission.calls) == 1


def test_invited_live_run_does_not_require_turnstile(monkeypatch) -> None:
    client, admission = _client(monkeypatch, tier="invite")
    monkeypatch.delenv("CATALYST_TURNSTILE_SECRET_KEY", raising=False)

    with client:
        response = client.post("/api/live-runs", json=_body())

    assert response.status_code == 200
    assert len(admission.calls) == 1


def test_invited_live_run_with_turnstile_header_validates_it(monkeypatch) -> None:
    from catalyst_app import turnstile

    client, admission = _client(monkeypatch, tier="invite")
    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")
    monkeypatch.setattr(
        turnstile,
        "urlopen",
        lambda *_args, **_kwargs: _siteverify_response(
            False, error_codes=["invalid-input-response"]
        ),
    )

    with client:
        response = client.post(
            "/api/live-runs",
            json=_body(),
            headers={"X-Turnstile-Token": "invalid-test-token"},
        )

    assert response.status_code == 401
    assert admission.calls == []


def test_invalid_turnstile_header_is_rejected_for_admin_too(monkeypatch) -> None:
    from catalyst_app import turnstile

    client, admission = _client(monkeypatch, tier="admin")
    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")
    monkeypatch.setattr(
        turnstile,
        "urlopen",
        lambda *_args, **_kwargs: _siteverify_response(
            False, error_codes=["invalid-input-response"]
        ),
    )

    with client:
        response = client.post(
            "/api/live-runs",
            json=_body(),
            headers={"X-Turnstile-Token": "invalid-test-token"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "turnstile_failed"}
    assert admission.calls == []


def test_success_requires_configured_allowed_hostname(monkeypatch) -> None:
    from catalyst_app import turnstile

    client, admission = _client(monkeypatch)
    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    monkeypatch.delenv("CATALYST_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(
        turnstile, "urlopen", lambda *_args, **_kwargs: _siteverify_response(True)
    )

    with client:
        response = client.post(
            "/api/live-runs", json=_body(), headers={"X-Turnstile-Token": "ok"}
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "runtime_unavailable: turnstile_not_configured"
    }
    assert admission.calls == []


def test_success_rejects_mismatched_hostname_or_configured_action(monkeypatch) -> None:
    from catalyst_app import turnstile

    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")
    monkeypatch.setenv("CATALYST_TURNSTILE_EXPECTED_ACTION", "live_run")
    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")

    for response_payload in (
        _siteverify_response(True, hostname="other.example.com"),
        _siteverify_response(True, action="admin_action"),
        _siteverify_response(True, hostname=None),
    ):
        client, admission = _client(monkeypatch)
        monkeypatch.setattr(
            turnstile,
            "urlopen",
            lambda *_args, _response=response_payload, **_kwargs: _response,
        )
        with client:
            response = client.post(
                "/api/live-runs", json=_body(), headers={"X-Turnstile-Token": "ok"}
            )
        assert response.status_code == 401
        assert response.json() == {"detail": "turnstile_failed"}
        assert admission.calls == []


def test_siteverify_error_codes_distinguish_bad_token_from_server_failure(
    monkeypatch,
) -> None:
    from catalyst_app import turnstile

    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")
    for error_codes, expected_status in (
        (["invalid-input-response"], 401),
        (["timeout-or-duplicate"], 401),
        (["invalid-input-secret"], 503),
        (["internal-error"], 503),
        (["unrecognized-code"], 503),
        (None, 503),
    ):
        client, admission = _client(monkeypatch)
        monkeypatch.setattr(
            turnstile,
            "urlopen",
            lambda *_args, _codes=error_codes, **_kwargs: _siteverify_response(
                False, error_codes=_codes
            ),
        )
        with client:
            response = client.post(
                "/api/live-runs", json=_body(), headers={"X-Turnstile-Token": "bad"}
            )
        assert response.status_code == expected_status
        assert admission.calls == []


def test_invalid_token_length_and_missing_secret_fail_before_siteverify(
    monkeypatch,
) -> None:
    from catalyst_app import turnstile

    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")
    calls = []
    monkeypatch.setattr(
        turnstile, "urlopen", lambda *_args, **_kwargs: calls.append("request")
    )
    client, admission = _client(monkeypatch)
    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    with client:
        blank = client.post(
            "/api/live-runs", json=_body(), headers={"X-Turnstile-Token": "   "}
        )
        too_long = client.post(
            "/api/live-runs",
            json=_body(),
            headers={"X-Turnstile-Token": "x" * 2049},
        )
    assert blank.status_code == too_long.status_code == 401
    assert calls == []
    assert admission.calls == []

    monkeypatch.delenv("CATALYST_TURNSTILE_SECRET_KEY")
    with client:
        missing_config = client.post("/api/live-runs", json=_body())
    assert missing_config.status_code == 503
    assert missing_config.json() == {
        "detail": "runtime_unavailable: turnstile_not_configured"
    }
    assert calls == []


def test_invalid_request_body_is_rejected_before_siteverify(monkeypatch) -> None:
    from catalyst_app import turnstile

    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")
    calls = []
    monkeypatch.setattr(
        turnstile, "urlopen", lambda *_args, **_kwargs: calls.append("request")
    )
    client, admission = _client(monkeypatch)

    with client:
        response = client.post(
            "/api/live-runs",
            json={**_body(), "ticker": "AAPL?"},
            headers={"X-Turnstile-Token": "one-time-token"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "ticker_not_supported"}
    assert calls == []
    assert admission.calls == []


def test_malformed_or_oversized_siteverify_response_fails_closed(monkeypatch) -> None:
    from catalyst_app import turnstile

    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")

    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit: int) -> bytes:
            return self.body[:limit]

    for body in (b"not-json", b"[]", b'{"success": "yes"}', b"x" * (64 * 1024 + 1)):
        client, admission = _client(monkeypatch)
        monkeypatch.setattr(
            turnstile,
            "urlopen",
            lambda *_args, _body=body, **_kwargs: Response(_body),
        )
        with client:
            response = client.post(
                "/api/live-runs", json=_body(), headers={"X-Turnstile-Token": "ok"}
            )
        assert response.status_code == 503
        assert admission.calls == []
