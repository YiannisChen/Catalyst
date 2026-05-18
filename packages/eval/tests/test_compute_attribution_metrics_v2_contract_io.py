from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "compute_attribution_metrics_v2.py"
    spec = importlib.util.spec_from_file_location("compute_attribution_metrics_v2", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_attribution_v2_maps_from_real_case_schema(tmp_path: Path):
    mod = _load_module()
    catalyst_rows = [
        {
            "case_id": "g001",
            "profile": "full",
            "causes": [{"category": "macro", "text": "tariff pause boosted risk appetite"}],
            "gold_categories": ["macro"],
            "gold_causes": ["risk-on rally after tariff pause"],
        }
    ]
    direct_rows = [
        {
            "case_id": "g001",
            "profile": "full",
            "answer": "Risk-on after tariff pause.",
            "pred_categories": ["macro"],
            "gold_categories": ["macro"],
            "gold_causes": ["risk-on rally after tariff pause"],
        }
    ]
    out = mod.compute_metrics(catalyst_rows=catalyst_rows, direct_rows=direct_rows)
    assert out["catalyst"]["category_f1"] >= 0.0
    assert out["direct_llm"]["cause_semantic_sim"] >= 0.0


def test_attribution_v2_fails_fast_when_required_fields_missing(tmp_path: Path):
    mod = _load_module()
    with pytest.raises(ValueError, match="missing required attribution fields"):
        mod.compute_metrics(catalyst_rows=[{"case_id": "g001"}], direct_rows=[{"case_id": "g001"}])
