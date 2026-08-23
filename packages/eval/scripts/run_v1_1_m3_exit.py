"""M3 production exit / four-arm entry (M3-12A/B).

The only M3 exit/four-arm entry point. Identity is resolved exclusively from
M3 artifacts (promotion journal, active generation, index manifest, preparation
and promotion evidence, DATA-01/Q-005 seals); the M1 APPROVED frozen-DB
constant is never used. Fail closed (no success token, no report) unless every
identity/gate passes. The final report binds ``git_revision`` to
IMPLEMENTATION_HEAD, is explicitly NON-COMPARABLE (Q-001/Q-002 unrecovered,
``promoted_env_recovered=false``), is path-redacted, and is never written on a
failed prerequisite. Production-pinned four-arm retrieval requires CUDA/BGE-M3;
FAST tests seam this call and never touch GPU.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_data.retrieval.index_manifest import IndexManifest

from catalyst_eval.post_import.case_pack import (
    compute_case_pack_id,
    load_case_pack,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
FOUR_ARM_SUCCESS_TOKEN = "FOUR_ARM_E2E_OK"
WAVE_TOKEN_FILENAME = "WAVE_TOKEN.txt"
REPORT_SCHEMA = "v1_1_m3_gate_report_v1"

_SECRET_PATTERNS = (
    re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8,})"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|credential|authorization)[=: ]+[^\s,;}\]]+"),
)


def _git_head(repo: Path = REPO_ROOT) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("git rev-parse HEAD failed")
    return result.stdout.strip()


def _git_status_porcelain(repo: Path = REPO_ROOT) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("git status --porcelain failed")
    return result.stdout


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not Path(path).is_file():
        raise ValueError(f"{label} missing")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} unreadable or malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _require_hex64(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _redact(text: str) -> str:
    redacted = str(text)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = re.sub(r"(?<![\w])(?:/[A-Za-z0-9._~-]+){2,}", "[PATH]", redacted)
    home = str(Path.home())
    if home and home != "/":
        redacted = redacted.replace(home, "[HOME]")
    return redacted


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--expected-db-sha256", required=True)
    parser.add_argument("--expected-user-version", required=True, type=int)
    parser.add_argument("--lancedb-dir", required=True, type=Path)
    parser.add_argument("--index-manifest", required=True, type=Path)
    parser.add_argument("--active-generation", required=True, type=Path)
    parser.add_argument("--case-pack", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--implementation-head", required=True)
    parser.add_argument("--promotion-journal", required=True, type=Path)
    parser.add_argument("--preparation-evidence", required=True, type=Path)
    parser.add_argument("--promotion-evidence", required=True, type=Path)
    parser.add_argument("--benchmark-manifest", required=True, type=Path)
    parser.add_argument("--q005-approval", required=True, type=Path)
    parser.add_argument("--report-output", required=True, type=Path)
    return parser.parse_args(argv)


def _preflight(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve and cross-validate every M3 identity; fail closed on any gap."""
    if not _HEX40.fullmatch(args.implementation_head):
        raise ValueError("--implementation-head must be 40 lowercase hex")
    if not args.run_id or not args.run_id.strip():
        raise ValueError("--run-id must be non-empty")
    for path, label in (
        (args.db, "--db"),
        (args.lancedb_dir, "--lancedb-dir"),
        (args.index_manifest, "--index-manifest"),
        (args.active_generation, "--active-generation"),
        (args.case_pack, "--case-pack"),
        (args.promotion_journal, "--promotion-journal"),
        (args.preparation_evidence, "--preparation-evidence"),
        (args.promotion_evidence, "--promotion-evidence"),
        (args.benchmark_manifest, "--benchmark-manifest"),
        (args.q005_approval, "--q005-approval"),
        (args.output_root, "--output-root"),
        (args.report_output, "--report-output"),
    ):
        if not Path(path).is_absolute():
            raise ValueError(f"{label} must be an absolute path")
    if not Path(args.db).is_file():
        raise ValueError("--db missing")
    if not Path(args.lancedb_dir).is_dir():
        raise ValueError("--lancedb-dir missing")
    if not Path(args.report_output).parent.is_dir():
        raise ValueError("--report-output parent directory missing")

    run_dir = Path(args.output_root) / args.run_id
    if run_dir.exists():
        raise ValueError("--run-id already exists; stale or reused run rejected")

    # Git HEAD binding.
    if _git_head() != args.implementation_head:
        raise ValueError("--implementation-head does not match git HEAD")
    if _git_status_porcelain():
        raise ValueError("worktree is not clean")

    # Derivative DB identity: explicit expected values, never M1 constants.
    db_sha = _sha256_file(args.db)
    if db_sha != args.expected_db_sha256:
        raise ValueError("derivative DB sha256 mismatch")
    uri = f"{Path(args.db).resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    if user_version != args.expected_user_version:
        raise ValueError(
            f"derivative DB user_version {user_version} != expected {args.expected_user_version}"
        )
    if args.expected_user_version != 14:
        raise ValueError("--expected-user-version must be 14")

    # Promotion journal: COMMITTED and identity-complete.
    journal = _read_json(args.promotion_journal, "promotion journal")
    if journal.get("schema_version") != "m3_promotion_journal_v2":
        raise ValueError("promotion journal schema mismatch")
    if journal.get("state") != "COMMITTED":
        raise ValueError("promotion journal is not COMMITTED")
    build_id = _require_hex64(journal.get("build_id"), "journal build_id")
    journal_corpus = _require_hex64(
        journal.get("corpus_manifest_id"), "journal corpus_manifest_id"
    )
    lexical_generation_id = _require_hex64(
        journal.get("lexical_generation_id"), "journal lexical_generation_id"
    )
    lexical_digest = _require_hex64(
        journal.get("lexical_digest"), "journal lexical_digest"
    )
    dense_index_manifest_id = _require_hex64(
        journal.get("dense_index_manifest_id"), "journal dense_index_manifest_id"
    )
    if lexical_generation_id != build_id:
        raise ValueError("journal lexical_generation_id must equal build_id")

    # Active generation pointer.
    active = _read_json(args.active_generation, "active_generation")
    if active.get("schema_version") != "active_generation_v1":
        raise ValueError("active_generation schema mismatch")
    active_index = _require_hex64(
        active.get("index_manifest_id"), "active index_manifest_id"
    )
    active_corpus = _require_hex64(
        active.get("corpus_manifest_id"), "active corpus_manifest_id"
    )
    active_source = _require_hex64(
        active.get("source_bundle_id"), "active source_bundle_id"
    )
    active_snapshot = _require_hex64(
        active.get("snapshot_id"), "active snapshot_id"
    )
    active_table = active.get("table_name")
    active_chunk_count = active.get("chunk_count")
    if not isinstance(active_table, str) or not active_table:
        raise ValueError("active_generation table_name missing")
    if not isinstance(active_chunk_count, int) or active_chunk_count <= 0:
        raise ValueError("active_generation chunk_count invalid")

    # Index manifest via the production contract.
    manifest_raw = _read_json(args.index_manifest, "index_manifest")
    manifest = IndexManifest.from_dict(manifest_raw)
    if manifest.index_manifest_id != active_index:
        raise ValueError("index_manifest index_manifest_id mismatch")
    if manifest.corpus_manifest_id != active_corpus:
        raise ValueError("index_manifest corpus_manifest_id mismatch")
    if manifest.source_bundle_id != active_source:
        raise ValueError("index_manifest source_bundle_id mismatch")
    if manifest.snapshot_id != active_snapshot:
        raise ValueError("index_manifest snapshot_id mismatch")
    if manifest.vector_count != active_chunk_count:
        raise ValueError("index_manifest vector_count mismatch")
    if manifest.code_revision != args.implementation_head:
        raise ValueError("index_manifest code_revision does not match implementation head")

    # Case pack identity.
    cases = load_case_pack(args.case_pack)
    case_pack_id = compute_case_pack_id(cases)

    # Sealed DATA-01/Q-005 operator inputs (seal git_revision is authoritative).
    benchmark = _read_json(args.benchmark_manifest, "benchmark manifest")
    q005 = _read_json(args.q005_approval, "q005 approval")
    seal_revision = benchmark.get("git_revision")
    if not isinstance(seal_revision, str) or _HEX40.fullmatch(seal_revision) is None:
        raise ValueError("benchmark git_revision invalid")
    if q005.get("git_revision") != seal_revision:
        raise ValueError("q005 git_revision does not match benchmark git_revision")
    from catalyst_data.sec.m3_8b_validator import validate_m3_8b_operator_inputs

    validate_m3_8b_operator_inputs(
        args.benchmark_manifest,
        args.q005_approval,
        expected_git_revision=seal_revision,
    )

    # Preparation evidence binds the sealed inputs and the journal build.
    prep = _read_json(args.preparation_evidence, "preparation evidence")
    if prep.get("schema_version") != "preparation_evidence_v1":
        raise ValueError("preparation evidence schema mismatch")
    if prep.get("git_revision") != seal_revision:
        raise ValueError("preparation evidence git_revision mismatch")
    if _require_hex64(prep.get("build_id"), "prep build_id") != build_id:
        raise ValueError("preparation evidence build_id mismatch")
    if _require_hex64(
        prep.get("corpus_manifest_id"), "prep corpus_manifest_id"
    ) != journal_corpus:
        raise ValueError("preparation evidence corpus_manifest_id mismatch")
    if _require_hex64(
        prep.get("source_bundle_id"), "prep source_bundle_id"
    ) != active_source:
        raise ValueError("preparation evidence source_bundle_id mismatch")
    if prep.get("chunk_count") != active_chunk_count:
        raise ValueError("preparation evidence chunk_count mismatch")
    if _require_hex64(
        prep.get("lexical_digest"), "prep lexical_digest"
    ) != lexical_digest:
        raise ValueError("preparation evidence lexical_digest mismatch")
    probe_report_id = _require_hex64(
        prep.get("probe_report_id"), "prep probe_report_id"
    )
    postbuild_readiness_id = _require_hex64(
        prep.get("postbuild_readiness_id"), "prep postbuild_readiness_id"
    )
    data01 = prep.get("data01")
    if not isinstance(data01, dict) or data01.get("gate_passed") is not True:
        raise ValueError("preparation evidence DATA-01 gate not passed")

    # Promotion evidence binds the journal, dense pointer, and GPU report.
    prom = _read_json(args.promotion_evidence, "promotion evidence")
    if prom.get("schema_version") != "promotion_evidence_v1":
        raise ValueError("promotion evidence schema mismatch")
    if prom.get("git_revision") != seal_revision:
        raise ValueError("promotion evidence git_revision mismatch")
    if prom.get("state") != "COMMITTED":
        raise ValueError("promotion evidence state is not COMMITTED")
    if _require_hex64(prom.get("build_id"), "prom build_id") != build_id:
        raise ValueError("promotion evidence build_id mismatch")
    if _require_hex64(
        prom.get("corpus_manifest_id"), "prom corpus_manifest_id"
    ) != journal_corpus:
        raise ValueError("promotion evidence corpus_manifest_id mismatch")
    if _require_hex64(
        prom.get("dense_index_manifest_id"), "prom dense_index_manifest_id"
    ) != dense_index_manifest_id:
        raise ValueError("promotion evidence dense_index_manifest_id mismatch")
    if _require_hex64(
        prom.get("index_manifest_id"), "prom index_manifest_id"
    ) != active_index:
        raise ValueError("promotion evidence index_manifest_id mismatch")
    gpu = prom.get("gpu")
    if not isinstance(gpu, dict):
        raise ValueError("promotion evidence GPU report missing")
    if gpu.get("vector_count") != active_chunk_count:
        raise ValueError("GPU report vector_count mismatch")
    if gpu.get("model") != manifest.model_name:
        raise ValueError("GPU report model mismatch")
    if gpu.get("revision") != manifest.model_revision:
        raise ValueError("GPU report revision mismatch")

    return {
        "build_id": build_id,
        "corpus_manifest_id": journal_corpus,
        "dense_index_manifest_id": dense_index_manifest_id,
        "index_manifest_id": active_index,
        "source_bundle_id": active_source,
        "snapshot_id": active_snapshot,
        "lexical_digest": lexical_digest,
        "active_table_name": active_table,
        "chunk_count": active_chunk_count,
        "case_pack_id": case_pack_id,
        "case_pack_path": str(args.case_pack),
        "probe_report_id": probe_report_id,
        "postbuild_readiness_id": postbuild_readiness_id,
        "seal_revision": seal_revision,
        "data01": {
            "denominator": data01.get("denominator"),
            "numerator": data01.get("numerator"),
            "gate_passed": data01.get("gate_passed"),
        },
        "gpu": {
            "model": manifest.model_name,
            "revision": manifest.model_revision,
            "dimension": manifest.dimension,
        },
    }


