from __future__ import annotations

import hashlib
import hmac
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from catalyst_app.access_control import install_access_control, sign_invite_token
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.routers import live_runs, public_access
from catalyst_app.runtime.admission import AdmissionOutcome
from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _snapshot(path: Path, *, pairs: list[dict[str, str]], last_day: str, generated: str = "2026-11-04T00:15:00Z") -> dict:
    canonical = json.dumps(pairs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {
        "schema_version": 2,
        "generated_at": generated,
        "pairs_sha256": hashlib.sha256(canonical).hexdigest(),
        "daily_last_completed_at": generated,
        "last_completed_session_date": last_day,
        "pairs": pairs,
    }


def _install_snapshot_reader(monkeypatch, path: Path, *, missing: bool = False) -> None:
    if missing:
        monkeypatch.setitem(sys.modules, "catalyst_data.read_api", None)
        return

    module = types.ModuleType("catalyst_data.read_api")

    class ReadySnapshotError(ValueError):
        pass

    def ready_snapshot(snapshot_path=None):
        source = Path(snapshot_path) if snapshot_path is not None else path
        try:
            return json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReadySnapshotError("snapshot unavailable") from exc

    module.ReadySnapshotError = ReadySnapshotError
    module.ready_snapshot = ready_snapshot
    monkeypatch.setitem(sys.modules, "catalyst_data.read_api", module)


class _Admission:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def admit(self, request, *, pre_submit=None):
        self.calls += 1
        run_id = f"run-{self.calls}"
        now = "2026-11-04T18:00:00+00:00"
        with open_rw(self.db_path) as conn:
            conn.execute(
                "INSERT INTO runs (run_id,lifecycle_status,idempotency_key,request_hash,"
                "run_manifest_id,manifest_hash,capacity_slot,created_at,updated_at,ticker) "
                "VALUES (?,'ACCEPTED',NULL,?,?,?,0,?,?,?)",
                (run_id, run_id * 64, f"manifest:{run_id}", "a" * 64, now, now, request.ticker),
            )
            conn.execute(
                "INSERT INTO run_events(run_id,seq,occurred_at,event_type,stage,payload_json,schema_version) "
                "VALUES(?,1,?,'run.accepted','ADMISSION','{}','v1')",
                (run_id, now),
            )
            conn.execute(
                "INSERT INTO run_artifacts (artifact_id,run_id,event_seq,artifact_type,payload_hash,payload_json,optional) "
                "VALUES (?, ?, 1, 'run_manifest', ?, ?, 0)",
                (
                    f"manifest:{run_id}", run_id, "b" * 64,
                    json.dumps({"temporal_identity": {"session_date": request.session_date}}),
                ),
            )
            conn.commit()
        return AdmissionOutcome("accepted", run_id, "c" * 64, f"/api/live-runs/{run_id}/stream")


def _client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "runtime.sqlite3"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()
    app = FastAPI()
    app.include_router(live_runs.router)
    app.include_router(public_access.router)
    app.state.db_path = db_path
    app.state.admission_controller = _Admission(db_path)
    install_access_control(app)
    return TestClient(app)


def _body(*, ticker: str = "AAPL", trade_date: str = "2026-11-03") -> dict:
    return {
        "ticker": ticker,
        "trade_date": trade_date,
        "model": {
            "provider": "deepseek",
            "model_id": "deepseek-v4-flash",
            "api_key": "test-only-key",
            "credential_source": "browser_key",
        },
    }


def test_options_returns_only_prepared_pairs_and_snapshot_metadata(tmp_path, monkeypatch):
    path = tmp_path / "ready.json"
    payload = _snapshot(
        path,
        pairs=[
            {"ticker": "MSFT", "session_date": "2026-11-03"},
            {"ticker": "AAPL", "session_date": "2026-11-03"},
        ],
        last_day="2026-11-04",
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    _install_snapshot_reader(monkeypatch, path)
    monkeypatch.setattr(public_access, "utc_now", lambda: datetime(2026, 11, 4, 22, tzinfo=timezone.utc))
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")

    with _client(tmp_path) as client:
        response = client.get("/api/options")

    assert response.status_code == 200
    assert response.json()["sessions"] == {"AAPL": ["2026-11-03"], "MSFT": ["2026-11-03"]}
    assert response.json()["generated_at"] == payload["generated_at"]
    assert response.json()["last_trading_day"] == "2026-11-04"
    assert response.json()["status"] == "ready"


def test_stale_snapshot_keeps_pairs_and_unreadable_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(_snapshot(path, pairs=[{"ticker": "AAPL", "session_date": "2026-11-02"}], last_day="2026-11-02")), encoding="utf-8")
    _install_snapshot_reader(monkeypatch, path)
    monkeypatch.setattr(public_access, "utc_now", lambda: datetime(2026, 11, 4, 22, tzinfo=timezone.utc))
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")

    with _client(tmp_path) as client:
        stale = client.get("/api/options").json()
        assert stale["status"] == "stale"
        assert stale["sessions"] == {"AAPL": ["2026-11-02"]}
        assert stale["generated_at"] is not None
        assert stale["last_trading_day"] == "2026-11-02"

        allowed_run = client.post("/api/live-runs", json=_body(trade_date="2026-11-02"))
        assert allowed_run.json().get("detail") != "not_in_allowlist"

        path.write_text("{corrupt", encoding="utf-8")
        unavailable = client.get("/api/options").json()
        assert unavailable["status"] == "unavailable"
        assert unavailable["tickers"] == []
        assert unavailable["sessions"] == {}
        failed_run = client.post("/api/live-runs", json=_body())

    assert failed_run.status_code == 503
    assert failed_run.json() == {"detail": "runtime_unavailable: prepared_range_unavailable"}


def test_options_resolves_company_names_and_sector_etfs(tmp_path, monkeypatch):
    from catalyst_data.storage.runtime_paths import RUNTIME_DATA_DIR_ENV

    path = tmp_path / "ready.json"
    pairs = [
        {"ticker": ticker, "session_date": "2026-11-03"}
        for ticker in ("TGT", "FIN", "MISS")
    ]
    path.write_text(json.dumps(_snapshot(path, pairs=pairs, last_day="2026-11-04")), encoding="utf-8")
    _install_snapshot_reader(monkeypatch, path)
    monkeypatch.setattr(public_access, "utc_now", lambda: datetime(2026, 11, 4, 22, tzinfo=timezone.utc))
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")

    runtime_dir = tmp_path / "runtime-data"
    runtime_dir.mkdir()
    (runtime_dir / "sec_company_tickers.json").write_text(json.dumps({
        "0": {"ticker": "TGT", "title": "Target Corporation"},
        "1": {"ticker": "FIN", "title": "Financial Company"},
    }), encoding="utf-8")
    monkeypatch.setenv(RUNTIME_DATA_DIR_ENV, str(runtime_dir))
    peer_map_path = tmp_path / "peer-map.json"
    peer_map_path.write_text(json.dumps({
        "benchmark": "SPY",
        "targets": {"TGT": {"sector_etf": "XLK"}},
        "gics": {"tickers": {"FIN": ["Financials", "Banks"]}},
    }), encoding="utf-8")
    monkeypatch.setenv("CATALYST_PEER_MAP", str(peer_map_path))
    public_access._sec_company_names.cache_clear()
    public_access._peer_sector_etfs.cache_clear()

    with _client(tmp_path) as client:
        response = client.get("/api/options")

    assert response.status_code == 200
    tickers = {item["ticker"]: item for item in response.json()["tickers"]}
    assert tickers["TGT"]["name"] == "Target Corporation"
    assert tickers["TGT"]["sector_etf"] == "XLK"
    assert tickers["FIN"]["name"] == "Financial Company"
    assert tickers["FIN"]["sector_etf"] == "XLF"
    assert tickers["MISS"]["name"] == "MISS"
    assert tickers["MISS"]["sector_etf"] is None


def test_snapshot_import_unavailable_offers_no_range_and_blocks_public_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    _install_snapshot_reader(monkeypatch, tmp_path / "missing.json", missing=True)
    with _client(tmp_path) as client:
        options = client.get("/api/options").json()
        run = client.post("/api/live-runs", json=_body())
    assert options["sessions"] == {}
    assert run.status_code == 503
    assert run.json()["detail"] == "runtime_unavailable: prepared_range_unavailable"


def test_options_reads_data_main_snapshot_with_the_data_core_reader(tmp_path, monkeypatch):
    """Fixture bytes were emitted by data/main ccd5800's ready_snapshot.build."""
    from catalyst_data import read_api

    repo_root = Path(__file__).resolve().parents[3]
    snapshot_path = (
        repo_root
        / "packages/data/tests/fixtures/ready_range_data_main_ccd5800.json"
    )
    snapshot = read_api.ready_snapshot(snapshot_path)
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_READY_SNAPSHOT", str(snapshot_path))
    monkeypatch.setattr(
        public_access,
        "utc_now",
        lambda: datetime(2026, 10, 30, 19, tzinfo=timezone.utc),
    )

    with _client(tmp_path) as client:
        response = client.get("/api/options")

    expected_sessions: dict[str, list[str]] = {}
    for pair in snapshot["pairs"]:
        expected_sessions.setdefault(pair["ticker"], []).append(pair["session_date"])
    for sessions in expected_sessions.values():
        sessions.sort()
    assert response.status_code == 200
    assert response.json()["sessions"] == expected_sessions
    assert response.json()["generated_at"] == snapshot["generated_at"]
    assert response.json()["last_trading_day"] == snapshot["last_completed_session_date"]
    assert response.json()["status"] == "ready"


def test_public_allowlist_and_anonymous_session_history_cookie(tmp_path, monkeypatch):
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(_snapshot(path, pairs=[{"ticker": "AAPL", "session_date": "2026-11-03"}], last_day="2026-11-03")), encoding="utf-8")
    _install_snapshot_reader(monkeypatch, path)
    monkeypatch.setattr(public_access, "utc_now", lambda: datetime(2026, 11, 4, 18, tzinfo=timezone.utc))
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-hash-key")
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with _client(tmp_path) as client:
        rejected = client.post("/api/live-runs", json=_body(ticker="MSFT"))
        accepted = client.post("/api/live-runs", json=_body())
        cookie = accepted.cookies.get(public_access.ANONYMOUS_SESSION_COOKIE)
        history = client.get("/api/live-runs")

    with _client(tmp_path) as other_client:
        other_session_history = other_client.get("/api/live-runs")

    monkeypatch.setattr(public_access, "utc_now", lambda: datetime(2026, 11, 5, 0, 1, tzinfo=timezone.utc))
    with _client(tmp_path) as client:
        next_day_history = client.get("/api/live-runs")

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "not_in_allowlist"
    assert accepted.status_code == 200
    assert cookie
    assert "httponly" in accepted.headers["set-cookie"].lower()
    # Plain http here (no proxy header), so the cookie is not Secure; see the proxied test below.
    assert "secure" not in accepted.headers["set-cookie"].lower()
    assert history.json()["items"][0]["run_id"] == accepted.json()["run_id"]
    assert cookie.startswith("2026-11-04.")
    assert next_day_history.json()["items"] == []
    assert other_session_history.json()["items"] == []


