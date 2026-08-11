"""T4 preparation: build smoke case pack, run served-corpus probe, persist evidence.

AMEND-2 P5: the prepare script rejects an existing final run ID, writes all
identity/probe/case-pack/meta/token inside a unique sibling staging directory,
reloads and validates the evidence with ``validate_t4_evidence``, then
atomically renames to the final run directory. Any failure leaves no final
directory and cleans up only its own staging directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from catalyst_eval.post_import.case_pack import (
    build_smoke_case_pack,
    compute_case_pack_id,
    write_case_pack,
)
from catalyst_eval.post_import.index_identity import (
    ApprovedFrozenIdentities,
    resolve_runtime_identity,
)
from catalyst_eval.post_import.probe import run_served_corpus_probe, write_probe_evidence
from catalyst_eval.post_import.t4_evidence import validate_t4_evidence

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
DEFAULT_LANCEDB_DIR = REPO_ROOT / "data" / "lancedb_gold" / "b6g_8ffae891b4e1"
DEFAULT_INDEX_MANIFEST = REPO_ROOT / "data" / "embeddings" / "b6g_import_bb43ebe" / "index_manifest.json"
GOLDEN_DIR = REPO_ROOT / "packages" / "eval" / "golden_set"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", default="data/run_reports/post_import", type=Path)
    parser.add_argument("--lancedb-dir", default=DEFAULT_LANCEDB_DIR, type=Path)
    parser.add_argument("--index-manifest", default=DEFAULT_INDEX_MANIFEST, type=Path)
    parser.add_argument("--embedding-mode", default="mock_unit_test",
                        choices=["production_pinned", "mock_unit_test"])
    return parser.parse_args(argv)


def _open_db_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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

    output_root = Path(args.output_root)
    final_dir = output_root / args.run_id
    if final_dir.exists():
        print(json.dumps({"ok": False, "error": f"run_id already exists: {args.run_id}"},
                         sort_keys=True))
        return 2

    staging_dir = output_root / f".{args.run_id}.staging-{uuid.uuid4().hex[:12]}"
    staging_dir.mkdir(parents=True, exist_ok=False)
    try:
        cases = build_smoke_case_pack(GOLDEN_DIR)
        case_pack_id = compute_case_pack_id(cases)
        case_pack_path = staging_dir / "case_pack.jsonl"
        write_case_pack(cases, case_pack_path)

        resolved = resolve_runtime_identity(
            lancedb_dir=args.lancedb_dir,
            index_manifest_path=args.index_manifest,
            db_path=db_path,
            repo_root=REPO_ROOT,
            expected=APPROVED,
            require_clean=True,
        )

        conn = _open_db_readonly(db_path)
        try:
            report = run_served_corpus_probe(
                conn, corpus_manifest_id=resolved.corpus_manifest_id, cases=cases,
            )
        finally:
            conn.close()

        if not report.all_passed:
            print(json.dumps({
                "ok": False,
                "case_pack_id": case_pack_id,
                "passed": report.passed_count,
                "total": report.case_count,
                "per_case": [r.to_dict() for r in report.per_case],
            }, sort_keys=True))
            shutil.rmtree(staging_dir, ignore_errors=True)
            return 2

        write_probe_evidence(
            report,
            run_dir=staging_dir,
            db_sha256=resolved.db_sha256,
            corpus_manifest_id=resolved.corpus_manifest_id,
            case_pack_id=case_pack_id,
            case_pack_path="case_pack.jsonl",
            runtime_git_head=resolved.git_head,
            index_build_code_revision=resolved.code_revision,
            snapshot_id=resolved.snapshot_id,
            source_bundle_id=resolved.source_bundle_id,
            probe_report_id=resolved.probe_report_id,
            postbuild_readiness_id=resolved.postbuild_readiness_id,
            index_manifest_id=resolved.index_manifest_id,
            db_path=str(resolved.db_path),
            db_user_version=resolved.db_user_version,
            db_foreign_key_violations=resolved.db_foreign_key_violations,
            lancedb_dir=str(resolved.lancedb_dir),
            active_table_name=resolved.active_table_name,
            model_name=resolved.model_name,
            model_revision=resolved.model_revision,
            tokenizer_revision=resolved.tokenizer_revision,
            dimension=resolved.dimension,
            dtype=resolved.dtype,
            normalization_mode=resolved.normalization_mode,
            embedding_mode=args.embedding_mode,
        )

        # Reload + validate the evidence before atomic rename.
        validate_t4_evidence(
            evidence_dir=staging_dir,
            current_case_pack=cases,
            resolved=resolved,
        )
        os.replace(staging_dir, final_dir)
    except Exception as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2

    print(json.dumps({
        "ok": True,
        "run_id": args.run_id,
        "case_pack_id": case_pack_id,
        "case_pack_path": str(final_dir / "case_pack.jsonl"),
        "nn_result": f"{report.passed_count}/{report.case_count}",
        "per_case": [r.to_dict() for r in report.per_case],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