def _run_four_arm(args: argparse.Namespace, identities: dict[str, Any]) -> Any:
    """Production-pinned four-arm retrieval on the M3 derivative/new dense gen.

    FAST tests monkeypatch this seam; production requires CUDA + pinned BGE-M3
    and writes ``FOUR_ARM_E2E_OK`` under ``output-root/run-id``.
    """
    from catalyst_agents.runtime.query_embedding import (
        ProductionBgeM3QueryEmbeddingFactory,
    )
    from catalyst_data.config import BGE_M3_MODEL, BGE_RERANKER_MODEL
    from catalyst_data.storage.lancedb_store import load_reranker

    from catalyst_eval.post_import.four_arm import (
        EmbeddingBoundary,
        RunIdentities,
        run_four_arm_cases,
    )

    cases = load_case_pack(args.case_pack)
    case_pack_id = compute_case_pack_id(cases)
    factory = ProductionBgeM3QueryEmbeddingFactory()
    embedder = factory.create(model_name=BGE_M3_MODEL)
    reranker = load_reranker(model_name=BGE_RERANKER_MODEL)
    if reranker is None:
        raise RuntimeError("production reranker could not be loaded")
    import torch

    cuda_available = bool(torch.cuda.is_available())

    run_identities = RunIdentities(
        code_revision=args.implementation_head,
        git_head=args.implementation_head,
        snapshot_id=identities["snapshot_id"],
        corpus_manifest_id=identities["corpus_manifest_id"],
        source_bundle_id=identities["source_bundle_id"],
        probe_report_id=identities["probe_report_id"],
        postbuild_readiness_id=identities["postbuild_readiness_id"],
        index_manifest_id=identities["index_manifest_id"],
        lancedb_dir=str(args.lancedb_dir),
        active_table_name=identities["active_table_name"],
        model_name=identities["gpu"]["model"],
        model_revision=identities["gpu"]["revision"],
        tokenizer_revision=identities["gpu"]["revision"],
        reranker_model=BGE_RERANKER_MODEL,
        reranker_revision="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        dimension=identities["gpu"]["dimension"],
        dtype="float32",
        normalization_mode="l2",
        vector_count=identities["chunk_count"],
        lancedb_row_count=identities["chunk_count"],
        db_path=str(args.db),
        db_sha256=args.expected_db_sha256,
        db_user_version=args.expected_user_version,
        db_foreign_key_violations=0,
    )
    boundary = EmbeddingBoundary(
        embedding_mode="production_pinned",
        dimension=identities["gpu"]["dimension"],
        model_revision=identities["gpu"]["revision"],
        tokenizer_revision=identities["gpu"]["revision"],
        is_mock=False,
        cuda_available=cuda_available,
    )
    import lancedb

    lancedb_db = lancedb.connect(str(args.lancedb_dir))
    table = lancedb_db.open_table(identities["active_table_name"])
    return run_four_arm_cases(
        db=sqlite3.connect(
            f"{Path(args.db).resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        ),
        lancedb_table=table,
        cases=cases,
        run_id=args.run_id,
        output_root=args.output_root,
        identities=run_identities,
        boundary=boundary,
        query_embedding_fn=embedder.embed_query,
        reranker=reranker,
        case_pack_id=case_pack_id,
        case_pack_path=str(args.case_pack),
    )


def _build_report(
    args: argparse.Namespace,
    identities: dict[str, Any],
    summary: Any,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA,
        "git_revision": args.implementation_head,
        "comparable": False,
        "promoted_env_recovered": False,
        "promoted_env_reason": "q_001_q_002_unrecovered",
        "run_id": args.run_id,
        "build_id": identities["build_id"],
        "corpus_manifest_id": identities["corpus_manifest_id"],
        "dense_index_manifest_id": identities["dense_index_manifest_id"],
        "index_manifest_id": identities["index_manifest_id"],
        "source_bundle_id": identities["source_bundle_id"],
        "case_pack_id": identities["case_pack_id"],
        "data01": identities["data01"],
        "four_arm": {
            "success_token": FOUR_ARM_SUCCESS_TOKEN,
            "meta_ref": f"{args.run_id}/meta.json",
        },
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys_argv(argv))
    try:
        identities = _preflight(args)
        summary = _run_four_arm(args, identities)
        run_dir = Path(args.output_root) / args.run_id
        token_path = run_dir / WAVE_TOKEN_FILENAME
        if getattr(summary, "token_written", False) is not True:
            raise ValueError("four-arm run did not report token_written")
        if not token_path.is_file():
            raise ValueError(f"{WAVE_TOKEN_FILENAME} missing")
        if token_path.read_text(encoding="utf-8").strip() != FOUR_ARM_SUCCESS_TOKEN:
            raise ValueError("four-arm success token mismatch")
        report = _build_report(args, identities, summary)
        _atomic_write_json(args.report_output, report)
        print(
            json.dumps(
                {
                    "ok": True,
                    "run_id": args.run_id,
                    "git_revision": args.implementation_head,
                    "comparable": False,
                    "promoted_env_recovered": False,
                    "report_output": str(args.report_output),
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": _redact(str(exc))},
                sort_keys=True,
            )
        )
        return 2


def sys_argv(argv: list[str] | None) -> list[str]:
    import sys

    return sys.argv[1:] if argv is None else argv


if __name__ == "__main__":
    raise SystemExit(main())
