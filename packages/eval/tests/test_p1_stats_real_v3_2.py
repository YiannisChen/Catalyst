import importlib.util

import numpy as np
from scipy.stats import wilcoxon

SCRIPT_PATH = "/Users/yiannischen/Desktop/Catalyst/scripts/p1_stats.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_stats_are_computed_not_stubbed():
    mod = load_script_module(SCRIPT_PATH, "p1_stats")
    pairs = [
        {"a_status": "SUFFICIENT", "b_status": "PARTIAL", "a_refuse": 0, "b_refuse": 1, "a_metric": 0.81, "b_metric": 0.63},
        {"a_status": "PARTIAL", "b_status": "PARTIAL", "a_refuse": 0, "b_refuse": 0, "a_metric": 0.59, "b_metric": 0.55},
        {"a_status": "INSUFFICIENT", "b_status": "INSUFFICIENT", "a_refuse": 1, "b_refuse": 1, "a_metric": 0.14, "b_metric": 0.10},
        {"a_status": "SUFFICIENT", "b_status": "SUFFICIENT", "a_refuse": 0, "b_refuse": 0, "a_metric": 0.93, "b_metric": 0.88},
    ]
    out = mod.run_all_tests(pairs=pairs, stats_seed=7)
    expected_w = float(
        wilcoxon(
            np.array([p["a_metric"] for p in pairs]),
            np.array([p["b_metric"] for p in pairs]),
            zero_method="wilcox",
        ).pvalue
    )

    assert abs(out["continuous_wilcoxon"]["p_value"] - expected_w) < 1e-12
    assert out["status_3class_test"]["method"] in ["stuart_maxwell", "bowker"]
    assert "cohens_dz" in out["effect_sizes"]
    assert "wilcoxon_r" in out["effect_sizes"]
