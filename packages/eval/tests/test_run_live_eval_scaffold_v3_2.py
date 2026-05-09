import importlib.util

SCRIPT_PATH = "/Users/yiannischen/Desktop/Catalyst/packages/eval/scripts/run_live_eval.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_scaffold_exposes_parse_args_and_run_entry():
    mod = load_script_module(SCRIPT_PATH, "run_live_eval")
    args = mod.parse_args(
        [
            "--golden-set",
            "/Users/yiannischen/Desktop/Catalyst/packages/eval/golden_set/v1_2_p1_set.jsonl",
            "--llm-seed",
            "11",
            "--stats-seed",
            "22",
            "--cost-cap-usd",
            "1.5",
        ]
    )
    assert args.llm_seed == 11
    assert args.stats_seed == 22
    assert hasattr(mod, "run_live_cases")
