import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from catalyst_data.retrieval import web_bodies
from catalyst_data.storage import temporal_evidence
from catalyst_data.storage.legacy_cohorts import LegacyCohort, LegacyRecord
from catalyst_data.storage.legacy_import import migrate_legacy_chunks

CAPTURED = "2026-10-26T16:30:00.000000+00:00"
ELIGIBLE = "2026-03-02T20:00:00.000000+00:00"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(path, chunks=None, *, tickers=("ACME",)):
    chunks = chunks or [("chunk-1", 0, "first original paragraph"),
                        ("chunk-2", 1, "second original paragraph")]
    conn = sqlite3.connect(path)
    conn.executescript(web_bodies.SCHEMA)
    for chunk_id, ordinal, text in chunks:
        conn.execute(
            "INSERT INTO web_body_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("asset-1", ordinal, chunk_id, text, _sha(text), "b" * 64,
             "https://example.test/article", "Original article title", "Example News",
             "reported_news", ELIGIBLE, "2026-03-02T20:05:00Z", "LIVE_REFETCH"),
        )
    for ticker in tickers:
        conn.execute("INSERT INTO web_headlines VALUES (?,?,?)", ("asset-1", ticker, ELIGIBLE))
    conn.commit()
    return conn


def _record(chunk_id, ordinal, text, *, document_id=None, evidence_id="evidence:article",
            ticker_list=("ACME",), evidence_role="evidence", representation="legacy_chunk",
            asset_id="asset-1", eligible_at=ELIGIBLE, original_body_sha256="b" * 64):
    return LegacyRecord(
        source_id="daily:legacy", document_id=document_id or f"document:{chunk_id}",
        evidence_id=evidence_id, kind="body", tickers=tuple(ticker_list),
        evidence_role=evidence_role, eligible_at=eligible_at, representation=representation,
        text_sha256=_sha(text), title_sha256=_sha("Original article title"),
        asset_id=asset_id, chunk_id=chunk_id, ordinal=ordinal,
        original_body_sha256=original_body_sha256,
    )


def _cohort(records, source, *, captured=CAPTURED, correction_bindings=()):
    source_path = Path(source.execute("PRAGMA database_list").fetchone()[2])
    return LegacyCohort(
        cohort_id="daily-original-v1", manifest_sha256="1" * 64,
        input_manifest_sha256="2" * 64, source_store_id="daily-snapshot",
        source_snapshot_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(), snapshot_captured_at=captured,
        source_tables=("web_body_chunks", "web_headlines"),
        eligibility_rule_id="daily-original-eligibility",
        eligibility_rule_version="v1", membership_sha256="4" * 64,
        records=tuple(records), correction_bindings=tuple(correction_bindings),
    )


def _default_records():
    return (
        _record("chunk-1", 0, "first original paragraph", evidence_role="evidence"),
        _record("chunk-2", 1, "second original paragraph", evidence_role="lead"),
    )


def test_import_keeps_original_chunks_as_individual_legacy_versions_and_roles(tmp_path):
    source = _source(tmp_path / "source.sqlite3")
    destination = sqlite3.connect(tmp_path / "destination.sqlite3")
    cohort = _cohort(_default_records(), source,
                     correction_bindings=(("corrections-2026", "5" * 64, "daily-snapshot"),))
    source_path = Path(source.execute("PRAGMA database_list").fetchone()[2])
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_query_only = source.execute("PRAGMA query_only").fetchone()[0]
    assert not source.in_transaction

    report = migrate_legacy_chunks(source, destination, cohort=cohort)

    rows = destination.execute(
        "SELECT source_id,document_id,title,body,observed_at,published_at,provider_updated_at,"
        "provenance,legacy_cohort,legacy_eligible_at FROM body_versions ORDER BY document_id"
    ).fetchall()
    assert [row[1] for row in rows] == ["document:chunk-1", "document:chunk-2"]
    assert [row[3] for row in rows] == ["first original paragraph", "second original paragraph"]
    assert all(row[0] == "daily:legacy" and row[2] == "Original article title" for row in rows)
    assert all(row[4] == CAPTURED and row[5] is None and row[6] is None for row in rows)
    assert all(row[7:10] == ("legacy", "daily-original-v1", ELIGIBLE) for row in rows)
    assert destination.execute(
        "SELECT m.ticker,m.kind,m.evidence_role,m.association_observed_at,m.eligible_at "
        "FROM temporal_evidence_map m JOIN body_versions v USING(version_id) ORDER BY v.document_id"
    ).fetchall() == [("ACME", "body", "evidence", CAPTURED, ELIGIBLE),
                     ("ACME", "body", "lead", CAPTURED, ELIGIBLE)]
    projections = destination.execute(
        "SELECT source_id,document_id,title,text,provenance FROM evidence_text_projection"
    ).fetchall()
    assert projections == [("daily:legacy", "document:chunk-1", "Original article title",
                            "first original paragraph", "legacy")]
    metadata = destination.execute(
        "SELECT cohort_manifest_sha256,membership_sha256,source_snapshot_sha256,source_id,document_id,"
        "asset_id,chunk_id,ordinal,representation,original_body_sha256,eligibility_rule_id,"
        "eligibility_rule_version,correction_bindings_json FROM legacy_version_metadata ORDER BY ordinal"
    ).fetchall()
    assert [row[4:10] for row in metadata] == [
        ("document:chunk-1", "asset-1", "chunk-1", 0, "legacy_chunk", "b" * 64),
        ("document:chunk-2", "asset-1", "chunk-2", 1, "legacy_chunk", "b" * 64),
    ]
    source_sha = hashlib.sha256(Path(source.execute("PRAGMA database_list").fetchone()[2]).read_bytes()).hexdigest()
    assert all(row[0:3] == ("1" * 64, "4" * 64, source_sha) for row in metadata)
    assert all(row[10:12] == ("daily-original-eligibility", "v1") for row in metadata)
    assert all(json.loads(row[12]) == [["corrections-2026", "5" * 64, "daily-snapshot"]]
               for row in metadata)
    assert report["inserted_versions"] == 2
    assert report["projected_evidence_chunks"] == 1
    assert source.execute("SELECT COUNT(*) FROM web_body_chunks").fetchone()[0] == 2
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_sha
    assert source.execute("PRAGMA query_only").fetchone()[0] == source_query_only
    assert not source.in_transaction
    assert destination.in_transaction  # caller owns the destination commit


