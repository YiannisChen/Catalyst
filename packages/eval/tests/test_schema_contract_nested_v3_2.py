import importlib.util

SCRIPT_PATH = "/Users/yiannischen/Desktop/Catalyst/packages/eval/catalyst_eval/reports/schema_contract.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_nested_key_paths_and_error_kinds_are_enforced():
    mod = load_script_module(SCRIPT_PATH, "schema_contract")
    contract = mod.frozen_contract()
    key_paths = {c["key_path"] for c in contract}

    required = {
        "run_cost_usd.api_total",
        "run_cost_usd.infra_total",
        "run_cost_usd.grand_total",
        "node_latency_ms.miner",
        "node_latency_ms.critic",
        "node_latency_ms.judge",
        "node_latency_ms.validator",
        "node_latency_ms.finalizer",
        "miner_step_latency_ms.bm25",
        "miner_step_latency_ms.vector",
        "miner_step_latency_ms.rrf",
        "miner_step_latency_ms.rerank",
        "miner_step_latency_ms.total",
        "node_cost_usd.miner.api",
        "node_cost_usd.miner.infra",
        "node_cost_usd.miner.total",
        "node_cost_usd.critic.api",
        "node_cost_usd.critic.infra",
        "node_cost_usd.critic.total",
        "node_cost_usd.judge.api",
        "node_cost_usd.judge.infra",
        "node_cost_usd.judge.total",
        "node_cost_usd.validator.api",
        "node_cost_usd.validator.infra",
        "node_cost_usd.validator.total",
        "node_cost_usd.finalizer.api",
        "node_cost_usd.finalizer.infra",
        "node_cost_usd.finalizer.total",
    }
    assert required.issubset(key_paths)

    bad_payload = {"run_cost_usd": {"api_total": "1.0"}}
    errors = mod.validate_contract(bad_payload, contract)
    assert any(e["error_kind"] == "missing_key" for e in errors)
    assert any(e["error_kind"] == "type_mismatch" for e in errors)
