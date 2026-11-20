from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from catalyst_app.deployment_configuration import PUBLIC_DEPLOYMENT_PINNED_ENV
from catalyst_app.featured import (
    load_featured_case,
    load_featured_replay,
)
from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.public_projection import (
    public_projection_violations,
    public_projection_violations_many,
)
from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[3]


def _load_script(name: str):
    path = REPO / "scripts" / "featured" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load_script("build_bundle.py")
lister = _load_script("list_candidates.py")


def _manifest(session_date: str) -> dict[str, object]:
    return {
        "temporal_identity": {"session_date": session_date},
        "analyst_model_id": "deepseek-flash",
        "attribution_policy": {"verifier_enabled": True},
    }


def _snapshot(
    *,
    run_id: str = "run:featured",
    ticker: str = "AAPL",
    session_date: str = "2026-01-06",
    publisher: str = "Example Wire",
    url: str = "https://wire.example/acme",
    text: str = (
        "The company announced a new contract. "
        "Analysts said the contract explains the move. "
        "A third sentence is not needed."
    ),
    outcome: str = "ANSWERED",
    basis: str | None = "REPORTED_ATTRIBUTION",
) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "schema_version": "answer_snapshot_v1",
        "run_id": run_id,
        "ticker": ticker,
        "session_date": session_date,
        "headline": f"{ticker} moved.",
        "outcome": outcome,
        "status": "PARTIAL",
        "tier": "T3",
        "cascade_stage": 1,
        "basis": basis,
        "key_sentence": {
            "text": text,
            "publisher": publisher,
            "url": url,
            "evidence_id": "ev-1",
        },
        "primary": {
            "quotes": [
                {
                    "text": text,
                    "publisher": publisher,
                    "url": url,
                    "evidence_id": "ev-1",
                }
            ],
            "event_type": "CONTRACT",
            "scope": "ISSUER",
            "event_date": session_date,
        },
        "sources": [
            {
                "text": text,
                "publisher": publisher,
                "url": url,
                "evidence_id": "ev-1",
                "role": "PRIMARY",
            }
        ],
        "facts": {"target_return_pct": 2.1},
        "window": {
            "information_window_start_at": "2026-01-05T20:00:00Z",
            "cutoff_at": "2026-01-06T20:00:00Z",
            "session_open_at": "2026-01-06T14:30:00Z",
        },
        "limitations": [],
    }
    if basis is None:
        snapshot.pop("basis", None)
    if outcome == "ABSTAINED":
        snapshot.pop("primary", None)
        snapshot["abstention"] = {
            "reason_code": "NO_PRIMARY_PROPOSED",
            "reason_text": "No cause was published.",
            "possible_explanation": None,
        }
    return snapshot