@pytest.mark.parametrize("mutation", [
    lambda row: replace(row, chunk_id="wrong-chunk"),
    lambda row: replace(row, text_sha256="a" * 64),
    lambda row: replace(row, title_sha256="a" * 64),
    lambda row: replace(row, original_body_sha256="a" * 64),
    lambda row: replace(row, eligible_at="2026-03-02T21:00:00.000000+00:00"),
    lambda row: replace(row, tickers=("OTHER",)),
    lambda row: replace(row, asset_id="other-asset"),
])
def test_import_rejects_any_catalog_member_that_does_not_match_its_source_row(tmp_path, mutation):
    source = _source(tmp_path / "source.sqlite3")
    destination = sqlite3.connect(tmp_path / "destination.sqlite3")
    row = _default_records()[0]
    invalid = mutation(row)

    with pytest.raises(ValueError, match="legacy|source|cohort|chunk|ticker|identity"):
        migrate_legacy_chunks(source, destination, cohort=_cohort((invalid,), source))

    assert not destination.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_versions'"
    ).fetchone()


def test_import_rejects_missing_source_chunk_and_unlisted_tickers(tmp_path):
    source = _source(tmp_path / "source.sqlite3", chunks=[("chunk-1", 0, "first original paragraph")])
    destination = sqlite3.connect(tmp_path / "destination.sqlite3")
    missing = _record("absent", 0, "first original paragraph")
    with pytest.raises(ValueError, match="source|chunk|legacy"):
        migrate_legacy_chunks(source, destination, cohort=_cohort((missing,), source))

    duplicate = replace(_default_records()[0], document_id="document:duplicate",
                        evidence_id="evidence:duplicate")
    with pytest.raises(ValueError, match="duplicate|source|chunk|legacy"):
        migrate_legacy_chunks(source, destination, cohort=_cohort((_default_records()[0], duplicate), source))

    source.close()
    source = _source(tmp_path / "source-2.sqlite3", tickers=("ACME", "OTHER"))
    with pytest.raises(ValueError, match="ticker|association|legacy"):
        migrate_legacy_chunks(source, destination, cohort=_cohort((_default_records()[0],), source))
    assert not destination.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_versions'"
    ).fetchone()


def test_raw_full_body_is_outside_chunk_import_path(tmp_path):
    source = _source(tmp_path / "source.sqlite3")
    destination = sqlite3.connect(tmp_path / "destination.sqlite3")
    raw = replace(_default_records()[0], representation="raw_full_body", asset_id=None,
                  chunk_id=None, ordinal=None, original_body_sha256=None)

    with pytest.raises(ValueError, match="legacy_chunk|representation"):
        migrate_legacy_chunks(source, destination, cohort=_cohort((raw,), source))


