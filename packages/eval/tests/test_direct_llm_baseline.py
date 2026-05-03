"""Tests for the direct LLM baseline contract."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from catalyst_data.storage.sqlite import (
    compute_asset_id,
    init_db,
    upsert_clean_asset,
    upsert_raw_asset,
)
from catalyst_eval.harness.baselines.direct_llm import (
    apply_direct_llm_cost_guard,
    run_direct_llm,
)
from catalyst_eval.schema.result import AttributionResult


def _build_fixture_db(tmp_path: Path) -> tuple[Path, str]:
    db_path = tmp_path / "direct_llm_fixture.db"
    conn = sqlite3.connect(db_path)
    init_db(conn)
    asset_id = compute_asset_id("AAPL", "2026-01-15", "polygon_news")
    upsert_raw_asset(
        conn,
        asset_id=asset_id,
        ticker="AAPL",
        source_type="polygon_news",
        reference_date="2026-01-15",
        content_raw=b"Apple fell after export restrictions tightened.",
        http_status=200,
        metadata={},
    )
    upsert_clean_asset(
        conn,
        asset_id=asset_id,
        ticker="AAPL",
        source_type="polygon_news",
        reference_date="2026-01-15",
        content_md="Apple fell after export restrictions tightened.",
    )
    conn.close()
    return db_path, asset_id


class MockUsage:
    def __init__(self, input_tokens: int = 120, output_tokens: int = 40) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = input_tokens + output_tokens


class MockResponse:
    def __init__(self, content: str, *, input_tokens: int = 120, output_tokens: int = 40) -> None:
        self.content = content
        self.usage = MockUsage(input_tokens=input_tokens, output_tokens=output_tokens)


class MockLLM:
    def __init__(self, payload: dict, *, input_tokens: int = 120, output_tokens: int = 40) -> None:
        self._payload = payload
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    def invoke(self, prompt: str) -> MockResponse:
        return MockResponse(
            json.dumps(self._payload),
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )


def test_run_direct_llm_returns_schema_compatible_output_and_telemetry(tmp_path: Path):
    db_path, asset_id = _build_fixture_db(tmp_path)
    case = {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": "Why did AAPL fall?",
        "price_move_pct": -4.2,
    }
    llm = MockLLM(
        {
            "causes": [
                {
                    "text": "Export restrictions pressured Apple shares.",
                    "category": "geopolitical",
                    "confidence": 0.82,
                    "evidence_ids": [asset_id],
                    "direction": "negative",
                }
            ],
            "summary_md": f"AAPL fell because [{asset_id}] tightened export restrictions.",
        }
    )

    output = run_direct_llm(case, "claude-sonnet-4-6", db_path, llm=llm)

    validated = AttributionResult.model_validate(
        {
            "ticker": output["ticker"],
            "trade_date": output["trade_date"],
            "causes": output["causes"],
            "summary": output["summary_md"],
            "retrieved_evidence": output["retrieved_evidence"],
            "cost_breakdown": output["cost_breakdown"],
            "total_cost_usd": output["total_cost_usd"],
            "total_tokens": output["total_tokens"],
        }
    )

    assert validated.ticker == "AAPL"
    assert output["output_status"] == "SUFFICIENT"
    assert output["model_id"] == "claude-sonnet-4-6"
    assert output["retrieved_evidence"][0]["asset_id"] == asset_id
    assert output["cost_usd"] > 0
    assert output["latency_ms"] >= 0
    assert output["tokens_in"] == 120
    assert output["tokens_out"] == 40
    assert output["total_tokens"] == 160


def test_run_direct_llm_downgrades_partial_when_evidence_ids_are_missing(tmp_path: Path):
    db_path, _asset_id = _build_fixture_db(tmp_path)
    case = {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": "Why did AAPL fall?",
        "price_move_pct": -4.2,
    }
    llm = MockLLM(
        {
            "causes": [
                {
                    "text": "Unverified claim.",
                    "category": "geopolitical",
                    "confidence": 0.75,
                    "evidence_ids": ["missing-evidence-id"],
                    "direction": "negative",
                }
            ],
            "summary_md": "AAPL fell because of a missing citation.",
        }
    )

    output = run_direct_llm(case, "claude-sonnet-4-6", db_path, llm=llm)

    assert output["output_status"] == "PARTIAL"
    assert output["validation_error"] == "evidence_id_missing"
    assert output["retrieved_evidence"] == []
    assert output["causes"][0]["evidence_ids"] == []


def test_apply_direct_llm_cost_guard_downgrades_model_and_records_header():
    header: dict[str, object] = {}

    selected_model = apply_direct_llm_cost_guard(
        current_model_id="claude-sonnet-4-6",
        observed_costs=[1.8, 1.6],
        header=header,
    )

    assert selected_model == "claude-haiku-4-5"
    assert header["direct_llm_model_substitution"] == {
        "from": "claude-sonnet-4-6",
        "to": "claude-haiku-4-5",
        "projected_total_cost_usd": pytest.approx(25.5),
    }
