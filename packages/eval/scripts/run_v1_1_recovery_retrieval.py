#!/usr/bin/env python3
"""M8-B candidate-FTS retrieval-quality gate CLI (pointer-free, read-only).

Usage:
  run_v1_1_recovery_retrieval.py \
      --derivative DB --corpus-manifest-id SHA256 --build-id SHA256 \
      --dataset-manifest manifest.json [--cases cases.jsonl] \
      --output report.json [--frozen-ranked ranked.json]

Ranks every benchmark case against one exact INACTIVE candidate build and
scores the frozen ranking with the sealed M7 retrieval metrics. It never
promotes a generation, never touches active serving state, and never sends
expected evidence (or any gold field) into retrieval. Exit code 0 means every
M8-B gate passed; the canonical report is written either way.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
for package in ("data-core", "eval"):
    path = str(REPO_ROOT / "packages" / package)
    if path not in sys.path:
        sys.path.insert(0, path)

from catalyst_eval.v1_1.loader import (  # noqa: E402
    load_benchmark_cases,
    resolve_benchmark_cases_path,
)
from catalyst_eval.v1_1.recovery_retrieval import (  # noqa: E402
    CANDIDATE_DEPTH_FLOOR,
    RecoveryRetrievalError,
    run_candidate_fts_retrieval,
)


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--derivative", type=Path, required=True)
    parser.add_argument("--corpus-manifest-id", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frozen-ranked", type=Path, default=None)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--candidate-depth", type=int, default=CANDIDATE_DEPTH_FLOOR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = json.loads(Path(args.dataset_manifest).read_text(encoding="utf-8"))
    cases_path = (
        Path(args.cases)
        if args.cases is not None
        else resolve_benchmark_cases_path(manifest, args.dataset_manifest)
    )
    try:
        cases = load_benchmark_cases(cases_path, manifest=manifest)
    except Exception as exc:  # contract error: fail closed
        print(f"benchmark case load failed: {exc}", file=sys.stderr)
        return 2
    try:
        report = run_candidate_fts_retrieval(
            db_path=args.derivative,
            corpus_manifest_id=args.corpus_manifest_id,
            build_id=args.build_id,
            cases=cases,
            top_k=args.top_k,
            candidate_depth=args.candidate_depth,
            frozen_ranked_path=args.frozen_ranked,
        )
    except RecoveryRetrievalError as exc:
        print(f"recovery retrieval failed: {exc}", file=sys.stderr)
        return 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_canonical_bytes(report.as_dict()))
    print(
        _canonical_bytes(
            {
                "ok": report.gate_passed,
                "build_id": report.build_id,
                "gate_status": dict(sorted(report.gate_status.items())),
                "case_count": len(report.cases),
            }
        ).decode("utf-8")
    )
    return 0 if report.gate_passed else 1


if __name__ == "__main__":
    sys.exit(main())
