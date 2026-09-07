#!/usr/bin/env python3
"""Q-011 inactive-candidate evidence-pool CLI (pointer-free, pre-approval).

Usage:
  run_q011_candidate_pool.py preflight [options]
  run_q011_candidate_pool.py execute  [options]

Generates one identity-bound evidence pool per c01..c12 from the unsigned
Q-011 packet against an INACTIVE candidate dense generation. Outputs are
non-authoritative; no GoldenCase files are published and no active pointer is
touched. production_pinned execute is cloud-only (CUDA + pinned BGE models)
and is never run locally.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
for package in ("data-core", "eval"):
    path = str(REPO_ROOT / "packages" / package)
    if path not in sys.path:
        sys.path.insert(0, path)

from catalyst_data.config import BGE_M3_MODEL, BGE_RERANKER_MODEL  # noqa: E402
from catalyst_eval.v1_1.candidate_pool import (  # noqa: E402
    CandidatePoolError,
    load_candidate_identity,
    preflight_candidate_pool,
    run_candidate_pools,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("preflight", "execute"):
        p = sub.add_parser(mode)
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
        p.add_argument("--output-dir", type=Path, default=None)
        p.add_argument("--run-id", default="q011-candidate-pool")
        p.add_argument("--active-lancedb-dir", type=Path, default=None)
        p.add_argument("--active-generation-pointer", type=Path, default=None)
        p.add_argument("--reranker-timeout", type=float, default=2.0)
        p.add_argument(
            "--embedding-mode", default="production_pinned",
            choices=("production_pinned", "mock_unit_test"),
        )
    return parser


def _retriever_factory_from_args(args: argparse.Namespace) -> Callable[[Any], Any]:
    """Build the production retrieve_case callable over the inactive candidate."""
    import lancedb
    import torch

    from catalyst_agents.runtime.query_embedding import ProductionBgeM3QueryEmbeddingFactory
    from catalyst_data.retrieval.hybrid import retrieve_hybrid as _retrieve_hybrid
    from catalyst_data.storage.lancedb_store import load_reranker

    if not torch.cuda.is_available():
        raise CandidatePoolError("production_pinned requires CUDA (cloud-only)")
    db_uri = f"{args.derivative.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(db_uri, uri=True)
    table = lancedb.connect(str(args.lancedb_dir)).open_table(args.table_name)
    factory = ProductionBgeM3QueryEmbeddingFactory()
    embedder = factory.create(model_name=BGE_M3_MODEL)
    reranker = load_reranker(model_name=BGE_RERANKER_MODEL)
    if reranker is None:
        raise CandidatePoolError("production reranker could not be loaded")

    def retrieve_case(case):
        try:
            return _retrieve_hybrid(
                conn,
                query=case.question,
                ticker=case.ticker,
                cutoff=case.cutoff,
                mode="reranked",
                query_embedding=embedder.embed_query(case.question),
                requested_manifest_id=args.corpus_manifest_id,
                index_manifest_id=args.index_manifest_id,
                lancedb_table=table,
                reranker=reranker,
                reranker_timeout_seconds=args.reranker_timeout,
                return_v1=False,
            )
        except Exception:
            conn.close()
            raise

    return retrieve_case


def main(
    argv: list[str] | None = None,
    *,
    retriever_factory: Callable[[Any], Any] | None = None,
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
            else:
                retrieve_case = _retriever_factory_from_args(args)
            result = run_candidate_pools(
                identity=identity,
                packet_path=args.packet,
                output_dir=args.output_dir,
                retrieve_case=retrieve_case,
                active_lancedb_dir=args.active_lancedb_dir,
                active_generation_pointer=args.active_generation_pointer,
                embedding_mode=args.embedding_mode,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
