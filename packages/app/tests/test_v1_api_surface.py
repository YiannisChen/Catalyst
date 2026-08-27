"""V1.1 API surface contract (M6-9).

Final Migration TSD §20.1/§21; Frozen §8-9. The authoritative surface:
POST /api/live-runs (ACCEPTED + stream_url + optional Idempotency-Key),
GET run (lifecycle + nullable attribution + manifest refs), stream, cancel,
paged artifacts, same-run artifact projection, evidence/claims details,
/api/health, /api/capabilities. No SUCCEEDED alias; FAILED/CANCELLED never
synthesize AttributionStatus; credentials never enter responses/events/
artifacts/errors; Host/Origin policy and closed CORS.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from catalyst_app.api_dto import (
    ArtifactRefDTO,
    CapabilityResponse,
    ClaimDetailDTO,
    EvidenceDetailDTO,
    HealthDTO,
    RunAcceptedResponse,
    RunDTO,
)
from catalyst_app.events import (
    AssuranceCompletedPayload,
    RunAcceptedPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
    StageStartedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import ArtifactPayload, EventRepository
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.admission import (
    AdmissionController,
    AdmissionRequest,
)
from catalyst_app.runtime.executor import RunExecutor
from catalyst_agents.attribution.analyst import AttributionStatus, AttributionType
from catalyst_agents.attribution.context_pack import ContextBudget
from catalyst_agents.runtime.manifest import (
    ObservationPolicyConfig,
    RunManifest,
    RuntimeConfiguration,
)
from catalyst_data.canonical.temporal import TemporalIdentity


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _manifest(request: AdmissionRequest, run_id: str, request_hash: str) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        request_hash=request_hash,
        temporal_identity=TemporalIdentity(
            session_date="2026-01-06",
            market_timezone="America/New_York",
            session_open_at=_utc("2026-01-06T14:30:00Z"),
            session_close_at=_utc("2026-01-06T21:00:00Z"),
            information_window_start_at=_utc("2026-01-05T21:00:00Z"),
            cutoff_at=_utc("2026-01-06T21:00:00Z"),
        ),
        data_runtime_identity_ref="runtime-id:test",
        data_runtime_identity_hash="f" * 64,
        code_revision="m6-test",
        workflow_version="v1.1",
        policy_version="p1",
        analyst_model_id=request.model_id,
        analyst_prompt_hash="a" * 64,
        writer_model_id=request.model_id,
        writer_prompt_hash="b" * 64,
        context_pack_schema_version="v1",
        packing_policy_version="p1",
        context_token_budget=4000,
        tokenizer_policy="registered-bge-m3",
        hypothesis_schema_version="v1",
        claim_schema_version="v1",
        max_corrective_rounds=1,
        max_actions_per_batch=1,
        run_timeout_seconds=60,
        provider_capability_revision="cap:v1",
        runtime_configuration=RuntimeConfiguration(
            observation_policy=ObservationPolicyConfig(
                material_target_return_pct=2.0,
                material_prior_return_pct=1.5,
                quiet_target_return_pct=0.5,
                flat_reference_return_pct=0.25,
                aligned_residual_pct=1.0,
                volume_elevated_ratio=1.5,
                volume_extreme_ratio=3.0,
                minimum_peer_count=3,
                require_sector_and_peer_for_broad_sector=True,
                scenario_policy_version="sp:v1",
            ),
            context_budget=ContextBudget(
                model_context_limit=128_000,
                reserved_output_tokens=2_000,
                reserved_system_instruction_tokens=1_000,
                observation_tokens=300,
                coverage_summary_tokens=200,
                research_history_tokens=100,
                inventory_tokens=500,
                evidence_payload_tokens=60_000,
                per_news_item_max_tokens=800,
                per_sec_chunk_max_tokens=1_200,
                lead_only_tokens=1_000,
                safety_margin_tokens=2_000,
            ),
        ),
    )


def _fixture_db(db_path: Path) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()


def _request(**overrides: object) -> AdmissionRequest:
    base: dict[str, object] = {
        "ticker": "AAPL",
        "session_date": "2026-01-06",
        "query": "Why did AAPL move today?",
        "provider": "openai",
        "model_id": "gpt-4.1-mini",
        "base_url": None,
        "credential_source_identifier": "server_env",
        "workflow_version": "v1.1",
        "config_version": "v1.1",
        "idempotency_key": None,
    }
    base.update(overrides)
    return AdmissionRequest(**base)


def _app_with_admission(db_path: Path, *, admission_slots: int = 4, max_workers: int = 2) -> FastAPI:
    repo = EventRepository(db_path=db_path)
    executor = RunExecutor(
        admission_slots=admission_slots,
        max_workers=max_workers,
        run_adapter=lambda run_id, t: {"ok": True},
        shutdown_grace_seconds=0.1,
    )
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=repo,
        manifest_factory=_manifest,
    )
    return create_app(admission_controller=controller, db_path=db_path)


def _seed_completed_run(
    db_path: Path,
    *,
    run_id: str = "run:done",
    attribution_status: str = "PARTIAL",
    attribution_type: str = "EVIDENCE_BACKED_CAUSAL",
) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES (?, 'COMPLETED', NULL, ?, ?, ?, 0, '2026-01-06T14:00:00Z', '2026-01-06T14:00:10Z')",
            (run_id, "d" * 64, f"manifest:{run_id}", "e" * 64),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES (?, 1, 't', 'run.accepted', 'ADMISSION', '{}', 'v1')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES (?, 2, 't', 'run.completed', 'TERMINAL', ?, 'v1')",
            (run_id, json.dumps({"result_status": attribution_status})),
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)"
            " VALUES (?, ?, 2, 'attribution_result', ?, ?, 0)",
            (
                f"attribution:{run_id}",
                run_id,
                "c" * 64,
                json.dumps(
                    {
                        "attribution_status": attribution_status,
                        "attribution_type": attribution_type,
                    },
                    sort_keys=True,
                ),
            ),
        )
        conn.commit()


# ── POST /api/live-runs ─────────────────────────────────────────────────────

def test_post_live_runs_returns_accepted_with_stream_url(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/live-runs",
            json={
                "ticker": "AAPL",
                "trade_date": "2026-01-06",
                "query": "Why did AAPL move today?",
                "model": {
                    "provider": "openai",
                    "model_id": "gpt-4.1-mini",
                    "api_key": "",
                    "credential_source": "server_env",
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ACCEPTED"
    assert payload["run_id"]
    assert payload["stream_url"] == f"/api/live-runs/{payload['run_id']}/stream"


def test_post_live_runs_honors_idempotency_key_header(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        body = {
            "ticker": "AAPL",
            "trade_date": "2026-01-06",
            "query": "Why did AAPL move today?",
            "model": {
                "provider": "openai",
                "model_id": "gpt-4.1-mini",
                "api_key": "",
                "credential_source": "server_env",
            },
        }
        first = client.post("/api/live-runs", json=body, headers={"Idempotency-Key": "key-1"})
        second = client.post("/api/live-runs", json=body, headers={"Idempotency-Key": "key-1"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["run_id"] == first.json()["run_id"]


def test_post_live_runs_same_key_different_hash_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        body = {
            "ticker": "AAPL",
            "trade_date": "2026-01-06",
            "query": "Why did AAPL move today?",
            "model": {
                "provider": "openai",
                "model_id": "gpt-4.1-mini",
                "api_key": "",
                "credential_source": "server_env",
            },
        }
        client.post("/api/live-runs", json=body, headers={"Idempotency-Key": "key-1"})
        changed = dict(body, query="A different question")
        conflict = client.post("/api/live-runs", json=changed, headers={"Idempotency-Key": "key-1"})

    assert conflict.status_code == 409


def test_post_live_runs_capacity_exceeded_returns_429(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    app = _app_with_admission(db_path, admission_slots=2, max_workers=1)

    with TestClient(app) as client:
        def body(ticker: str) -> dict:
            return {
                "ticker": ticker,
                "trade_date": "2026-01-06",
                "query": "Why did the stock move?",
                "model": {
                    "provider": "openai",
                    "model_id": "gpt-4.1-mini",
                    "api_key": "",
                    "credential_source": "server_env",
                },
            }

        client.post("/api/live-runs", json=body("AAPL"))
        client.post("/api/live-runs", json=body("MSFT"))
        third = client.post("/api/live-runs", json=body("NVDA"))

    assert third.status_code == 429


def test_post_live_runs_credentials_never_enter_responses(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    app = _app_with_admission(db_path)
    secret = "sk-test-secret-value-123456"

    with TestClient(app) as client:
        response = client.post(
            "/api/live-runs",
            json={
                "ticker": "AAPL",
                "trade_date": "2026-01-06",
                "query": "Why did AAPL move?",
                "model": {
                    "provider": "openai",
                    "model_id": "gpt-4.1-mini",
                    "api_key": secret,
                    "base_url": None,
                    "credential_source": "browser_key",
                },
            },
        )
        run_id = response.json()["run_id"]
        run_resp = client.get(f"/api/live-runs/{run_id}")
        artifacts_resp = client.get(f"/api/live-runs/{run_id}/artifacts")

    assert secret not in response.text
    assert secret not in run_resp.text
    assert secret not in artifacts_resp.text
    with open_rw(db_path) as conn:
        stored = conn.execute(
            "SELECT payload_json FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchall()
        artifacts = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id = ?", (run_id,)
        ).fetchall()
    assert all(secret not in json.dumps(dict(row)) for row in stored)
    assert all(secret not in json.dumps(dict(row)) for row in artifacts)


# ── GET /api/live-runs/{run_id} ─────────────────────────────────────────────

def test_get_live_run_dto_completed_partial(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_completed_run(db_path, attribution_status="PARTIAL")
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        response = client.get("/api/live-runs/run:done")

    assert response.status_code == 200
    payload = response.json()
    assert payload["lifecycle_status"] == "COMPLETED"
    assert payload["attribution_status"] == "PARTIAL"
    assert payload["attribution_type"] == "EVIDENCE_BACKED_CAUSAL"
    assert payload["manifest_summary"]["run_manifest_id"] == "manifest:run:done"
    assert payload["duration_ms"] == 10_000


def test_get_live_run_dto_completed_abstain(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_completed_run(
        db_path,
        attribution_status="ABSTAIN",
        attribution_type="EVIDENCE_BACKED_CAUSAL",
    )
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        payload = client.get("/api/live-runs/run:done").json()

    assert payload["lifecycle_status"] == "COMPLETED"
    assert payload["attribution_status"] == "ABSTAIN"


def test_get_live_run_failed_never_synthesizes_attribution(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at, failure_code)"
            " VALUES ('run:failed', 'FAILED', NULL, 'a'*64, 'manifest:run:failed', 'b'*64, 0, 't', 't', 'TIMEOUT')"
        )
        conn.commit()
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        payload = client.get("/api/live-runs/run:failed").json()

    assert payload["lifecycle_status"] == "FAILED"
    assert payload["attribution_status"] is None
    assert payload["attribution_type"] is None
    assert payload["failure"]["code"] == "TIMEOUT"


def test_no_succeeded_alias_in_lifecycle(tmp_path: Path) -> None:
    assert RunLifecycleStatus.COMPLETED.value == "COMPLETED"
    assert "SUCCEEDED" not in {s.value for s in RunLifecycleStatus}
    dto = RunDTO(
        run_id="r",
        lifecycle_status=RunLifecycleStatus.COMPLETED,
        attribution_status=AttributionStatus.PARTIAL,
        created_at=_utc("2026-01-06T14:00:00Z"),
    )
    assert dto.attribution_status is AttributionStatus.PARTIAL


# ── artifacts / evidence / claims ───────────────────────────────────────────

def test_artifacts_paged_refs(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:arts', 'COMPLETED', NULL, 'a'*64, 'm', 'b'*64, 0, 't', 't')"
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES ('run:arts', 1, 't', 'run.completed', 'TERMINAL', '{}', 'v1')"
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)"
                " VALUES (?, 'run:arts', 1, 'evidence_state', ?, ?, 0)",
                (f"artifact:{i}", (str(i) + "0" * 63)[:64], json.dumps({"i": i})),
            )
        conn.commit()
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        response = client.get("/api/live-runs/run:arts/artifacts?limit=2&offset=1")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 2
    assert payload["total"] == 3
    assert payload["items"][0]["artifact_id"] == "artifact:1"
    assert payload["items"][0]["ref"]["run_id"] == "run:arts"
    assert payload["items"][0]["ref"]["content_sha256"] == "1" + "0" * 63


def test_artifact_same_run_safe_projection(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:a', 'COMPLETED', NULL, 'a'*64, 'm', 'b'*64, 0, 't', 't')"
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES ('run:a', 1, 't', 'run.completed', 'TERMINAL', '{}', 'v1')"
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)"
            " VALUES ('artifact:x', 'run:a', 1, 'answer', ?, ?, 0)",
            ("c" * 64, json.dumps({"text": "final answer"})),
        )
        conn.commit()
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        ok = client.get("/api/live-runs/run:a/artifacts/artifact:x")
        cross_run = client.get("/api/live-runs/run:a/artifacts/artifact:other")

    assert ok.status_code == 200
    assert ok.json()["payload"]["text"] == "final answer"
    assert cross_run.status_code == 404


def test_evidence_detail_endpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:ev', 'COMPLETED', NULL, 'a'*64, 'm', 'b'*64, 0, 't', 't')"
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES ('run:ev', 1, 't', 'evidence.retrieved', 'INITIAL_RESEARCH', '{}', 'v1')"
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)"
            " VALUES ('evidence_detail:corpus:chunk:0001', 'run:ev', 1, 'evidence_detail', ?, ?, 0)",
            (
                "c" * 64,
                json.dumps(
                    {
                        "evidence_id": "corpus:chunk:0001",
                        "canonical_asset_id": "issuer:AAPL:news:0001",
                        "content_version_id": "content:v1:0001",
                        "chunk_id": "corpus:chunk:0001",
                        "fact_id": None,
                        "excerpt": "AAPL reported record quarterly revenue.",
                        "source_class": "reported_news",
                        "content_state": "FULL_TEXT",
                        "eligible_at": "2026-01-05T21:05:00Z",
                        "ticker_scope": ["AAPL"],
                        "provider": "polygon",
                        "publisher": "example-news",
                        "dedup_cluster_id": None,
                    }
                ),
            ),
        )
        conn.commit()
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        response = client.get("/api/live-runs/run:ev/evidence/corpus:chunk:0001")

    assert response.status_code == 200
    payload = response.json()
    assert payload["evidence_id"] == "corpus:chunk:0001"
    assert payload["source_class"] == "reported_news"


def test_claim_detail_endpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:cl', 'COMPLETED', NULL, 'a'*64, 'm', 'b'*64, 0, 't', 't')"
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES ('run:cl', 1, 't', 'evidence.assessed', 'ASSESSMENT', '{}', 'v1')"
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)"
            " VALUES ('claim_detail:claim:1', 'run:cl', 1, 'claim_detail', ?, ?, 0)",
            (
                "c" * 64,
                json.dumps(
                    {
                        "claim_id": "claim:1",
                        "role": "PRIMARY",
                        "statement": "Strong revenue drove the move.",
                        "mechanism": "revenue surprise",
                        "support_evidence_ids": ["corpus:chunk:0001"],
                        "counter_evidence_ids": [],
                        "limitations": ["News coverage only."],
                        "citation_evidence_ids": ["corpus:chunk:0001"],
                        "validation_status": "validated",
                        "validation_codes": [],
                    }
                ),
            ),
        )
        conn.commit()
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        response = client.get("/api/live-runs/run:cl/claims/claim:1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["claim_id"] == "claim:1"
    assert payload["support_evidence_ids"] == ["corpus:chunk:0001"]
    assert payload["citation_evidence_ids"] == ["corpus:chunk:0001"]


# ── health / capabilities / cancel-all removal ──────────────────────────────

def test_health_endpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"status", "process", "executor", "runtime_db", "event_store"}
    assert payload["status"] in {"ready", "degraded", "failed"}


def test_capabilities_endpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        response = client.get("/api/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert "models" in payload
    for model in payload["models"]:
        assert set(model) >= {
            "provider",
            "model_id",
            "executable",
            "writer_streaming",
            "analyst_structured_output",
            "cancellation",
            "token_usage",
            "timeout_seconds",
            "offline_only",
            "readiness",
        }


def test_cancel_all_not_in_public_api(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    app = _app_with_admission(db_path)

    with TestClient(app) as client:
        response = client.post("/api/live-runs/cancel-all")

    assert response.status_code in {404, 405}


# ── Host/Origin policy ──────────────────────────────────────────────────────

def test_host_origin_policy_rejects_unknown_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "app.example.com")
    monkeypatch.setenv("CATALYST_ALLOWED_ORIGINS", "https://app.example.com")
    app = _app_with_admission(db_path)

    with TestClient(app, base_url="http://evil.example.com") as client:
        response = client.get("/api/live-runs/anything")

    assert response.status_code == 403


def test_host_origin_policy_requires_explicit_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.delenv("CATALYST_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("CATALYST_ALLOWED_ORIGINS", raising=False)

    with pytest.raises(RuntimeError):
        _app_with_admission(db_path)