def test_live_run_history_matching_respects_ticker_and_outcome_filters(tmp_path, monkeypatch):
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(_snapshot(
        path, pairs=[
            {"ticker": "AAPL", "session_date": "2026-11-03"},
            {"ticker": "MSFT", "session_date": "2026-11-03"},
        ], last_day="2026-11-03",
    )), encoding="utf-8")
    _install_snapshot_reader(monkeypatch, path)
    monkeypatch.setattr(public_access, "utc_now", lambda: datetime(2026, 11, 4, 18, tzinfo=timezone.utc))
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-hash-key")
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)

    with _client(tmp_path) as client:
        answered = client.post("/api/live-runs", json=_body(ticker="AAPL"))
        failed = client.post("/api/live-runs", json=_body(ticker="MSFT"))
        assert answered.status_code == failed.status_code == 200
        db_path = tmp_path / "runtime.sqlite3"
        with open_rw(db_path) as conn:
            conn.execute("UPDATE runs SET lifecycle_status='COMPLETED' WHERE run_id=?", (answered.json()["run_id"],))
            conn.execute("UPDATE runs SET lifecycle_status='FAILED' WHERE run_id=?", (failed.json()["run_id"],))
            conn.execute(
                "INSERT INTO run_events(run_id,seq,occurred_at,event_type,stage,payload_json,schema_version) "
                "VALUES(?,2,'2026-11-04T18:00:01+00:00','run.completed','FINAL','{}','v1')",
                (answered.json()["run_id"],),
            )
            conn.execute(
                "INSERT INTO run_artifacts (artifact_id,run_id,event_seq,artifact_type,payload_hash,payload_json,optional) "
                "VALUES (?, ?, 2, 'answer_snapshot', ?, ?, 0)",
                ("answer:history-test", answered.json()["run_id"], "c" * 64,
                 json.dumps({"outcome": "ANSWERED", "headline": "AAPL result"})),
            )
            conn.commit()

        all_runs = client.get("/api/live-runs").json()["summary"]
        ticker_runs = client.get("/api/live-runs?ticker=AAPL").json()["summary"]
        ticker_answered = client.get("/api/live-runs?ticker=AAPL&outcome=ANSWERED").json()["summary"]
        answered_runs = client.get("/api/live-runs?outcome=ANSWERED").json()["summary"]
        failed_runs = client.get("/api/live-runs?outcome=FAILED").json()["summary"]

    assert all_runs == {"runs": 2, "answered": 1, "spent_usd": 0.0, "matching": 2}
    assert ticker_runs["matching"] == 1
    assert answered_runs["matching"] == 1
    assert failed_runs["matching"] == 1
    assert (ticker_answered["runs"], ticker_answered["answered"], ticker_answered["spent_usd"]) == (
        ticker_runs["runs"], ticker_runs["answered"], ticker_runs["spent_usd"]
    )
    for filtered in (answered_runs, failed_runs):
        assert (filtered["runs"], filtered["answered"], filtered["spent_usd"]) == (
            all_runs["runs"], all_runs["answered"], all_runs["spent_usd"]
        )


