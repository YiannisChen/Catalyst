from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "compute_attribution_metrics_v2.py"
    spec = importlib.util.spec_from_file_location("compute_attribution_metrics_v2", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_category_f1_handles_set_overlap():
    mod = _load_module()
    assert mod._category_f1({"macro", "earnings"}, {"macro"}) == 2 / 3


def test_semantic_similarity_returns_nonzero_for_paraphrase():
    mod = _load_module()
    sim = mod._cause_semantic_sim([
        "tariff pause boosted risk appetite"
    ], [
        "risk-on rally after tariff pause"
    ])
    assert sim > 0.2
