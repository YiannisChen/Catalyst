from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_compare_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "compare_catalyst_vs_direct_llm.py"
    spec = importlib.util.spec_from_file_location("compare_catalyst_vs_direct_llm", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_scheme_c_artifact_set_and_schema():
    mod = _load_compare_module()
    freeze = {
        "freeze_id": "f1",
        "frozen_units": [{"case_id": "g001", "profile": "full", "expected_status": "INSUFFICIENT", "should_refuse": True}],
    }
    catalyst = {"per_case": [{"case_id": "g001", "profile": "full", "output_status": "INSUFFICIENT", "grounding_rate": 1.0}]}
    direct_rows = [{"case_id": "g001", "profile": "full", "output_status": "INSUFFICIENT", "refusal_flag": True}]
    attr = {"catalyst": {"category_f1": 0.2, "cause_semantic_sim": 0.3}, "direct_llm": {"category_f1": 0.1, "cause_semantic_sim": 0.1}}
    rt = {"avg_total_cost_usd": 0.12, "avg_latency_ms": 1.0, "avg_total_tokens": 30}

    metrics = mod.compute_comparison(
        freeze,
        catalyst,
        direct_rows,
        baseline_tier="tier2_same_evidence",
        attribution_v2=attr,
        catalyst_runtime=rt,
    )
    assert metrics["baseline_tier"] == "tier2_same_evidence"
    assert metrics["catalyst"]["avg_total_cost_usd"] is not None
    assert metrics["catalyst"]["cause_semantic_sim"] >= 0.0


def test_runbook_references_existing_scripts_and_outputs():
    assert Path("scripts/reports/run_scheme_c_batch.sh").exists()
