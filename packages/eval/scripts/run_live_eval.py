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
    return {"status": "not_implemented_yet"}


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
