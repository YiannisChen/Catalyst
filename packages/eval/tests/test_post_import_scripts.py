"""CLI entry-point tests for the Wave 2 preparation scripts.

AMEND-2 P3: ``--active-table`` is no longer a CLI flag; the validated
active_generation.json pointer is the only table source. P5: prepare script
uses run-level staging and rejects existing run IDs. The wrong-table test must
pass the DB SHA preflight and reach active-table validation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_eval.post_import.case_pack import CasePackCase, SCHEMA_VERSION as CASE_PACK_SCHEMA_VERSION
from catalyst_eval.post_import.index_identity import ResolvedRuntimeIdentity
from catalyst_eval.post_import.probe import ServedCorpusProbeReport, CaseProbeResult

RUNNER_SCRIPT = Path(__file__).resolve().parents[3] / "packages" / "eval" / "scripts" / "run_post_import_four_arm.py"
PREPARE_SCRIPT = Path(__file__).resolve().parents[3] / "packages" / "eval" / "scripts" / "prepare_post_import_wave2.py"

FROZEN_DB_SHA256 = "bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40"
GIT_HEAD = "8dd9ee9b5f04e848e3d8248dad6470189af79573"
CODE_REVISION = "bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolved() -> ResolvedRuntimeIdentity:
    return ResolvedRuntimeIdentity(
        lancedb_dir=Path("data/lancedb_gold/b6g_8ffae891b4e1"),
        active_table_name="chunks__staging__b3761f4b943542a8",
        snapshot_id="7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
        corpus_manifest_id="3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
        source_bundle_id="8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
        index_manifest_id="c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083",
        probe_report_id="25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
        postbuild_readiness_id="9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
        code_revision=CODE_REVISION,
        git_head=GIT_HEAD,
        model_name="BAAI/bge-m3",
        model_revision="5617a9f61b028005a4858fdac845db406aefb181",
        tokenizer_revision="5617a9f61b028005a4858fdac845db406aefb181",
        dimension=1024,
        dtype="float32",
        normalization_mode="l2",
        vector_count=295506,
        db_path=Path("data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db"),
        db_sha256=FROZEN_DB_SHA256,
        db_user_version=13,
        db_foreign_key_violations=0,
        lancedb_row_count=295506,
    )


def test_runner_script_parses_minimal_required_args():
    module = _load(RUNNER_SCRIPT)
    args = module._parse_args([
        "--db", "data/snapshots/x.db",
        "--lancedb-dir", "data/lancedb_gold/x",
        "--case-pack", "pack.jsonl",
        "--run-id", "smoke_1",
        "--embedding-mode", "mock_unit_test",
    ])
    assert args.run_id == "smoke_1"
    assert args.embedding_mode == "mock_unit_test"
    assert args.output_root == Path("data/run_reports/post_import")
    assert not hasattr(args, "code_revision")
    assert not hasattr(args, "git_head")
    assert not hasattr(args, "token")
    assert not hasattr(args, "active_table")


def test_runner_script_rejects_legacy_identity_flags():
    """--git-head, --code-revision, --token, and --active-table are rejected."""
    module = _load(RUNNER_SCRIPT)
    for extra in (
        ["--git-head", "8dd9ee9b5f04e848e3d8248dad6470189af79573"],
        ["--code-revision", "bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8"],
        ["--token", "FOUR_ARM_E2E_OK"],
        ["--active-table", "chunks__staging__b3761f4b943542a8"],
    ):
        argv = [
            "--db", "data/snapshots/x.db",
            "--lancedb-dir", "data/lancedb_gold/x",
            "--case-pack", "pack.jsonl",
            "--run-id", "smoke_1",
            "--embedding-mode", "mock_unit_test",
        ] + extra
        with pytest.raises(SystemExit):
            module._parse_args(argv)


def test_runner_script_rejects_missing_frozen_db():
    module = _load(RUNNER_SCRIPT)
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--db", "data/snapshots/missing.db"])
    assert excinfo.value.code == 2


def test_runner_script_rejects_production_pinned_on_mac(tmp_path, monkeypatch):
    """production_pinned must fail closed before writing anything on a Mac."""
    module = _load(RUNNER_SCRIPT)
    db = tmp_path / "frozen.db"
    db.write_bytes(b"not-a-real-db")
    rc = module.main([
        "--db", str(db),
        "--lancedb-dir", str(tmp_path),
        "--case-pack", "pack.jsonl",
        "--run-id", "smoke_prod",
        "--embedding-mode", "production_pinned",
    ])
    assert rc == 2
    assert not (tmp_path / "smoke_prod").exists()


def test_runner_script_rejects_wrong_lancedb_path(tmp_path):
    module = _load(RUNNER_SCRIPT)
    db = tmp_path / "frozen.db"
    db.write_bytes(b"fake")
    rc = module.main([
        "--db", str(db),
        "--lancedb-dir", str(tmp_path / "missing-gold"),
        "--case-pack", "pack.jsonl",
        "--run-id", "smoke",
        "--embedding-mode", "mock_unit_test",
    ])
    assert rc == 2
    assert not (tmp_path / "smoke").exists()


def test_runner_script_rejects_wrong_active_table_after_preflight(tmp_path, monkeypatch):
    """Active-table validation must be reached after DB SHA + identity preflight."""
    module = _load(RUNNER_SCRIPT)
    db = tmp_path / "frozen.db"
    db.write_bytes(b"fake")
    monkeypatch.setattr(module, "_sha256_file", lambda path: FROZEN_DB_SHA256)
    monkeypatch.setattr(module, "_open_db_readonly", lambda path: sqlite3.connect(":memory:"))
    monkeypatch.setattr(module, "_verify_frozen_db_counts", lambda conn: None)

    # Case pack must load: write one valid case line.
    case_pack = tmp_path / "pack.jsonl"
    case_pack.write_text(json.dumps({
        "schema_version": CASE_PACK_SCHEMA_VERSION,
        "case_id": "c1", "ticker": "AAPL", "session_date": "2025-06-12",
        "cutoff": "2025-06-12T20:00:00Z", "query": "Why did AAPL move on 2025-06-12?",
        "source_set": "fixture", "golden": {"golden_id": "c1"},
    }) + "\n")

    # Identity preflight fails because pointer table does not match approved
    # expected table; this is the active-table validation path. All three
    # identity files must exist so resolution reaches the table-name check.
    import sqlite3 as _sqlite3

    lancedb_dir = tmp_path / "gold"
    lancedb_dir.mkdir()
    approved_pointer = {
        "schema_version": "active_generation_v1",
        "table_name": "wrong_table",
        "chunk_count": 6,
        "snapshot_id": "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
        "corpus_manifest_id": "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
        "source_bundle_id": "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
        "index_manifest_id": "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083",
    }
    (lancedb_dir / "active_generation.json").write_text(json.dumps(approved_pointer))
    (lancedb_dir / "import_report.json").write_text(json.dumps({
        "status": "committed", "ok": True, "table_name": "wrong_table", "chunk_count": 6,
        "corpus_manifest_id": approved_pointer["corpus_manifest_id"],
        "index_manifest_id": approved_pointer["index_manifest_id"],
        "snapshot_id": approved_pointer["snapshot_id"],
        "source_bundle_id": approved_pointer["source_bundle_id"],
        "probe_report_id": "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
        "postbuild_readiness_id": "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
        "db": {"current_manifest_id": approved_pointer["corpus_manifest_id"],
               "user_version": 13, "foreign_key_violations": 0, "integrity": "ok"},
    }))
    (lancedb_dir / "index_manifest.json").write_text(json.dumps({
        "artifact_state": "lancedb_imported", "code_revision": CODE_REVISION,
        "dimension": 1024, "dtype": "float32", "normalization_mode": "l2",
        "vector_count": 6, "index_manifest_id": approved_pointer["index_manifest_id"],
        "model_name": "BAAI/bge-m3",
        "model_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "tokenizer_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "corpus_manifest_id": approved_pointer["corpus_manifest_id"],
        "snapshot_id": approved_pointer["snapshot_id"],
        "source_bundle_id": approved_pointer["source_bundle_id"],
        "probe_report_id": "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
        "postbuild_readiness_id": "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
        "schema_version": "1.0.0",
        "artifact_hashes": {"chunk_ids.json": "c1d4938560714eadca90e4d42019414606fa36c34481e1d0891abce7ae840231",
                            "lancedb_table": "0fcddf2ba84f2868327ce802b0677ac22826fe29327c645a02408352176e4c21",
                            "vectors.npy": "c5445312b985529cdb40332fd8f18683e05667f3e555779ac4b5b1d2def7e49a"},
    }))
    real_conn = _sqlite3.connect(":memory:")
    monkeypatch.setattr(module, "_open_db_readonly", lambda path: real_conn)
    rc = module.main([
        "--db", str(db),
        "--lancedb-dir", str(lancedb_dir),
        "--case-pack", str(case_pack),
        "--run-id", "smoke",
        "--embedding-mode", "mock_unit_test",
    ])
    assert rc == 2
    assert not (tmp_path / "smoke").exists()


def test_prepare_script_parses_args():
    module = _load(PREPARE_SCRIPT)
    args = module._parse_args(["--db", "data/snapshots/x.db", "--run-id", "t4_probe"])
    assert args.run_id == "t4_probe"
    assert args.output_root == Path("data/run_reports/post_import")


def test_prepare_script_rejects_missing_frozen_db():
    module = _load(PREPARE_SCRIPT)
    assert module.main(["--db", "data/snapshots/missing.db", "--run-id", "t4_probe"]) == 2


def test_prepare_script_rejects_existing_run_id(tmp_path, monkeypatch):
    module = _load(PREPARE_SCRIPT)
    (tmp_path / "existing").mkdir()
    monkeypatch.setattr(module, "_sha256_file", lambda path: FROZEN_DB_SHA256)
    monkeypatch.setattr(module, "resolve_runtime_identity", lambda **k: _resolved())
    rc = module.main([
        "--db", "data/snapshots/x.db",
        "--run-id", "existing",
        "--output-root", str(tmp_path),
        "--lancedb-dir", str(tmp_path / "gold"),
        "--index-manifest", str(tmp_path / "index_manifest.json"),
    ])
    assert rc == 2
    assert (tmp_path / "existing").is_dir()


def test_prepare_script_identity_failure_no_final_dir(tmp_path, monkeypatch):
    module = _load(PREPARE_SCRIPT)
    monkeypatch.setattr(module, "_sha256_file", lambda path: FROZEN_DB_SHA256)
    monkeypatch.setattr(module, "resolve_runtime_identity",
                        lambda **k: (_ for _ in ()).throw(ValueError("identity mismatch")))
    rc = module.main([
        "--db", "data/snapshots/x.db",
        "--run-id", "t4_identity_fail",
        "--output-root", str(tmp_path),
        "--lancedb-dir", str(tmp_path / "gold"),
        "--index-manifest", str(tmp_path / "index_manifest.json"),
    ])
    assert rc == 2
    assert not (tmp_path / "t4_identity_fail").exists()
    assert not list(tmp_path.glob(".t4_identity_fail*"))


def test_prepare_script_probe_failure_no_final_dir(tmp_path, monkeypatch):
    module = _load(PREPARE_SCRIPT)
    cases = module.build_smoke_case_pack(module.GOLDEN_DIR)
    report = ServedCorpusProbeReport(
        schema_version="served_corpus_probe_v1",
        corpus_manifest_id="3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
        case_count=10, passed_count=9, all_passed=False,
        per_case=tuple(CaseProbeResult(c.case_id, c.ticker, c.cutoff, 0 if i == 0 else 1)
                       for i, c in enumerate(cases)),
    )
    import sqlite3 as _sqlite3
    monkeypatch.setattr(module, "_sha256_file", lambda path: FROZEN_DB_SHA256)
    monkeypatch.setattr(module, "resolve_runtime_identity", lambda **k: _resolved())
    monkeypatch.setattr(module, "_open_db_readonly", lambda path: _sqlite3.connect(":memory:"))
    monkeypatch.setattr(module, "run_served_corpus_probe", lambda conn, **k: report)
    rc = module.main([
        "--db", "data/snapshots/x.db",
        "--run-id", "t4_probe_fail",
        "--output-root", str(tmp_path),
        "--lancedb-dir", str(tmp_path / "gold"),
        "--index-manifest", str(tmp_path / "index_manifest.json"),
    ])
    assert rc == 2
    assert not (tmp_path / "t4_probe_fail").exists()
    assert not list(tmp_path.glob(".t4_probe_fail*"))


def test_prepare_script_success_evidence_reloadable(tmp_path, monkeypatch):
    from catalyst_eval.post_import.t4_evidence import validate_t4_evidence

    module = _load(PREPARE_SCRIPT)
    cases = module.build_smoke_case_pack(module.GOLDEN_DIR)
    report = ServedCorpusProbeReport(
        schema_version="served_corpus_probe_v1",
        corpus_manifest_id="3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
        case_count=10, passed_count=10, all_passed=True,
        per_case=tuple(CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1) for c in cases),
    )
    import sqlite3 as _sqlite3
    monkeypatch.setattr(module, "_sha256_file", lambda path: FROZEN_DB_SHA256)
    monkeypatch.setattr(module, "resolve_runtime_identity", lambda **k: _resolved())
    monkeypatch.setattr(module, "_open_db_readonly", lambda path: _sqlite3.connect(":memory:"))
    monkeypatch.setattr(module, "run_served_corpus_probe", lambda conn, **k: report)
    rc = module.main([
        "--db", "data/snapshots/x.db",
        "--run-id", "t4_ok",
        "--output-root", str(tmp_path),
        "--lancedb-dir", str(tmp_path / "gold"),
        "--index-manifest", str(tmp_path / "index_manifest.json"),
    ])
    assert rc == 0
    assert (tmp_path / "t4_ok" / "T4_PROBE_TOKEN.txt").is_file()
    assert not (tmp_path / "t4_ok" / "WAVE_TOKEN.txt").exists()
    meta = json.loads((tmp_path / "t4_ok" / "meta.json").read_text())
    assert meta["case_pack_path"] == "case_pack.jsonl"
    assert (tmp_path / "t4_ok" / meta["case_pack_path"]).is_file()
    validated = validate_t4_evidence(
        evidence_dir=tmp_path / "t4_ok",
        current_case_pack=cases,
        resolved=_resolved(),
    )
    assert validated.case_count == 10
    assert not list(tmp_path.glob(".t4_ok*"))


# ---------------------------------------------------------------------------
# AMEND-3: production CLI contract enforcement + T4 evidence gates
# ---------------------------------------------------------------------------

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden_set"


def _write_case_pack(tmp_path: Path, cases) -> Path:
    from catalyst_eval.post_import.case_pack import write_case_pack

    path = tmp_path / "pack.jsonl"
    write_case_pack(cases, path)
    return path


def _write_evidence_dir(tmp_path: Path, cases, probe_body: dict) -> Path:
    from catalyst_eval.post_import.case_pack import compute_case_pack_id, write_case_pack

    evidence_dir = tmp_path / "evidence"
    write_case_pack(cases, evidence_dir / "case_pack.jsonl")
    (evidence_dir / "probe_report.json").write_text(json.dumps(probe_body, sort_keys=True))
    probe_sha = hashlib.sha256((evidence_dir / "probe_report.json").read_bytes()).hexdigest()
    resolved = _resolved()
    meta = {
        "schema_version": "t4_probe_meta_v1",
        "task": "T4",
        "phase": "wave2_preparation",
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-02T00:00:00+00:00",
        "runtime_git_head": GIT_HEAD,
        "index_build_code_revision": CODE_REVISION,
        "snapshot_id": resolved.snapshot_id,
        "corpus_manifest_id": resolved.corpus_manifest_id,
        "source_bundle_id": resolved.source_bundle_id,
        "probe_report_id": resolved.probe_report_id,
        "postbuild_readiness_id": resolved.postbuild_readiness_id,
        "index_manifest_id": resolved.index_manifest_id,
        "db_path": str(resolved.db_path),
        "db_sha256": resolved.db_sha256,
        "db_user_version": resolved.db_user_version,
        "db_foreign_key_violations": resolved.db_foreign_key_violations,
        "lancedb_dir": str(resolved.lancedb_dir),
        "active_table_name": resolved.active_table_name,
        "model_name": resolved.model_name,
        "model_revision": resolved.model_revision,
        "tokenizer_revision": resolved.tokenizer_revision,
        "dimension": resolved.dimension,
        "dtype": resolved.dtype,
        "normalization_mode": resolved.normalization_mode,
        "embedding_mode": "mock_unit_test",
        "case_pack_id": compute_case_pack_id(cases),
        "case_pack_path": "case_pack.jsonl",
        "case_count": probe_body.get("case_count", len(cases)),
        "passed_count": probe_body.get("passed_count", len(cases)),
        "probe_report_path": "probe_report.json",
        "probe_report_sha256": probe_sha,
        "nn_result": f"{probe_body.get('passed_count', len(cases))}/{probe_body.get('case_count', len(cases))}",
        "per_case_summary": probe_body.get("per_case", []),
    }
    (evidence_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    (evidence_dir / "T4_PROBE_TOKEN.txt").write_text("T4_PROBE_OK\n")
    return evidence_dir


def _ok_probe_body(cases) -> dict:
    from catalyst_eval.post_import.probe import CaseProbeResult

    return {
        "schema_version": "served_corpus_probe_v1",
        "corpus_manifest_id": _resolved().corpus_manifest_id,
        "db_sha256": _resolved().db_sha256,
        "case_count": len(cases),
        "passed_count": len(cases),
        "all_passed": True,
        "per_case": [CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1).to_dict() for c in cases],
    }


def test_runner_script_rejects_non_approved_case_pack_before_model_load(tmp_path, monkeypatch):
    """9-case pack must fail closed before model load, even with valid preflight."""
    from catalyst_eval.post_import.case_pack import build_smoke_case_pack

    module = _load(RUNNER_SCRIPT)
    cases = build_smoke_case_pack(GOLDEN_DIR)[:9]
    pack_path = _write_case_pack(tmp_path, cases)
    monkeypatch.setattr(module, "_sha256_file", lambda path: FROZEN_DB_SHA256)
    monkeypatch.setattr(module, "_open_db_readonly", lambda path: sqlite3.connect(":memory:"))
    monkeypatch.setattr(module, "_verify_frozen_db_counts", lambda conn: None)
    monkeypatch.setattr(module, "resolve_runtime_identity", lambda **k: _resolved())
    called = {"factory": False}

    def failing_factory():
        called["factory"] = True
        raise AssertionError("model factory must not be called")

    monkeypatch.setattr(
        "catalyst_agents.runtime.query_embedding.ProductionBgeM3QueryEmbeddingFactory",
        failing_factory,
    )
    rc = module.main([
        "--db", str(tmp_path / "frozen.db"),
        "--lancedb-dir", str(tmp_path / "gold"),
        "--case-pack", str(pack_path),
        "--run-id", "smoke",
        "--embedding-mode", "production_pinned",
        "--t4-evidence-dir", str(tmp_path / "evidence"),
    ])
    assert rc == 2
    assert called["factory"] is False
    assert not (tmp_path / "smoke").exists()


def test_runner_script_duplicate_probe_evidence_rejected_before_model_load(tmp_path, monkeypatch):
    from catalyst_eval.post_import.case_pack import build_smoke_case_pack
    from catalyst_eval.post_import.probe import CaseProbeResult

    module = _load(RUNNER_SCRIPT)
    cases = build_smoke_case_pack(GOLDEN_DIR)
    pack_path = _write_case_pack(tmp_path, cases)
    duplicate = CaseProbeResult(cases[0].case_id, cases[0].ticker, cases[0].cutoff, 1).to_dict()
    probe_body = _ok_probe_body(cases)
    probe_body["per_case"] = [duplicate] * 10
    evidence_dir = _write_evidence_dir(tmp_path, cases, probe_body)
    monkeypatch.setattr(module, "_sha256_file", lambda path: FROZEN_DB_SHA256)
    monkeypatch.setattr(module, "_open_db_readonly", lambda path: sqlite3.connect(":memory:"))
    monkeypatch.setattr(module, "_verify_frozen_db_counts", lambda conn: None)
    monkeypatch.setattr(module, "resolve_runtime_identity", lambda **k: _resolved())
    called = {"factory": False}

    def failing_factory():
        called["factory"] = True
        raise AssertionError("model factory must not be called")

    monkeypatch.setattr(
        "catalyst_agents.runtime.query_embedding.ProductionBgeM3QueryEmbeddingFactory",
        failing_factory,
    )
    rc = module.main([
        "--db", str(tmp_path / "frozen.db"),
        "--lancedb-dir", str(tmp_path / "gold"),
        "--case-pack", str(pack_path),
        "--run-id", "smoke",
        "--embedding-mode", "production_pinned",
        "--t4-evidence-dir", str(evidence_dir),
    ])
    assert rc == 2
    assert called["factory"] is False
    assert not (tmp_path / "smoke").exists()


def test_runner_script_probe_identity_mismatch_rejected_before_model_load(tmp_path, monkeypatch):
    from catalyst_eval.post_import.case_pack import build_smoke_case_pack

    module = _load(RUNNER_SCRIPT)
    cases = build_smoke_case_pack(GOLDEN_DIR)
    pack_path = _write_case_pack(tmp_path, cases)
    probe_body = _ok_probe_body(cases)
    probe_body["corpus_manifest_id"] = "e" * 64
    evidence_dir = _write_evidence_dir(tmp_path, cases, probe_body)
    monkeypatch.setattr(module, "_sha256_file", lambda path: FROZEN_DB_SHA256)
    monkeypatch.setattr(module, "_open_db_readonly", lambda path: sqlite3.connect(":memory:"))
    monkeypatch.setattr(module, "_verify_frozen_db_counts", lambda conn: None)
    monkeypatch.setattr(module, "resolve_runtime_identity", lambda **k: _resolved())
    called = {"factory": False}

    def failing_factory():
        called["factory"] = True
        raise AssertionError("model factory must not be called")

    monkeypatch.setattr(
        "catalyst_agents.runtime.query_embedding.ProductionBgeM3QueryEmbeddingFactory",
        failing_factory,
    )
    rc = module.main([
        "--db", str(tmp_path / "frozen.db"),
        "--lancedb-dir", str(tmp_path / "gold"),
        "--case-pack", str(pack_path),
        "--run-id", "smoke",
        "--embedding-mode", "production_pinned",
        "--t4-evidence-dir", str(evidence_dir),
    ])
    assert rc == 2
    assert called["factory"] is False
    assert not (tmp_path / "smoke").exists()
