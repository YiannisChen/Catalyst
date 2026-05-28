from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live eval")
    parser.add_argument("--golden-set", required=True)
    parser.add_argument("--llm-seed", type=int, required=True)
    parser.add_argument("--stats-seed", type=int, required=True)
    parser.add_argument("--cost-cap-usd", type=float, required=True)
    return parser.parse_args(argv)


def run_live_cases(*args: Any, **kwargs: Any) -> dict[str, Any]:
    cases = args[0]
    provider = args[1]
    llm_seed = kwargs.get("llm_seed")
    stats_seed = kwargs.get("stats_seed")
    cost_cap_usd = float(kwargs.get("cost_cap_usd"))

    per_case: list[dict[str, Any]] = []
    cumulative_cost = 0.0
    cost_cap_triggered = False
    stopped_after_case_id: str | None = None
    cost_cap_policy = "strict_gt"

    for case in cases:
        case_id = case["id"]
        try:
            result = provider.run_case(case, llm_seed)
        except Exception as exc:  # pragma: no cover - exercised in tests
            result = {
                "output_status": "INSUFFICIENT",
                "should_refuse": True,
                "error_type": "provider_call_failed",
                "error_message": str(exc),
                "api_cost_usd": 0.0,
            }

        case_cost = float(result.get("api_cost_usd", 0.0))
        cumulative_cost += case_cost
        per_case.append({"case_id": case_id, **result})

        # strict_gt means exactly equal to cap is still allowed.
        if cumulative_cost > cost_cap_usd:
            cost_cap_triggered = True
            stopped_after_case_id = case_id
            break

    return {
        "llm_seed": llm_seed,
        "stats_seed": stats_seed,
        "cost_cap_policy": cost_cap_policy,
        "cost_cap_triggered": cost_cap_triggered,
        "stopped_after_case_id": stopped_after_case_id,
        "executed_case_count": len(per_case),
        "per_case": per_case,
    }


def validate_live_payload_schema(payload: dict[str, Any]) -> list[dict[str, Any]]:
    contract_path = (
        Path(__file__).resolve().parents[1]
        / "catalyst_eval"
        / "reports"
        / "schema_contract.py"
    )
    spec = importlib.util.spec_from_file_location("schema_contract", str(contract_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.validate_contract(payload, mod.frozen_contract())


def build_cost_latency_payload(
    *,
    node_latency_ms: dict[str, int],
    miner_step_latency_ms: dict[str, int],
    node_api_cost_usd: dict[str, float],
    gpu_hourly_usd: float,
    run_wall_time_sec: float,
) -> dict[str, Any]:
    nodes = ["miner", "critic", "judge", "validator", "finalizer"]
    infra_total = float(gpu_hourly_usd) * (float(run_wall_time_sec) / 3600.0)
    sum_node_latency_ms = float(sum(float(node_latency_ms.get(n, 0)) for n in nodes))
    infra_allocation_warning = sum_node_latency_ms == 0.0

    node_cost_usd: dict[str, dict[str, float]] = {}
    for node in nodes:
        api = float(node_api_cost_usd.get(node, 0.0))
        if infra_allocation_warning:
            infra = 0.0
        else:
            infra = infra_total * float(node_latency_ms.get(node, 0)) / sum_node_latency_ms
        node_cost_usd[node] = {"api": api, "infra": infra, "total": api + infra}

    api_total = float(sum(v["api"] for v in node_cost_usd.values()))
    return {
        "node_latency_ms": {n: int(node_latency_ms.get(n, 0)) for n in nodes},
        "miner_step_latency_ms": {
            "bm25": int(miner_step_latency_ms.get("bm25", 0)),
            "vector": int(miner_step_latency_ms.get("vector", 0)),
            "rrf": int(miner_step_latency_ms.get("rrf", 0)),
            "rerank": int(miner_step_latency_ms.get("rerank", 0)),
            "total": int(miner_step_latency_ms.get("total", 0)),
        },
        "node_cost_usd": node_cost_usd,
        "run_cost_usd": {
            "api_total": api_total,
            "infra_total": infra_total,
            "grand_total": api_total + infra_total,
        },
        "infra_allocation_warning": infra_allocation_warning,
    }


def resolve_langsmith_header(env: dict[str, Any] | None = None) -> dict[str, Any]:
    active_env = env if env is not None else os.environ
    enabled = str(active_env.get("LANGCHAIN_TRACING_V2", "false")).lower() == "true"
    project = active_env.get("LANGCHAIN_PROJECT") if enabled else None
    return {"langsmith_enabled": enabled, "langsmith_project": project}


if __name__ == "__main__":
    parse_args()
