from __future__ import annotations

from pathlib import Path
from typing import Any


_NODES = ["miner", "critic", "judge", "validator", "finalizer"]


def frozen_contract() -> list[dict[str, str]]:
    contract: list[dict[str, str]] = [
        {"key_path": "run_cost_usd.api_total", "type_name": "float"},
        {"key_path": "run_cost_usd.infra_total", "type_name": "float"},
        {"key_path": "run_cost_usd.grand_total", "type_name": "float"},
        {"key_path": "node_latency_ms.miner", "type_name": "int"},
        {"key_path": "node_latency_ms.critic", "type_name": "int"},
        {"key_path": "node_latency_ms.judge", "type_name": "int"},
        {"key_path": "node_latency_ms.validator", "type_name": "int"},
        {"key_path": "node_latency_ms.finalizer", "type_name": "int"},
        {"key_path": "miner_step_latency_ms.bm25", "type_name": "int"},
        {"key_path": "miner_step_latency_ms.vector", "type_name": "int"},
        {"key_path": "miner_step_latency_ms.rrf", "type_name": "int"},
        {"key_path": "miner_step_latency_ms.rerank", "type_name": "int"},
        {"key_path": "miner_step_latency_ms.total", "type_name": "int"},
    ]
    for node in _NODES:
        contract.append({"key_path": f"node_cost_usd.{node}.api", "type_name": "float"})
        contract.append({"key_path": f"node_cost_usd.{node}.infra", "type_name": "float"})
        contract.append({"key_path": f"node_cost_usd.{node}.total", "type_name": "float"})
    return contract


def _resolve_key_path(payload: dict[str, Any], key_path: str) -> tuple[bool, Any]:
    cur: Any = payload
    for key in key_path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _is_expected_type(value: Any, type_name: str) -> bool:
    if type_name == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "str":
        return isinstance(value, str)
    if type_name == "bool":
        return isinstance(value, bool)
    if type_name == "dict":
        return isinstance(value, dict)
    if type_name == "list":
        return isinstance(value, list)
    return False


def validate_contract(payload: dict[str, Any], contract: list[dict[str, str]]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for rule in contract:
        key_path = rule["key_path"]
        expected = rule["type_name"]
        found, value = _resolve_key_path(payload, key_path)
        if not found:
            errors.append(
                {
                    "key_path": key_path,
                    "error_kind": "missing_key",
                    "expected": expected,
                    "actual": None,
                }
            )
            continue
        if not _is_expected_type(value, expected):
            errors.append(
                {
                    "key_path": key_path,
                    "error_kind": "type_mismatch",
                    "expected": expected,
                    "actual": type(value).__name__,
                }
            )
    return errors
