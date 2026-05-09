import importlib.util

SCRIPT_PATH = "/Users/yiannischen/Desktop/Catalyst/scripts/p1_stats.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_balance_warning_and_limitation_template():
    mod = load_script_module(SCRIPT_PATH, "p1_stats")
    out = mod.class_balance_block(["SUFFICIENT"] * 18 + ["PARTIAL"] * 4 + ["INSUFFICIENT"] * 3)
    assert out["class_balance_warning"] is True
    assert out["per_class_n"] == {"SUFFICIENT": 18, "PARTIAL": 4, "INSUFFICIENT": 3}
    assert "not statistically significant at α=0.05" in out["limitation_template"]
