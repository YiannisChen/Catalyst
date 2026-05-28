from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "aggregate_catalyst_runtime_stats.py"
    spec = importlib.util.spec_from_file_location("aggregate_catalyst_runtime_stats", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_aggregate_runtime_stats_from_summaries(tmp_path: Path):
    mod = _load_module()
    s1 = tmp_path / "a.summary.json"
    s2 = tmp_path / "b.summary.json"
    s1.write_text(json.dumps({"runtime": {"latency_ms": 100, "total_tokens": 200, "total_cost_usd": 0.3}}))
    s2.write_text(json.dumps({"runtime": {"latency_ms": 300, "total_tokens": 400, "total_cost_usd": 0.5}}))
    stats = mod.aggregate_runtime_stats([s1, s2])
    assert stats["avg_latency_ms"] is not None
    assert stats["avg_total_tokens"] is not None
    assert stats["avg_total_cost_usd"] is not None
