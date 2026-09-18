"""M3-12A: M3 exit runner contract tests (FAST only).

The M3 production exit/four-arm entry resolves identity from M3 artifacts only;
the M1 APPROVED frozen-DB constant is never used. Fixture artifacts under
tmp_path; four-arm execution is a seam (no GPU).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
from catalyst_data.retrieval.index_manifest import IndexManifest
from catalyst_data.storage.sqlite import init_db

from catalyst_eval.post_import.case_pack import (
    CasePackCase,
    build_smoke_case_pack,
    compute_case_pack_id,
    load_case_pack,
    write_case_pack,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden_set"
SCRIPT = REPO_ROOT / "packages" / "eval" / "scripts" / "run_v1_1_m3_exit.py"
SEAL_BENCH = REPO_ROOT / "data" / "baseline" / "benchmark_accessions_v1.json"
SEAL_Q005 = REPO_ROOT / "data" / "baseline" / "q005_sec_time_approval_v1.json"

HEAD = "a" * 40
BUILD_ID = "b" * 64
CORPUS = "1" * 64
SOURCE = "2" * 64
SNAPSHOT = "3" * 64
PROBE = "4" * 64
POSTBUILD = "5" * 64
LEXICAL_DIGEST = "d" * 64
RUN_ID = "m3-run-1"


def _load_script():
    spec = importlib.util.spec_from_file_location("run_v1_1_m3_exit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest() -> IndexManifest:
    hashes = {
        "vectors.npy": "a" * 64,
        "chunk_ids.json": "b" * 64,
        "lancedb_table": "c" * 64,
    }
    return IndexManifest(
        model_name=BGE_M3_MODEL,
        model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION,
        normalization_mode="l2",
        dtype="float32",
        dimension=BGE_M3_DIMENSION,
        corpus_manifest_id=CORPUS,
        source_bundle_id=SOURCE,
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
        artifact_hashes=hashes,
        code_revision=HEAD,
        vector_count=2,
        artifact_state="lancedb_imported",
    )


def _fixture(tmp_path: Path):
    """Build all M3 artifacts under tmp_path; returns argv + module seam data."""
    manifest = _manifest()
    index_manifest_id = manifest.index_manifest_id

    db = tmp_path / "derivative.db"
    conn = sqlite3.connect(db)
    init_db(conn)
    conn.close()
    assert int(sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0]) == 14

    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    index_manifest_path = tmp_path / "index_manifest.json"
    index_manifest_path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    active_path = tmp_path / "active_generation.json"
    active_path.write_text(
        json.dumps(
            {
                "schema_version": "active_generation_v1",
                "index_manifest_id": index_manifest_id,
                "table_name": "candidate_abcdef1234567890",
                "source_bundle_id": SOURCE,
                "snapshot_id": SNAPSHOT,
                "corpus_manifest_id": CORPUS,
                "chunk_count": 2,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    bench = tmp_path / "benchmark_accessions_v1.json"
    q005 = tmp_path / "q005_sec_time_approval_v1.json"
    bench.write_bytes(SEAL_BENCH.read_bytes())
    q005.write_bytes(SEAL_Q005.read_bytes())
    seal_revision = json.loads(bench.read_text(encoding="utf-8"))["git_revision"]

    journal_path = tmp_path / "journal.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": "m3_promotion_journal_v2",
                "state": "COMMITTED",
                "promotion_id": "x" * 32,
                "build_id": BUILD_ID,
                "corpus_manifest_id": CORPUS,
                "lexical_generation_id": BUILD_ID,
                "lexical_digest": LEXICAL_DIGEST,
                "dense_index_manifest_id": index_manifest_id,
                "active_generation_path": str(active_path),
                "created_at": "2026-08-23T00:00:00Z",
                "updated_at": "2026-08-23T00:00:00Z",
                "prior": {
                    "corpus_manifest_id": "0" * 64,
                    "corpus_manifest_json": "{}",
                    "build_status": "lexical_ready",
                    "lexical_index_state": {},
                    "dense_pointer": None,
                    "served_chunk_count": 0,
                    "fts_row_count": 0,
                    "dense_chunk_count": None,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    case_pack_path = tmp_path / "case_pack.jsonl"
    write_case_pack(
        [
            CasePackCase(
                case_id="m3-case-1",
                ticker="AAPL",
                session_date="2026-01-05",
                cutoff="2026-01-05T16:00:00Z",
                query="Did Apple announce new AI features?",
                source_set="m3",
                golden={},
            )
        ],
        case_pack_path,
    )
    prep_path = tmp_path / "preparation_evidence.json"
    prep_path.write_text(
        json.dumps(
            {
                "schema_version": "preparation_evidence_v1",
                "build_id": BUILD_ID,
                "corpus_manifest_id": CORPUS,
                "source_bundle_id": SOURCE,
                "chunk_count": 2,
                "lexical_digest": LEXICAL_DIGEST,
                "probe_report_id": PROBE,
                "postbuild_readiness_id": POSTBUILD,
                "git_revision": seal_revision,
                "data01": {
                    "denominator": 5229,
                    "numerator": 5229,
                    "gate_passed": True,
                },
                "generated_at": "2026-08-23T00:00:00Z",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    prom_path = tmp_path / "promotion_evidence.json"
    prom_path.write_text(
        json.dumps(
            {
                "schema_version": "promotion_evidence_v1",
                "build_id": BUILD_ID,
                "corpus_manifest_id": CORPUS,
                "dense_index_manifest_id": index_manifest_id,
                "index_manifest_id": index_manifest_id,
                "source_bundle_id": SOURCE,
                "git_revision": seal_revision,
                "state": "COMMITTED",
                "promotion_id": "x" * 32,
                "gpu": {
                    "model": BGE_M3_MODEL,
                    "revision": BGE_M3_REVISION,
                    "dimension": BGE_M3_DIMENSION,
                    "vector_count": 2,
                },
                "generated_at": "2026-08-23T00:00:00Z",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "output"
    report_output = tmp_path / "report.json"

    argv = [
        "--db", str(db),
        "--expected-db-sha256", _sha256_file(db),
        "--expected-user-version", "14",
        "--lancedb-dir", str(lancedb_dir),
        "--index-manifest", str(index_manifest_path),
        "--active-generation", str(active_path),
        "--case-pack", str(case_pack_path),
        "--run-id", RUN_ID,
        "--output-root", str(output_root),
        "--implementation-head", HEAD,
        "--promotion-journal", str(journal_path),
        "--preparation-evidence", str(prep_path),
        "--promotion-evidence", str(prom_path),
        "--benchmark-manifest", str(bench),
        "--q005-approval", str(q005),
        "--report-output", str(report_output),
    ]
    return argv, {
        "db": db,
        "output_root": output_root,
        "report_output": report_output,
        "active_path": active_path,
        "index_manifest_id": index_manifest_id,
    }


def _install_four_arm_seam(module, monkeypatch, *, ok: bool = True):
    calls: list[str] = []

    def fake_four_arm(args, identities):
        calls.append(args.run_id)
        if not ok:
            raise RuntimeError("injected:four-arm-failure")
        run_dir = Path(args.output_root) / args.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "meta.json").write_text(
            json.dumps({"run_id": args.run_id, "identity": identities.to_dict() if hasattr(identities, "to_dict") else str(identities)}),
            encoding="utf-8",
        )
        (run_dir / "WAVE_TOKEN.txt").write_text("FOUR_ARM_E2E_OK", encoding="utf-8")
        return SimpleNamespace(run_id=args.run_id, token_written=True, meta_path=run_dir / "meta.json")

    monkeypatch.setattr(module, "_run_four_arm", fake_four_arm)
    return calls


def test_exit_success_writes_token_and_non_comparable_report(
    tmp_path, monkeypatch, capsys
):
    module = _load_script()
    argv, meta = _fixture(tmp_path)
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    monkeypatch.setattr(module, "_git_status_porcelain", lambda *a, **k: "")
    calls = _install_four_arm_seam(module, monkeypatch)

    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [RUN_ID]
    run_dir = meta["output_root"] / RUN_ID
    assert (run_dir / "WAVE_TOKEN.txt").read_text(encoding="utf-8") == "FOUR_ARM_E2E_OK"
    report = json.loads(meta["report_output"].read_text(encoding="utf-8"))
    assert report["schema_version"] == "v1_1_m3_gate_report_v1"
    assert report["git_revision"] == HEAD
    assert report["comparable"] is False
    assert report["promoted_env_recovered"] is False
    assert report["run_id"] == RUN_ID
    assert json.loads(out)["ok"] is True


def test_db_sha_mismatch_fails_closed_no_report(tmp_path, monkeypatch, capsys):
    module = _load_script()
    argv, meta = _fixture(tmp_path)
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    monkeypatch.setattr(module, "_git_status_porcelain", lambda *a, **k: "")
    monkeypatch.setattr(module, "_run_four_arm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    bad_argv = list(argv)
    bad_argv[bad_argv.index("--expected-db-sha256") + 1] = "f" * 64
    rc = module.main(bad_argv)
    assert rc == 2
    assert not meta["report_output"].exists()
    assert not (meta["output_root"] / RUN_ID / "WAVE_TOKEN.txt").exists()


def test_user_version_mismatch_fails_closed(tmp_path, monkeypatch, capsys):
    module = _load_script()
    argv, meta = _fixture(tmp_path)
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    monkeypatch.setattr(module, "_git_status_porcelain", lambda *a, **k: "")
    monkeypatch.setattr(module, "_run_four_arm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    bad_argv = list(argv)
    bad_argv[bad_argv.index("--expected-user-version") + 1] = "13"
    rc = module.main(bad_argv)
    assert rc == 2
    assert not meta["report_output"].exists()


def test_journal_not_committed_fails_closed(tmp_path, monkeypatch, capsys):
    module = _load_script()
    argv, meta = _fixture(tmp_path)
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    monkeypatch.setattr(module, "_git_status_porcelain", lambda *a, **k: "")
    monkeypatch.setattr(module, "_run_four_arm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    journal_idx = argv.index("--promotion-journal") + 1
    journal = json.loads(Path(argv[journal_idx]).read_text(encoding="utf-8"))
    journal["state"] = "PREPARED"
    Path(argv[journal_idx]).write_text(json.dumps(journal), encoding="utf-8")
    rc = module.main(argv)
    assert rc == 2
    assert not meta["report_output"].exists()


def test_identity_mismatch_between_artifacts_fails_closed(tmp_path, monkeypatch, capsys):
    module = _load_script()
    argv, meta = _fixture(tmp_path)
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    monkeypatch.setattr(module, "_git_status_porcelain", lambda *a, **k: "")
    monkeypatch.setattr(module, "_run_four_arm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    active_idx = argv.index("--active-generation") + 1
    active = json.loads(Path(argv[active_idx]).read_text(encoding="utf-8"))
    active["corpus_manifest_id"] = "9" * 64
    Path(argv[active_idx]).write_text(json.dumps(active), encoding="utf-8")
    rc = module.main(argv)
    assert rc == 2
    assert not meta["report_output"].exists()


def test_stale_run_id_fails_closed(tmp_path, monkeypatch, capsys):
    module = _load_script()
    argv, meta = _fixture(tmp_path)
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    monkeypatch.setattr(module, "_git_status_porcelain", lambda *a, **k: "")
    monkeypatch.setattr(module, "_run_four_arm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    stale = meta["output_root"] / RUN_ID
    stale.mkdir(parents=True)
    rc = module.main(argv)
    assert rc == 2
    assert not meta["report_output"].exists()


def test_four_arm_failure_writes_no_success_token_or_report(
    tmp_path, monkeypatch, capsys
):
    module = _load_script()
    argv, meta = _fixture(tmp_path)
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    monkeypatch.setattr(module, "_git_status_porcelain", lambda *a, **k: "")
    _install_four_arm_seam(module, monkeypatch, ok=False)
    rc = module.main(argv)
    assert rc == 2
    assert not (meta["output_root"] / RUN_ID / "WAVE_TOKEN.txt").exists()
    assert not meta["report_output"].exists()


def test_m1_approved_constant_never_used():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ApprovedFrozenIdentities" not in source
    assert "run_post_import_four_arm" not in source
    assert "from catalyst_eval.post_import.index_identity import" not in source
    assert "APPROVED_FROZEN_DB_SHA256" not in source
    assert "APPROVED = ApprovedFrozenIdentities" not in source


def test_report_is_path_redacted_and_secret_free(tmp_path, monkeypatch, capsys):
    module = _load_script()
    argv, meta = _fixture(tmp_path)
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    monkeypatch.setattr(module, "_git_status_porcelain", lambda *a, **k: "")
    _install_four_arm_seam(module, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-m3-exit-secret-free-98765")
    rc = module.main(argv)
    assert rc == 0
    report_text = meta["report_output"].read_text(encoding="utf-8")
    assert "sk-m3-exit-secret-free-98765" not in report_text
    assert "/Users/" not in report_text
    report = json.loads(report_text)
    assert "DEEPSEEK_API_KEY" not in report


def _m3_args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        db=tmp_path / "derivative.db",
        lancedb_dir=tmp_path / "lancedb",
        case_pack=tmp_path / "case_pack.jsonl",
        run_id="m3-evidence-test",
        output_root=tmp_path / "output",
        implementation_head=HEAD,
        expected_db_sha256="e" * 64,
        expected_user_version=14,
    )


def _m3_identities() -> dict:
    return {
        "build_id": BUILD_ID,
        "corpus_manifest_id": CORPUS,
        "dense_index_manifest_id": "9" * 64,
        "index_manifest_id": "8" * 64,
        "source_bundle_id": SOURCE,
        "snapshot_id": SNAPSHOT,
        "probe_report_id": PROBE,
        "postbuild_readiness_id": POSTBUILD,
        "active_table_name": "candidate_abcdef1234567890",
        "chunk_count": 2,
        "case_pack_id": "c" * 64,
        "gpu": {
            "model": BGE_M3_MODEL,
            "revision": BGE_M3_REVISION,
            "dimension": BGE_M3_DIMENSION,
        },
    }


def test_m3_runtime_identity_binds_m3_artifacts(tmp_path):
    """Resolved runtime identity is built from M3 artifacts only; the M1
    APPROVED constant is never used."""
    module = _load_script()
    identities = _m3_identities()
    resolved = module._m3_runtime_identity(
        _m3_args(tmp_path), identities, db_foreign_key_violations=0
    )
    assert resolved.corpus_manifest_id == CORPUS
    assert resolved.index_manifest_id == identities["index_manifest_id"]
    assert resolved.snapshot_id == SNAPSHOT
    assert resolved.source_bundle_id == SOURCE
    assert resolved.probe_report_id == PROBE
    assert resolved.postbuild_readiness_id == POSTBUILD
    assert resolved.active_table_name == identities["active_table_name"]
    assert resolved.code_revision == HEAD
    assert resolved.git_head == HEAD
    assert resolved.db_sha256 == "e" * 64
    assert resolved.db_user_version == 14
    assert resolved.db_foreign_key_violations == 0
    assert resolved.model_name == BGE_M3_MODEL
    assert resolved.model_revision == BGE_M3_REVISION
    assert resolved.dimension == BGE_M3_DIMENSION
    assert resolved.vector_count == identities["chunk_count"]


def test_build_m3_t4_evidence_generates_valid_m3_evidence(tmp_path):
    """The M3 exit runner generates and validates its own T4 probe evidence
    bound to the M3 derivative/manifest (never the M1 evidence pack)."""
    module = _load_script()
    args = _m3_args(tmp_path)
    identities = _m3_identities()
    args.output_root.mkdir()
    db = args.db
    conn = sqlite3.connect(db)
    init_db(conn)
    conn.execute(
        "PRAGMA foreign_keys=OFF"
    )
    case_rows = []
    tickers = ["TSLA", "NVDA", "AMD", "UNH", "AMZN", "TSLA", "AAPL", "GOOGL", "JPM", "MSFT"]
    for index, ticker in enumerate(tickers):
        case_rows.append(
            (
                f"chunk:{index:04d}", f"doc:{index}", "news_v2", "body", "0001",
                "text", "a" * 64, "b" * 64, "reported_news", None, None, None,
                "2025-01-01T00:00:00Z", json.dumps([ticker]), "eligible", CORPUS,
                "active", "sentence", 0, 10, 0, 0, 0, 0,
                "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
            )
        )
    conn.executemany(
        """INSERT INTO corpus_chunks (
               chunk_id, document_id, chunk_profile_version, section_key,
               ordinal, content_text, content_hash, metadata_hash,
               source_class, dedup_cluster_id, cluster_first_available_at,
               representative_document_id, available_at, ticker_associations,
               eligibility, manifest_id, status, boundary_kind,
               body_token_start, body_token_end, body_overlap_tokens,
               prefix_token_count, prefix_truncated, section_parse_degraded,
               created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        case_rows,
    )
    conn.commit()
    conn.close()

    # Build the 10-case T4 smoke pack from this repo's golden sets so the test
    # never depends on another checkout or a personal absolute path.
    cases = build_smoke_case_pack(GOLDEN_DIR)
    assert len(cases) == 10
    write_case_pack(cases, args.case_pack)
    resolved = module._m3_runtime_identity(args, identities, db_foreign_key_violations=0)
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        evidence = module._build_m3_t4_evidence(
            args, identities, conn, cases, resolved=resolved
        )
    finally:
        conn.close()
    assert evidence.case_pack_id == compute_case_pack_id(cases)
    assert evidence.case_count == 10
    assert evidence.passed_count == 10
    assert evidence.corpus_manifest_id == CORPUS
    assert evidence.db_user_version == 14
    assert evidence.index_manifest_id == identities["index_manifest_id"]
    assert evidence.active_table_name == identities["active_table_name"]
    token = args.output_root / f"{args.run_id}_t4_evidence" / "T4_PROBE_TOKEN.txt"
    assert token.is_file()
