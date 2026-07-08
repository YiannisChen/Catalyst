"""Tests for the workspace projection module and the /workspace endpoint."""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from catalyst_app.main import create_app
from catalyst_app.workbench_store import WorkbenchStore


class FakeLoader:
    def health(self):
        return {
            "status": "ready",
            "sqlite": {"status": "ready"},
            "lancedb": {"status": "ready"},
            "embedding": {"status": "ready"},
            "reranker": {"status": "ready"},
            "default_model": {"status": "ready", "model": "m"},
            "errors": [],
        }


class WorkspaceFakeService:
    """Provides realistic run data for workspace projection testing."""

    def __init__(self):
        self._summary = {
            "run_id": "ws-run-1",
            "status": "SUFFICIENT",
            "ticker": "AAPL",
            "trade_date": "2025-09-08",
            "model_id": "model-default",
            "started_at": "2025-09-08T14:30:00Z",
            "ended_at": "2025-09-08T14:30:05Z",
            "last_completed_node": "finalizer",
            "predicted_next_node": None,
        }
        self._events = [
            {
                "run_id": "ws-run-1", "trace_id": "t1", "event_seq": 1,
                "node": "miner", "started_at": "2025-09-08T14:30:00Z",
                "ended_at": "2025-09-08T14:30:02Z", "latency_ms": 2000,
                "status_before": "QUEUED", "status_after": "SUFFICIENT",
                "input_tokens": 500, "output_tokens": 200, "cost_usd": 0.01,
                "model_id": "model-default",
            },
            {
                "run_id": "ws-run-1", "trace_id": "t1", "event_seq": 2,
                "node": "critic", "started_at": "2025-09-08T14:30:02Z",
                "ended_at": "2025-09-08T14:30:03Z", "latency_ms": 1000,
                "status_before": "SUFFICIENT", "status_after": "SUFFICIENT",
                "input_tokens": 300, "output_tokens": 100, "cost_usd": 0.005,
                "model_id": "model-default",
            },
            {
                "run_id": "ws-run-1", "trace_id": "t1", "event_seq": 3,
                "node": "judge", "started_at": "2025-09-08T14:30:03Z",
                "ended_at": "2025-09-08T14:30:04Z", "latency_ms": 1000,
                "status_before": "SUFFICIENT", "status_after": "SUFFICIENT",
                "input_tokens": 400, "output_tokens": 150, "cost_usd": 0.008,
                "model_id": "model-default",
            },
            {
                "run_id": "ws-run-1", "trace_id": "t1", "event_seq": 4,
                "node": "validator", "started_at": "2025-09-08T14:30:04Z",
                "ended_at": "2025-09-08T14:30:05Z", "latency_ms": 500,
                "status_before": "SUFFICIENT", "status_after": "SUFFICIENT",
                "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.002,
                "model_id": "model-default",
            },
        ]
        self._artifacts = [
            # miner artifacts
            {
                "run_id": "ws-run-1", "event_seq": 1, "node": "miner",
                "artifact_type": "retrieved_chunks",
                "payload_json": json.dumps({"chunks": [
                    {"asset_id": "ev1", "ticker": "AAPL", "headline": "Rate hike",
                     "snippet": "Fed raises rates...", "source_type": "polygon_news",
                     "reference_date": "2025-09-08", "score": 0.85},
                ]}),
            },
            {
                "run_id": "ws-run-1", "event_seq": 1, "node": "miner",
                "artifact_type": "reranked_chunks",
                "payload_json": json.dumps({"chunks": [
                    {"asset_id": "ev1", "ticker": "AAPL", "headline": "Rate hike",
                     "snippet": "Fed raises rates...", "source_type": "polygon_news",
                     "reference_date": "2025-09-08", "score": 0.85, "rerank_score": 0.92},
                ]}),
            },
            # critic artifacts
            {
                "run_id": "ws-run-1", "event_seq": 2, "node": "critic",
                "artifact_type": "graded_evidence",
                "payload_json": json.dumps({"items": [{
                    "chunk_id": "ev1", "category": "macro", "relevance": 0.9,
                    "reasoning": "Directly relevant",
                    "temporal_match": True, "conflict_signal": 0.1,
                }]}),
            },
            {
                "run_id": "ws-run-1", "event_seq": 2, "node": "critic",
                "artifact_type": "all_graded_chunks",
                "payload_json": json.dumps({"items": []}),
            },
            # judge artifacts
            {
                "run_id": "ws-run-1", "event_seq": 3, "node": "judge",
                "artifact_type": "judge_causes",
                "payload_json": json.dumps({"causes": [{
                    "text": "Rate hike pressured tech",
                    "category": "macro",
                    "direction": "negative",
                    "confidence": 0.85,
                    "evidence_ids": ["ev1"],
                }]}),
            },
            {
                "run_id": "ws-run-1", "event_seq": 3, "node": "judge",
                "artifact_type": "judge_summary",
                "payload_json": json.dumps({
                    "summary_md": "The decline was driven by rate hike fears.",
                    "grounding_rate": 0.95,
                }),
            },
            # validator artifacts
            {
                "run_id": "ws-run-1", "event_seq": 4, "node": "validator",
                "artifact_type": "validator_decision",
                "payload_json": json.dumps({
                    "output_status": "SUFFICIENT",
                    "validation_error": None,
                    "validator_attempts": 1,
                }),
            },
        ]

    def create_run(self, **kwargs):
        return {"run_id": "ws-run-1", "status": "QUEUED", "failure": None}

    def run_one(self, run_id):
        return {"status": "SUCCEEDED"}

    def get_run(self, run_id):
        if run_id != "ws-run-1":
            return None
        return self._summary

    def get_events(self, run_id, *, after_seq=None):
        return self._events

    def get_artifacts(self, run_id, *, event_seq=None, artifact_type=None):
        return self._artifacts

    def retry_run(self, run_id, *, model=None):
        return {"ok": True, "run_id": "retry-1", "status": "QUEUED"}


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL,
           low REAL, close REAL, volume REAL, source TEXT)"""
    )
    conn.execute("INSERT INTO ohlcv VALUES ('AAPL','2025-09-08',100,101,99,100.5,100,'polygon')")
    conn.commit()
    conn.close()


def _client():
    import tempfile
    db_path = tempfile.mktemp(suffix='.db')
    _make_db(db_path)
    app = create_app(
        service_override=WorkspaceFakeService(),
        dependency_loader_override=FakeLoader(),
        workbench_store_override=WorkbenchStore(db_path=db_path),
    )
    return TestClient(app)


# ── Workspace endpoint tests ──

def test_workspace_returns_200_with_structure():
    client = _client()
    resp = client.get("/api/live-runs/ws-run-1/workspace")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["run_id"] == "ws-run-1"
    assert payload["status"] == "SUFFICIENT"
    assert payload["ticker"] == "AAPL"
    assert payload["trade_date"] == "2025-09-08"


def test_workspace_runtime_ms_uses_timestamps():
    client = _client()
    resp = client.get("/api/live-runs/ws-run-1/workspace")
    payload = resp.json()
    # 5 seconds between started_at and ended_at
    assert payload["runtime_ms"] == 5000


def test_workspace_stages_have_labels_and_status():
    client = _client()
    resp = client.get("/api/live-runs/ws-run-1/workspace")
    payload = resp.json()
    stages = payload["stages"]
    assert len(stages) == 4
    labels = {s["id"]: s["label"] for s in stages}
    assert labels["miner"] == "Retriever"
    assert labels["critic"] == "Critic"
    assert labels["judge"] == "Attribution Model"
    assert labels["validator"] == "Validator"
    for s in stages:
        assert s["status"] in ("pending", "active", "complete", "warning", "error", "skipped")
        assert s["duration_ms"] is not None


def test_workspace_evidence_joins_retrieval_rerank_grading():
    client = _client()
    resp = client.get("/api/live-runs/ws-run-1/workspace")
    payload = resp.json()
    evidence = payload["evidence"]
    assert len(evidence) >= 1
    ev = evidence[0]
    assert ev["id"] == "ev1"
    assert ev["retrieval_score"] == 0.85
    assert ev["rerank_score"] == 0.92
    assert ev["critic_relevance"] == 0.9
    assert ev["critic_decision"] == "accepted"
    assert ev["cited"] is True


def test_workspace_cited_evidence_derived_from_judge_evidence_ids():
    client = _client()
    resp = client.get("/api/live-runs/ws-run-1/workspace")
    payload = resp.json()
    # The cause references ev1
    cited_items = [e for e in payload["evidence"] if e["cited"]]
    assert len(cited_items) >= 1
    assert cited_items[0]["id"] == "ev1"


def test_workspace_cause_confidence_preserved():
    client = _client()
    resp = client.get("/api/live-runs/ws-run-1/workspace")
    payload = resp.json()
    causes = payload["result"]["causes"]
    assert len(causes) == 1
    assert causes[0]["confidence"] == 0.85
    # No fabricated contribution weight
    assert "contribution_weight" not in causes[0]
    assert "contribution" not in causes[0]
    assert "probability" not in causes[0]
    assert "causal_share" not in causes[0]


def test_workspace_no_fabricated_global_confidence():
    client = _client()
    resp = client.get("/api/live-runs/ws-run-1/workspace")
    payload = resp.json()
    assert "global_confidence" not in payload
    assert payload["result"].get("global_confidence") is None


def test_workspace_diagnostics_sane():
    client = _client()
    resp = client.get("/api/live-runs/ws-run-1/workspace")
    payload = resp.json()
    diag = payload["diagnostics"]
    assert diag["resolved_stage_count"] == 4
    assert diag["total_stage_count"] == 4
    assert diag["retrieved_count"] == 1
    assert diag["reranked_count"] == 1
    assert diag["graded_count"] == 1
    assert diag["cited_count"] == 1
    assert diag["top_evidence_score"] == 0.9


def test_workspace_unknown_run_returns_404():
    client = _client()
    resp = client.get("/api/live-runs/nonexistent/workspace")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "run_not_found"


# ── Session endpoint tests ──

def test_session_returns_selected_candle():
    client = _client()
    resp = client.get("/api/session/AAPL", params={"trade_date": "2025-09-08"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ticker"] == "AAPL"
    assert payload["trade_date"] == "2025-09-08"
    assert payload["open"] == 100.0
    assert payload["close"] == 100.5
    assert payload["is_trading_day"] is True


def test_session_missing_date_returns_not_trading_day():
    client = _client()
    resp = client.get("/api/session/AAPL", params={"trade_date": "2025-12-25"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["is_trading_day"] is False


def test_session_previous_close_uses_prior_trading_session():
    """Verify previous_close comes from the immediately preceding available
    trading session, not one calendar day earlier."""
    import tempfile
    db_path = tempfile.mktemp(suffix='.db')
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)"
    )
    # Friday close = 200, Monday (gap) trade
    conn.executemany("INSERT INTO ohlcv VALUES (?,?,?,?,?,?,?,?)", [
        ("AAPL", "2025-09-05", 198.0, 202.0, 197.0, 200.0, 1000, "polygon"),
        ("AAPL", "2025-09-08", 201.0, 203.0, 199.0, 198.5, 1200, "polygon"),
    ])
    conn.commit()
    conn.close()

    app = create_app(
        service_override=WorkspaceFakeService(),
        dependency_loader_override=FakeLoader(),
        workbench_store_override=WorkbenchStore(db_path=db_path),
    )
    client = TestClient(app)

    resp = client.get("/api/session/AAPL", params={"trade_date": "2025-09-08"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["previous_close"] == 200.0  # Friday's close, not Saturday's
    assert payload["close_move_pct"] == -0.75  # (198.5 - 200) / 200 * 100


def test_session_close_move_pct_calculation():
    import tempfile
    db_path = tempfile.mktemp(suffix='.db')
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)"
    )
    conn.executemany("INSERT INTO ohlcv VALUES (?,?,?,?,?,?,?,?)", [
        ("AAPL", "2025-09-05", 100.0, 101.0, 99.0, 100.0, 1000, "polygon"),
        ("AAPL", "2025-09-08", 100.0, 105.0, 100.0, 103.0, 1200, "polygon"),
    ])
    conn.commit()
    conn.close()

    app = create_app(
        service_override=WorkspaceFakeService(),
        dependency_loader_override=FakeLoader(),
        workbench_store_override=WorkbenchStore(db_path=db_path),
    )
    client = TestClient(app)

    resp = client.get("/api/session/AAPL", params={"trade_date": "2025-09-08"})
    assert resp.json()["close_move_pct"] == 3.0  # (103-100)/100 * 100


def test_fundamentals_no_future_leak(tmp_path):
    """Regression: fundamentals must not return a snapshot whose fiscal
    period end date falls after the trade_date."""
    db_path = tmp_path / "fund.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)"
    )
    conn.execute("INSERT INTO ohlcv VALUES ('AAPL','2025-09-08',100,101,99,100.5,100,'polygon')")
    conn.execute(
        """CREATE TABLE clean_assets (asset_id TEXT, ticker TEXT, source_type TEXT,
           reference_date TEXT, cleaned_at TEXT, content_md TEXT, title_hash TEXT,
           is_duplicate INTEGER, is_rag_eligible INTEGER, is_canonical INTEGER,
           published_utc TEXT)"""
    )
    # Q4 snapshot with future fiscal date
    conn.execute(
        "INSERT INTO clean_assets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("a1", "AAPL", "fmp_fundamentals", "2025-09-08", "2025-10-31",
         "| Field | Value |\n| date | 2025-09-27 |\n| revenue | 94930000000 |",
         "h1", 0, 1, 1, "2025-10-31"),
    )
    # Q3 snapshot (safe)
    conn.execute(
        "INSERT INTO clean_assets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("a2", "AAPL", "fmp_fundamentals", "2025-06-28", "2025-08-01",
         "| Field | Value |\n| date | 2025-06-28 |\n| revenue | 85777000000 |",
         "h2", 0, 1, 1, "2025-08-01"),
    )
    conn.commit()
    conn.close()

    app = create_app(
        service_override=WorkspaceFakeService(),
        dependency_loader_override=FakeLoader(),
        workbench_store_override=WorkbenchStore(db_path=str(db_path)),
    )
    client = TestClient(app)

    resp = client.get("/api/fundamentals/AAPL", params={"trade_date": "2025-09-08"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["metrics"].get("revenue") == "85777000000", \
        f"Expected Q3 revenue 85777000000, got {payload['metrics']}"
    assert payload["reference_date"] == "2025-06-28"
