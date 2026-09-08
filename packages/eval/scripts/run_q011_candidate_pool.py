#!/usr/bin/env python3
"""Q-011 inactive-candidate evidence-pool CLI (pointer-free, pre-approval).

Usage:
  run_q011_candidate_pool.py preflight [options]
  run_q011_candidate_pool.py execute  [options]

Generates one identity-bound evidence pool per c01..c12 from the unsigned
Q-011 packet against an INACTIVE candidate dense generation. Outputs are
non-authoritative; no GoldenCase files are published and no active pointer is
touched. ``execute`` requires explicit active-generation protection
(``--active-lancedb-dir`` and ``--active-generation-pointer``). production_pinned
execute is cloud-only (CUDA + pinned offline BGE models) and is never run
locally.
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

from catalyst_eval.v1_1.candidate_pool import (  # noqa: E402
    CandidatePoolError,
    build_production_retriever,
    load_candidate_identity,
    preflight_candidate_pool,
    run_candidate_pools,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--packet", type=Path, required=True)
        p.add_argument("--derivative", type=Path, required=True)
        p.add_argument("--build-id", required=True)
        p.add_argument("--corpus-manifest-id", required=True)
        p.add_argument("--source-bundle-id", required=True)
        p.add_argument("--snapshot-id", required=True)
        p.add_argument("--probe-report-id", required=True)
        p.add_argument("--postbuild-readiness-id", required=True)
        p.add_argument("--lancedb-dir", type=Path, required=True)
        p.add_argument("--index-manifest-id", default=None)
        p.add_argument("--table-name", default=None)
        p.add_argument("--run-id", default="q011-candidate-pool")
        p.add_argument("--reranker-timeout", type=float, default=2.0)
        p.add_argument(
            "--embedding-mode", default="mock_unit_test",
            choices=("production_pinned", "mock_unit_test"),
        )

    pre = sub.add_parser("preflight", help="read-only identity/path validation")
    _common(pre)
    pre.add_argument("--output-dir", type=Path, default=None)
    pre.add_argument("--active-lancedb-dir", type=Path, default=None)
    pre.add_argument("--active-generation-pointer", type=Path, default=None)
    exe = sub.add_parser("execute", help="run the inactive candidate pool")
    _common(exe)
    # Mandatory active-generation protection on the mutating command (B1).
    exe.add_argument("--output-dir", type=Path, required=True)
    exe.add_argument("--active-lancedb-dir", type=Path, required=True)
    exe.add_argument("--active-generation-pointer", type=Path, required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    retriever_factory=None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        identity = load_candidate_identity(
            run_id=args.run_id,
            packet_path=args.packet,
            derivative=args.derivative,
            build_id=args.build_id,
            corpus_manifest_id=args.corpus_manifest_id,
            source_bundle_id=args.source_bundle_id,
            snapshot_id=args.snapshot_id,
            probe_report_id=args.probe_report_id,
            postbuild_readiness_id=args.postbuild_readiness_id,
            lancedb_dir=args.lancedb_dir,
            index_manifest_id=args.index_manifest_id,
            table_name=args.table_name,
        )
        if args.mode == "preflight":
            result = preflight_candidate_pool(
                identity=identity,
                packet_path=args.packet,
                active_lancedb_dir=args.active_lancedb_dir,
                active_generation_pointer=args.active_generation_pointer,
            )
        else:
            if args.output_dir is None:
                raise CandidatePoolError("execute requires --output-dir")
            if args.embedding_mode == "mock_unit_test":
                if retriever_factory is None:
                    raise CandidatePoolError(
                        "mock_unit_test requires a test-injected retriever; "
                        "cloud runs must use production_pinned"
                    )
                retrieve_case = retriever_factory()
                close = getattr(retrieve_case, "close", None)
            else:
                retrieve_case, close = build_production_retriever(
                    derivative=args.derivative,
                    lancedb_dir=args.lancedb_dir,
                    table_name=identity.table_name,
                    index_manifest_id=identity.index_manifest_id,
                    corpus_manifest_id=identity.corpus_manifest_id,
                    build_id=identity.build_id,
                    reranker_timeout=args.reranker_timeout,
                )
            result = run_candidate_pools(
                identity=identity,
                packet_path=args.packet,
                output_dir=args.output_dir,
                retrieve_case=retrieve_case,
                active_lancedb_dir=args.active_lancedb_dir,
                active_generation_pointer=args.active_generation_pointer,
                embedding_mode=args.embedding_mode,
                retriever_close=close if close is not None else None,
                reranker_timeout_seconds=args.reranker_timeout,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
