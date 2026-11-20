from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from catalyst_app.deployment_configuration import PUBLIC_DEPLOYMENT_PINNED_ENV
from catalyst_app.llm_factory import PUBLIC_RUN_MODEL
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger
from catalyst_app.runtime.public_result_cache import (
    compute_public_cache_key,
    compute_strategy_identity,
    find_completed_public_run,
    production_strategy_identity,
)
from catalyst_data.storage.errors import StoreReadError

_READY_GENERATED_AT = "2026-11-04T00:15:00Z"


def _cache_key(
    *,
    ticker: str = "AAPL",
    model_id: str = "deepseek-v4-flash",
    data_identity: str = _READY_GENERATED_AT,
) -> str:
    return compute_public_cache_key(
        ticker=ticker,
        session_date="2026-01-06",
        data_identity=data_identity,
        provider=PUBLIC_RUN_MODEL.provider,
        model_id=model_id,
        base_url=PUBLIC_RUN_MODEL.base_url,
        config_version="v1.1",
        strategy_identity=production_strategy_identity(),
    )


def _seed_run(
    db_path: Path,
    *,
    run_id: str,
    lifecycle: str,
    visibility: str,
    cache_key: str | None,
    access_tier: str | None = None,
    updated_at: str = "2026-01-06T00:00:01Z",
) -> None:
    if access_tier is None:
        access_tier = "anonymous" if visibility == "public" else "local"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs"
            " (run_id, lifecycle_status, request_hash, run_manifest_id, manifest_hash,"
            " capacity_slot, created_at, updated_at, visibility, public_cache_key,"
            " access_tier)"
            " VALUES (?, ?, ?, ?, ?, 0, '2026-01-06T00:00:00Z', ?, ?, ?, ?)",
            (
                run_id,
                lifecycle,
                (run_id.encode().hex() * 64)[:64],
                f"manifest:{run_id}",
                "e" * 64,
                updated_at,
                visibility,
                cache_key,
                access_tier,
            ),
        )
        conn.commit()


