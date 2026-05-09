import importlib.util

SCRIPT_PATH = "/Users/yiannischen/Desktop/Catalyst/packages/eval/scripts/run_live_eval.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cost_latency_payload_contract():
    mod = load_script_module(SCRIPT_PATH, "run_live_eval")
    out = mod.build_cost_latency_payload(
        node_latency_ms={"miner": 12, "critic": 14, "judge": 16, "validator": 10, "finalizer": 4},
        miner_step_latency_ms={"bm25": 2, "vector": 4, "rrf": 2, "rerank": 4, "total": 12},
        node_api_cost_usd={"miner": 0.0, "critic": 0.01, "judge": 0.02, "validator": 0.01, "finalizer": 0.0},
        gpu_hourly_usd=1.5,
        run_wall_time_sec=2.5,
    )
    assert set(out["node_latency_ms"].keys()) == {"miner", "critic", "judge", "validator", "finalizer"}
    assert set(out["miner_step_latency_ms"].keys()) == {"bm25", "vector", "rrf", "rerank", "total"}
    assert set(out["run_cost_usd"].keys()) == {"api_total", "infra_total", "grand_total"}
