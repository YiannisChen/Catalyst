import importlib.util
from pathlib import Path
import sys

ABLATION_PATH = "/Users/yiannischen/Desktop/Catalyst/scripts/p1_ablation.py"
VIZ_PATH = "/Users/yiannischen/Desktop/Catalyst/scripts/p1_trace_viz.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_ablation_axes_and_fixed_png_outputs(tmp_path: Path):
    ab = load_script_module(ABLATION_PATH, "p1_ablation")
    vz = load_script_module(VIZ_PATH, "p1_trace_viz")

    rows = ab.build_matrix(
        model_profiles=["gemini-2.5-flash-nothink", "deepseek-v4-flash"],
        routing_profiles=["R0_single", "R1_split"],
        retrieval_profiles=["full", "no_rerank", "no_vector", "degraded"],
        index_variants=["l1l2", "l1"],
        case_ids=["g013", "h001"],
    )
    assert len(rows) == 32

    src = tmp_path / "a.json"
    src.write_text("{}", encoding="utf-8")
    vz.render_pngs(src, tmp_path)
    assert (tmp_path / "latency_breakdown.png").exists()
    assert (tmp_path / "cost_quality_scatter.png").exists()
    assert (tmp_path / "profile_comparison_bar.png").exists()