def _ip_caller_key(ip: str) -> str:
    return hmac.new(
        b"test-only-quota-hash-key", ip.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _seed_quota_usage(
    db_path: Path,
    *,
    count: int,
    caller_key: str,
) -> None:
    now = datetime.now(timezone.utc)
    ledger = BudgetLedger()
    for index in range(count):
        _seed_run(
            db_path,
            run_id=f"quota-usage-{index}",
            lifecycle="COMPLETED",
            visibility="private",
            cache_key=None,
            access_tier="admin",
        )
    with open_rw(db_path) as conn:
        for index in range(count):
            ledger.reserve(
                conn,
                f"quota-usage-{index}",
                BudgetAdmission(
                    tier="anonymous",
                    caller_key=caller_key,
                    max_cost_usd=0.05,
                ),
                now=now,
            )
        conn.commit()


def _public_app_env(monkeypatch) -> None:
    for key, value in PUBLIC_DEPLOYMENT_PINNED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("CATALYST_ALLOWED_ORIGINS", "http://testserver")
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-quota-hash-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-server-key")


def _install_ready_snapshot(
    monkeypatch,
    tmp_path: Path,
    pairs: list[dict[str, str]],
    *,
    generated_at: str = _READY_GENERATED_AT,
    last_day: str = "2026-11-03",
) -> None:
    path = tmp_path / "ready.json"
    canonical = json.dumps(
        pairs,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    snapshot = {
        "schema_version": 2,
        "generated_at": generated_at,
        "pairs_sha256": hashlib.sha256(canonical).hexdigest(),
        "daily_last_completed_at": "2026-11-03T21:00:00Z",
        "last_completed_session_date": last_day,
        "pairs": pairs,
    }
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setenv("CATALYST_READY_SNAPSHOT", str(path))

    from catalyst_app.routers import public_access

    monkeypatch.setattr(
        public_access,
        "utc_now",
        lambda: datetime(2026, 11, 4, 18, tzinfo=timezone.utc),
    )


def _anonymous_route(monkeypatch):
    from catalyst_app import access_control
    from catalyst_app.routers import live_runs

    monkeypatch.delenv("CATALYST_ALLOW_NON_LOOPBACK", raising=False)
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-quota-hash-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-server-key")
    monkeypatch.setattr(
        live_runs,
        "_http_access_context",
        lambda _request: access_control.AccessContext(tier="anonymous"),
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)


def _request_body(
    *,
    ticker: str = "AAPL",
    browser_key: bool = False,
    model_id: str = "deepseek-v4-flash",
) -> dict:
    model = {
        "provider": "deepseek",
        "model_id": model_id,
        "credential_source": "browser_key" if browser_key else "server_env",
    }
    if browser_key:
        model["api_key"] = "test-only-browser-key"
    return {"ticker": ticker, "trade_date": "2026-01-06", "model": model}


def test_public_cache_key_is_stable_and_binds_strategy_and_model() -> None:
    kwargs = {
        "ticker": "AAPL",
        "session_date": "2026-01-06",
        "data_identity": _READY_GENERATED_AT,
        "provider": "openai",
        "model_id": "gpt-4.1-mini",
        "base_url": "https://api.example.com",
        "config_version": "v1.1",
        "strategy_identity": production_strategy_identity(),
    }

    assert compute_public_cache_key(**kwargs) == compute_public_cache_key(**kwargs)
    assert compute_public_cache_key(**kwargs) != compute_public_cache_key(
        **{**kwargs, "strategy_identity": "other-strategy"}
    )
    assert compute_public_cache_key(**kwargs) != compute_public_cache_key(
        **{**kwargs, "model_id": "other-model"}
    )
    assert compute_public_cache_key(**kwargs) != compute_public_cache_key(
        **{**kwargs, "data_identity": "2026-11-04T16:45:00Z"}
    )


def test_strategy_identity_binds_manifest_policy_and_policy_version() -> None:
    policy = {"evidence_window_policy": "desk_cascade_v1", "v2_rules": "A,B"}
    identity = compute_strategy_identity(
        policy_version="policy:v1", attribution_policy=policy
    )

    assert identity == compute_strategy_identity(
        policy_version="policy:v1", attribution_policy=dict(policy)
    )
    assert identity != compute_strategy_identity(
        policy_version="policy:v2", attribution_policy=policy
    )
    assert identity != compute_strategy_identity(
        policy_version="policy:v1",
        attribution_policy={**policy, "v2_rules": "A"},
    )


def test_ready_range_uses_generated_at_as_data_identity(
    monkeypatch, tmp_path: Path
) -> None:
    from catalyst_app.runtime.ready_range import load_ready_range

    generated_at = "2026-11-04T16:45:00Z"
    _install_ready_snapshot(
        monkeypatch,
        tmp_path,
        [{"ticker": "AAPL", "session_date": "2026-01-06"}],
        generated_at=generated_at,
    )

    ready = load_ready_range(now=datetime(2026, 11, 4, 18, tzinfo=timezone.utc))

    assert ready.status == "ready"
    assert ready.data_identity == generated_at


def test_public_cache_lookup_only_returns_completed_public_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    key = "a" * 64
    _seed_run(
        db_path,
        run_id="public",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=key,
        access_tier="anonymous",
        updated_at="2026-01-06T00:00:01Z",
    )
    for index, (run_id, lifecycle, visibility, cache_key, tier) in enumerate((
        ("private", "COMPLETED", "private", key, "admin"),
        ("active", "RUNNING", "public", key, "anonymous"),
        ("failed", "FAILED", "public", key, "anonymous"),
        ("cancelled", "CANCELLED", "public", key, "anonymous"),
        ("other-key", "COMPLETED", "public", "b" * 64, "anonymous"),
    )):
        _seed_run(
            db_path,
            run_id=run_id,
            lifecycle=lifecycle,
            visibility=visibility,
            cache_key=cache_key,
            access_tier=tier,
            updated_at=f"2026-01-06T00:00:0{index + 2}Z",
        )

    assert find_completed_public_run(db_path, key) == "public"
    assert find_completed_public_run(db_path, "d" * 64) is None

    excluded_only = tmp_path / "excluded-only.db"
    _seed_run(
        excluded_only,
        run_id="running-only",
        lifecycle="RUNNING",
        visibility="public",
        cache_key=key,
        updated_at="2026-01-06T00:01:00Z",
    )
    assert find_completed_public_run(excluded_only, key) is None


@pytest.mark.parametrize("kind", ["missing", "missing-table"])
def test_public_cache_store_failure_is_not_a_cache_miss(tmp_path: Path, kind: str) -> None:
    path = tmp_path / f"{kind}.db"
    if kind == "missing-table":
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")

    with pytest.raises(StoreReadError, match="public result cache"):
        find_completed_public_run(path, "a" * 64)


def test_public_cache_missing_selected_column_is_not_a_cache_miss(tmp_path: Path) -> None:
    path = tmp_path / "missing-column.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")

    with pytest.raises(StoreReadError, match="public result cache"):
        find_completed_public_run(path, "a" * 64)


def test_anonymous_cache_hit_returns_existing_completed_run_without_budget(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs, public_access
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_app_env(monkeypatch)
    _seed_run(
        db_path,
        run_id="cached-public",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=_cache_key(),
    )
    _install_ready_snapshot(
        monkeypatch, tmp_path, [{"ticker": "AAPL", "session_date": "2026-01-06"}]
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)
    app = make_v1_app(db_path)

    with TestClient(app) as client:
        response = client.post("/api/live-runs", json=_request_body())
        history = client.get("/api/live-runs")

    with TestClient(make_v1_app(db_path)) as other_client:
        other_history = other_client.get("/api/live-runs")

    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == "cached-public"
    assert response.json()["served_from"] == "public_cache"
    assert response.json()["lifecycle_status"] == "COMPLETED"

    cookie = response.cookies.get(public_access.ANONYMOUS_SESSION_COOKIE)
    assert cookie and cookie.startswith("2026-11-04.")
    assert history.json()["items"][0]["run_id"] == "cached-public"
    assert other_history.json()["items"] == []

    with open_rw(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM run_budget_reservations").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("exhaustion", ["caller", "global"])
def test_cache_hit_is_served_when_quota_is_exhausted(
    tmp_path: Path, monkeypatch, exhaustion: str
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_app_env(monkeypatch)
    _seed_run(
        db_path,
        run_id="cached-public",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=_cache_key(),
    )
    _install_ready_snapshot(
        monkeypatch, tmp_path, [{"ticker": "AAPL", "session_date": "2026-01-06"}]
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)
    monkeypatch.delenv("CATALYST_DAILY_MODEL_BUDGET_USD", raising=False)
    if exhaustion == "caller":
        monkeypatch.setenv("CATALYST_ANONYMOUS_DAILY_RUN_LIMIT", "1")
    else:
        monkeypatch.setenv("CATALYST_ANONYMOUS_DAILY_RUN_LIMIT", "5")
        monkeypatch.setenv("CATALYST_DAILY_MODEL_BUDGET_USD", "0.05")
    _seed_quota_usage(
        db_path, count=1, caller_key=_ip_caller_key("testclient")
    )

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_request_body())

    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == "cached-public"
    assert response.json()["served_from"] == "public_cache"
    with open_rw(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM run_budget_reservations").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    ("exhaustion", "expected_detail"),
    [
        ("caller", "rate_limited_ip"),
        ("global", "daily_budget_exhausted"),
    ],
)
def test_quota_exhausted_miss_returns_message(
    tmp_path: Path, monkeypatch, exhaustion: str, expected_detail: str
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_app_env(monkeypatch)
    _install_ready_snapshot(
        monkeypatch, tmp_path, [{"ticker": "AAPL", "session_date": "2026-01-06"}]
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)
    monkeypatch.delenv("CATALYST_DAILY_MODEL_BUDGET_USD", raising=False)
    if exhaustion == "caller":
        monkeypatch.setenv("CATALYST_ANONYMOUS_DAILY_RUN_LIMIT", "1")
    else:
        monkeypatch.setenv("CATALYST_ANONYMOUS_DAILY_RUN_LIMIT", "5")
        monkeypatch.setenv("CATALYST_DAILY_MODEL_BUDGET_USD", "0.05")
    _seed_quota_usage(
        db_path, count=1, caller_key=_ip_caller_key("testclient")
    )

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_request_body())

    assert response.status_code == 429, response.text
    assert response.json() == {
        "detail": expected_detail,
        "message": "今天的额度已用完",
    }
    with open_rw(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM run_budget_reservations").fetchone()[0]
            == 1
        )


def test_anonymous_sixth_run_from_same_ip_is_refused_while_another_ip_runs(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_app_env(monkeypatch)
    _install_ready_snapshot(
        monkeypatch, tmp_path, [{"ticker": "AAPL", "session_date": "2026-01-06"}]
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)
    monkeypatch.delenv("CATALYST_ANONYMOUS_DAILY_RUN_LIMIT", raising=False)
    monkeypatch.delenv("CATALYST_DAILY_MODEL_BUDGET_USD", raising=False)
    _seed_quota_usage(
        db_path, count=5, caller_key=_ip_caller_key("testclient")
    )

    with TestClient(
        make_v1_app(db_path), client=("testclient", 50000)
    ) as same_ip_client:
        refused = same_ip_client.post("/api/live-runs", json=_request_body())
    with TestClient(
        make_v1_app(db_path), client=("198.51.100.20", 50001)
    ) as other_ip_client:
        allowed = other_ip_client.post("/api/live-runs", json=_request_body())

    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "rate_limited_ip"
    assert allowed.status_code == 200, allowed.text
    with open_rw(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM run_budget_reservations").fetchone()[0]
            == 6
        )


def test_cache_miss_when_generated_at_changes(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _seed_run(
        db_path,
        run_id="cached-old-data",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=_cache_key(data_identity="2026-11-04T00:15:00Z"),
    )
    _public_app_env(monkeypatch)
    _install_ready_snapshot(
        monkeypatch,
        tmp_path,
        [{"ticker": "AAPL", "session_date": "2026-01-06"}],
        generated_at="2026-11-04T16:45:00Z",
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_request_body())

    assert response.status_code == 200, response.text
    assert response.json()["run_id"] != "cached-old-data"
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT visibility, public_cache_key FROM runs WHERE run_id = ?",
            (response.json()["run_id"],),
        ).fetchone()
    assert row["visibility"] == "public"
    assert row["public_cache_key"] == _cache_key(
        data_identity="2026-11-04T16:45:00Z"
    )


def test_blank_data_identity_disables_cache_lookup_and_key(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from catalyst_app.runtime.ready_range import ReadyRange
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _seed_run(
        db_path,
        run_id="cached-public",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=_cache_key(),
    )
    _public_app_env(monkeypatch)
    monkeypatch.setattr(
        "catalyst_app.runtime.ready_range.load_ready_range",
        lambda **_kwargs: ReadyRange(
            generated_at="2026-11-04T00:15:00Z",
            last_trading_day="2026-11-03",
            pairs=frozenset({("AAPL", "2026-01-06")}),
            status="ready",
            data_identity=None,
        ),
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)
    lookups: list[str] = []
    monkeypatch.setattr(
        live_runs,
        "find_completed_public_run",
        lambda _path, key: lookups.append(key) or "cached-public",
    )

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_request_body())

    assert response.status_code == 200, response.text
    assert response.json()["run_id"] != "cached-public"
    assert lookups == []
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT visibility, public_cache_key FROM runs WHERE run_id = ?",
            (response.json()["run_id"],),
        ).fetchone()
    assert row["visibility"] == "public"
    assert row["public_cache_key"] is None


def test_off_allowlist_public_request_does_not_reach_cache(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _seed_run(
        db_path,
        run_id="cached-msft",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=_cache_key(ticker="MSFT"),
    )
    _public_app_env(monkeypatch)
    _install_ready_snapshot(
        monkeypatch, tmp_path, [{"ticker": "AAPL", "session_date": "2026-01-06"}]
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)
    lookups: list[str] = []
    monkeypatch.setattr(
        live_runs,
        "find_completed_public_run",
        lambda _path, key: lookups.append(key) or "cached-msft",
    )

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_request_body(ticker="MSFT"))

    assert response.status_code == 400
    assert response.json()["detail"] == "not_in_allowlist"
    assert lookups == []


def test_stale_range_does_not_reach_cache(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _seed_run(
        db_path,
        run_id="cached-public",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=_cache_key(),
    )
    _public_app_env(monkeypatch)
    _install_ready_snapshot(
        monkeypatch,
        tmp_path,
        [{"ticker": "AAPL", "session_date": "2026-01-06"}],
        last_day="2026-11-01",
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)
    lookups: list[str] = []
    monkeypatch.setattr(
        live_runs,
        "find_completed_public_run",
        lambda _path, key: lookups.append(key) or "cached-public",
    )

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_request_body())

    # A stale range still admits listed pairs, but never serves or keys the public cache.
    assert response.json().get("detail") != "not_in_allowlist"
    assert lookups == []


def test_failed_turnstile_never_reaches_cache(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app import turnstile
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _seed_run(
        db_path,
        run_id="cached-public",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=_cache_key(),
    )
    _public_app_env(monkeypatch)
    _install_ready_snapshot(
        monkeypatch, tmp_path, [{"ticker": "AAPL", "session_date": "2026-01-06"}]
    )
    monkeypatch.setenv("CATALYST_TURNSTILE_SECRET_KEY", "test-only-secret")
    lookups: list[str] = []
    monkeypatch.setattr(
        live_runs,
        "find_completed_public_run",
        lambda _path, key: lookups.append(key) or "cached-public",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit: int | None = None) -> bytes:
            payload = {
                "success": False,
                "error-codes": ["invalid-input-response"],
            }
            return json.dumps(payload).encode()[:limit]

    monkeypatch.setattr(turnstile, "urlopen", lambda *_args, **_kwargs: Response())

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post(
            "/api/live-runs",
            json=_request_body(),
            headers={"X-Turnstile-Token": "invalid-test-token"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "turnstile_failed"}
    assert lookups == []


def test_invite_and_admin_requests_bypass_public_cache(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.access_control import sign_invite_token
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _seed_run(
        db_path,
        run_id="cached-public",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=_cache_key(),
    )
    _seed_run(
        db_path,
        run_id="cached-admin-key",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=_cache_key(model_id="gpt-4.1"),
    )
    monkeypatch.delenv("CATALYST_ALLOW_NON_LOOPBACK", raising=False)
    monkeypatch.setenv(
        "CATALYST_INVITE_SIGNING_KEY",
        "test-only-signing-key-which-is-at-least-32-bytes",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-server-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-server-key")
    monkeypatch.setenv("deepseek_api_key", "test-only-server-key")
    invite = sign_invite_token("reviewer", "2026-12-01T00:00:00Z")

    with TestClient(make_v1_app(db_path)) as client:
        invite_response = client.post(
            "/api/live-runs",
            headers={"X-Invite-Token": invite},
            json=_request_body(),
        )
        admin_response = client.post(
            "/api/live-runs", json=_request_body(model_id="gpt-4.1")
        )

    assert invite_response.status_code == 200, invite_response.text
    assert admin_response.status_code == 200, admin_response.text
    assert invite_response.json()["run_id"] not in {"cached-public", "cached-admin-key"}
    assert admin_response.json()["run_id"] not in {"cached-public", "cached-admin-key"}
    with open_rw(db_path) as conn:
        rows = conn.execute(
            "SELECT visibility, public_cache_key FROM runs WHERE run_id IN (?, ?)",
            (invite_response.json()["run_id"], admin_response.json()["run_id"]),
        ).fetchall()
    assert len(rows) == 2
    assert all(row["visibility"] == "private" for row in rows)
    assert all(row["public_cache_key"] is None for row in rows)


def test_anonymous_byok_request_bypasses_public_cache(
    tmp_path: Path, monkeypatch
) -> None:
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _seed_run(
        db_path,
        run_id="cached-public",
        lifecycle="COMPLETED",
        visibility="public",
        cache_key=_cache_key(),
    )
    _anonymous_route(monkeypatch)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post(
            "/api/live-runs", json=_request_body(browser_key=True)
        )

    assert response.status_code == 200, response.text
    assert response.json()["run_id"] != "cached-public"
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT visibility, public_cache_key FROM runs WHERE run_id = ?",
            (response.json()["run_id"],),
        ).fetchone()
    assert row["visibility"] == "private"
    assert row["public_cache_key"] is None


def test_non_completed_or_admin_run_is_not_served(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_app_env(monkeypatch)
    key = _cache_key()
    _seed_run(
        db_path,
        run_id="failed-public",
        lifecycle="FAILED",
        visibility="public",
        cache_key=key,
    )
    _seed_run(
        db_path,
        run_id="admin-completed",
        lifecycle="COMPLETED",
        visibility="private",
        cache_key=key,
        access_tier="admin",
    )
    _install_ready_snapshot(
        monkeypatch, tmp_path, [{"ticker": "AAPL", "session_date": "2026-01-06"}]
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post("/api/live-runs", json=_request_body())

    assert response.status_code == 200, response.text
    assert response.json()["run_id"] not in {"failed-public", "admin-completed"}
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT visibility, public_cache_key FROM runs WHERE run_id = ?",
            (response.json()["run_id"],),
        ).fetchone()
    assert row["visibility"] == "public"
    assert row["public_cache_key"] == key


def test_new_anonymous_run_is_marked_public_and_cacheable(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    _public_app_env(monkeypatch)
    _install_ready_snapshot(
        monkeypatch, tmp_path, [{"ticker": "AAPL", "session_date": "2026-01-06"}]
    )
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with TestClient(make_v1_app(db_path)) as client:
        response = client.post(
            "/api/live-runs",
            json={
                "ticker": "AAPL",
                "trade_date": "2026-01-06",
                "model": {
                    "provider": "deepseek",
                    "model_id": "deepseek-v4-flash",
                    "credential_source": "server_env",
                },
            },
        )

    assert response.status_code == 200, response.text
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT visibility, public_cache_key FROM runs WHERE run_id = ?",
            (response.json()["run_id"],),
        ).fetchone()
    assert row["visibility"] == "public"
    assert row["public_cache_key"]