def _seed_run(
    db_path: Path,
    run_id: str,
    *,
    ticker: str = "AAPL",
    session_date: str = "2026-01-06",
    snapshot: dict[str, object] | None = None,
    lifecycle: str = "COMPLETED",
    secrets: dict[str, str] | None = None,
) -> None:
    snapshot = snapshot or _snapshot(run_id=run_id, ticker=ticker, session_date=session_date)
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id,"
            " manifest_hash, capacity_slot, created_at, updated_at, ticker, provider,"
            " visibility, access_tier) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 'deepseek',"
            " 'private', 'local')",
            (
                run_id,
                lifecycle,
                hashlib.sha256(run_id.encode()).hexdigest(),
                f"manifest:{run_id}",
                "a" * 64,
                f"{session_date}T21:00:00Z",
                f"{session_date}T21:00:03Z",
                ticker,
            ),
        )
        events = [
            (
                1,
                f"{session_date}T21:00:00Z",
                "run.accepted",
                "ADMISSION",
                {
                    "request_identity_digest": "d" * 64,
                    "ticker": ticker,
                    "trade_date": session_date,
                    "workflow_version": "v1.1",
                    "model_provider_label": "deepseek/deepseek-flash",
                    "stream_url": f"/api/live-runs/{run_id}/stream",
                },
            ),
            (
                2,
                f"{session_date}T21:00:00.250000Z",
                "evidence.assessed",
                "EVIDENCE",
                {
                    "accepted_evidence_ids": [],
                    "lead_only_evidence_ids": [],
                    "rejected_evidence_ids": [],
                    "reason_codes": [],
                    "gap_ids": [],
                    "status_ceiling": "ABSTAIN",
                    "decision": "ABSTAIN",
                    "normalization_diagnostics": [
                        "QUOTE_OK:h1:hash:web_body:0001:One. Two. Three.",
                        "primary_verifier_supported:reason:VERIFIER_MODEL_REASONING_SECRET",
                    ],
                },
            ),
            (
                3,
                f"{session_date}T21:00:00.500000Z",
                "answer.delta",
                "ANSWER",
                {
                    "writer_stream_id": f"stream:{run_id}",
                    "delta_text": "MODEL_DELTA_SECRET",
                    "delta_ordinal": 1,
                    "cumulative_character_count": 18,
                    "provisional": True,
                },
            ),
            (
                4,
                f"{session_date}T21:00:01Z",
                "answer.ready",
                "ANSWER",
                {"answer_snapshot": snapshot, "provisional": True},
            ),
            (
                5,
                f"{session_date}T21:00:02Z",
                "investigator.done",
                "ANSWER",
                {
                    "mode": "agent_v3",
                    "stop_reason": "model_stop",
                    "steps": [
                        {
                            "step": 1,
                            "tool": "search_news",
                            "status": "OK",
                            "summary": "SECRET_INVESTIGATION_SUMMARY",
                        }
                    ],
                    "evidence_added": 0,
                },
            ),
            (
                6,
                f"{session_date}T21:00:02.500000Z",
                "verifier.done",
                "VERIFIER",
                {
                    "called": True,
                    "verdict": "SUPPORTED",
                    "category": "COMPANY_NEWS",
                    "reason": "VERIFIER_MODEL_REASONING_SECRET",
                },
            ),
            (
                7,
                f"{session_date}T21:00:03Z",
                "run.completed",
                "TERMINAL",
                {
                    "result_status": "SUCCEEDED" if snapshot.get("outcome") == "ANSWERED" else "ABSTAIN",
                    "final_output_artifact_ref": f"artifact:answer:{run_id}",
                    "total_latency_ms": 3000,
                    "runtime_identity_ref": "test-runtime",
                },
            ),
        ]
        for seq, occurred_at, event_type, stage, payload in events:
            conn.execute(
                "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage,"
                " payload_json, schema_version) VALUES (?, ?, ?, ?, ?, ?, 'v1')",
                (run_id, seq, occurred_at, event_type, stage, json.dumps(payload)),
            )
        artifacts: dict[str, object] = {
            "run_manifest": _manifest(session_date),
            "answer_snapshot": snapshot,
            "decision_trace": {
                "total_ms": 3000,
                "secret": "SECRET_TRACE_TEXT",
                "investigation": {
                    "steps": [{"summary": "Investigation note sentence."}]
                },
            },
            "context_pack": {"secret": "SECRET_CONTEXT_PACK"},
            "rendered_messages": {
                "secret": "SECRET_RENDERED_PROMPT",
                "messages": [{"content": "Prompt instruction sentence."}],
            },
            "evidence_detail": {"secret": "SECRET_EVIDENCE_DETAIL"},
            "run_provider_accounting": {"secret": "SECRET_PROVIDER_ACCOUNTING"},
        }
        for index, (artifact_type, payload) in enumerate(artifacts.items()):
            conn.execute(
                "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type,"
                " payload_hash, payload_json, optional) VALUES (?, ?, 1, ?, ?, ?, 0)",
                (
                    f"{artifact_type}:{run_id}:{index}",
                    run_id,
                    artifact_type,
                    "b" * 64,
                    json.dumps(payload),
                ),
            )
        conn.commit()


def _selection(tmp_path: Path, db_path: Path, **overrides: object) -> Path:
    entry = {
        "case_id": "featured-001",
        "run_id": "run:featured",
        "source_runtime_db": str(db_path),
        "category": "ANSWERED_REPORTED_ATTRIBUTION",
        "title": "Example featured case",
        "tags": ["示例", "reported"],
        "summary": "A public summary written by the reviewer.",
        "notes": "Reviewer notes.",
    }
    entry.update(overrides)
    path = tmp_path / "selection.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    return path


def _build(tmp_path: Path, db_path: Path, **overrides: object) -> tuple[Path, Path]:
    selection = _selection(tmp_path, db_path, **overrides)
    bundle = tmp_path / "bundle.sqlite3"
    manifest = tmp_path / "manifest.json"
    builder.build_bundle(
        selection=selection,
        bundle=bundle,
        manifest=manifest,
        built_at="2026-01-07T00:00:00Z",
        builder_commit="test-commit",
    )
    return bundle, manifest



@pytest.fixture(autouse=True)
def _source_limits_on(monkeypatch):
    """These tests cover the Benzinga/CNBC visitor limits, which are opt-in since 2026-11-18."""
    monkeypatch.setenv("CATALYST_PUBLIC_SOURCE_LIMITS", "1")

def test_builder_writes_only_public_material_and_manifest_has_no_quote_text(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")
    bundle, manifest_path = _build(tmp_path, db_path)

    with sqlite3.connect(bundle) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"bundle_meta", "featured_cases", "featured_events"} <= tables
        assert conn.execute("SELECT COUNT(*) FROM featured_cases").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM featured_events").fetchone()[0] >= 2
        meta = dict(conn.execute("SELECT key, value FROM bundle_meta"))
        assert meta["schema_version"] == "featured_bundle_v1"
        assert len(meta["bundle_content_sha256"]) == 64

    blob = bundle.read_bytes()
    for secret in (
        b"SECRET_TRACE_TEXT",
        b"SECRET_CONTEXT_PACK",
        b"SECRET_RENDERED_PROMPT",
        b"SECRET_EVIDENCE_DETAIL",
        b"SECRET_PROVIDER_ACCOUNTING",
        b"SECRET_INVESTIGATION_SUMMARY",
    ):
        assert secret not in blob

    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["schema_version"] == "featured_manifest_v1"
    assert manifest["cases"][0]["run_id"] == "run:featured"
    assert manifest["cases"][0]["source_db_sha256"] == hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert "The company announced a new contract" not in manifest_text
    assert "SECRET_" not in manifest_text


