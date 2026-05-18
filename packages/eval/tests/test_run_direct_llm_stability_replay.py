from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_direct_llm_stability_replay.py"
    spec = importlib.util.spec_from_file_location("run_direct_llm_stability_replay", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_replay_outputs_consistency_metrics():
    mod = _load_module()
    runs = [
        [{"case_id": "g001", "profile": "full", "output_status": "SUFFICIENT", "refusal_flag": False}],
        [{"case_id": "g001", "profile": "full", "output_status": "SUFFICIENT", "refusal_flag": False}],
        [{"case_id": "g001", "profile": "full", "output_status": "PARTIAL", "refusal_flag": False}],
    ]
    out = mod.aggregate_consistency(runs)
    assert "status_consistency_rate" in out
    assert "refusal_consistency_rate" in out
    assert "per_case_transition_counts" in out
