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
from types import SimpleNamespace

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
        "--snapshot-id", "7" * 64,
        "--probe-report-id", "8" * 64,
        "--postbuild-readiness-id", "9" * 64,
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
            if name == "_step_data01":
                return {
                    "name": name,
                    "denominator": 5229,
                    "numerator": 5229,
                    "gate_passed": True,
                }
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

    captured: dict = {}

    def candidate_step(*a, **k):
        order.append("_step_corpus_candidate")
        captured.update(k)
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
    assert captured["certified_snapshot_identity"] == "7" * 64
    assert captured["snapshot_id"] == "7" * 64
    assert captured["probe_report_id"] == "8" * 64
    assert captured["postbuild_readiness_id"] == "9" * 64
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


# ===================== GATE A regression tests =====================

_LONG_BODY = (
    "Apple Inc. announced new AI features during its product event. "
    "The company said the updates will roll out to customers starting next "
    "month. Analysts expect the changes to improve device performance and "
    "battery life across the lineup. This paragraph is deliberately long "
    "enough to clear the minimum material body threshold so the projection "
    "classifies the article as full text evidence rather than metadata."
)
_FILING_BODY = (
    "Item 1.01 Entry into a Material Definitive Agreement\n"
    "The registrant entered into a material agreement with counterparties. "
    "The terms include customary covenants and closing conditions that apply "
    "to the parties under the agreement as of the date of this report.\n"
    "Item 2.02 Results of Operations and Financial Condition\n"
    "The registrant announced financial results for the fiscal period. "
    "Revenue and operating income are discussed in the accompanying exhibit. "
    "This paragraph is deliberately long enough to clear the minimum primary "
    "document extraction threshold for the versioned SEC parser."
)


def _news_db(tmp_path: Path, name: str):
    from catalyst_data.storage.sqlite import init_db

    db = tmp_path / f"{name}.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_step_news_persistence_handles_sqlite_rows(tmp_path):
    """A1: sqlite3.Row has no .get; the news persistence step must still run."""
    module = _load_script()
    conn = _news_db(tmp_path, "news")
    conn.execute(
        """INSERT INTO raw_assets
           (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES (?,?,?,?,?,?)""",
        (
            "raw:news:1",
            "AAPL",
            "polygon_news",
            "2026-07-31",
            "2026-07-31T00:00:00Z",
            json.dumps({"body": _LONG_BODY}).encode("utf-8"),
        ),
    )
    conn.execute(
        """INSERT INTO articles
           (article_id, raw_asset_id, provider, source_type, ticker,
            reference_date, published_utc, title, description, article_url)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "poly:1",
            "raw:news:1",
            "polygon",
            "polygon_news",
            "AAPL",
            "2026-07-31",
            "2026-07-31T00:00:00Z",
            "Unicode café title",
            "description",
            "https://example.test/news/1",
        ),
    )
    conn.commit()
    module._step_news_persistence(conn)
    row = conn.execute(
        "SELECT recovered_content_state, recovered_content_hash FROM articles "
        "WHERE article_id='poly:1'"
    ).fetchone()
    assert row["recovered_content_state"] == "FULL_TEXT"
    assert row["recovered_content_hash"]
    conn.close()


def test_step_accepted_time_persists_every_filing(tmp_path):
    """A2: every filings row gets a persisted temporal repair (never NULL reason)."""
    module = _load_script()
    bench, q005 = _seal_files(tmp_path)
    conn = _news_db(tmp_path, "accepted")
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
    for filing_id, accession, filed_at in (
        ("filing:1", "0000320193-26-000001", "2026-01-05"),
        ("filing:2", "0000320193-26-000002", "2026-01-06"),
        ("filing:3", "0000320193-26-000003", "not-a-date"),
    ):
        conn.execute(
            """INSERT INTO filings
               (filing_id, cik, ticker, form_type, filed_at, accession_number, url)
               VALUES (?,?,?,?,?,?,?)""",
            (
                filing_id,
                "0000320193",
                "AAPL",
                "8-K",
                filed_at,
                accession,
                f"https://example.test/{filing_id}",
            ),
        )
    conn.commit()

    module._step_accepted_time(conn, q005_path=q005)
    rows = conn.execute(
        "SELECT filing_id, eligible_at, eligible_at_reason, temporal_precision, "
        "accepted_time_recovered FROM filings ORDER BY filing_id"
    ).fetchall()
    assert len(rows) == 3
    null_reason = [
        row["filing_id"] for row in rows if row["eligible_at_reason"] is None
    ]
    assert null_reason == []
    by_id = {row["filing_id"]: row for row in rows}
    assert by_id["filing:1"]["accepted_time_recovered"] == 1
    assert by_id["filing:1"]["eligible_at"] == "2026-01-05T16:00:00Z"
    assert by_id["filing:2"]["accepted_time_recovered"] == 0
    assert by_id["filing:2"]["temporal_precision"] == "date_only_latest_plausible"
    assert by_id["filing:2"]["eligible_at"] == "2026-01-07T05:00:00Z"
    assert by_id["filing:3"]["accepted_time_recovered"] == 0
    assert by_id["filing:3"]["eligible_at"] is None
    assert by_id["filing:3"]["eligible_at_reason"] == "fail_closed_no_accepted_time"
    conn.close()


def test_m35b_backfill_requires_repairs(tmp_path, monkeypatch):
    """A3: production backfill must pass require_repairs=True."""
    module = _load_script()
    calls: dict = {}

    def fake_backfill(inner_conn, *, dry_run=False, require_repairs=False):
        calls["require_repairs"] = require_repairs
        return SimpleNamespace(assets=0, content_versions=0, associations=0)

    import catalyst_data.canonical.backfill as backfill_mod

    monkeypatch.setattr(backfill_mod, "backfill_from_subtypes", fake_backfill)
    conn = _news_db(tmp_path, "backfill")
    module._step_m35b_backfill(conn)
    assert calls.get("require_repairs") is True
    conn.close()


def test_step_sec_reparse_persists_from_stored_text(tmp_path):
    """A4: archived submissions envelopes have no primary bytes; stored text is reparsed."""
    module = _load_script()
    conn = _news_db(tmp_path, "reparse")
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
    conn.execute(
        """INSERT INTO filing_documents
           (filing_id, document_url, document_type, text, char_len, content_type,
            byte_size, extraction_status, extracted_at, document_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "filing:1",
            "https://example.test/8k",
            "primary_doc",
            _FILING_BODY,
            len(_FILING_BODY),
            "text/html",
            len(_FILING_BODY.encode("utf-8")),
            "success",
            "2026-01-05T00:00:00Z",
            "e" * 64,
        ),
    )
    conn.commit()
    result = module._step_sec_reparse(conn)
    assert result["reparsed"] == 1
    row = conn.execute(
        "SELECT parser_version, parse_quality, document_hash FROM filing_documents "
        "WHERE filing_id='filing:1' AND document_id='" + "e" * 64 + "'"
    ).fetchone()
    assert row["parser_version"]
    assert row["parse_quality"] in ("full", "degraded")
    assert row["document_hash"]
    conn.close()


