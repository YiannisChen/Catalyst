#!/usr/bin/env python3
"""Pointer-free candidate dense staging CLI (Q-011, M7).

Wraps ``catalyst_data.index.v1_staging.stage_dense`` through the
pointer-free core in ``catalyst_data.index.candidate_staging_cli``.

Usage:
  stage_candidate_dense.py preflight [options]
  stage_candidate_dense.py execute  [options]

The candidate derivative must be inactive (corpus_manifest.is_current=0) and
the candidate build must be lexical-ready. ``execute`` never calls
``promote_v1_generation`` and never creates/modifies active pointers. An
already-staged matching ``candidate_generation.json`` is returned unchanged
(idempotent resume).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "packages" / "data-core") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packages" / "data-core"))

from catalyst_data.index.candidate_staging_cli import (  # noqa: E402
    CandidateDenseStagingInputs,
    execute_stage_candidate_dense,
    preflight_stage_candidate_dense,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("preflight", "execute"):
        p = sub.add_parser(mode)
        p.add_argument("--derivative", type=Path, required=True)
        p.add_argument("--build-id", required=True)
        p.add_argument("--embedding-artifact", type=Path, required=True)
        p.add_argument("--candidate-manifest-dir", type=Path, required=True)
        p.add_argument("--source-bundle", type=Path, default=None)
        p.add_argument("--active-lancedb-dir", type=Path, default=None)
        p.add_argument("--active-generation-pointer", type=Path, default=None)
        p.add_argument("--expected-index-manifest-id", default=None)
        p.add_argument("--code-revision", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    inputs = CandidateDenseStagingInputs(
        derivative=args.derivative,
        build_id=args.build_id,
        embedding_artifact_dir=args.embedding_artifact,
        candidate_manifest_dir=args.candidate_manifest_dir,
        source_bundle=args.source_bundle,
        active_lancedb_dir=args.active_lancedb_dir,
        active_generation_pointer=args.active_generation_pointer,
        expected_index_manifest_id=args.expected_index_manifest_id,
        code_revision=args.code_revision,
    )
    if args.mode == "preflight":
        result = preflight_stage_candidate_dense(inputs)
    elif args.mode == "execute":
        result = execute_stage_candidate_dense(inputs)
    else:
        raise SystemExit(f"unknown mode: {args.mode}")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