def test_builder_accepts_earnings_fallback_public_view(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    snapshot = _snapshot(run_id="run:earnings", outcome="ANSWERED", basis="EARNINGS_FILING")
    snapshot["cascade_stage"] = 4
    snapshot.pop("primary", None)
    _seed_run(db_path, "run:earnings", snapshot=snapshot)
    bundle, _ = _build(
        tmp_path,
        db_path,
        case_id="featured-earnings",
        run_id="run:earnings",
        category="ANSWERED_EARNINGS_FALLBACK",
    )
    assert bundle.is_file()
    monkeypatch.setenv("CATALYST_FEATURED_BUNDLE", str(bundle))
    detail = load_featured_case("featured-earnings")
    assert detail is not None
    assert detail.view.answer is not None
    assert detail.view.answer["basis"] == "EARNINGS_FILING"
    assert detail.view.answer["cascade_stage"] == 4


def test_featured_bundle_events_drop_diagnostics_and_model_text(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")
    bundle, _ = _build(tmp_path, db_path)

    blob = bundle.read_bytes()
    for sentinel in (
        b"normalization_diagnostics",
        b"QUOTE_OK",
        b"primary_verifier_supported",
        b"VERIFIER_MODEL_REASONING_SECRET",
    ):
        assert sentinel not in blob

    with sqlite3.connect(bundle) as conn:
        rows = conn.execute(
            "SELECT event_type, event_json FROM featured_events"
        ).fetchall()
    for event_type, event_json in rows:
        if event_type == "evidence.assessed":
            payload = json.loads(event_json)["payload"]
            assert "normalization_diagnostics" not in payload
            assert payload["status_ceiling"] == "ABSTAIN"
            assert payload["decision"] == "ABSTAIN"
        if event_type == "verifier.done":
            payload = json.loads(event_json)["payload"]
            assert "reason" not in payload
            assert payload["verdict"] == "SUPPORTED"
            assert payload["category"] == "COMPANY_NEWS"

    monkeypatch.setenv("CATALYST_FEATURED_BUNDLE", str(bundle))
    replay = load_featured_replay("featured-001")
    assert replay is not None
    replay_text = json.dumps(
        [item.event.payload.model_dump(mode="json") for item in replay.events]
    )
    assert "normalization_diagnostics" not in replay_text
    assert "QUOTE_OK" not in replay_text
    assert "primary_verifier_supported" not in replay_text
    assert "VERIFIER_MODEL_REASONING_SECRET" not in replay_text

def test_builder_refuses_forbidden_artifacts_and_over_limit_benzinga(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")
    selection_path = _selection(tmp_path, db_path)
    original = builder._project_run_view

    def forbidden(_db, _run_id):
        view = original(_db, _run_id)
        view["decision_trace"] = {"secret": "forbidden"}
        return view

    monkeypatch.setattr(builder, "_project_run_view", forbidden)
    with pytest.raises(builder.BuildError, match="forbidden public artifact"):
        builder.build_case(
            builder.load_selection(selection_path)[0],
            sort_order=0,
            selection_path=selection_path,
        )

    def over_limit(_db, _run_id):
        view = original(_db, _run_id)
        view["answer"]["sources"] = [
            {
                "text": "First sentence. Second sentence. Third sentence.",
                "publisher": "Benzinga",
                "url": "https://www.benzinga.com/news/acme",
            }
        ]
        return view

    monkeypatch.setattr(builder, "_project_run_view", over_limit)
    with pytest.raises(builder.BuildError, match="two complete sentences"):
        builder.build_case(
            builder.load_selection(selection_path)[0],
            sort_order=0,
            selection_path=selection_path,
        )


def test_featured_endpoints_shapes_headers_404_and_no_private_text(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")
    bundle, _ = _build(tmp_path, db_path)
    monkeypatch.setenv("CATALYST_FEATURED_BUNDLE", str(bundle))
    monkeypatch.delenv("CATALYST_FEATURED_MANIFEST", raising=False)

    with TestClient(create_app(db_path=db_path)) as client:
        listing = client.get("/api/featured")
        detail = client.get("/api/featured/featured-001")
        replay = client.get("/api/featured/featured-001/replay")
        missing = client.get("/api/featured/nope")
        health = client.get("/api/health")

    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "public, max-age=300"
    body = listing.json()
    assert set(body) == {"items", "categories"}
    assert body["categories"] == ["ANSWERED_REPORTED_ATTRIBUTION"]
    assert body["items"][0] == {
        "case_id": "featured-001",
        "title": "Example featured case",
        "ticker": "AAPL",
        "session_date": "2026-01-06",
        "category": "ANSWERED_REPORTED_ATTRIBUTION",
        "tags": ["示例", "reported"],
        "outcome": "ANSWERED",
        "summary": "A public summary written by the reviewer.",
        "source_run_id": "run:featured",
        "product_tier": "CONFIRMED",
    }

    assert detail.status_code == 200
    assert set(detail.json()) == {"case_id", "run_id", "recorded_at", "view", "notes"}
    assert detail.json()["run_id"] == "run:featured"
    assert detail.json()["view"]["trace"] is None

    assert replay.status_code == 200
    assert set(replay.json()) == {"case_id", "speed_hint", "events"}
    offsets = [item["offset_ms"] for item in replay.json()["events"]]
    assert offsets == sorted(offsets)
    assert offsets[0] == 0
    assert all(item["event"]["event_type"] != "answer.delta" for item in replay.json()["events"])

    assert missing.status_code == 404
    assert missing.json() == {"detail": "case_not_found"}
    assert health.json()["featured"] == "ready"

    for response in (listing, detail, replay):
        assert "SECRET_TRACE_TEXT" not in response.text
        assert "SECRET_CONTEXT_PACK" not in response.text
        assert "SECRET_INVESTIGATION_SUMMARY" not in response.text
        assert "SECRET_RENDERED_PROMPT" not in response.text
        assert "normalization_diagnostics" not in response.text
        assert "QUOTE_OK" not in response.text
        assert "primary_verifier_supported" not in response.text
        assert "VERIFIER_MODEL_REASONING_SECRET" not in response.text


def test_featured_missing_bundle_and_corruption(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")
    monkeypatch.delenv("CATALYST_FEATURED_BUNDLE", raising=False)
    with TestClient(create_app(db_path=db_path)) as client:
        listing = client.get("/api/featured")
        detail = client.get("/api/featured/featured-001")
        health = client.get("/api/health")
    assert listing.status_code == 200
    assert listing.json() == {"items": [], "categories": []}
    assert detail.status_code == 404
    assert health.json()["featured"] == "not_configured"

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_text("not a sqlite database", encoding="utf-8")
    monkeypatch.setenv("CATALYST_FEATURED_BUNDLE", str(corrupt))
    with TestClient(create_app(db_path=db_path)) as client:
        listing = client.get("/api/featured")
        detail = client.get("/api/featured/featured-001")
        health = client.get("/api/health")
    assert listing.status_code == 503
    assert listing.json()["detail"].startswith("runtime_unavailable")
    assert detail.status_code == 503
    assert health.json()["featured"] == "corrupt"


def test_featured_manifest_bundle_mismatch_is_503(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")
    bundle, _ = _build(tmp_path, db_path)
    with sqlite3.connect(bundle) as conn:
        conn.execute(
            "UPDATE featured_cases SET title='tampered' WHERE case_id='featured-001'"
        )
        conn.commit()
    monkeypatch.setenv("CATALYST_FEATURED_BUNDLE", str(bundle))
    with TestClient(create_app(db_path=db_path)) as client:
        response = client.get("/api/featured")
    assert response.status_code == 503
    assert response.json()["detail"].startswith("runtime_unavailable")


def _seed_quota_usage(db_path: Path, *, caller_key: str) -> None:
    ledger = BudgetLedger()
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id,"
            " manifest_hash, capacity_slot, created_at, updated_at, access_tier)"
            " VALUES ('quota-usage', 'COMPLETED', ?, 'manifest:quota', ?, 0,"
            " '2026-01-06T00:00:00Z', '2026-01-06T00:00:01Z', 'admin')",
            ("a" * 64, "b" * 64),
        )
        ledger.reserve(
            conn,
            "quota-usage",
            BudgetAdmission(tier="anonymous", caller_key=caller_key, max_cost_usd=0.05),
        )
        conn.commit()


def _ready_snapshot(tmp_path: Path, monkeypatch) -> None:
    pairs = [{"ticker": "AAPL", "session_date": "2026-01-06"}]
    canonical = json.dumps(pairs, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "ready.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generated_at": "2026-11-04T00:15:00Z",
                "pairs_sha256": hashlib.sha256(canonical).hexdigest(),
                "daily_last_completed_at": "2026-11-03T21:00:00Z",
                "last_completed_session_date": "2026-11-03",
                "pairs": pairs,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CATALYST_READY_SNAPSHOT", str(path))


def test_quota_429_body_includes_featured_url_only_when_configured(
    tmp_path: Path, monkeypatch
) -> None:
    from catalyst_app.routers import live_runs, public_access
    from fastapi.testclient import TestClient
    from v1_helpers import fixture_db, make_v1_app

    db_path = tmp_path / "runtime.sqlite3"
    fixture_db(db_path)
    _seed_run(db_path, "run:featured")
    bundle, _ = _build(tmp_path, db_path)
    for name, value in PUBLIC_DEPLOYMENT_PINNED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("CATALYST_ALLOWED_ORIGINS", "http://testserver")
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "test-only-quota-hash-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-server-key")
    monkeypatch.setenv("CATALYST_ANONYMOUS_DAILY_RUN_LIMIT", "1")
    monkeypatch.setattr(live_runs, "require_valid_turnstile", lambda _token: None)
    monkeypatch.setattr(
        live_runs,
        "_http_access_context",
        lambda _request: __import__("catalyst_app.access_control", fromlist=["AccessContext"]).AccessContext(tier="anonymous"),
    )
    _ready_snapshot(tmp_path, monkeypatch)
    monkeypatch.setattr(
        public_access,
        "utc_now",
        lambda: datetime(2026, 11, 4, 18, tzinfo=timezone.utc),
    )
    import hashlib as _hashlib
    import hmac as _hmac

    caller_key = _hmac.new(
        b"test-only-quota-hash-key", b"testclient", _hashlib.sha256
    ).hexdigest()
    _seed_quota_usage(db_path, caller_key=caller_key)
    request = {
        "ticker": "AAPL",
        "trade_date": "2026-01-06",
        "model": {
            "provider": "deepseek",
            "model_id": "deepseek-v4-flash",
            "credential_source": "server_env",
        },
    }

    monkeypatch.delenv("CATALYST_FEATURED_BUNDLE", raising=False)
    with TestClient(make_v1_app(db_path)) as client:
        without = client.post("/api/live-runs", json=request)
    assert without.status_code == 429
    assert "featured_url" not in without.json()

    monkeypatch.setenv("CATALYST_FEATURED_BUNDLE", str(bundle))
    with TestClient(make_v1_app(db_path)) as client:
        with_featured = client.post("/api/live-runs", json=request)
    assert with_featured.status_code == 429
    assert with_featured.json()["featured_url"] == "/api/featured"


def test_candidate_lister_excludes_benzinga_cnbc_and_sec_corrections(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(
        db_path,
        "run:clean",
        ticker="MSFT",
        snapshot=_snapshot(
            run_id="run:clean",
            ticker="MSFT",
            publisher="Example Wire",
            url="https://wire.example/acme",
        ),
    )
    _seed_run(
        db_path,
        "run:benzinga",
        snapshot=_snapshot(
            run_id="run:benzinga",
            publisher="Benzinga",
            url="https://www.benzinga.com/news/acme",
        ),
    )
    _seed_run(
        db_path,
        "run:cnbc",
        snapshot=_snapshot(
            run_id="run:cnbc",
            publisher="Example Wire",
            url="https://www.cnbc.com/2026/01/06/acme.html",
        ),
    )
    _seed_run(
        db_path,
        "run:corrected",
        session_date="2026-01-08",
        snapshot=_snapshot(run_id="run:corrected", session_date="2026-01-08"),
    )
    _seed_run(
        db_path,
        "run:shifted",
        session_date="2026-01-07",
        snapshot=_snapshot(run_id="run:shifted", session_date="2026-01-07"),
    )
    _seed_run(
        db_path,
        "run:holiday_early_close",
        session_date="2026-11-30",
        snapshot=_snapshot(
            run_id="run:holiday_early_close", session_date="2026-11-30"
        ),
    )
    placebo_dir = tmp_path / "placebo"
    placebo_dir.mkdir()
    placebo_db = placebo_dir / "runtime.sqlite3"
    _seed_run(
        placebo_db,
        "run:placebo",
        ticker="AXP",
        session_date="2026-01-08",
        snapshot=_snapshot(
            run_id="run:placebo",
            ticker="AXP",
            session_date="2026-01-08",
            outcome="ABSTAINED",
            basis=None,
        ),
    )
    corrections = tmp_path / "sec_acceptance_v1.csv"
    corrections.write_text(
        "accession,cik,form,items,json_acceptance_utc,edgar_accepted_et,"
        "edgar_accepted_utc,json_minus_edgar_h,in_stores\n"
        "0000320193-26-000001,320193,8-K,2.02,2026-01-08T20:30:00Z,"
        "2026-01-08 16:00:00,2026-01-08T21:00:00Z,0.5,\n"
        "0000320193-26-000002,320193,8-K,2.02,2026-01-06T20:59:00Z,"
        "2026-01-06 16:01:00,2026-01-06T21:01:00Z,0.03,\n"
        "0000320193-26-000003,320193,8-K,2.02,2026-11-26T16:00:00Z,"
        "2026-11-27 13:01:00,2026-11-27T18:01:00Z,26.02,\n",
        encoding="utf-8",
    )
    tickers = tmp_path / "company_tickers.json"
    tickers.write_text(
        json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"}}),
        encoding="utf-8",
    )

    rows, summary = lister.list_candidates(
        [db_path, placebo_db],
        corrections_path=corrections,
        sec_tickers_path=tickers,
        include_excluded=True,
    )
    by_run = {row["run_id"]: row for row in rows}
    assert by_run["run:benzinga"]["exclusion_reason"] == "blocked_source"
    assert by_run["run:cnbc"]["exclusion_reason"] == "blocked_source"
    assert by_run["run:corrected"]["proposed_category"] == "ANSWERED_REPORTED_ATTRIBUTION"
    assert "exclusion_reason" not in by_run["run:corrected"]
    assert by_run["run:shifted"]["exclusion_reason"] == "sec_correction_changes_session"
    assert by_run["run:holiday_early_close"]["exclusion_reason"] == "sec_correction_changes_session"
    assert by_run["run:clean"]["proposed_category"] == "ANSWERED_REPORTED_ATTRIBUTION"
    assert by_run["run:placebo"]["proposed_category"] == "PLACEBO_DAY"
    assert summary["excluded"]["blocked_source"] == 2
    assert summary["excluded"]["sec_correction_changes_session"] == 2

    included, _ = lister.list_candidates(
        [db_path, placebo_db],
        corrections_path=corrections,
        sec_tickers_path=tickers,
    )
    assert {row["run_id"] for row in included} == {
        "run:clean",
        "run:corrected",
        "run:placebo",
    }


def test_cited_accession_requires_a_changed_session_and_matching_run_date() -> None:
    same_session_accession = "0000320193-26-000001"
    changed_session_accession = "0000320193-26-000002"
    index = lister.CorrectionIndex(
        [
            {
                "accession": same_session_accession,
                "cik": "320193",
                "form": "8-K",
                "json_acceptance_utc": "2026-01-08T20:30:00Z",
                "edgar_accepted_utc": "2026-01-08T21:00:00Z",
            },
            {
                "accession": changed_session_accession,
                "cik": "320193",
                "form": "8-K",
                "json_acceptance_utc": "2026-01-06T20:59:00Z",
                "edgar_accepted_utc": "2026-01-06T21:01:00Z",
            },
        ],
        {},
    )

    assert index.match_run(
        ticker=None,
        session_date="2026-01-08",
        snapshot={"accession": same_session_accession},
    ) is None
    assert index.match_run(
        ticker=None,
        session_date="2026-01-07",
        snapshot={"accession": changed_session_accession},
    ) is not None
    assert index.match_run(
        ticker=None,
        session_date="2026-01-08",
        snapshot={"accession": changed_session_accession},
    ) is None


def test_review2_item1_benzinga_contiguous_sentence_run() -> None:
    url = "https://www.benzinga.com/news/acme"
    article = (
        "Opening sentence. Middle sentence one. Middle sentence two. "
        "Closing sentence."
    )
    mid_article = {
        "answer": {
            "sources": [
                {
                    "text": "Middle sentence one. Middle sentence two.",
                    "publisher": "Benzinga",
                    "url": url,
                }
            ]
        }
    }
    assert not public_projection_violations_many(
        [(mid_article, "$mid")],
        original_texts={"benzinga.com/news/acme": article},
    )

    spanning_fragment = {
        "answer": {
            "sources": [
                {
                    "text": "sentence one. Middle sentence two.",
                    "publisher": "Benzinga",
                    "url": url,
                }
            ]
        }
    }
    assert public_projection_violations_many(
        [(spanning_fragment, "$spanning")],
        original_texts={"benzinga.com/news/acme": article},
    )

    preamble = {
        "answer": {
            "sources": [
                {
                    "text": "Breaking: Middle sentence one. Middle sentence two.",
                    "publisher": "Benzinga",
                    "url": url,
                }
            ]
        }
    }
    assert public_projection_violations_many(
        [(preamble, "$preamble")],
        original_texts={
            "benzinga.com/news/acme": (
                "Breaking: Middle sentence one. Middle sentence two. Closing sentence."
            )
        },
    )


def test_review2_item2_benzinga_missing_original_and_serve_budget() -> None:
    url = "https://www.benzinga.com/news/acme"
    same_quote = [
        (
            {
                "answer": {
                    "sources": [
                        {
                            "text": "Middle sentence one. Middle sentence two.",
                            "publisher": "Benzinga",
                            "url": url,
                        }
                    ]
                }
            },
            "$two",
        ),
        (
            {
                "answer": {
                    "sources": [
                        {
                            "text": "Middle sentence one.",
                            "publisher": "Benzinga",
                            "url": url,
                        }
                    ]
                }
            },
            "$one",
        ),
    ]
    assert not public_projection_violations_many(same_quote)

    three_sentences = [
        (
            {
                "answer": {
                    "sources": [
                        {
                            "text": "Middle sentence one. Middle sentence two.",
                            "publisher": "Benzinga",
                            "url": url,
                        }
                    ]
                }
            },
            "$two",
        ),
        (
            {
                "answer": {
                    "sources": [
                        {
                            "text": "Closing sentence.",
                            "publisher": "Benzinga",
                            "url": url,
                        }
                    ]
                }
            },
            "$third",
        ),
    ]
    assert public_projection_violations_many(three_sentences)

    missing_original = {
        "answer": {
            "sources": [
                {
                    "text": "Middle sentence one.",
                    "publisher": "Benzinga",
                    "url": url,
                }
            ]
        }
    }
    assert public_projection_violations_many(
        [(missing_original, "$missing")], original_texts={}
    )


def test_defect2_event_level_forbidden_text_and_model_token_filter(
    tmp_path: Path, monkeypatch
) -> None:
    view = {"answer": {}, "trace": None, "legacy_text": None}
    for event in (
        {"event_type": "run.accepted", "trace": {"secret": "trace"}},
        {"event_type": "run.accepted", "legacy_text": "legacy"},
        {"event_type": "run.accepted", "summary": "model summary"},
        {"event_type": "run.accepted", "payload": {"text": "model token"}},
        {"event_type": "answer.delta", "payload": {"delta_text": "model delta"}},
    ):
        with pytest.raises(builder.BuildError):
            builder.validate_public_material(
                view, [{"offset_ms": 0, "event": event}]
            )

    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")

    class IdentityProjector:
        def __call__(self, event):
            return event

    monkeypatch.setattr(
        builder, "PublicEventProjector", lambda: IdentityProjector()
    )
    events = builder._project_events(db_path, "run:featured")
    assert all(item["event"]["event_type"] != "answer.delta" for item in events)


def test_review2_item3_source_object_bare_text_and_cnbc_blank() -> None:
    with_body = {
        "answer": {
            "sources": [
                {
                    "text": "Short public quote.",
                    "publisher": "Example Wire",
                    "url": "https://wire.example/a",
                    "body": "Full private article body.",
                }
            ]
        }
    }
    assert public_projection_violations(with_body)

    cnbc_text = {
        "answer": {
            "sources": [
                {
                    "text": "CNBC article text must not be public.",
                    "publisher": "CNBC",
                    "url": "https://www.cnbc.com/a",
                }
            ]
        }
    }
    assert public_projection_violations(cnbc_text)

    blank_cnbc = {
        "answer": {
            "sources": [
                {
                    "text": "   ",
                    "publisher": "CNBC",
                    "url": "https://www.cnbc.com/a",
                    "body": "Private CNBC article body.",
                }
            ]
        }
    }
    assert public_projection_violations(blank_cnbc)

    text_list = {
        "answer": {
            "sources": [
                {
                    "text": ["Private sentence."],
                    "publisher": "Example Wire",
                    "url": "https://wire.example/a",
                }
            ]
        }
    }
    assert public_projection_violations(text_list)

    long_fact = {
        "answer": {"earnings_fallback": {"fact": {"text": "x" * 1001}}}
    }
    assert public_projection_violations(long_fact)

def test_defect3_answer_delta_model_text_is_absent_from_bundle_and_replay(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")
    bundle, _ = _build(tmp_path, db_path)
    assert b"MODEL_DELTA_SECRET" not in bundle.read_bytes()
    monkeypatch.setenv("CATALYST_FEATURED_BUNDLE", str(bundle))
    replay = load_featured_replay("featured-001")
    assert replay is not None
    assert "MODEL_DELTA_SECRET" not in json.dumps(
        [item.event.model_dump(mode="json") for item in replay.events]
    )

    class IdentityProjector:
        def __call__(self, event):
            return event

    monkeypatch.setattr(builder, "PublicEventProjector", lambda: IdentityProjector())
    projected = builder._project_events(db_path, "run:featured")
    assert all(item["event"]["event_type"] != "answer.delta" for item in projected)
    assert "MODEL_DELTA_SECRET" not in json.dumps(projected)


def test_review2_item4_metadata_sources_prompts_notes_and_notes_not_served(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")
    with pytest.raises(builder.BuildError, match="selection metadata"):
        _build(tmp_path, db_path, summary="x" * 1001)

    short_source = "Acme shares rose today."
    snapshot = _snapshot(
        run_id="run:featured",
        text=f"{short_source} The company announced a contract.",
    )
    db_path2 = tmp_path / "runtime2.sqlite3"
    _seed_run(db_path2, "run:featured", snapshot=snapshot)
    with pytest.raises(builder.BuildError, match="source, prompt, or investigation"):
        _build(tmp_path, db_path2, summary=short_source, notes="reviewer note")
    with pytest.raises(builder.BuildError, match="source, prompt, or investigation"):
        _build(tmp_path, db_path, summary="Prompt instruction sentence.")
    with pytest.raises(builder.BuildError, match="source, prompt, or investigation"):
        _build(tmp_path, db_path, summary="Investigation note sentence.")

    notes_case = tmp_path / "notes-case"
    notes_case.mkdir()
    bundle, _ = _build(
        notes_case,
        db_path,
        notes="reviewer freeform note",
    )
    monkeypatch.setenv("CATALYST_FEATURED_BUNDLE", str(bundle))
    detail = load_featured_case("featured-001")
    assert detail is not None
    assert detail.notes is None

    from catalyst_app.featured import _metadata_view_violations

    stored_view = {
        "answer": {
            "sources": [
                {
                    "text": short_source,
                    "publisher": "Example Wire",
                    "url": "https://wire.example/a",
                }
            ]
        }
    }
    assert _metadata_view_violations(short_source, "summary", stored_view)


def test_metadata_leak_predicate_allows_tickers_and_refuses_four_word_sentence(
    tmp_path: Path,
) -> None:
    from catalyst_app.public_text import is_sensitive_metadata_sentence

    assert not is_sensitive_metadata_sentence("GE")
    assert not is_sensitive_metadata_sentence("UPS")
    assert not is_sensitive_metadata_sentence("ABSTAIN")
    assert not is_sensitive_metadata_sentence("NO_PRIMARY_PROPOSED")
    assert not is_sensitive_metadata_sentence("0000320193-26-000001")
    assert not is_sensitive_metadata_sentence("https://www.example.com/a")
    assert is_sensitive_metadata_sentence("Acme shares rose today.")
    assert is_sensitive_metadata_sentence("军工股上涨但无同日公司事件")

    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")
    ticker_case = tmp_path / "ticker-case"
    ticker_case.mkdir()
    _build(ticker_case, db_path, title="GE上涨缺少当日催化")
    _build(ticker_case, db_path, title="UPS回落非当日新闻")

    pasted = "Acme shares rose today."
    snapshot = _snapshot(
        run_id="run:featured",
        text=f"{pasted} Another source sentence follows.",
    )
    pasted_db = tmp_path / "pasted.sqlite3"
    _seed_run(pasted_db, "run:featured", snapshot=snapshot)
    with pytest.raises(builder.BuildError) as excinfo:
        _build(tmp_path, pasted_db, summary=pasted)
    message = str(excinfo.value)
    assert "featured-001.summary" in message
    assert pasted in message

def test_review2_item5_cnbc_subdomains_userinfo_and_root_relative() -> None:
    assert lister._blocked_publisher_or_host(
        {"url": "https://example.com/cnbc/2026/01/acme.html"}
    )
    assert lister._blocked_publisher_or_host(
        {"url": "//news.cnbc.com/2026/01/06/acme.html"}
    )
    assert lister._blocked_publisher_or_host(
        {"url": "//video.cnbc.com/2026/01/06/acme.html"}
    )
    assert lister._blocked_publisher_or_host(
        {"url": "//user:pass@www.cnbc.com/2026/01/06/acme.html"}
    )
    assert lister._blocked_publisher_or_host(
        {"url": "//m.benzinga.com/news/acme"}
    )
    assert lister._blocked_publisher_or_host({"url": "/cnbc/2026/01/06/acme.html"})


def test_defect6_corrupt_numeric_bundle_value_is_503(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    _seed_run(db_path, "run:featured")
    bundle, _ = _build(tmp_path, db_path)
    with sqlite3.connect(bundle) as conn:
        conn.execute(
            "UPDATE featured_cases SET speed_hint='not-a-number' WHERE case_id='featured-001'"
        )
        conn.commit()
    monkeypatch.setenv("CATALYST_FEATURED_BUNDLE", str(bundle))
    with TestClient(create_app(db_path=db_path)) as client:
        response = client.get("/api/featured")
    assert response.status_code == 503
    assert response.json()["detail"].startswith("runtime_unavailable")


def test_placebo_db_is_added_to_the_inputs(tmp_path: Path) -> None:
    clean_db = tmp_path / "clean" / "runtime.sqlite3"
    _seed_run(clean_db, "run:clean", ticker="MSFT")
    placebo_db = tmp_path / "placebo" / "runtime.sqlite3"
    _seed_run(
        placebo_db,
        "run:placebo",
        ticker="AXP",
        session_date="2026-01-08",
        snapshot=_snapshot(
            run_id="run:placebo",
            ticker="AXP",
            session_date="2026-01-08",
            outcome="ABSTAINED",
            basis=None,
        ),
    )
    corrections = tmp_path / "corrections.csv"
    corrections.write_text(
        "accession,cik,form,items,json_acceptance_utc,edgar_accepted_et,"
        "edgar_accepted_utc,json_minus_edgar_h,in_stores\n",
        encoding="utf-8",
    )
    rows, _ = lister.list_candidates(
        [clean_db],
        corrections_path=corrections,
        placebo_dbs=[placebo_db],
    )
    by_run = {row["run_id"]: row for row in rows}
    assert "run:placebo" in by_run
    assert by_run["run:placebo"]["proposed_category"] == "PLACEBO_DAY"