def test_frozen_snapshot_path_in_alias_rejection_default(tmp_path):
    """A5: the exact frozen snapshot path is always in the alias-rejection set."""
    module = _load_script()
    paths = module._frozen_live_db_paths(tmp_path)
    assert module.FROZEN_SNAPSHOT_PATH in paths
    with pytest.raises(ValueError):
        module._reject_aliased_db(module.FROZEN_SNAPSHOT_PATH, repo_root=tmp_path)


def test_prepare_requires_certified_identity_flags(tmp_path):
    """A6: prepare must require certified snapshot/probe/postbuild identity flags."""
    module = _load_script()
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
    with pytest.raises(SystemExit):
        module._parse_args(argv)


# ===================== STEP 0 resume regression tests =====================

def _promotable_derivative(tmp_path: Path, name: str = "derivative.db") -> Path:
    """Derivative with a prepared lexical_ready candidate build row."""
    from catalyst_data.corpus.streaming_publication import (
        ensure_streaming_publication_schema,
    )
    from catalyst_data.storage.sqlite import init_db

    db = tmp_path / name
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    ensure_streaming_publication_schema(conn)
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status,
            manifest_id, chunk_count, lexical_digest, lexical_row_count,
            lexical_ready, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "b" * 64,
            "7" * 64,
            "{}",
            "lexical_ready",
            "1" * 64,
            2,
            "d" * 64,
            2,
            1,
            "2026-08-23T00:00:00Z",
            "2026-08-23T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()
    return db


def _fake_promote_inputs(module, tmp_path, monkeypatch):
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
    fake_dense = SimpleNamespace(
        corpus_manifest_id="1" * 64,
        chunk_count=2,
        source_bundle_id="2" * 64,
        index_manifest_id="6" * 64,
    )
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    monkeypatch.setattr(module, "verify_source_bundle", lambda bundle, **kw: fake_verified)
    monkeypatch.setattr(module, "_read_artifact_manifest", lambda artifact_dir: fake_manifest)
    monkeypatch.setattr(module, "stage_dense", lambda *a, **k: fake_dense)
    return fake_verified, fake_manifest, fake_dense


def test_prepare_fails_closed_when_data01_gate_false(tmp_path, monkeypatch, capsys):
    """STEP 0: DATA-01 gate_passed=false aborts prepare before corpus/FTS/bundle export."""
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    argv = _prepare_argv(tmp_path)
    derivative = Path(argv[2])

    for name in (
        "_step_derivative_migration",
        "_step_accepted_time",
        "_step_sec_reparse",
        "_step_news_persistence",
        "_step_m35b_backfill",
        "_step_m36_dedup",
        "_step_audit",
    ):
        monkeypatch.setattr(module, name, lambda *a, **k: {})

    monkeypatch.setattr(
        module,
        "_step_data01",
        lambda *a, **k: {
            "denominator": 5229,
            "numerator": 0,
            "gate_passed": False,
            "accepted_time_recovered_count": 0,
            "fail_closed_eligibility_count": 5229,
        },
    )
    calls: list[str] = []

    def explode(*a, **k):
        calls.append("export")
        raise AssertionError("corpus/FTS/bundle export must not run on failed DATA-01")

    monkeypatch.setattr(module, "_step_corpus_candidate", explode)
    monkeypatch.setattr(module, "_step_bundle_export", explode)

    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    assert calls == []
    assert not (tmp_path / "preparation_evidence.json").exists()
    assert json.loads(out)["ok"] is False
    assert derivative.exists()


