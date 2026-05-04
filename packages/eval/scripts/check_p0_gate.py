"""Evaluate frozen P0 gates from the comparison artifact."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


W15_WARNING = "W-15: vector index deferred to P1 — retrieval quality may be degraded"
GATE_THRESHOLDS: dict[str, tuple[str, Any]] = {
    "evidence_validity": (">=", 0.95),
    "schema_validity": ("==", 1.0),
    "trace_completeness": ("==", 1.0),
    "should_refuse_hit_rate": (">=", 2 / 3),
    "cost_latency_reported": ("==", True),
}


@dataclass(frozen=True)
class GateResult:
    rows: list[dict[str, Any]]
    exit_code: int
    warning: str | None


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    if operator == ">=":
        return float(actual) >= float(expected)
    if operator == "==":
        return actual == expected
    raise ValueError(f"unsupported operator: {operator}")


def evaluate_gates(payload: dict[str, Any]) -> GateResult:
    if payload.get("schema_version") != "1.0":
        return GateResult(rows=[], exit_code=2, warning=None)

    gates = payload.get("gates")
    if not isinstance(gates, dict):
        return GateResult(rows=[], exit_code=2, warning=None)

    rows: list[dict[str, Any]] = []
    for gate_name, (operator, expected) in GATE_THRESHOLDS.items():
        if gate_name not in gates:
            return GateResult(rows=[], exit_code=2, warning=None)
        actual = gates[gate_name]
        rows.append(
            {
                "gate": gate_name,
                "actual": actual,
                "expected": expected,
                "operator": operator,
                "passed": _compare(actual, operator, expected),
            }
        )

    header = payload.get("header", {})
    warning = None
    if header.get("lancedb_dir_sha256") == "DEFERRED_P1":
        warning = W15_WARNING

    exit_code = 0 if all(row["passed"] for row in rows) else 1
    return GateResult(rows=rows, exit_code=exit_code, warning=warning)


def _format_table(result: GateResult) -> str:
    lines = [
        "| Gate | Actual | Target | Result |",
        "|---|---:|---:|---|",
    ]
    for row in result.rows:
        target = f"{row['operator']} {row['expected']}"
        verdict = "PASS" if row["passed"] else "FAIL"
        lines.append(f"| {row['gate']} | {row['actual']} | {target} | {verdict} |")
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the frozen P0 comparison report against gate thresholds.")
    parser.add_argument("comparison_report", help="Path to the frozen comparison JSON artifact.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report_path = Path(args.comparison_report)
    try:
        payload = json.loads(report_path.read_text())
    except Exception as exc:
        print(f"SCRIPT_ERROR: unable to parse {report_path}: {exc}")
        return 2

    result = evaluate_gates(payload)
    if result.exit_code == 2:
        print("SCRIPT_ERROR: invalid comparison payload")
        return 2

    print(f"comparison_report={report_path}")
    if result.warning:
        print(f"WARNING: {result.warning}")
    print(_format_table(result))
    print(f"OVERALL={'GREEN' if result.exit_code == 0 else 'RED'}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
