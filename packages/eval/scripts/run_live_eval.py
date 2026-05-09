from __future__ import annotations

import argparse
import importlib.util
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


if __name__ == "__main__":
    parse_args()
