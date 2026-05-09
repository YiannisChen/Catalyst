import importlib.util

SCRIPT_PATH = "/Users/yiannischen/Desktop/Catalyst/packages/eval/scripts/run_live_eval.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cost_cap_policy_strict_gt_boundaries_and_fields():
    mod = load_script_module(SCRIPT_PATH, "run_live_eval")

    class P:
        def __init__(self, costs):
            self.costs = costs
            self.i = 0

        def run_case(self, case, llm_seed):
            c = self.costs[self.i]
            self.i += 1
            return {"output_status": "INSUFFICIENT", "api_cost_usd": c}

    cases = [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]

    equal_out = mod.run_live_cases(cases, P([1.0, 1.0, 0.0]), llm_seed=1, stats_seed=2, cost_cap_usd=2.0)
    assert equal_out["cost_cap_policy"] == "strict_gt"
    assert equal_out["cost_cap_triggered"] is False
    assert equal_out["executed_case_count"] == 3

    exceed_out = mod.run_live_cases(cases, P([1.0, 1.01, 0.5]), llm_seed=1, stats_seed=2, cost_cap_usd=2.0)
    assert exceed_out["cost_cap_policy"] == "strict_gt"
    assert exceed_out["cost_cap_triggered"] is True
    assert exceed_out["stopped_after_case_id"] == "c2"
    assert exceed_out["executed_case_count"] == 2


def test_provider_failure_fallback_does_not_crash():
    mod = load_script_module(SCRIPT_PATH, "run_live_eval")

    class BadProvider:
        def run_case(self, case, llm_seed):
            raise RuntimeError("boom")

    out = mod.run_live_cases([{"id": "g013"}], BadProvider(), llm_seed=1, stats_seed=2, cost_cap_usd=10.0)
    assert out["per_case"][0]["error_type"] == "provider_call_failed"
