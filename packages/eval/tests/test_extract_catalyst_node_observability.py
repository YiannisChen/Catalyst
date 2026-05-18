from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "extract_catalyst_node_observability.py"
    spec = importlib.util.spec_from_file_location("extract_catalyst_node_observability", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_extract_node_metrics_from_trace_json():
    mod = _load_module()
    trace = {
        "steps": [
            {"node": "miner", "latency_ms": 10, "usage": {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.01}},
            {"node": "critic", "latency_ms": 15, "usage": {"input_tokens": 120, "output_tokens": 10, "cost_usd": 0.02}},
            {"node": "miner", "latency_ms": 8, "usage": {"input_tokens": 80, "output_tokens": 15, "cost_usd": 0.008}},
        ]
    }
    rows = mod.extract_trace_node_usage(trace)
    assert {"node", "latency_ms_sum", "input_tokens_sum", "output_tokens_sum", "cost_usd_sum"} <= set(rows[0])
