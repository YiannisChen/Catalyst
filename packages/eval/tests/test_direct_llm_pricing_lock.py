from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_direct_llm_frozen_baseline.py"
    spec = importlib.util.spec_from_file_location("run_direct_llm_frozen_baseline", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_compute_cost_uses_locked_pricing_file(tmp_path: Path):
    mod = _load_module()
    pricing_path = tmp_path / "pricing.json"
    pricing = {"deepseek-v4-flash": {"input": 0.27, "output": 1.1}}
    pricing_path.write_text(json.dumps(pricing), encoding="utf-8")

    loaded = mod._load_pricing_lock(pricing_path)
    cost = mod._compute_cost("deepseek-v4-flash", 1000, 1000, loaded)
    assert cost > 0
