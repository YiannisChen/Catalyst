from __future__ import annotations

import json
from pathlib import Path

from catalyst_eval.reports.json_writer import normalize_comparison_payload, write_comparison_json
from catalyst_eval.reports.markdown_writer import render_frozen_comparison, write_comparison_markdown
from scripts.check_p0_gate import evaluate_gates


def _comparison_payload(*, lancedb_dir_sha256: str = "DEFERRED_P1") -> dict:
    return {
        "schema_version": "1.0",
        "header": {
            "frozen_ts": "20260503_154053",
            "model_id_per_role": {"direct_llm": "claude-sonnet-4-6"},
            "provider_version": {"anthropic": "0.89.0"},
            "db_sha256": "abc123",
            "lancedb_dir_sha256": lancedb_dir_sha256,
            "code_git_sha": "deadbeef",
            "random_seed": 42,
            "case_distribution": {"sufficient": 5, "partial": 2, "should_refuse": 3},
            "geo_corpus_tier": 2,
        },
        "gates": {
            "evidence_validity": 1.0,
            "schema_validity": 1.0,
            "trace_completeness": 1.0,
            "should_refuse_hit_rate": 1.0,
            "cost_latency_reported": True,
        },
        "quality_metrics": {
            "direct_llm": {"status_accuracy": 1.0},
            "mcj_full": {"status_accuracy": 1.0},
        },
        "cost_latency": {
            "direct_llm": {"avg_cost_usd": 0.1, "avg_latency_ms": 10, "avg_tokens": 100},
            "mcj_full": {"avg_cost_usd": 0.2, "avg_latency_ms": 20, "avg_tokens": 200},
        },
        "per_case": [
            {
                "case_id": "g006",
                "ticker": "TSLA",
                "trade_date": "2025-07-24",
                "expected_status": "SUFFICIENT",
                "should_refuse": False,
                "direct_llm": {"output_status": "SUFFICIENT"},
                "mcj_full": {"output_status": "SUFFICIENT"},
            }
        ],
    }


def test_evaluate_gates_warns_but_passes_on_w15_sentinel():
    result = evaluate_gates(_comparison_payload(lancedb_dir_sha256="DEFERRED_P1"))

    assert result.exit_code == 0
    assert result.warning == "W-15: vector index deferred to P1 — retrieval quality may be degraded"
    assert all(row["passed"] for row in result.rows)


def test_evaluate_gates_fails_when_core_gate_is_red():
    payload = _comparison_payload()
    payload["gates"]["evidence_validity"] = 0.8

    result = evaluate_gates(payload)

    assert result.exit_code == 1
    failed = [row for row in result.rows if not row["passed"]]
    assert failed[0]["gate"] == "evidence_validity"


def test_comparison_writers_roundtrip_payload(tmp_path: Path):
    payload = normalize_comparison_payload(_comparison_payload())

    json_path = tmp_path / "comparison.json"
    md_path = tmp_path / "comparison.md"

    write_comparison_json(payload, json_path)
    write_comparison_markdown(payload, md_path)

    reloaded = json.loads(json_path.read_text())
    markdown = md_path.read_text()

    assert reloaded["schema_version"] == "1.0"
    assert "## Gates" in markdown
    assert "| g006 | SUFFICIENT | SUFFICIENT | SUFFICIENT |" in markdown
    assert render_frozen_comparison(payload) == markdown
