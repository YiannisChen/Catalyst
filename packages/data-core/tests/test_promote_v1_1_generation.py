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


# ---------------------------------------------------------------------------
# recover-sec-primary CLI contract (offline; no live EDGAR, no frozen DB)
# ---------------------------------------------------------------------------

RECOVER_ACCESSION = "0000320193-26-000001"
RECOVER_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/"
    "000032019326000001/aapl-20260328.htm"
)
SEALED_A_SHA256 = "dd3f02eadf54b4fa80844ecaef877b0b7de2678b2fc8d642666e4a6d941112ac"


RECOVER_ACCESSIONS = [
    RECOVER_ACCESSION,
    "0000320193-26-000002",
]
RECOVER_ACCESSIONS_HASH = None  # set by _synthetic_seal_files


def _synthetic_seal_files(tmp_path: Path, accessions) -> tuple[Path, Path]:
    """Valid sealed benchmark/Q-005 pair over a tiny synthetic accession set."""
    from catalyst_data.canonical.ids import sha256_identity

    global RECOVER_ACCESSIONS_HASH
    authority = "synthetic recover-sec-primary CLI test authority"
    case_hash = sha256_identity(
        {
            "schema_version": "benchmark_accessions_v1",
            "selection_authority": authority,
            "ordered_unique_accession_ids": accessions,
        }
    )
    RECOVER_ACCESSIONS_HASH = case_hash
    bench = {
        "schema_version": "benchmark_accessions_v1",
        "selection_authority": authority,
        "frozen_at": "2026-08-24T00:00:00Z",
        "git_revision": HEAD,
        "ordered_unique_accession_ids": accessions,
        "excluded_accessions": [],
        "selection_exclusions": [],
        "case_list_sha256": case_hash,
        "denominator": len(accessions),
    }
    q005 = {
        "schema_version": "q005_sec_time_approval_v1",
        "benchmark_case_list_sha256": case_hash,
        "selection_authority": authority,
        "frozen_at": "2026-08-24T00:00:00Z",
        "git_revision": HEAD,
        "decision": "approve_latest_plausible_instant",
        "raw_payload_inventory": [
            {"accession": a, "acceptance_datetime_retained": True}
            for a in accessions
        ],
    }
    bench_p = tmp_path / "benchmark_accessions_v1.json"
    q005_p = tmp_path / "q005_sec_time_approval_v1.json"
    bench_p.write_text(json.dumps(bench), encoding="utf-8")
    q005_p.write_text(json.dumps(q005), encoding="utf-8")
    return bench_p, q005_p


