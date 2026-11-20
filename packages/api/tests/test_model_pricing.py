from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from catalyst_app.deployment_configuration import PUBLIC_DEPLOYMENT_PINNED_ENV
from catalyst_app.persistence.connect import open_rw
from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger, _micros
from catalyst_app.runtime.model_pricing import (
    DEFAULT_PRICING_PATH,
    PRICING_FILE_ENV,
    PricingUnavailable,
    load_pricing_config,
    priced_run_max_cost_usd,
    required_role_names,
)
from v1_helpers import fixture_db, make_v1_app

_READY_GENERATED_AT = "2026-11-04T00:15:00Z"
_READY_NOW = datetime(2026, 11, 4, 18, tzinfo=timezone.utc)


def _public_env(monkeypatch) -> None:
    for key, value in PUBLIC_DEPLOYMENT_PINNED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("CATALYST_ALLOWED_ORIGINS", "http://testserver")
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-quota-hash-key")
    # server_env runs resolve the provider-specific key at request time. Keep
    # the fake value local to the test so the suite order cannot affect it.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-server-key")
    monkeypatch.setenv("deepseek_api_key", "test-only-server-key")
    monkeypatch.delenv(PRICING_FILE_ENV, raising=False)


def _install_ready_snapshot(monkeypatch, tmp_path: Path) -> None:
    pairs = [{"ticker": "AAPL", "session_date": "2026-01-06"}]
    canonical = json.dumps(
        pairs, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    path = tmp_path / "ready.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generated_at": _READY_GENERATED_AT,
                "pairs_sha256": hashlib.sha256(canonical).hexdigest(),
                "daily_last_completed_at": "2026-11-03T21:00:00Z",
                "last_completed_session_date": "2026-11-03",
                "pairs": pairs,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CATALYST_READY_SNAPSHOT", str(path))
    from catalyst_app.routers import public_access

    monkeypatch.setattr(public_access, "utc_now", lambda: _READY_NOW)


def _body(
    *, provider: str = "deepseek", model_id: str = "deepseek-v4-flash"
) -> dict:
    return {
        "ticker": "AAPL",
        "trade_date": "2026-01-06",
        "model": {
            "provider": provider,
            "model_id": model_id,
            "credential_source": "server_env",
        },
    }


def _pricing_payload(
    *,
    model_key: str = "deepseek/deepseek-v4-flash",
    input_price: float = 0.27,
    output_price: float = 1.1,
) -> dict:
    return {
        "models": {
            model_key: {
                "input_usd_per_million_tokens": input_price,
                "output_usd_per_million_tokens": output_price,
            }
        },
        "role_token_limits": {
            "evidence_analyst": {"max_input_tokens": 128000, "max_output_tokens": 4000},
            "streaming_writer": {"max_input_tokens": 128000, "max_output_tokens": 8000},
            "primary_verifier": {"max_input_tokens": 6000, "max_output_tokens": 400},
            "reported_reason": {"max_input_tokens": 24000, "max_output_tokens": 3000},
            "desk_extractor": {"max_input_tokens": 4000, "max_output_tokens": 600},
            "event_dater": {"max_input_tokens": 4000, "max_output_tokens": 200},
        },
    }


def _write_pricing(path: Path, payload: dict | str) -> Path:
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return path


def test_default_pricing_file_contains_every_production_role() -> None:
    config = load_pricing_config()

    assert config.path == DEFAULT_PRICING_PATH
    assert required_role_names() <= set(config.role_token_limits)
    price = config.price_for("deepseek", "deepseek-v4-flash")
    assert price.input_usd_per_million_tokens == pytest.approx(0.30)
    assert price.output_usd_per_million_tokens == pytest.approx(1.20)
    # Every model a user can pick for their own key needs a price, or the run fails closed.
    for provider, model_id in [("openai", "gpt-4o-mini"), ("gemini", "gemini-2.5-flash"), ("xai", "grok-3-mini")]:
        assert config.price_for(provider, model_id).input_usd_per_million_tokens > 0


def test_public_run_without_model_uses_fixed_public_model_and_price(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    _install_ready_snapshot(monkeypatch, tmp_path)
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post(
            "/api/live-runs",
            json={"ticker": "AAPL", "trade_date": "2026-01-06"},
        )

    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    with open_rw(db_path) as conn:
        run = conn.execute(
            "SELECT provider, base_url, access_tier "
            "FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        manifest_row = conn.execute(
            "SELECT payload_json FROM run_artifacts "
            "WHERE run_id=? AND artifact_type='run_manifest' "
            "ORDER BY event_seq ASC LIMIT 1",
            (run_id,),
        ).fetchone()
        reservation = conn.execute(
            "SELECT max_run_cost_micros, reserved_cost_micros "
            "FROM run_budget_reservations WHERE run_id=?",
            (run_id,),
        ).fetchone()

    manifest = json.loads(manifest_row["payload_json"])
    assert run["provider"] == "deepseek"
    assert manifest["analyst_model_id"] == "deepseek-v4-flash"
    assert manifest["writer_model_id"] == "deepseek-v4-flash"
    assert run["base_url"] == "https://api.deepseek.com/v1"
    assert run["access_tier"] == "anonymous"
    assert reservation["max_run_cost_micros"] > 0
    assert reservation["reserved_cost_micros"] > 0


@pytest.mark.parametrize(
    "requested_model",
    [
        {
            "provider": "deepseek",
            "model_id": "deepseek-v3",
            "credential_source": "server_env",
        },
        {
            "provider": "deepseek",
            "model_id": "deepseek-v4-flash",
            "base_url": "https://attacker.example/v1",
            "credential_source": "server_env",
        },
    ],
)
def test_public_run_rejects_client_selected_model_or_base_url(
    tmp_path: Path, monkeypatch, requested_model: dict
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    _install_ready_snapshot(monkeypatch, tmp_path)
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post(
            "/api/live-runs",
            json={
                "ticker": "AAPL",
                "trade_date": "2026-01-06",
                "model": requested_model,
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_request: public_model_fixed"}
    with open_rw(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_models_for_anonymous_and_invite_list_only_fixed_public_model(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.access_control import sign_invite_token
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    monkeypatch.setenv(
        "CATALYST_INVITE_SIGNING_KEY",
        "test-only-invite-signing-key-with-32-plus-bytes",
    )

    with TestClient(make_v1_app(db_path)) as client:
        anonymous = client.get("/api/models")
        invite = client.get(
            "/api/models",
            headers={
                "X-Invite-Token": sign_invite_token(
                    "invite-models",
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                )
            },
        )

    expected = {
        "models": [
            {
                "model_id": "deepseek-v4-flash",
                "label": "deepseek-v4-flash",
                "is_default": True,
            }
        ],
        "default_model_id": "deepseek-v4-flash",
    }
    assert anonymous.status_code == 200
    assert anonymous.json() == expected
    assert invite.status_code == 200
    assert invite.json() == expected


def test_models_for_admin_remain_unchanged(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")

    with TestClient(make_v1_app(db_path)) as client:
        response = client.get(
            "/api/models",
            headers={"Authorization": "Bearer test-admin-token"},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["default_model_id"] == "gemini-2.5-flash-nothink"
    assert len(payload["models"]) == 8


def test_public_browser_key_uses_catalog_base_url(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    _install_ready_snapshot(monkeypatch, tmp_path)
    pricing = _write_pricing(
        tmp_path / "byok-pricing.json",
        {
            "models": {
                "deepseek/deepseek-v4-flash": {
                    "input_usd_per_million_tokens": 0.27,
                    "output_usd_per_million_tokens": 1.1,
                },
                "openai/gpt-4o-mini": {
                    "input_usd_per_million_tokens": 0.15,
                    "output_usd_per_million_tokens": 0.6,
                },
            },
            "role_token_limits": _pricing_payload()["role_token_limits"],
        },
    )
    monkeypatch.setenv(PRICING_FILE_ENV, str(pricing))
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post(
            "/api/live-runs",
            json={
                "ticker": "AAPL",
                "trade_date": "2026-01-06",
                "model": {
                    "provider": "openai",
                    "model_id": "gpt-4o-mini",
                    "api_key": "sk-browser-only",
                    "base_url": "https://api.openai.com/v1",
                    "credential_source": "browser_key",
                },
            },
        )

    assert response.status_code == 200, response.text
    with open_rw(db_path) as conn:
        run = conn.execute(
            "SELECT provider, base_url FROM runs WHERE run_id=?",
            (response.json()["run_id"],),
        ).fetchone()
        manifest_row = conn.execute(
            "SELECT payload_json FROM run_artifacts "
            "WHERE run_id=? AND artifact_type='run_manifest' "
            "ORDER BY event_seq ASC LIMIT 1",
            (response.json()["run_id"],),
        ).fetchone()
    manifest = json.loads(manifest_row["payload_json"])
    assert run["provider"] == "openai"
    assert manifest["analyst_model_id"] == "gpt-4o-mini"
    assert run["base_url"] == "https://api.openai.com/v1"


def test_health_fails_when_public_model_is_missing_from_pricing(
    tmp_path: Path, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    override = _write_pricing(
        tmp_path / "missing-public-model.json",
        _pricing_payload(model_key="deepseek/custom-model"),
    )
    monkeypatch.setenv(PRICING_FILE_ENV, str(override))

    with TestClient(make_v1_app(db_path)) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_public_run_uses_default_pricing_and_reserves_computed_max(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    _install_ready_snapshot(monkeypatch, tmp_path)
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_body())

    assert response.status_code == 200, response.text
    expected = priced_run_max_cost_usd(
        load_pricing_config(),
        provider="deepseek",
        model_id="deepseek-v4-flash",
        ceiling_usd=0.05,
    )
    assert expected == pytest.approx(0.05)
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT max_run_cost_micros, reserved_cost_micros "
            "FROM run_budget_reservations WHERE run_id=?",
            (response.json()["run_id"],),
        ).fetchone()
    assert row is not None
    assert row["max_run_cost_micros"] == _micros(expected)
    assert row["reserved_cost_micros"] == _micros(expected)


def test_missing_pricing_file_refuses_admission_and_health_is_not_ready(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    monkeypatch.setenv(PRICING_FILE_ENV, str(tmp_path / "missing-pricing.json"))
    _install_ready_snapshot(monkeypatch, tmp_path)
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_body())
        health = client.get("/api/health").json()

    assert response.status_code == 503
    assert response.json() == {"detail": "MODEL_PRICE_UNAVAILABLE"}
    assert health["status"] == "failed"
    with open_rw(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM run_budget_reservations").fetchone()[0]
            == 0
        )


def test_malformed_pricing_file_refuses_admission_and_health_is_not_ready(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    bad = _write_pricing(tmp_path / "bad-pricing.json", "{not json")
    monkeypatch.setenv(PRICING_FILE_ENV, str(bad))
    _install_ready_snapshot(monkeypatch, tmp_path)
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_body())
        health = client.get("/api/health").json()

    assert response.status_code == 503
    assert response.json() == {"detail": "MODEL_PRICE_UNAVAILABLE"}
    assert health["status"] == "failed"
    with open_rw(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_own_key_run_with_unpriced_model_is_admitted_with_a_token_bound(tmp_path: Path, monkeypatch) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    _install_ready_snapshot(monkeypatch, tmp_path)
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post(
            "/api/live-runs",
            json={
                "ticker": "AAPL",
                "trade_date": "2026-01-06",
                "model": {
                    "provider": "deepseek",
                    "model_id": "not-in-the-pricing-file",
                    "api_key": "sk-test-only",
                    "credential_source": "browser_key",
                },
            },
        )

    # Own-key runs are bounded by tokens, so a model without a price still runs.
    assert response.status_code == 200, response.text
    with open_rw(db_path) as conn:
        row = conn.execute("SELECT max_run_tokens FROM run_budget_reservations").fetchone()
        assert row["max_run_tokens"] == 300_000


def test_pricing_file_env_override_is_used(tmp_path: Path, monkeypatch) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_env(monkeypatch)
    override = _write_pricing(
        tmp_path / "override-pricing.json",
        _pricing_payload(
            model_key="deepseek/deepseek-v4-flash",
            input_price=2.0,
            output_price=4.0,
        ),
    )
    monkeypatch.setenv(PRICING_FILE_ENV, str(override))
    _install_ready_snapshot(monkeypatch, tmp_path)
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_body())

    assert response.status_code == 200, response.text
    expected = priced_run_max_cost_usd(
        load_pricing_config(override),
        provider="deepseek",
        model_id="deepseek-v4-flash",
        ceiling_usd=0.05,
    )
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT max_run_cost_micros, reserved_cost_micros "
            "FROM run_budget_reservations WHERE run_id=?",
            (response.json()["run_id"],),
        ).fetchone()
    assert row["max_run_cost_micros"] == _micros(expected)
    assert row["reserved_cost_micros"] == _micros(expected)


def test_public_guard_has_prices_and_role_bounds_without_inline_json(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.runtime.composition import ProductionRunAdapter

    monkeypatch.delenv("CATALYST_MODEL_PRICES_JSON", raising=False)
    monkeypatch.delenv(PRICING_FILE_ENV, raising=False)
    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        from catalyst_app.persistence.schema import init_runtime_db

        init_runtime_db(conn)
        now = "2026-11-02T12:00:00+00:00"
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id, "
            "manifest_hash, capacity_slot, created_at, updated_at, access_tier) "
            "VALUES ('public-run', 'ACCEPTED', 'hash', 'manifest', 'hash', 0, ?, ?, 'anonymous')",
            (now, now),
        )
        BudgetLedger(max_run_cost_usd=1.0).reserve(
            conn,
            "public-run",
            BudgetAdmission(
                tier="anonymous", caller_key="ip-hash", max_cost_usd=1.0
            ),
            now=datetime.fromisoformat(now),
        )
        conn.commit()

    adapter = ProductionRunAdapter.__new__(ProductionRunAdapter)
    adapter.db_path = db_path
    adapter.provider_budget = None
    guard = adapter._provider_budget_for_run("public-run")

    reserved = {
        role: guard.reserve(
            role=role, provider="deepseek", model_id="deepseek-v4-flash"
        ).reserved_cost_usd
        for role in sorted(required_role_names())
    }
    assert all(value is not None and value > 0 for value in reserved.values())
    assert set(reserved) == set(required_role_names())


def test_loader_rejects_missing_roles_and_duplicate_keys(tmp_path: Path) -> None:
    missing_role = _pricing_payload()
    del missing_role["role_token_limits"]["event_dater"]
    path = _write_pricing(tmp_path / "missing-role.json", missing_role)
    with pytest.raises(PricingUnavailable, match="missing production role"):
        load_pricing_config(path)

    duplicate = (
        '{"models": {"a/b": {"input_usd_per_million_tokens": 1, '
        '"output_usd_per_million_tokens": 1}, '
        '"a/b": {"input_usd_per_million_tokens": 1, '
        '"output_usd_per_million_tokens": 1}}, '
        '"role_token_limits": {}}'
    )
    path = _write_pricing(tmp_path / "duplicate.json", duplicate)
    with pytest.raises(PricingUnavailable, match="malformed"):
        load_pricing_config(path)


def test_admin_guard_has_prices_and_role_bounds_without_inline_json(
    tmp_path: Path, monkeypatch
) -> None:
    # Admin runs used to start from an empty price table and fail every
    # cost-capped run with MODEL_PRICE_UNAVAILABLE before the first call.
    from catalyst_app.runtime.composition import ProductionRunAdapter

    monkeypatch.delenv("CATALYST_MODEL_PRICES_JSON", raising=False)
    monkeypatch.delenv(PRICING_FILE_ENV, raising=False)
    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        from catalyst_app.persistence.schema import init_runtime_db

        init_runtime_db(conn)
        now = "2026-11-16T12:00:00+00:00"
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id, "
            "manifest_hash, capacity_slot, created_at, updated_at, access_tier) "
            "VALUES ('admin-run', 'ACCEPTED', 'hash', 'manifest', 'hash', 0, ?, ?, 'admin')",
            (now, now),
        )
        BudgetLedger(max_run_cost_usd=1.0).reserve(
            conn,
            "admin-run",
            BudgetAdmission(tier="admin", caller_key=None, max_cost_usd=1.0),
            now=datetime.fromisoformat(now),
        )
        conn.commit()

    adapter = ProductionRunAdapter.__new__(ProductionRunAdapter)
    adapter.db_path = db_path
    adapter.provider_budget = None
    guard = adapter._provider_budget_for_run("admin-run")

    reserved = {
        role: guard.reserve(
            role=role, provider="deepseek", model_id="deepseek-v4-flash"
        ).reserved_cost_usd
        for role in sorted(required_role_names())
    }
    assert all(value is not None and value > 0 for value in reserved.values())