def test_invites_are_allowlisted_but_admin_is_not(tmp_path, monkeypatch):
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(_snapshot(path, pairs=[{"ticker": "AAPL", "session_date": "2026-11-03"}], last_day="2026-11-03")), encoding="utf-8")
    _install_snapshot_reader(monkeypatch, path)
    monkeypatch.setattr(public_access, "utc_now", lambda: datetime(2026, 11, 4, 18, tzinfo=timezone.utc))
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_INVITE_SIGNING_KEY", "test-invite-signing-key-which-is-at-least-32-bytes")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    invite = sign_invite_token("invite-1", expires_at)

    with _client(tmp_path) as client:
        rejected = client.post(
            "/api/live-runs", json=_body(ticker="MSFT"), headers={"X-Invite-Token": invite}
        )
        admin = client.post(
            "/api/live-runs", json=_body(ticker="MSFT"),
            headers={"Authorization": "Bearer test-admin-token"},
        )

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "not_in_allowlist"
    assert admin.status_code == 200
    assert "set-cookie" not in admin.headers


def test_quota_uses_utc_day_and_only_exposes_current_callers_counts(tmp_path, monkeypatch):
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(_snapshot(path, pairs=[], last_day="2026-11-03")), encoding="utf-8")
    _install_snapshot_reader(monkeypatch, path)
    now = datetime(2026, 11, 5, 0, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(public_access, "utc_now", lambda: now)
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-hash-key")

    with _client(tmp_path) as client:
        db_path = client.app.state.db_path
        caller_key = hmac.new(b"test-only-hash-key", b"testclient", hashlib.sha256).hexdigest()
        with open_rw(db_path) as conn:
            for run_id, reserved_day, reservation_caller, actual_cost in (
                ("quota-current", now, caller_key, 0.02),
                ("quota-previous-day", datetime(2026, 11, 4, 12, tzinfo=timezone.utc), caller_key, 0.05),
                ("quota-other-caller", now, "other-callers-hash", 0.05),
            ):
                created_at = reserved_day.isoformat()
                conn.execute(
                    "INSERT INTO runs (run_id,lifecycle_status,request_hash,run_manifest_id,"
                    "manifest_hash,capacity_slot,created_at,updated_at) "
                    "VALUES (?,'ACCEPTED',?,?,?,0,?,?)",
                    (run_id, run_id * 64, f"manifest:{run_id}", "a" * 64, created_at, created_at),
                )
                ledger = BudgetLedger()
                ledger.reserve(
                    conn,
                    run_id,
                    BudgetAdmission(
                        tier="anonymous", caller_key=reservation_caller, max_cost_usd=0.05
                    ),
                    now=reserved_day,
                )
                ledger.settle(conn, run_id, actual_cost_usd=actual_cost)
            conn.commit()
        response = client.get("/api/quota")

    assert response.status_code == 200
    payload = response.json()
    assert payload["per_caller"]["used"] == 1
    assert payload["per_caller"]["remaining"] == payload["per_caller"]["limit"] - 1
    assert payload["per_caller"]["resets_at"] == "2026-11-06T00:00:00Z"
    assert payload["global"]["model_cost_usd"]["used"] == pytest.approx(0.07)
    assert payload["global"]["tavily_credits"]["used"] == 0
    assert caller_key not in response.text
    assert "other-callers-hash" not in response.text


@pytest.mark.parametrize(
    ("now", "expected_reset"),
    [
        (datetime(2026, 11, 4, 23, 59, 59, tzinfo=timezone.utc), "2026-11-05T00:00:00Z"),
        (datetime(2026, 11, 5, 0, 0, 0, tzinfo=timezone.utc), "2026-11-06T00:00:00Z"),
    ],
)
def test_quota_reset_tracks_next_utc_midnight(tmp_path, monkeypatch, now, expected_reset):
    monkeypatch.setattr(public_access, "utc_now", lambda: now)
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-hash-key")

    with _client(tmp_path) as client:
        response = client.get("/api/quota")

    assert response.status_code == 200
    assert response.json()["per_caller"]["resets_at"] == expected_reset


def test_session_cookie_is_secure_behind_an_https_proxy(tmp_path, monkeypatch):
    from starlette.requests import Request
    from starlette.responses import Response

    monkeypatch.setattr(public_access, "_record_session_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(public_access, "_db_path", lambda _request: tmp_path / "runtime.sqlite3")
    monkeypatch.setattr(public_access, "_session_identity", lambda _request: None)

    def cookie_for(headers):
        scope = {"type": "http", "method": "POST", "path": "/api/live-runs", "scheme": "http",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                 "query_string": b"", "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 1234)}
        response = Response()
        public_access.set_anonymous_session_cookie(Request(scope), response, "run-1")
        return response.headers["set-cookie"].lower()

    assert "secure" in cookie_for({"X-Forwarded-Proto": "https"})
    assert "secure" not in cookie_for({})
