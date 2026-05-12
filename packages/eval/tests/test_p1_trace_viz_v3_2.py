import importlib.util
import json
from pathlib import Path
import sys

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[3]
VIZ_PATH = PROJECT_ROOT / "scripts" / "p1_trace_viz.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_render_publication_grade_pngs(tmp_path: Path):
    vz = load_script_module(str(VIZ_PATH), "p1_trace_viz")

    payload = {
        "aggregate": [
            {
                "profile": "full",
                "index_variant": "l1l2",
                "avg_latency_ms": 2100.0,
                "avg_total_cost_usd": 0.021,
                "status_accuracy": 0.84,
                "avg_grounding_rate": 0.79,
            },
            {
                "profile": "no_rerank",
                "index_variant": "l1l2",
                "avg_latency_ms": 1700.0,
                "avg_total_cost_usd": 0.018,
                "status_accuracy": 0.80,
                "avg_grounding_rate": 0.74,
            },
            {
                "profile": "degraded",
                "index_variant": "l1",
                "avg_latency_ms": 900.0,
                "avg_total_cost_usd": 0.008,
                "status_accuracy": 0.68,
                "avg_grounding_rate": 0.61,
            },
        ]
    }
    src = tmp_path / "ablation.json"
    src.write_text(json.dumps(payload), encoding="utf-8")

    vz.render_pngs(src, tmp_path)

    names = ["latency_breakdown.png", "cost_quality_scatter.png", "profile_comparison_bar.png"]
    for name in names:
        path = tmp_path / name
        assert path.exists(), f"missing {name}"
        assert path.stat().st_size > 5 * 1024, f"{name} too small; likely placeholder"
        with Image.open(path) as img:
            w, h = img.size
        assert w > 400 and h > 300, f"{name} has insufficient resolution: {w}x{h}"