def _recover_derivative(tmp_path: Path) -> Path:
    """Tiny derivative: 1 valid primary + 1 missing filing (never the frozen DB)."""
    from catalyst_data.storage.sqlite import init_db

    db = tmp_path / "recover_derivative.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    init_db(conn)
    conn.execute(
        """INSERT INTO filings (
               filing_id, cik, ticker, form_type, filed_at,
               accession_number, primary_document, url
           ) VALUES (?, '0000320193', 'AAPL', '8-K', '2026-03-28', ?, 'aapl-20260328.htm', ?)""",
        (
            f"sec:0000320193:{RECOVER_ACCESSION}",
            RECOVER_ACCESSION,
            RECOVER_URL,
        ),
    )
    conn.execute(
        """INSERT INTO filings (
               filing_id, cik, ticker, form_type, filed_at,
               accession_number, primary_document, url
           ) VALUES (?, '0000320193', 'AAPL', '8-K', '2026-03-28', ?, 'aapl-20260430.htm', ?)""",
        (
            "sec:0000320193:0000320193-26-000002",
            "0000320193-26-000002",
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000002/aapl-20260430.htm",
        ),
    )
    from catalyst_data.sec.document_cells import compute_document_id

    document_id = compute_document_id(
        filing_id=f"sec:0000320193:{RECOVER_ACCESSION}",
        accession_number=RECOVER_ACCESSION,
        document_role="primary",
        document_file="aapl-20260328.htm",
        document_url=RECOVER_URL,
    )
    conn.execute(
        """INSERT INTO filing_documents (
               filing_id, document_url, document_type, text, char_len,
               content_type, byte_size, extraction_status, extracted_at,
               document_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            f"sec:0000320193:{RECOVER_ACCESSION}",
            RECOVER_URL,
            "primary",
            "valid primary text for the already-present filing in the CLI fixture",
            80,
            "text/html",
            128,
            "success",
            "2026-03-28T00:00:00Z",
            document_id,
        ),
    )
    conn.commit()
    conn.close()
    return db


def _recover_argv(tmp_path: Path, *, dry_run: bool = False, rate=None, limit=None) -> list[str]:
    bench, q005 = _synthetic_seal_files(tmp_path, RECOVER_ACCESSIONS)
    frozen = tmp_path / "frozen_snapshot.db"
    frozen.write_bytes(b"tiny-frozen-bytes")
    argv = [
        "recover-sec-primary",
        "--derivative", str(_recover_derivative(tmp_path)),
        "--benchmark-manifest", str(bench),
        "--q005-approval", str(q005),
        "--frozen-snapshot", str(frozen),
        "--checkpoint", str(tmp_path / "recover_checkpoint.json"),
        "--recovery-report", str(tmp_path / "recovery_report.json"),
        "--expected-implementation-head", HEAD,
    ]
    if dry_run:
        argv.append("--dry-run")
    if rate is not None:
        argv += ["--rate-per-second", str(rate)]
    if limit is not None:
        argv += ["--limit", str(limit)]
    return argv


def test_recover_sec_primary_dry_run_reports_missing(tmp_path, monkeypatch, capsys):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    monkeypatch.setenv("SEC_USER_AGENT", "M3 Test/1.0 (test@example.com)")
    argv = _recover_argv(tmp_path, dry_run=True)
    derivative = Path(argv[2])
    before = derivative.read_bytes()

    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert derivative.read_bytes() == before
    assert not (tmp_path / "recover_checkpoint.json").exists()
    assert not (tmp_path / "recovery_report.json").exists()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["work_set_total"] == 2
    assert payload["already_present"] == 1
    assert payload["requested"] == 1
    assert payload["accession_list_sha256"] == RECOVER_ACCESSIONS_HASH
    assert payload["git_revision"] == HEAD


def test_recover_sec_primary_rejects_frozen_alias(tmp_path, monkeypatch, capsys):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    argv = _recover_argv(tmp_path, dry_run=True)
    # --derivative pointing at the hard-coded frozen snapshot path must be
    # rejected by path comparison alone; the 5.5GB file is never opened/read.
    argv[2] = str(module.FROZEN_SNAPSHOT_PATH)
    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "forbidden" in payload["error"]


def test_recover_sec_primary_requires_user_agent(tmp_path, monkeypatch, capsys):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    argv = _recover_argv(tmp_path)
    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "SEC_USER_AGENT" in payload["error"]


def test_recover_sec_primary_rate_limit_validation(tmp_path, monkeypatch, capsys):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    for bad in (0.05, 5.01, 6.0):
        argv = _recover_argv(tmp_path, dry_run=True, rate=bad)
        rc = module.main(argv)
        out = capsys.readouterr().out
        assert rc == 2, bad
        payload = json.loads(out)
        assert "rate-per-second" in payload["error"]


def test_recover_sec_primary_limit_fails_closed(tmp_path, monkeypatch, capsys):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    monkeypatch.setenv("SEC_USER_AGENT", "M3 Test/1.0 (test@example.com)")
    argv = _recover_argv(tmp_path, limit=1)
    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "test-only" in payload["error"]


def test_recover_sec_primary_writes_report_with_secret_safe_output(
    tmp_path, monkeypatch, capsys
):
    import catalyst_data.sec.recover_primary as recover_module

    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    monkeypatch.setenv("SEC_USER_AGENT", "M3 SecretUA/1.0 (secret@example.com)")
    calls: list[list[str]] = []
    fake_counters = {
        "requested": 1,
        "already_present": 1,
        "succeeded": 1,
        "pdf_skipped": 0,
        "empty": 0,
        "permanent_404": 0,
        "retry_exhausted": 0,
        "transient_failed": 0,
        "http_403": 0,
        "rejected_content": 0,
        "bytes_written": 825,
        "remaining": 0,
        "requests_made": 1,
        "interrupted": False,
    }

    async def fake_recover(conn, **kwargs):
        calls.append(list(kwargs["accessions"]))
        return dict(fake_counters)

    monkeypatch.setattr(recover_module, "recover_primary_documents", fake_recover)
    argv = _recover_argv(tmp_path)
    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert len(calls) == 1
    assert "SecretUA" not in out
    report_path = tmp_path / "recovery_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "sec_primary_recovery_report_v1"
    assert report["git_revision"] == HEAD
    assert report["accession_list_sha256"] == RECOVER_ACCESSIONS_HASH
    assert report["succeeded"] == 1
    assert report["already_present"] == 1
    assert report["requested"] == 1
    assert report["frozen_sha256_before"] == report["frozen_sha256_after"]
    assert len(report["derivative_sha256"]) == 64
    assert "SecretUA" not in report_path.read_text(encoding="utf-8")
    assert "secret@example.com" not in report_path.read_text(encoding="utf-8")


def test_recover_sec_primary_secret_safe_errors(tmp_path, monkeypatch, capsys):
    import catalyst_data.sec.recover_primary as recover_module

    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda repo: HEAD)
    monkeypatch.setenv("SEC_USER_AGENT", "M3 SecretUA/1.0 (secret@example.com)")

    async def boom(conn, **kwargs):
        raise ValueError(
            "fetch exploded with sk-abcdef1234567890 and M3 SecretUA/1.0 (secret@example.com)"
        )

    monkeypatch.setattr(recover_module, "recover_primary_documents", boom)
    argv = _recover_argv(tmp_path)
    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "sk-abcdef1234567890" not in out
    assert "SecretUA" not in out
    assert "[REDACTED]" in payload["error"]
    assert not (tmp_path / "recovery_report.json").exists()


def test_prepare_parser_accepts_resume_build_id(tmp_path):
    """L4: prepare accepts --resume-build-id and defaults it to None."""
    module = _load_script()
    argv = _prepare_argv(tmp_path) + ["--resume-build-id", "a" * 64]
    args = module._parse_args(argv)
    assert args.resume_build_id == "a" * 64
    plain = module._parse_args(_prepare_argv(tmp_path))
    assert plain.resume_build_id is None


def _resumable_derivative(
    tmp_path: Path,
    *,
    checkpoint: str | None = None,
    name: str = "resumable.db",
) -> Path:
    """Derivative with a manifest_ready candidate build row and one chunk."""
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
        """INSERT INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 1, ?)""",
        ("f" * 64, "2026-08-23T00:00:00Z"),
    )
    conn.execute(
        """INSERT INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 0, ?)""",
        ("1" * 64, "2026-08-23T00:00:00Z"),
    )
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status,
            manifest_id, document_count, chunk_count, inventory_digest,
            reconciliation_ready, reconciliation_checkpoint, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "a" * 64,
            "7" * 64,
            "{}",
            "manifest_ready",
            "1" * 64,
            1,
            1,
            "9" * 64,
            0,
            checkpoint,
            "2026-08-23T00:00:00Z",
            "2026-08-23T00:00:00Z",
        ),
    )
    conn.execute(
        """INSERT INTO corpus_build_chunks
           (build_id, chunk_id, document_id, chunk_profile_version, section_key,
            ordinal, content_text, content_hash, metadata_hash, source_class,
            available_at, ticker_associations, eligibility, status, boundary_kind,
            body_token_start, body_token_end, body_overlap_tokens,
            prefix_token_count, prefix_truncated, section_parse_degraded,
            source_kind, created_at, updated_at)
           VALUES (?, ?, 'doc:1', 'news_v2', 'body', '0001', 'resume text',
                   ?, ?, 'reported_news', '2026-08-23T00:00:00Z', '["AAPL"]',
                   'eligible', 'active', 'document_end', 0, 1, 0, 0, 0, 0,
                   'article', ?, ?)""",
        (
            "a" * 64,
            "resume:news_v2:body:0001",
            hashlib.sha256(b"resume text").hexdigest(),
            hashlib.sha256(b"resume metadata").hexdigest(),
            "2026-08-23T00:00:00Z",
            "2026-08-23T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()
    return db


def _resume_argv(tmp_path: Path, *, dry_run: bool = False) -> list[str]:
    bench, q005 = _seal_files(tmp_path)
    argv = [
        "prepare",
        "--derivative", str(_resumable_derivative(tmp_path)),
        "--benchmark-manifest", str(bench),
        "--q005-approval", str(q005),
        "--source-bundle-output-root", str(tmp_path / "bundles"),
        "--preparation-evidence", str(tmp_path / "preparation_evidence.json"),
        "--snapshot-id", "7" * 64,
        "--probe-report-id", "8" * 64,
        "--postbuild-readiness-id", "9" * 64,
        "--expected-implementation-head", HEAD,
        "--resume-build-id", "a" * 64,
    ]
    if dry_run:
        argv.append("--dry-run")
    return argv


def _resume_state_snapshot(conn: sqlite3.Connection) -> dict[str, object]:
    build_columns = [
        "build_id",
        "certified_snapshot_identity",
        "header_json",
        "status",
        "manifest_id",
        "manifest_json",
        "document_count",
        "chunk_count",
        "source_utf8_bytes",
        "inventory_digest",
        "lexical_expected_digest",
        "lexical_digest",
        "lexical_row_count",
        "lexical_repair_checkpoint",
        "lexical_repair_cursor",
        "reconciliation_ready",
        "lexical_ready",
        "created_at",
        "updated_at",
        "published_at",
    ]
    build_select = ", ".join(build_columns)
    return {
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "builds": tuple(
            tuple(row) for row in conn.execute(f"SELECT {build_select} FROM corpus_publication_builds")
        ),
        "manifests": tuple(
            tuple(row) for row in conn.execute(
                "SELECT manifest_id, manifest_json, is_current, created_at FROM corpus_manifest"
            )
        ),
        "lexical": tuple(
            tuple(row) for row in conn.execute(
                "SELECT * FROM lexical_index_state"
            )
        ),
        "chunks": conn.execute("SELECT COUNT(*) FROM corpus_build_chunks").fetchone()[0],
        "deltas": conn.execute("SELECT COUNT(*) FROM corpus_build_deltas").fetchone()[0],
        "fts_batches": conn.execute("SELECT COUNT(*) FROM corpus_build_fts_batches").fetchone()[0],
    }


def _forbidden_seam(name: str):
    """Return a function that raises if a forbidden resume seam is invoked."""

    def seam(*a, **k):
        raise AssertionError(f"forbidden resume seam invoked: {name}")

    return seam


def test_prepare_resume_dry_run_allows_additive_columns_but_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    argv = _resume_argv(tmp_path, dry_run=True)
    derivative = Path(argv[2])
    evidence_path = Path(argv[argv.index("--preparation-evidence") + 1])
    conn = sqlite3.connect(derivative)
    conn.row_factory = sqlite3.Row
    before = _resume_state_snapshot(conn)
    conn.close()

    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["ok"] is True
    assert not evidence_path.exists()
    assert not (tmp_path / "bundles").exists()

    conn = sqlite3.connect(derivative)
    conn.row_factory = sqlite3.Row
    after = _resume_state_snapshot(conn)
    assert after == before
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(corpus_publication_builds)")
    }
    for name in (
        "reconciliation_checkpoint",
        "reconciliation_base_manifest_id",
        "reconciliation_base_chunk_count",
        "reconciliation_base_inventory_digest",
    ):
        assert name in columns
    status, checkpoint = conn.execute(
        "SELECT status, reconciliation_checkpoint FROM corpus_publication_builds"
    ).fetchone()
    assert (status, checkpoint) == ("manifest_ready", None)
    conn.close()


def test_prepare_resume_recomputes_audit_and_data01_and_mocks_forbidden_steps(
    tmp_path, monkeypatch, capsys
):
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    argv = _resume_argv(tmp_path)
    derivative = Path(argv[2])
    evidence_path = Path(argv[argv.index("--preparation-evidence") + 1])
    order: list[str] = []

    def record(name):
        def step(*a, **k):
            order.append(name)
            if name == "_step_data01":
                return {
                    "denominator": 5229,
                    "numerator": 5229,
                    "gate_passed": True,
                    "accepted_time_recovered_count": 0,
                    "fail_closed_eligibility_count": 0,
                }
            if name == "_step_audit":
                return {"failures": 0}
            return {"name": name}

        return step

    for name in (
        "_step_derivative_migration",
        "_step_accepted_time",
        "_step_sec_reparse",
        "_step_news_persistence",
        "_step_m35b_backfill",
        "_step_m36_dedup",
        "_step_corpus_candidate",
        "_step_bundle_export",
    ):
        monkeypatch.setattr(module, name, record(name))
    monkeypatch.setattr(module, "_step_audit", record("_step_audit"))
    monkeypatch.setattr(module, "_step_data01", record("_step_data01"))

    forbidden = {
        "_manifest_phase": "catalyst_data.corpus.streaming_publication._manifest_phase",
        "_cutover": "catalyst_data.corpus.streaming_publication._cutover",
        "_stage_document": "catalyst_data.corpus.streaming_publication._stage_document",
        "_stage_source_presence": "catalyst_data.corpus.streaming_publication._stage_source_presence",
        "_build_id": "catalyst_data.corpus.streaming_publication._build_id",
        "build_canonical_corpus_records": "catalyst_data.index_builder.build_canonical_corpus_records",
        "build_fts5_index": "catalyst_data.retrieval.fts5_builder.build_fts5_index",
    }
    for target, dotted in forbidden.items():
        import importlib

        package, _, name = dotted.rpartition(".")
        mod = importlib.import_module(package)
        monkeypatch.setattr(mod, name, _forbidden_seam(target))

    from types import SimpleNamespace

    fake_bundle = SimpleNamespace(
        source_bundle_id="2" * 64,
        chunk_count=1,
        path=tmp_path / "bundles" / "source_2222",
    )

    def fake_bundle_export(*a, **k):
        order.append("export_candidate_source_bundle")
        return ("2" * 64, tmp_path / "bundles" / "source_2222")

    monkeypatch.setattr(
        "catalyst_data.retrieval.source_bundle.export_candidate_source_bundle",
        fake_bundle_export,
    )

    def fake_fts(*a, **k):
        order.append("build_candidate_fts")
        return SimpleNamespace(
            manifest_id="1" * 64, mode_served="fts5", row_count=1, digest="d" * 64
        )

    monkeypatch.setattr(
        "catalyst_data.corpus.streaming_publication.build_candidate_fts", fake_fts
    )

    def fake_verify(bundle_path, **kw):
        order.append("verify_source_bundle")
        return SimpleNamespace(
            source_bundle_id="2" * 64, chunk_count=1, path=Path(bundle_path)
        )

    monkeypatch.setattr(
        "catalyst_data.retrieval.gpu_contract.verify_source_bundle", fake_verify
    )

    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert json.loads(out)["ok"] is True
    assert order == [
        "_step_audit",
        "_step_data01",
        "export_candidate_source_bundle",
        "build_candidate_fts",
        "verify_source_bundle",
    ]
    assert evidence_path.is_file()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == "preparation_evidence_v1"
    assert evidence["resume"]["implementation_head"] == HEAD
    assert evidence["resume"]["build_id"] == "a" * 64
    assert evidence["resume"]["corpus_manifest_id"] == "1" * 64
    assert evidence["resume"]["original_status"] == "manifest_ready"
    assert evidence["resume"]["original_manifest_ready_at"] == "2026-08-23T00:00:00Z"
    assert evidence["resume"]["original_git_revision"] == evidence["git_revision"]
    assert evidence["resume"]["original_git_revision"] != HEAD


def test_prepare_resume_end_to_end_real_bundle_fts_and_verify(
    tmp_path, monkeypatch, capsys
):
    """L4 end-to-end: real recon -> real bundle export -> real FTS -> real verify."""
    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    argv = _resume_argv(tmp_path)
    evidence_path = Path(argv[argv.index("--preparation-evidence") + 1])
    bundle_root = Path(argv[argv.index("--source-bundle-output-root") + 1])

    def audit(conn):
        return {"failures": 0}

    def data01(conn, benchmark, q005, git_revision):
        return {
            "denominator": 5229,
            "numerator": 5229,
            "gate_passed": True,
            "accepted_time_recovered_count": 0,
            "fail_closed_eligibility_count": 0,
        }

    monkeypatch.setattr(module, "_step_audit", audit)
    monkeypatch.setattr(module, "_step_data01", data01)
    for name in (
        "_step_derivative_migration",
        "_step_accepted_time",
        "_step_sec_reparse",
        "_step_news_persistence",
        "_step_m35b_backfill",
        "_step_m36_dedup",
        "_step_corpus_candidate",
        "_step_bundle_export",
    ):
        monkeypatch.setattr(module, name, _forbidden_seam(name))

    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert json.loads(out)["ok"] is True
    assert evidence_path.is_file()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["build_id"] == "a" * 64
    assert evidence["corpus_manifest_id"] == "1" * 64
    assert evidence["chunk_count"] == 1
    assert evidence["reconciliation"] == {
        "to_embed_count": 1,
        "metadata_update_count": 0,
        "tombstone_count": 0,
    }
    bundle_dir = Path(evidence["source_bundle_path"])
    assert bundle_dir.is_dir()
    assert {
        path.name for path in bundle_dir.iterdir() if path.is_file()
    } == {"chunks.jsonl", "source_bundle_manifest.json", "checksums.sha256"}
    assert evidence["source_bundle_id"]
    assert evidence["lexical_digest"]
    # Real verification of the exported bundle succeeded inside _prepare_resume.
    from catalyst_data.retrieval.gpu_contract import verify_source_bundle

    verified = verify_source_bundle(
        bundle_dir,
        expected_source_bundle_id=evidence["source_bundle_id"],
    )
    assert verified.chunk_count == 1
    # The candidate build reached lexical_ready without cutover.
    conn = sqlite3.connect(argv[2])
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, lexical_ready FROM corpus_publication_builds WHERE build_id=?",
        ("a" * 64,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["status"] == "lexical_ready"
    assert row["lexical_ready"] == 1
    # The resume evidence carries the additive resume keys.
    assert evidence["resume"]["original_status"] == "manifest_ready"
    assert evidence["resume"]["original_manifest_ready_at"] == "2026-08-23T00:00:00Z"
    assert evidence["resume"]["original_git_revision"] == evidence["git_revision"]
    assert not (bundle_root / "other").exists()


def test_prepare_resume_signal_sets_operator_interrupt_flag_and_restores(
    tmp_path, monkeypatch, capsys
):
    """SIGINT/SIGTERM during _prepare_resume set the operator flag only."""
    import signal

    from catalyst_data.corpus.streaming_publication import ResumableResourceStop

    module = _load_script()
    monkeypatch.setattr(module, "_git_head", lambda *a, **k: HEAD)
    monkeypatch.setattr(module, "_step_audit", lambda conn: {"failures": 0})
    monkeypatch.setattr(
        module,
        "_step_data01",
        lambda *a, **k: {
            "denominator": 5229,
            "numerator": 5229,
            "gate_passed": True,
            "accepted_time_recovered_count": 0,
            "fail_closed_eligibility_count": 0,
        },
    )
    for name in (
        "_step_derivative_migration",
        "_step_accepted_time",
        "_step_sec_reparse",
        "_step_news_persistence",
        "_step_m35b_backfill",
        "_step_m36_dedup",
        "_step_corpus_candidate",
        "_step_bundle_export",
    ):
        monkeypatch.setattr(module, name, _forbidden_seam(name))

    argv = _resume_argv(tmp_path)
    signal_calls: list[tuple[int, object]] = []

    def fake_signal(signum, handler):
        signal_calls.append((signum, handler))
        return "previous-handler"

    def fake_getsignal(signum):
        return "previous-handler"

    monkeypatch.setattr(module.signal, "signal", fake_signal)
    monkeypatch.setattr(module.signal, "getsignal", fake_getsignal)

    observed: dict[str, object] = {}

    def fake_resume(conn, *, build_id, now=None, failure_injector=None,
                    deadline=900.0, operator_interrupt=None):
        observed["operator_interrupt"] = operator_interrupt
        assert isinstance(operator_interrupt, dict)
        installed = dict(signal_calls)
        handler = installed.get(signal.SIGINT) or installed.get(signal.SIGTERM)
        assert handler is not None
        # The handler must only set the flag and never raise.
        handler(signal.SIGINT, None)
        assert operator_interrupt["operator_interrupt"] is True
        raise ResumableResourceStop("operator_interrupt", "interrupted by operator")

    monkeypatch.setattr(
        "catalyst_data.corpus.streaming_publication.resume_candidate_reconciliation",
        fake_resume,
    )

    rc = module.main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    assert json.loads(out)["ok"] is False
    assert json.loads(out)["error"] == "[operator_interrupt] interrupted by operator"
    assert observed["operator_interrupt"]["operator_interrupt"] is True
    # Handlers must be restored after _prepare_resume raised: the last signal
    # install for each signal is the previous handler.
    for signum in (signal.SIGINT, signal.SIGTERM):
        last = [h for s, h in signal_calls if s == signum][-1]
        assert last == "previous-handler"
    # getSignal reports the previous handler after restore.
    assert module.signal.getsignal(signal.SIGINT) == "previous-handler"
    assert module.signal.getsignal(signal.SIGTERM) == "previous-handler"
