from __future__ import annotations

from fastapi.testclient import TestClient

from catalyst_app.main import create_app


class FakeService:
    def __init__(self):
        self.calls = []
        self.run_one_calls = []
        self._runs = {
            "run-ok": {
                "run_id": "run-ok",
                "status": "RUNNING",
                "last_completed_node": "miner",
                "predicted_next_node": "critic",
                "model_id": "model-default",
            }
        }

    def create_run(self, *, ticker, trade_date, query=None, model=None, config="mcj_full"):
        self.calls.append(("create_run", ticker, trade_date, query, model, config))
        if ticker == "BAD":
            return {
                "run_id": "run-bad",
                "status": "FAILED_REQUEST",
                "failure": {
                    "status": "FAILED_REQUEST",
                    "sub_reason": "unsupported_ticker",
                    "message": "Ticker is not available in the runtime dataset.",
                    "retryable": False,
                },
            }
        return {"run_id": "run-new", "status": "QUEUED", "failure": None}

    def get_run(self, run_id):
        self.calls.append(("get_run", run_id))
        return self._runs.get(run_id)

    def get_events(self, run_id, *, after_seq=None):
        self.calls.append(("get_events", run_id, after_seq))
        return [
            {
                "run_id": run_id,
                "trace_id": "t1",
                "event_seq": 1,
                "node": "miner",
                "status_before": "RUNNING",
                "status_after": "SUFFICIENT",
                "model_id": "model-default",
                "input_tokens": 10,
                "output_tokens": 20,
                "cost_usd": 0.1,
            },
            {
                "run_id": run_id,
                "trace_id": "t1",
                "event_seq": 2,
                "node": "system_error_handler",
                "status_before": "RUNNING",
                "status_after": "SYSTEM_ERROR",
                "model_id": "model-default",
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            },
        ]

    def get_artifacts(self, run_id, *, event_seq=None, artifact_type=None):
        self.calls.append(("get_artifacts", run_id, event_seq, artifact_type))
        return [
            {
                "run_id": run_id,
                "event_seq": event_seq or 1,
                "node": "miner",
                "artifact_type": artifact_type or "state_snapshot",
                "payload_json": {"state": {"phase": "miner"}},
            }
        ]

    def retry_run(self, run_id, *, model=None):
        self.calls.append(("retry_run", run_id, model))
        if run_id == "missing":
            return {
                "ok": False,
                "failure": {
                    "status": "FAILED_REQUEST",
                    "sub_reason": "run_not_found",
                    "message": "Run not found.",
                    "retryable": False,
                },
            }
        if run_id == "running":
            return {
                "ok": False,
                "failure": {
                    "status": "FAILED_REQUEST",
                    "sub_reason": "run_not_terminal",
                    "message": "Only terminal runs can be retried.",
                    "retryable": False,
                },
            }
        return {"ok": True, "run_id": "run-retry", "status": "QUEUED"}

    def run_one(self, run_id):
        self.run_one_calls.append(run_id)
        self.calls.append(("run_one", run_id))
        return {"status": "SUCCEEDED"}


class FakeLoader:
    def __init__(self, status="ready"):
        self._status = status

    def health(self):
        return {
            "status": self._status,
            "sqlite": {"status": "ready"},
            "lancedb": {"status": "ready"},
            "embedding": {"status": "ready"},
            "reranker": {"status": "degraded" if self._status == "degraded" else "ready"},
            "default_model": {"status": "ready", "model": "model-default"},
            "errors": [],
        }


def _client(status="ready"):
    service = FakeService()
    app = create_app(service_override=service, dependency_loader_override=FakeLoader(status=status))
    return TestClient(app), service


def test_post_live_runs_queue_only_response():
    client, service = _client()
    response = client.post(
        "/api/live-runs",
        json={
            "ticker": "AAPL",
            "trade_date": "2026-01-15",
            "query": "explain",
            "model_id": "model-fast",
            "config": "mcj_full",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run-new"
    assert payload["status"] == "QUEUED"
    assert service.run_one_calls == ["run-new"]


def test_post_live_runs_validation_failure_payload():
    client, service = _client()
    response = client.post(
        "/api/live-runs",
        json={"ticker": "BAD", "trade_date": "2026-01-15", "query": None, "model_id": "model-default"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "FAILED_REQUEST"
    assert payload["failure"]["sub_reason"] == "unsupported_ticker"
    assert service.run_one_calls == []


def test_get_live_run_success_and_no_current_node():
    client, _ = _client()
    response = client.get("/api/live-runs/run-ok")
    assert response.status_code == 200
    payload = response.json()
    assert payload["last_completed_node"] == "miner"
    assert payload["predicted_next_node"] == "critic"
    assert "current_node" not in payload


def test_get_live_run_not_found_returns_404():
    client, _ = _client()
    response = client.get("/api/live-runs/unknown")
    assert response.status_code == 404
    assert response.json()["detail"] == "run_not_found"


def test_get_live_run_events_supports_after_seq_and_internal_status():
    client, _ = _client()
    response = client.get("/api/live-runs/run-ok/events", params={"after_seq": 1})
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["status_after"] == "SUFFICIENT"
    assert payload[1]["status_after"] == "SYSTEM_ERROR"


def test_get_live_run_artifacts_supports_filters_and_payload_alias():
    client, _ = _client()
    response = client.get(
        "/api/live-runs/run-ok/artifacts",
        params={"event_seq": 1, "artifact_type": "state_snapshot"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["artifact_type"] == "state_snapshot"
    assert payload[0]["payload"] == {"state": {"phase": "miner"}}


def test_get_live_run_artifacts_invalid_artifact_type_returns_422():
    client, _ = _client()
    response = client.get("/api/live-runs/run-ok/artifacts", params={"artifact_type": "bad_type"})
    assert response.status_code == 422


def test_retry_live_run_success_and_failures_do_not_500():
    client, service = _client()
    ok = client.post("/api/live-runs/run-ok/retry", json={"model_id": "model-fast"})
    missing = client.post("/api/live-runs/missing/retry", json={"model_id": "model-fast"})
    running = client.post("/api/live-runs/running/retry", json={"model_id": "model-fast"})

    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert service.run_one_calls == ["run-retry"]
    assert missing.status_code == 200
    assert missing.json()["ok"] is False
    assert missing.json()["failure"]["sub_reason"] == "run_not_found"
    assert running.status_code == 200
    assert running.json()["ok"] is False
    assert running.json()["failure"]["sub_reason"] == "run_not_terminal"
    assert service.run_one_calls == ["run-retry"]


def test_runtime_health_degraded_returns_200():
    client, _ = _client(status="degraded")
    response = client.get("/api/health/runtime")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["reranker"]["status"] == "degraded"
