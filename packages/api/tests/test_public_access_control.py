from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from catalyst_app.access_control import (
    AccessContext,
    install_access_control,
    sign_invite_token,
    verify_invite_token,
)
from catalyst_app.routers import data_jobs, live_runs
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_TEST_SIGNING_KEY = "test-only-invite-signing-key-with-32-plus-bytes"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(data_jobs.router)
    app.include_router(live_runs.router)
    install_access_control(app)
    return TestClient(app)


def _accept_test_turnstile(monkeypatch) -> None:
    """Keep these route-validation tests focused on their post-CAPTCHA paths."""
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)


def _signed_invite(secret: str, *, token_id: str, expires_at: str) -> str:
    payload = json.dumps(
        {"expires_at": expires_at, "token_id": token_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    payload_segment = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    message = f"inv_{payload_segment}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    signature_segment = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"inv_{payload_segment}.{signature_segment}"


def _future_expiry(days: int = 10) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_main_installs_global_access_credential_validation():
    main_path = Path(__file__).parents[1] / "catalyst_app" / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "install_access_control"
    ]

    assert len(calls) == 1


def test_job_status_is_public_but_starting_an_update_needs_the_admin_token(tmp_path, monkeypatch):
    # 2026-11-18: everyone may read the data status; only the admin may start an update.
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALYST_UPDATE_JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    client = _client()

    with client:
        public = client.get("/api/data/jobs/latest")
        start = client.post("/api/data/update")

    assert public.status_code == 200
    assert public.json() == {"status": "no_job"}
    assert start.status_code == 401
    assert start.json() == {"detail": "admin_unauthenticated"}


def test_data_overview_is_admin_only_and_reports_read_only_coverage(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALYST_RUNTIME_DATA_DIR", str(tmp_path))
    market = sqlite3.connect(tmp_path / "market_context.sqlite3")
    market.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    market.executemany("INSERT INTO ohlcv VALUES (?,?,?)", [("AAPL", "2026-11-12", 10), ("MSFT", "2026-11-12", 20)])
    market.commit()
    market.close()
    archive = sqlite3.connect(tmp_path / "headline_archive.sqlite3")
    archive.execute("CREATE TABLE finnhub_news (symbol TEXT, published_utc TEXT, source TEXT)")
    archive.executemany("INSERT INTO finnhub_news VALUES (?,?,?)", [
        ("AAPL", "2026-11-12T18:00:00Z", "Yahoo"),
        # Benzinga's New York clock stored as UTC: 2026-11-12 16:30 New York is after the 16:00 cutoff.
        ("MSFT", "2026-11-12T16:30:00Z", "Benzinga"),
    ])
    archive.commit()
    archive.close()
    client = _client()
    with client:
        public = client.get("/api/data/overview?sessions=1")
        allowed = client.get("/api/data/overview?sessions=1", headers={"Authorization": "Bearer test-admin-token"})
    assert public.status_code == 200
    assert public.json()["disk_free_bytes"] is None  # the disk is admin-only
    assert public.json()["coverage"] == allowed.json()["coverage"]
    assert allowed.status_code == 200
    result = allowed.json()
    assert {item["name"] for item in result["stores"]} >= {"market_db", "archive_db"}
    assert result["coverage"] == [{"session_date": "2026-11-12", "tickers_with_prices": ["AAPL", "MSFT"],
                                   "tickers_with_news": ["AAPL"]}]
    assert result["search_index"]["path_kind"] == "served_db"


def test_manual_update_uses_contract_bearer_header(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALYST_UPDATE_JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(data_jobs.subprocess, "Popen", lambda *args, **kwargs: object())
    client = _client()

    with client:
        response = client.post(
            "/api/data/update",
            headers={"Authorization": "Bearer test-admin-token"},
        )

    # The test only verifies authentication; avoid spawning the data updater.
    assert response.status_code in {202, 409}


def test_invalid_invite_does_not_downgrade_to_anonymous(monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.delenv("CATALYST_ADMIN_TOKEN", raising=False)
    client = _client()

    with client:
        response = client.get(
            "/api/data/jobs/latest",
            headers={"X-Invite-Token": "manual-token-without-configured-verifier"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invite_invalid"}


def test_invalid_bearer_does_not_downgrade_to_anonymous(monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")
    client = _client()

    with client:
        response = client.get(
            "/api/data/jobs/latest",
            headers={"Authorization": "Bearer wrong-token"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "admin_unauthenticated"}


def test_raw_artifact_endpoint_requires_admin(monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    client = _client()

    with client:
        response = client.get("/api/live-runs/unknown/artifacts")

    assert response.status_code == 401
    assert response.json() == {"detail": "admin_unauthenticated"}


def test_invalid_credential_is_rejected_before_public_route(monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    client = _client()

    with client:
        response = client.get(
            "/api/live-runs/unknown",
            headers={"X-Invite-Token": "unsupported-token"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invite_invalid"}


def test_client_tier_headers_do_not_change_public_http_budget_tier(monkeypatch):
    from catalyst_app.runtime.budget import BudgetLedger
    from starlette.requests import Request

    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-quota-hash-key")
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/live-runs",
        "headers": [(b"x-access-tier", b"local"), (b"x-admin", b"true")],
        "client": ("198.51.100.10", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
        "query_string": b"",
    })

    access = live_runs.resolve_access_context(request)
    budget = live_runs._http_budget_admission(
        request,
        access,
        BudgetLedger(),
        max_cost_usd=0.05,
    )

    assert access.tier == "anonymous"
    assert budget.tier == "anonymous"
    assert budget.caller_key


def test_quota_identity_requires_hash_key(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.delenv("CATALYST_QUOTA_HASH_KEY", raising=False)
    request = SimpleNamespace(client=SimpleNamespace(host="198.51.100.10"), headers={})

    with pytest.raises(HTTPException) as error:
        live_runs._quota_caller_key(request)

    assert error.value.status_code == 503
    assert error.value.detail == "runtime_unavailable: quota_config_missing"


@pytest.mark.parametrize(
    ("peer", "forwarded", "trusted_cidrs", "expected_ip"),
    [
        ("203.0.113.10", "198.51.100.20", "203.0.113.0/24", "198.51.100.20"),
        ("192.0.2.10", "198.51.100.20", "203.0.113.0/24", "192.0.2.10"),
    ],
)
def test_quota_identity_uses_forwarded_ip_only_from_trusted_proxy(
    monkeypatch, peer, forwarded, trusted_cidrs, expected_ip
):
    from types import SimpleNamespace

    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-quota-hash-key")
    monkeypatch.setenv("CATALYST_TRUSTED_PROXY_CIDRS", trusted_cidrs)
    request = SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers={"cf-connecting-ip": forwarded},
    )

    expected = hmac.new(
        b"test-only-quota-hash-key", expected_ip.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert live_runs._quota_caller_key(request) == expected


def test_valid_signed_invite_is_resolved_without_admin_privileges(monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    signing_key = _TEST_SIGNING_KEY
    monkeypatch.setenv("CATALYST_INVITE_SIGNING_KEY", signing_key)
    token = _signed_invite(
        signing_key,
        token_id="invite-123",
        expires_at=_future_expiry(),
    )
    client = _client()

    with client:
        response = client.post(
            "/api/data/update",
            headers={"X-Invite-Token": token},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "admin_required"}


def test_valid_invite_does_not_disable_local_operator_admin(monkeypatch):
    monkeypatch.delenv("CATALYST_ALLOW_NON_LOOPBACK", raising=False)
    monkeypatch.setenv("CATALYST_INVITE_SIGNING_KEY", _TEST_SIGNING_KEY)
    token = _signed_invite(
        _TEST_SIGNING_KEY,
        token_id="invite-local-request",
        expires_at=_future_expiry(),
    )
    client = _client()

    with client:
        response = client.get(
            "/api/data/jobs/latest",
            headers={"X-Invite-Token": token},
        )

    assert response.status_code == 200


def test_signer_rejects_signing_key_shorter_than_32_bytes():
    with pytest.raises(ValueError, match="at least 32 bytes"):
        sign_invite_token(
            "short-key",
            _future_expiry(),
            signing_key="x" * 31,
        )


def test_signer_rejects_expiry_more_than_30_days_away():
    with pytest.raises(ValueError, match="maximum invite lifetime of 30 days"):
        sign_invite_token(
            "long-lived",
            _future_expiry(31),
            signing_key=_TEST_SIGNING_KEY,
        )


def test_signer_accepts_expiry_at_30_day_limit():
    token = sign_invite_token(
        "thirty-days",
        _future_expiry(30),
        signing_key=_TEST_SIGNING_KEY,
    )

    context = verify_invite_token(token, signing_key=_TEST_SIGNING_KEY)

    assert context == AccessContext(tier="invite", token_id="thirty-days")


def test_verifier_rejects_signed_expiry_more_than_30_days_away():
    now = datetime.now(timezone.utc)
    token = _signed_invite(
        _TEST_SIGNING_KEY,
        token_id="long-lived",
        expires_at=(now + timedelta(days=31)).isoformat(),
    )

    with pytest.raises(HTTPException) as error:
        verify_invite_token(token, signing_key=_TEST_SIGNING_KEY, now=now)

    assert error.value.status_code == 401
    assert error.value.detail == "invite_invalid"


def test_verifier_accepts_signed_expiry_at_30_day_limit():
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=30)
    token = _signed_invite(
        _TEST_SIGNING_KEY,
        token_id="thirty-days",
        expires_at=expiry.isoformat(),
    )

    context = verify_invite_token(token, signing_key=_TEST_SIGNING_KEY, now=now)

    assert context == AccessContext(tier="invite", token_id="thirty-days")


def test_signer_normalizes_outer_whitespace_and_round_trips_unicode_token_id():
    token = sign_invite_token(
        "  邀请-123  ",
        _future_expiry(),
        signing_key=_TEST_SIGNING_KEY,
    )

    context = verify_invite_token(token, signing_key=_TEST_SIGNING_KEY)

    assert context == AccessContext(tier="invite", token_id="邀请-123")


def test_verifier_rejects_control_characters_in_token_id():
    token = _signed_invite(
        _TEST_SIGNING_KEY,
        token_id="invite\nforged-line",
        expires_at=_future_expiry(),
    )

    with pytest.raises(HTTPException) as error:
        verify_invite_token(token, signing_key=_TEST_SIGNING_KEY)

    assert error.value.status_code == 401
    assert error.value.detail == "invite_invalid"


def test_verifier_rejects_noncanonical_token_id_whitespace():
    token = _signed_invite(
        _TEST_SIGNING_KEY,
        token_id=" invite-123 ",
        expires_at=_future_expiry(),
    )

    with pytest.raises(HTTPException) as error:
        verify_invite_token(token, signing_key=_TEST_SIGNING_KEY)

    assert error.value.status_code == 401
    assert error.value.detail == "invite_invalid"


def test_verifier_rejects_signing_key_shorter_than_32_bytes():
    token = _signed_invite(
        "x" * 31,
        token_id="short-key",
        expires_at=_future_expiry(),
    )

    with pytest.raises(HTTPException) as error:
        verify_invite_token(token, signing_key="x" * 31)

    assert error.value.status_code == 401
    assert error.value.detail == "invite_invalid"


def test_verifier_rejects_expiry_that_overflows_utc_normalization():
    token = _signed_invite(
        _TEST_SIGNING_KEY,
        token_id="extreme-expiry",
        expires_at="0001-01-01T00:00:00+14:00",
    )

    with pytest.raises(HTTPException) as error:
        verify_invite_token(token, signing_key=_TEST_SIGNING_KEY)

    assert error.value.status_code == 401
    assert error.value.detail == "invite_invalid"


def test_verifier_rejects_naive_now_as_invalid_invite_time():
    token = sign_invite_token(
        "naive-now",
        _future_expiry(),
        signing_key=_TEST_SIGNING_KEY,
    )

    with pytest.raises(HTTPException) as error:
        verify_invite_token(
            token,
            signing_key=_TEST_SIGNING_KEY,
            now=datetime(2098, 1, 1),
        )

    assert error.value.status_code == 401
    assert error.value.detail == "invite_invalid"


def test_verifier_expires_token_at_exact_expiry_instant():
    expiry = datetime.now(timezone.utc) + timedelta(days=10)
    token = sign_invite_token(
        "exact-expiry",
        expiry.isoformat(),
        signing_key=_TEST_SIGNING_KEY,
    )

    with pytest.raises(HTTPException) as error:
        verify_invite_token(token, signing_key=_TEST_SIGNING_KEY, now=expiry)

    assert error.value.status_code == 401
    assert error.value.detail == "invite_expired"


def test_expired_signed_invite_is_rejected(monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    signing_key = _TEST_SIGNING_KEY
    monkeypatch.setenv("CATALYST_INVITE_SIGNING_KEY", signing_key)
    token = _signed_invite(
        signing_key,
        token_id="invite-expired",
        expires_at="2000-01-01T00:00:00Z",
    )
    client = _client()

    with client:
        response = client.get(
            "/api/data/jobs/latest",
            headers={"X-Invite-Token": token},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invite_expired"}


def test_tampered_signed_invite_is_rejected(monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    signing_key = _TEST_SIGNING_KEY
    monkeypatch.setenv("CATALYST_INVITE_SIGNING_KEY", signing_key)
    token = _signed_invite(
        signing_key,
        token_id="invite-tampered",
        expires_at=_future_expiry(),
    )
    token = token[:-1] + ("A" if token[-1] != "A" else "B")
    client = _client()

    with client:
        response = client.get(
            "/api/data/jobs/latest",
            headers={"X-Invite-Token": token},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invite_invalid"}


def test_offline_invite_signer_round_trips_only_contract_fields():
    token = sign_invite_token(
        "invite-round-trip",
        _future_expiry(),
        signing_key=_TEST_SIGNING_KEY,
    )

    context = verify_invite_token(
        token,
        signing_key=_TEST_SIGNING_KEY,
    )

    assert context == AccessContext(tier="invite", token_id="invite-round-trip")


def test_signer_preserves_fractional_expiry_until_its_stated_instant():
    expiry = (datetime.now(timezone.utc) + timedelta(days=10)).replace(microsecond=100_000)
    token = sign_invite_token(
        "fractional-expiry",
        expiry.isoformat(timespec="milliseconds"),
        signing_key=_TEST_SIGNING_KEY,
    )

    context = verify_invite_token(
        token,
        signing_key=_TEST_SIGNING_KEY,
        now=expiry - timedelta(milliseconds=50),
    )

    assert context == AccessContext(tier="invite", token_id="fractional-expiry")


def test_invite_verifier_fails_closed_without_signing_key(monkeypatch):
    monkeypatch.delenv("CATALYST_INVITE_SIGNING_KEY", raising=False)

    with pytest.raises(HTTPException) as error:
        verify_invite_token("inv_payload.signature")

    assert error.value.status_code == 401
    assert error.value.detail == "invite_invalid"


def test_anonymous_custom_query_is_rejected_before_runtime_admission(monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    _accept_test_turnstile(monkeypatch)
    calls = []

    def unexpected_admission(_request):
        calls.append("admission")
        raise AssertionError("anonymous custom query reached runtime admission")

    monkeypatch.setattr(live_runs, "_admission_controller", unexpected_admission)
    client = _client()

    with client:
        response = client.post(
            "/api/live-runs",
            headers={"X-Turnstile-Token": "test-token"},
            json={
                "ticker": "AAPL",
                "trade_date": "2026-01-06",
                "query": "Why did AAPL move?",
                "model_id": "test-model",
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "query_not_allowed"}
    assert calls == []


def test_public_live_run_fails_closed_until_ready_range_is_connected(monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    _accept_test_turnstile(monkeypatch)
    calls = []

    def unexpected_admission(_request):
        calls.append("admission")
        raise HTTPException(status_code=418, detail="admission_reached")

    monkeypatch.setattr(
        live_runs,
        "_admission_controller",
        unexpected_admission,
    )
    client = _client()

    with client:
        response = client.post(
            "/api/live-runs",
            headers={"X-Turnstile-Token": "test-token"},
            json={"ticker": "AAPL", "trade_date": "2026-01-06", "model_id": "test-model"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "runtime_unavailable: prepared_range_unavailable"}
    assert calls == []


@pytest.mark.parametrize(
    ("ticker", "trade_date", "expected_detail"),
    [
        ("AAPL?", "2026-01-06", "ticker_not_supported"),
        ("AAPL", "2026-1-6", "invalid_request: trade_date"),
    ],
)
def test_live_run_rejects_malformed_ticker_and_date_before_admission(
    ticker, trade_date, expected_detail, monkeypatch
):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    _accept_test_turnstile(monkeypatch)
    calls = []
    monkeypatch.setattr(
        live_runs,
        "_admission_controller",
        lambda _request: calls.append("admission"),
    )
    client = _client()

    with client:
        response = client.post(
            "/api/live-runs",
            headers={"X-Turnstile-Token": "test-token"},
            json={"ticker": ticker, "trade_date": trade_date, "model_id": "test-model"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": expected_detail}
    assert calls == []


def test_options_and_live_run_list_cache_headers(monkeypatch):
    from catalyst_app.main import create_app
    from catalyst_app.routers import public_access
    from catalyst_app.runtime.ready_range import ReadyRangeUnavailable
    from fastapi.testclient import TestClient

    monkeypatch.setattr(public_access, "_tier", lambda request: "anonymous")
    monkeypatch.setattr(
        public_access,
        "load_ready_range",
        lambda **_: (_ for _ in ()).throw(ReadyRangeUnavailable("missing")),
    )
    with TestClient(create_app()) as client:
        options = client.get("/api/options")
        runs = client.get("/api/live-runs")

    assert options.headers["cache-control"] == "public, max-age=60"
    assert runs.headers["cache-control"] == "private, no-store"
