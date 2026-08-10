"""Production four-arm post-import runner (the ONLY permitted entry point).

AMEND-2 P2/P3: runtime identity is bound to the approved frozen identities
(``ApprovedFrozenIdentities``) via ``resolve_runtime_identity`` before any model
load / embedding / retrieval / artifact write. ``--active-table`` is removed:
the validated active_generation.json pointer is the only table source.

AMEND-2 P1/P4: the CLI validates T4 evidence through ``validate_t4_evidence``
(returning ``ValidatedT4Evidence``), required for production_pinned. The
success token gate holds both validated objects and rejects caller-forgeable
strings or booleans. ``--limit`` never writes the token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from catalyst_data.config import BGE_M3_MODEL, BGE_RERANKER_MODEL

from catalyst_eval.post_import.case_pack import compute_case_pack_id, load_case_pack
from catalyst_eval.post_import.four_arm import (
    EmbeddingBoundary,
    RunIdentities,
    run_four_arm_cases,
)
from catalyst_eval.post_import.index_identity import (
    ApprovedFrozenIdentities,
    resolve_runtime_identity,
)
from catalyst_eval.post_import.t4_evidence import (
    validate_case_pack_against_contract,
    validate_t4_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

APPROVED = ApprovedFrozenIdentities(
    snapshot_id="7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
    corpus_manifest_id="3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
    source_bundle_id="8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
    probe_report_id="25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
    postbuild_readiness_id="9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
    index_manifest_id="c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083",
    code_revision="bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8",
    model_name="BAAI/bge-m3",
    model_revision="5617a9f61b028005a4858fdac845db406aefb181",
    tokenizer_revision="5617a9f61b028005a4858fdac845db406aefb181",
    dimension=1024,
    dtype="float32",
    normalization_mode="l2",
    table_name="chunks__staging__b3761f4b943542a8",
    vector_count=295506,
    db_sha256="bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40",
    db_user_version=13,
    db_fk_violations=0,
)
FROZEN_USER_VERSION = 13
EXPECTED_SERVED_ROWS = 295506
EXPECTED_ACTIVE_ELIGIBLE = 253984
EXPECTED_METADATA_ONLY = 41522
DEFAULT_INDEX_MANIFEST = REPO_ROOT / "data" / "embeddings" / "b6g_import_bb43ebe" / "index_manifest.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _open_db_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _verify_frozen_db_counts(conn: sqlite3.Connection) -> None:
    served = conn.execute("SELECT COUNT(*) FROM corpus_served_chunks").fetchone()[0]
    if served != EXPECTED_SERVED_ROWS:
        raise RuntimeError(f"corpus_served_chunks count {served} != {EXPECTED_SERVED_ROWS}")
    active = conn.execute(
        "SELECT COUNT(*) FROM corpus_served_chunks WHERE status='active' AND eligibility='eligible'"
    ).fetchone()[0]
    meta_only = conn.execute(
        "SELECT COUNT(*) FROM corpus_served_chunks WHERE status='metadata_only'"
    ).fetchone()[0]
    if active != EXPECTED_ACTIVE_ELIGIBLE or meta_only != EXPECTED_METADATA_ONLY:
        raise RuntimeError(
            f"served split mismatch active={active} metadata_only={meta_only}"
        )


class _MockQueryEmbedder:
    """Deterministic normalized embedder for mock_unit_test runs only."""

    is_mock = True
    dimension = 1024
    model_revision = APPROVED.model_revision

    def __init__(self) -> None:
        import numpy as np
        self._np = np

    def embed_query(self, query: str) -> object:
        seed = sum(ord(ch) for ch in query)
        values = self._np.arange(seed, seed + 1024, dtype=self._np.float32)
        return values / self._np.linalg.norm(values)


class _MockReranker:
    def __init__(self) -> None:
        self.calls = 0

    def score(self, query: str, candidates: list) -> list[float]:
        self.calls += 1
        return [float(100 - i) for i in range(len(candidates))]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--lancedb-dir", required=True, type=Path)
    parser.add_argument("--case-pack", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", default="data/run_reports/post_import", type=Path)
    parser.add_argument("--embedding-mode", required=True,
                        choices=["production_pinned", "mock_unit_test"])
    parser.add_argument("--index-manifest", default=None, type=Path)
    parser.add_argument("--t4-evidence-dir", default=None, type=Path,
                        help="T4 evidence run dir validated into ValidatedT4Evidence")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--reranker-timeout", type=float, default=2.0)
    parser.add_argument("--manager-authorization", default=None, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.db)
    try:
        db_sha = _sha256_file(db_path)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"cannot read frozen DB: {exc}"}, sort_keys=True))
        return 2
    if db_sha != APPROVED.db_sha256:
        print(json.dumps({"ok": False, "error": "frozen DB sha mismatch"}, sort_keys=True))
        return 2

    cases = load_case_pack(args.case_pack)
    case_pack_id = compute_case_pack_id(cases)

    if args.embedding_mode == "production_pinned" and args.t4_evidence_dir is None:
        print(json.dumps({"ok": False, "error": "production_pinned requires --t4-evidence-dir"},
                         sort_keys=True))
        return 2

    conn = _open_db_readonly(db_path)
    try:
        _verify_frozen_db_counts(conn)
        resolved = resolve_runtime_identity(
            lancedb_dir=args.lancedb_dir,
            index_manifest_path=args.index_manifest or DEFAULT_INDEX_MANIFEST,
            db_path=db_path,
            repo_root=REPO_ROOT,
            expected=APPROVED,
        )

        if args.embedding_mode == "production_pinned":
            validate_case_pack_against_contract(cases)

        validated_evidence = None
        if args.t4_evidence_dir is not None:
            validated_evidence = validate_t4_evidence(
                evidence_dir=args.t4_evidence_dir,
                current_case_pack=cases,
                resolved=resolved,
            )

        if args.embedding_mode == "production_pinned":
            from catalyst_agents.runtime.query_embedding import (
                ProductionBgeM3QueryEmbeddingFactory,
            )
            factory = ProductionBgeM3QueryEmbeddingFactory()
            embedder = factory.create(model_name=BGE_M3_MODEL)
            from catalyst_data.storage.lancedb_store import load_reranker
            reranker = load_reranker(model_name=BGE_RERANKER_MODEL)
            if reranker is None:
                raise RuntimeError("production reranker could not be loaded")
            cuda_available = True
            is_mock = False
        else:  # mock_unit_test
            embedder = _MockQueryEmbedder()
            reranker = _MockReranker()
            cuda_available = False
            is_mock = True

        identities = RunIdentities(
            code_revision=resolved.code_revision,
            git_head=resolved.git_head,
            snapshot_id=resolved.snapshot_id,
            corpus_manifest_id=resolved.corpus_manifest_id,
            source_bundle_id=resolved.source_bundle_id,
            probe_report_id=resolved.probe_report_id,
            postbuild_readiness_id=resolved.postbuild_readiness_id,
            index_manifest_id=resolved.index_manifest_id,
            lancedb_dir=str(resolved.lancedb_dir),
            active_table_name=resolved.active_table_name,
            model_name=resolved.model_name,
            model_revision=resolved.model_revision,
            tokenizer_revision=resolved.tokenizer_revision,
            reranker_model=BGE_RERANKER_MODEL,
            reranker_revision="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        )
        boundary = EmbeddingBoundary(
            embedding_mode=args.embedding_mode,
            dimension=embedder.dimension,
            model_revision=embedder.model_revision,
            tokenizer_revision=resolved.tokenizer_revision,
            is_mock=is_mock,
            cuda_available=cuda_available,
        )
        import lancedb

        lancedb_db = lancedb.connect(str(resolved.lancedb_dir))
        table = lancedb_db.open_table(resolved.active_table_name)
        summary = run_four_arm_cases(
            db=conn,
            lancedb_table=table,
            cases=cases,
            run_id=args.run_id,
            output_root=args.output_root,
            identities=identities,
            boundary=boundary,
            query_embedding_fn=embedder.embed_query,
            reranker=reranker,
            reranker_timeout_seconds=args.reranker_timeout,
            limit=args.limit,
            case_pack_id=case_pack_id,
            case_pack_path=str(args.case_pack),
            validated_evidence=validated_evidence,
            validated_runtime_identity=resolved,
            manager_authorization_path=args.manager_authorization,
        )
    except Exception as exc:
        conn.close()
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(json.dumps({
        "ok": True,
        "run_id": summary.run_id,
        "case_count": summary.case_count,
        "arms_written": summary.arms_written,
        "pools_written": summary.pools_written,
        "embedding_mode": summary.embedding_mode,
        "meta_path": str(summary.meta_path),
        "token_written": summary.token_written,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
