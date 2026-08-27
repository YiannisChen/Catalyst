#!/usr/bin/env python3
"""Repo-owned CLI for the M5-10 gates (sealed baseline integrity + semantic
ontology regression). Exit codes: 0 pass (artifact written), 1 hard-gate fail
(no artifact written), 2 usage error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from catalyst_eval.baseline.gates import (
    GateFailure,
    sealed_baseline_integrity_gate,
    semantic_ontology_regression_gate,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GATE_A_OUT = (
    REPO_ROOT / "data/baseline/reports/v1_1_m5_gate_a_sealed_baseline_integrity.json"
)
DEFAULT_GATE_B_OUT = (
    REPO_ROOT / "data/baseline/reports/v1_1_m5_gate_b_semantic_ontology_regression.json"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the M5-10 gates (sealed baseline integrity + semantic "
        "ontology regression)."
    )
    parser.add_argument(
        "--gate",
        choices=("sealed_baseline_integrity", "semantic_ontology_regression", "both"),
        default="both",
        help="Gate to run (default: both, A then B).",
    )
    parser.add_argument(
        "--tag",
        default="v1.1-m1-baseline-corrective-seal",
        help="Sealed baseline tag (Gate A).",
    )
    parser.add_argument(
        "--report",
        default=str(REPO_ROOT / "data/baseline/reports/v1_1_baseline_621375bc_corrective_seal.json"),
        help="Sealed baseline report path (Gate A).",
    )
    parser.add_argument(
        "--golden-dir",
        default=str(REPO_ROOT / "packages/eval/golden_set"),
        help="Golden set directory (Gate B).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output artifact path (single-gate runs).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.gate in ("sealed_baseline_integrity", "both"):
            out = Path(args.out) if args.out else DEFAULT_GATE_A_OUT
            sealed_baseline_integrity_gate(
                repo_root=REPO_ROOT,
                tag=args.tag,
                report_path=Path(args.report),
                out_path=out,
            )
        if args.gate in ("semantic_ontology_regression", "both"):
            out = Path(args.out) if args.out else DEFAULT_GATE_B_OUT
            semantic_ontology_regression_gate(
                repo_root=REPO_ROOT,
                golden_dir=Path(args.golden_dir),
                out_path=out,
            )
    except GateFailure as exc:
        print(f"GATE FAILURE: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        print(f"GATE ERROR: {exc}", file=sys.stderr)
        return 1
    print("M5 gates passed (exit 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
