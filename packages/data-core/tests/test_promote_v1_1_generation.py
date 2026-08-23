"""M3-12A: operator promotion CLI contract tests (FAST only).

Covers the exact prepare/promote/rollback subcommand contract: --dry-run never
mutates pointers/journal/active_generation.json, --force is rejected by the
parser, rollback invokes rollback_v1_generation, and secrets/uncontrolled paths
never appear in CLI output. Fixture DBs + tmp_path only; no GPU.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = (
    REPO_ROOT / "packages" / "data-core" / "scripts" / "promote_v1_1_generation.py"
)
SEAL_BENCH = REPO_ROOT / "data" / "baseline" / "benchmark_accessions_v1.json"
SEAL_Q005 = REPO_ROOT / "data" / "baseline" / "q005_sec_time_approval_v1.json"

HEAD = "a" * 40


def _load_script():
    spec = importlib.util.spec_from_file_location("promote_v1_1_generation", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seal_files(tmp_path: Path) -> tuple[Path, Path]:
    bench = tmp_path / "benchmark_accessions_v1.json"
    q005 = tmp_path / "q005_sec_time_approval_v1.json"
    shutil.copyfile(SEAL_BENCH, bench)
    shutil.copyfile(SEAL_Q005, q005)
    return bench, q005


def _derivative(tmp_path: Path) -> Path:
    db = tmp_path / "derivative.db"
    conn = sqlite3.connect(db)
    conn.close()
    return db


def _prepare_argv(tmp_path: Path, *, dry_run: bool = False) -> list[str]:
    bench, q005 = _seal_files(tmp_path)
    argv = [
        "prepare",
        "--derivative", str(_derivative(tmp_path)),
        "--benchmark-manifest", str(bench),
        "--q005-approval", str(q005),
        "--source-bundle-output-root", str(tmp_path / "bundles"),
        "--preparation-evidence", str(tmp_path / "preparation_evidence.json"),
        "--expected-implementation-head", HEAD,
    ]
    if dry_run:
        argv.append("--dry-run")
    return argv


def _prep_evidence(tmp_path: Path) -> Path:
    bench, q005 = _seal_files(tmp_path)
    seal_revision = json.loads(bench.read_text(encoding="utf-8"))["git_revision"]
    evidence = {
        "schema_version": "preparation_evidence_v1",
        "git_revision": seal_revision,
        "build_id": "b" * 64,
        "corpus_manifest_id": "1" * 64,
        "source_bundle_id": "2" * 64,
        "chunk_count": 2,
        "lexical_digest": "d" * 64,
        "data01": {"denominator": 5229, "numerator": 5229, "gate_passed": True},
        "generated_at": "2026-08-23T00:00:00Z",
    }
    path = tmp_path / "preparation_evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def _promote_argv(tmp_path: Path, *, dry_run: bool = False) -> list[str]:
    bench, q005 = _seal_files(tmp_path)
    argv = [
        "promote",
        "--derivative", str(_derivative(tmp_path)),
        "--benchmark-manifest", str(bench),
        "--q005-approval", str(q005),
        "--source-bundle", str(tmp_path / "bundle"),
        "--embedding-artifact-dir", str(tmp_path / "artifact"),
        "--candidate-manifest-dir", str(tmp_path / "candidates"),
        "--active-generation", str(tmp_path / "active_generation.json"),
        "--journal", str(tmp_path / "journal.json"),
        "--promotion-evidence", str(tmp_path / "promotion_evidence.json"),
        "--expected-implementation-head", HEAD,
    ]
    if dry_run:
        argv.append("--dry-run")
    return argv


def _rollback_argv(tmp_path: Path) -> list[str]:
    return [
        "rollback",
        "--derivative", str(_derivative(tmp_path)),
        "--active-generation", str(tmp_path / "active_generation.json"),
        "--journal", str(tmp_path / "journal.json"),
        "--expected-implementation-head", HEAD,
    ]


def test_prepare_dry_run_never_mutates(tmp_path, monkeypatch, capsys):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    argv = _prepare_argv(tmp_path, dry_run=True)
    derivative = Path(argv[2])
    before = derivative.read_bytes()

    calls: list[str] = []

    def explode(*args, **kwargs):
        calls.append("step")
        raise AssertionError("prepare steps must not run in dry-run")

    for name in (
        "_step_derivative_migration",
        "_step_accepted_time",
        "_step_sec_reparse",
        "_step_news_persistence",
        "_step_m35b_backfill",
        "_step_m36_dedup",
        "_step_audit",
        "_step_data01",
        "_step_corpus_candidate",
        "_step_bundle_export",
    ):
        monkeypatch.setattr(module, name, explode)

    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == []
    assert derivative.read_bytes() == before
    assert not (tmp_path / "preparation_evidence.json").exists()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True


def test_promote_dry_run_never_mutates(tmp_path, monkeypatch, capsys):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    argv = _promote_argv(tmp_path, dry_run=True)

    calls: list[str] = []

    def explode(*args, **kwargs):
        calls.append("mutate")
        raise AssertionError("promote dry-run must not mutate")

    from types import SimpleNamespace

    fake_verified = SimpleNamespace(
        source_bundle_id="2" * 64,
        corpus_manifest_id="1" * 64,
        snapshot_id="3" * 64,
        probe_report_id="4" * 64,
        postbuild_readiness_id="5" * 64,
        chunk_count=2,
    )
    fake_manifest = SimpleNamespace(
        vector_count=2,
        corpus_manifest_id="1" * 64,
        source_bundle_id="2" * 64,
        snapshot_id="3" * 64,
        probe_report_id="4" * 64,
        postbuild_readiness_id="5" * 64,
        index_manifest_id="6" * 64,
        model_name="BAAI/bge-m3",
        model_revision="5617a9f61b028005a4858fdac845db406aefb181",
        dimension=1024,
    )
    monkeypatch.setattr(
        module, "verify_source_bundle", lambda bundle, **kw: fake_verified
    )
    monkeypatch.setattr(
        module, "_read_artifact_manifest", lambda artifact_dir: fake_manifest
    )
    monkeypatch.setattr(module, "stage_dense", explode)
    monkeypatch.setattr(module, "promote_v1_generation", explode)

    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == []
    assert not (tmp_path / "journal.json").exists()
    assert not (tmp_path / "promotion_evidence.json").exists()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True


def test_parser_rejects_force(tmp_path):
    module = _load_script()
    for argv in (
        _prepare_argv(tmp_path) + ["--force"],
        _promote_argv(tmp_path) + ["--force"],
        _rollback_argv(tmp_path) + ["--force"],
    ):
        with pytest.raises(SystemExit):
            module._parse_args(argv)


def test_rollback_subcommand_invokes_rollback_v1_generation(tmp_path, monkeypatch, capsys):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    calls: list[dict] = []

    def fake_rollback(conn, *, journal_path, active_generation_path):
        calls.append(
            {"journal": str(journal_path), "active": str(active_generation_path)}
        )
        return module.PromotionResult(
            state="ROLLED_BACK",
            promotion_id="x" * 32,
            build_id="b" * 64,
            corpus_manifest_id="c" * 64,
            lexical_generation_id="b" * 64,
            dense_index_manifest_id="d" * 64,
            journal_path=journal_path,
            admitted=False,
        )

    monkeypatch.setattr(module, "rollback_v1_generation", fake_rollback)
    argv = _rollback_argv(tmp_path)
    active_arg = argv[argv.index("--active-generation") + 1]
    journal_arg = argv[argv.index("--journal") + 1]
    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["journal"] == str(Path(journal_arg).resolve())
    assert calls[0]["active"] == str(Path(active_arg).resolve())
    assert json.loads(out)["ok"] is True


def test_prepare_runs_ten_steps_in_order_and_never_touches_pointers(
    tmp_path, monkeypatch, capsys
):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    argv = _prepare_argv(tmp_path)
    derivative = Path(argv[2])
    before = derivative.read_bytes()
    order: list[str] = []

    def make_step(name):
        def step(*args, **kwargs):
            order.append(name)
            return {"name": name}

        return step

    for index, name in enumerate(
        (
            "_step_derivative_migration",
            "_step_accepted_time",
            "_step_sec_reparse",
            "_step_news_persistence",
            "_step_m35b_backfill",
            "_step_m36_dedup",
            "_step_audit",
            "_step_data01",
            "_step_corpus_candidate",
            "_step_bundle_export",
        ),
        start=1,
    ):
        monkeypatch.setattr(module, name, make_step(name))

    monkeypatch.setattr(
        module,
        "_frozen_identity",
        lambda conn: {
            "certified_snapshot_identity": "b" * 64,
            "snapshot_id": "b" * 64,
            "probe_report_id": "c" * 64,
            "postbuild_readiness_id": "d" * 64,
        },
    )
    def candidate_step(*a, **k):
        order.append("_step_corpus_candidate")
        return {
            "build_id": "b" * 64,
            "corpus_manifest_id": "1" * 64,
            "chunk_count": 2,
            "lexical_digest": "d" * 64,
            "source_bundle_id": "2" * 64,
            "source_bundle_path": str(tmp_path / "bundle"),
        }

    monkeypatch.setattr(module, "_step_corpus_candidate", candidate_step)

    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert order == [
        "_step_derivative_migration",
        "_step_accepted_time",
        "_step_sec_reparse",
        "_step_news_persistence",
        "_step_m35b_backfill",
        "_step_m36_dedup",
        "_step_audit",
        "_step_data01",
        "_step_corpus_candidate",
        "_step_bundle_export",
    ]
    assert len(order) == 10
    assert derivative.read_bytes() == before
    assert json.loads(out)["ok"] is True
    evidence_path = Path(argv[argv.index("--preparation-evidence") + 1])
    assert evidence_path.is_file()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == "preparation_evidence_v1"


def test_recover_accepted_time_candidate_handles_nested_filings(tmp_path):
    module = _load_script()
    top = {
        "recent": {
            "accessionNumber": ["0000320193-26-000001"],
            "acceptanceDateTime": ["2026-01-05T16:00:00Z"],
        }
    }
    nested = {
        "cik": "0000320193",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000001"],
                "acceptanceDateTime": ["2026-01-05T16:00:00Z"],
            }
        },
    }
    assert (
        module.recover_accepted_time_candidate(top, accession="0000320193-26-000001")
        is not None
    )
    assert (
        module.recover_accepted_time_candidate(nested, accession="0000320193-26-000001")
        is not None
    )
    assert module.recover_accepted_time_candidate({"cik": "x"}, accession="a") is None
    assert module.recover_accepted_time_candidate(nested, accession="missing") is None


def test_prepare_accepted_time_persists_nested_filings_evidence(tmp_path, monkeypatch, capsys):
    from catalyst_data.storage.sqlite import init_db

    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    bench, q005 = _seal_files(tmp_path)
    db = tmp_path / "derivative.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    payload = {
        "cik": "0000320193",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000001"],
                "acceptanceDateTime": ["2026-01-05T16:00:00Z"],
            }
        },
    }
    conn.execute(
        """INSERT INTO raw_assets
           (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES (?,?,?,?,?,?)""",
        (
            "raw:sec:1",
            "AAPL",
            "sec_filings",
            "2026-01-05",
            "2026-01-05T00:00:00Z",
            zlib.compress(json.dumps(payload).encode("utf-8")),
        ),
    )
    conn.execute(
        """INSERT INTO filings
           (filing_id, cik, ticker, form_type, filed_at, accession_number, url)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "filing:1",
            "0000320193",
            "AAPL",
            "8-K",
            "2026-01-05",
            "0000320193-26-000001",
            "https://example.test/8k",
        ),
    )
    conn.commit()

    module._step_accepted_time(conn, q005_path=q005)
    row = conn.execute(
        "SELECT eligible_at, accepted_time_recovered FROM filings WHERE filing_id='filing:1'"
    ).fetchone()
    assert row["accepted_time_recovered"] == 1
    assert row["eligible_at"] == "2026-01-05T16:00:00Z"
    conn.close()