def test_same_cohort_replay_preserves_original_observation_and_is_idempotent(tmp_path):
    source = _source(tmp_path / "source.sqlite3")
    destination = sqlite3.connect(tmp_path / "destination.sqlite3")
    cohort = _cohort(_default_records(), source)

    first = migrate_legacy_chunks(source, destination, cohort=cohort)
    before = destination.execute("SELECT version_id,observed_at FROM body_versions ORDER BY version_id").fetchall()
    second = migrate_legacy_chunks(source, destination, cohort=cohort)

    assert first["inserted_versions"] == 2
    assert second["inserted_versions"] == 0 and second["existing_versions"] == 2
    assert destination.execute("SELECT version_id,observed_at FROM body_versions ORDER BY version_id").fetchall() == before
    assert destination.execute("SELECT COUNT(*) FROM legacy_version_metadata").fetchone()[0] == 2
    assert destination.execute("SELECT COUNT(*) FROM evidence_text_projection").fetchone()[0] == 1


def test_failure_rolls_back_all_import_rows_and_preserves_caller_transaction(tmp_path):
    source = _source(tmp_path / "source.sqlite3")
    destination = sqlite3.connect(tmp_path / "destination.sqlite3")
    destination.execute("CREATE TABLE caller_work (value TEXT)")
    destination.execute("INSERT INTO caller_work VALUES ('keep')")
    rows = list(_default_records())
    rows[1] = replace(rows[1], text_sha256="a" * 64)

    with pytest.raises(ValueError):
        migrate_legacy_chunks(source, destination, cohort=_cohort(rows, source))

    assert destination.in_transaction
    assert destination.execute("SELECT value FROM caller_work").fetchone() == ("keep",)
    assert not destination.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_versions'"
    ).fetchone()


def test_source_and_destination_must_be_distinct_connections_and_files(tmp_path):
    path = tmp_path / "same.sqlite3"
    source = _source(path)
    with pytest.raises(ValueError, match="distinct|separate|same"):
        migrate_legacy_chunks(source, source, cohort=_cohort(_default_records(), source))

    second_connection = sqlite3.connect(path)
    with pytest.raises(ValueError, match="distinct|separate|same"):
        migrate_legacy_chunks(source, second_connection, cohort=_cohort(_default_records(), source))


def test_source_and_destination_hardlink_alias_is_rejected_before_destination_write(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    source = _source(source_path)
    alias_path = tmp_path / "destination-alias.sqlite3"
    alias_path.hardlink_to(source_path)
    destination = sqlite3.connect(alias_path)
    cohort = _cohort(_default_records(), source)

    with pytest.raises(ValueError, match="separate|same|distinct"):
        migrate_legacy_chunks(source, destination, cohort=cohort)

    assert not source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_versions'"
    ).fetchone()


def test_import_requires_both_legacy_source_tables(tmp_path):
    source = sqlite3.connect(tmp_path / "source.sqlite3")
    source.execute("CREATE TABLE web_body_chunks (asset_id TEXT)")
    destination = sqlite3.connect(tmp_path / "destination.sqlite3")
    with pytest.raises(ValueError, match="web_headlines|source table|legacy"):
        migrate_legacy_chunks(source, destination, cohort=_cohort(_default_records(), source))


def test_import_requires_exact_source_file_pin_and_refuses_uncommitted_source(tmp_path):
    source = _source(tmp_path / "source.sqlite3")
    destination = sqlite3.connect(tmp_path / "destination.sqlite3")
    cohort = _cohort(_default_records(), source)

    with pytest.raises(ValueError, match="snapshot SHA"):
        migrate_legacy_chunks(source, destination,
                             cohort=replace(cohort, source_snapshot_sha256="0" * 64))
    assert not destination.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_versions'"
    ).fetchone()

    source.execute("UPDATE web_headlines SET ticker='OTHER' WHERE asset_id='asset-1'")
    with pytest.raises(ValueError, match="uncommitted"):
        migrate_legacy_chunks(source, destination, cohort=cohort)
    source.rollback()


def test_mid_batch_failure_rolls_back_import_but_preserves_caller_work(tmp_path, monkeypatch):
    source = _source(tmp_path / "source.sqlite3")
    destination = sqlite3.connect(tmp_path / "destination.sqlite3")
    destination.execute("CREATE TABLE caller_work (value TEXT)")
    destination.execute("INSERT INTO caller_work VALUES ('keep')")
    original = temporal_evidence.register_version
    calls = 0

    def fail_second_registration(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected registration failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(temporal_evidence, "register_version", fail_second_registration)
    with pytest.raises(RuntimeError, match="injected"):
        migrate_legacy_chunks(source, destination, cohort=_cohort(_default_records(), source))

    assert destination.in_transaction
    assert destination.execute("SELECT value FROM caller_work").fetchone() == ("keep",)
    for table in ("body_versions", "legacy_version_metadata", "temporal_evidence_map",
                  "evidence_text_projection"):
        assert not destination.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
