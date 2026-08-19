"""M1-5: sealed legacy MCJ comparison adapter (semantic redaction) tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_eval.baseline.adapter import read_legacy_run_artifacts

RUN_ID = "run-legacy-1"
LONG_RAW = "r" * 3000
LONG_HEADLINE = "h" * 5000
MAX_DISPLAY = 4000


def _build_fixture_db(tmp_path: Path, *, with_assurance: bool = False) -> Path:
    db_path = tmp_path / "legacy_runtime.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE agent_runs (
                run_id TEXT PRIMARY KEY, trace_id TEXT, ticker TEXT, trade_date TEXT,
                status TEXT, queued_at TEXT, started_at TEXT, ended_at TEXT,
                total_latency_ms INTEGER, total_cost_usd REAL, model_id_per_role TEXT,
                config TEXT, error_type TEXT, error_message TEXT
            );
            CREATE TABLE trace_events (
                run_id TEXT, trace_id TEXT, event_seq INTEGER, node TEXT,
                started_at TEXT, ended_at TEXT, latency_ms INTEGER, model_id TEXT,
                input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
                decision TEXT, error_type TEXT, error_message TEXT,
                status_before TEXT, status_after TEXT, credential_note TEXT
            );
            CREATE TABLE node_artifacts (
                run_id TEXT, event_seq INTEGER, node TEXT, artifact_type TEXT,
                payload_json TEXT, created_at TEXT
            );
            """
        )
        if with_assurance:
            conn.executescript(
                """
                CREATE TABLE run_assurance (
                    run_id TEXT, schema_version TEXT, record_json TEXT, created_at TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO run_assurance VALUES (?, '1.0.0', ?, 'now')",
                (RUN_ID, json.dumps({
                    "run_id": RUN_ID,
                    "output_status": "SUFFICIENT",
                    "checks": [{
                        "check_name": "cutoff",
                        "status": "pass",
                        "detail": "credential=short-hidden",
                    }],
                    "secret_token": "sk-hidden",
                })),
            )
        conn.execute(
            "INSERT INTO agent_runs VALUES (?, ?, 'AAPL', '2025-09-08', 'COMPLETED', "
            "'2026-05-28T01:50:17Z', '2026-05-28T01:50:18Z', '2026-05-28T01:50:19Z', "
            "1000, 0.01, ?, ?, NULL, 'internal secret error message')",
            (
                RUN_ID,
                "trace-1",
                json.dumps({"writer": "model-safe", "secret_token": "model-hidden"}),
                json.dumps({"api_key": "short-key", "mode": "baseline"}),
            ),
        )
        conn.execute(
            "INSERT INTO trace_events VALUES (?, ?, 1, 'critic', 't0', 't1', 10, 'gemini-x', "
            "10, 20, 0.001, 'continue', NULL, 'hidden trace error', 'RUNNING', 'RUNNING', "
            "'token=trace-hidden')",
            (RUN_ID, "trace-1"),
        )
        artifacts = [
            (RUN_ID, 1, "critic", "critic_decision", json.dumps({
                "sufficiency": "PARTIAL",
                "next_action": "stop",
                "critic_reasoning": "hidden chain-of-thought text",
            })),
            (RUN_ID, 2, "miner", "raw_llm_response", json.dumps({
                "response": LONG_RAW,
            })),
            (RUN_ID, 3, "miner", "graded_evidence", json.dumps({
                "items": [
                    {
                        "asset_id": "asset-1",
                        "headline": LONG_HEADLINE,
                        "rank": 1,
                        "internal_hidden_field": "should-not-survive",
                    }
                ],
            })),
        ]
        conn.executemany(
            "INSERT INTO node_artifacts VALUES (?, ?, ?, ?, ?, 'now')",
            artifacts,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_legacy_reader_returns_versioned_redacted_dict(tmp_path):
    db_path = _build_fixture_db(tmp_path)
    result = read_legacy_run_artifacts(db_path, RUN_ID)
    assert result["schema_version"] == "legacy_mcj_v1"
    assert result["run_id"] == RUN_ID
    assert result["agent_run"]["status"] == "COMPLETED"
    assert result["agent_run"]["error_message"] is None
    assert "config" not in result["agent_run"]
    assert "agent_run.config" in result["redacted_fields"]
    assert "agent_run.error_message" in result["redacted_fields"]
    assert len(result["trace_events"]) == 1
    assert result["trace_events"][0]["error_message"] is None
    assert "trace_events.error_message" in result["redacted_fields"]
    assert result["run_assurance"] is None


def test_critic_reasoning_is_redacted_and_named(tmp_path):
    db_path = _build_fixture_db(tmp_path)
    result = read_legacy_run_artifacts(db_path, RUN_ID)
    text = json.dumps(result)
    assert "hidden chain-of-thought text" not in text
    assert "critic_reasoning" in result["redacted_fields"]
    critic = [a for a in result["node_artifacts"] if a["artifact_type"] == "critic_decision"][0]
    assert critic["payload"]["sufficiency"] == "PARTIAL"
    # The field name is recorded without its value; the key must not survive in payloads.
    assert "critic_reasoning" not in critic["payload"]
    assert "critic_reasoning" not in json.dumps(result["node_artifacts"])


def test_short_raw_llm_response_is_always_redacted(tmp_path):
    db_path = _build_fixture_db(tmp_path)
    result = read_legacy_run_artifacts(db_path, RUN_ID)
    text = json.dumps(result)
    assert "raw_llm_response" in text  # metadata remains
    assert LONG_RAW not in text
    assert "raw_llm_response.payload" in result["redacted_fields"]
    raw = [a for a in result["node_artifacts"] if a["artifact_type"] == "raw_llm_response"][0]
    assert raw["payload"] is None


def test_public_display_fields_are_bounded_truncated(tmp_path):
    db_path = _build_fixture_db(tmp_path)
    result = read_legacy_run_artifacts(db_path, RUN_ID)
    text = json.dumps(result)
    assert LONG_HEADLINE not in text
    graded = [a for a in result["node_artifacts"] if a["artifact_type"] == "graded_evidence"][0]
    item = graded["payload"]["items"][0]
    assert item["asset_id"] == "asset-1"
    assert item["rank"] == 1
    assert len(item["headline"]) <= MAX_DISPLAY + 3
    assert "internal_hidden_field" not in item
    assert "internal_hidden_field" in result["redacted_fields"]
    assert result["truncated_fields"]


def test_run_assurance_is_read_and_secret_keys_redacted(tmp_path):
    db_path = _build_fixture_db(tmp_path, with_assurance=True)
    result = read_legacy_run_artifacts(db_path, RUN_ID)
    assert result["run_assurance"]["schema_version"] == "1.0.0"
    assert result["run_assurance"]["record"]["output_status"] == "SUFFICIENT"
    text = json.dumps(result)
    assert "sk-hidden" not in text
    assert "short-hidden" not in text
    assert "secret_token" in result["redacted_fields"]


def test_secret_bearing_agent_trace_and_assurance_fields_never_survive(tmp_path):
    db_path = _build_fixture_db(tmp_path, with_assurance=True)
    result = read_legacy_run_artifacts(db_path, RUN_ID)
    text = json.dumps(result)
    for forbidden in (
        "short-key",
        "model-hidden",
        "token=trace-hidden",
        "credential=short-hidden",
    ):
        assert forbidden not in text
    assert "config" not in result["agent_run"]
    assert "credential_note" not in result["trace_events"][0]
    assert "secret_token" not in result["run_assurance"]["record"]
    assert "agent_run.config" in result["redacted_fields"]
    assert "trace_events.credential_note" in result["redacted_fields"]


def test_missing_run_returns_empty_artifacts(tmp_path):
    db_path = _build_fixture_db(tmp_path)
    result = read_legacy_run_artifacts(db_path, "run-does-not-exist")
    assert result["agent_run"] is None
    assert result["trace_events"] == []
    assert result["node_artifacts"] == []
    assert result["redacted_fields"] == []
