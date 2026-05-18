from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "compare_catalyst_vs_direct_llm.py"
    spec = importlib.util.spec_from_file_location("compare_catalyst_vs_direct_llm", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_comparator_outputs_tier_name_and_attribution_columns(tmp_path: Path):
    mod = _load_module()
    freeze = {
        "freeze_id": "f1",
        "frozen_units": [{"case_id": "g001", "profile": "full", "expected_status": "INSUFFICIENT", "should_refuse": True}],
    }
    catalyst = {"per_case": [{"case_id": "g001", "profile": "full", "output_status": "INSUFFICIENT", "grounding_rate": 1.0}]}
    direct_rows = [{"case_id": "g001", "profile": "full", "output_status": "INSUFFICIENT", "refusal_flag": True, "expected_status": "INSUFFICIENT", "should_refuse": True}]
    attr = {"catalyst": {"category_f1": 0.5, "cause_semantic_sim": 0.4}, "direct_llm": {"category_f1": 0.3, "cause_semantic_sim": 0.2}}
    tier2 = {"tier2_eligible_n": 1, "tier2_excluded_n": 2, "tier2_exclusion_reason_counts": {"no_evidence_chunks": 2}}

    out = mod.compute_comparison(freeze, catalyst, direct_rows, baseline_tier="tier2_same_evidence", attribution_v2=attr, tier2_meta=tier2)
    assert out["baseline_tier"] in {"tier0_closed_book", "tier1_search_augmented", "tier2_same_evidence"}
    assert "category_f1" in out["catalyst"]
    assert "cause_semantic_sim" in out["catalyst"]
    assert "tier2_eligible_n" in out
    assert "tier2_excluded_n" in out
    assert "tier2_exclusion_reason_counts" in out