def test_output_is_redacted_and_secret_free(tmp_path, monkeypatch, capsys):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    secret = "sk-live-credential-12345"
    home_path = "/Users/yiannischen/.ssh/id_rsa"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)

    def explode(*args, **kwargs):
        raise ValueError(
            f"credential {os.environ['DEEPSEEK_API_KEY']} leaked from {home_path}"
        )

    monkeypatch.setattr(module, "_require_head", explode)
    argv = _prepare_argv(tmp_path)
    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    assert secret not in out
    assert "id_rsa" not in out
    assert "/Users/yiannischen" not in out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "REDACTED" in payload["error"]


def test_aliased_derivative_to_frozen_or_live_is_rejected(tmp_path, monkeypatch, capsys):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    frozen = tmp_path / "frozen.db"
    frozen.write_bytes(b"frozen-bytes")
    live = tmp_path / "live.db"
    live.write_bytes(b"live-bytes")
    monkeypatch.setattr(module, "_frozen_live_db_paths", lambda repo_root: [frozen, live])

    # hardlink alias
    alias = tmp_path / "alias.db"
    os.link(frozen, alias)
    argv = _prepare_argv(tmp_path)
    argv[2] = str(alias)
    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    assert json.loads(out)["ok"] is False

    # symlink alias
    sym = tmp_path / "sym.db"
    sym.symlink_to(frozen)
    argv = _prepare_argv(tmp_path)
    argv[2] = str(sym)
    rc = module.main(argv)
    assert rc == 2