def test_promote_fails_closed_unless_committed_and_admitted(tmp_path, monkeypatch, capsys):
    """STEP 0: promote returns non-zero unless PromotionResult is COMMITTED+admitted."""
    module = _load_script()
    argv = _promote_argv(tmp_path)
    # point --derivative at a prepared candidate build
    argv[argv.index("--derivative") + 1] = str(_promotable_derivative(tmp_path))
    _fake_promote_inputs(module, tmp_path, monkeypatch)

    calls: list[str] = []

    def fake_promote(conn, *, journal_path, **kwargs):
        calls.append(str(journal_path))
        return module.PromotionResult(
            state="ROLLED_BACK",
            promotion_id="x" * 32,
            build_id="b" * 64,
            corpus_manifest_id="1" * 64,
            lexical_generation_id="b" * 64,
            dense_index_manifest_id="6" * 64,
            journal_path=journal_path,
            admitted=False,
        )

    monkeypatch.setattr(module, "promote_v1_generation", fake_promote)
    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    assert len(calls) == 1
    assert not (tmp_path / "promotion_evidence.json").exists()
    assert json.loads(out)["ok"] is False


def test_promote_succeeds_only_when_committed_and_admitted(tmp_path, monkeypatch, capsys):
    """STEP 0: committed+admitted promote writes evidence and returns 0."""
    module = _load_script()
    argv = _promote_argv(tmp_path)
    argv[argv.index("--derivative") + 1] = str(_promotable_derivative(tmp_path))
    _fake_promote_inputs(module, tmp_path, monkeypatch)

    def fake_promote(conn, *, journal_path, **kwargs):
        return module.PromotionResult(
            state="COMMITTED",
            promotion_id="x" * 32,
            build_id="b" * 64,
            corpus_manifest_id="1" * 64,
            lexical_generation_id="b" * 64,
            dense_index_manifest_id="6" * 64,
            journal_path=journal_path,
            admitted=True,
        )

    monkeypatch.setattr(module, "promote_v1_generation", fake_promote)
    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    evidence = json.loads(
        (tmp_path / "promotion_evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["state"] == "COMMITTED"
    assert json.loads(out)["ok"] is True


def test_step_sec_reparse_handles_real_primary_document_type(tmp_path):
    """Real filing_documents use document_type='primary'; reparse must not skip them."""
    module = _load_script()
    conn = _news_db(tmp_path, "reparse-primary")
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
    conn.execute(
        """INSERT INTO filing_documents
           (filing_id, document_url, document_type, text, char_len, content_type,
            byte_size, extraction_status, extracted_at, document_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "filing:1",
            "https://example.test/8k",
            "primary",
            _FILING_BODY,
            len(_FILING_BODY),
            "text/html",
            len(_FILING_BODY.encode("utf-8")),
            "success",
            "2026-01-05T00:00:00Z",
            "e" * 64,
        ),
    )
    conn.commit()
    result = module._step_sec_reparse(conn)
    assert result["reparsed"] == 1
    row = conn.execute(
        "SELECT parser_version, parse_quality, document_hash FROM filing_documents "
        "WHERE filing_id='filing:1' AND document_id='" + "e" * 64 + "'"
    ).fetchone()
    assert row["parser_version"]
    assert row["parse_quality"] in ("full", "degraded")
    assert row["document_hash"]
    conn.close()


def test_step_sec_reparse_persists_repair_for_empty_text(tmp_path):
    """Empty stored text still gets a persisted not_applicable repair (no fabricated HTML)."""
    module = _load_script()
    conn = _news_db(tmp_path, "reparse-empty")
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
    conn.execute(
        """INSERT INTO filing_documents
           (filing_id, document_url, document_type, text, char_len, content_type,
            byte_size, extraction_status, extracted_at, document_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "filing:1",
            "https://example.test/8k",
            "primary",
            "",
            0,
            "text/html",
            0,
            "success",
            "2026-01-05T00:00:00Z",
            "e" * 64,
        ),
    )
    conn.commit()
    result = module._step_sec_reparse(conn)
    assert result["reparsed"] == 0
    assert result["skipped"] == 1
    row = conn.execute(
        "SELECT parser_version, parse_quality, document_hash FROM filing_documents "
        "WHERE filing_id='filing:1' AND document_id='" + "e" * 64 + "'"
    ).fetchone()
    assert row["parser_version"]
    assert row["parse_quality"] == "not_applicable"
    assert row["document_hash"] is None
    conn.close()
