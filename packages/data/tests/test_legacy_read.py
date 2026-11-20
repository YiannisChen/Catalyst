"""Parity tests for reading a migrated legacy web-body cohort."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from catalyst_data.retrieval import legacy_read, web_bodies

ENV = legacy_read.ENV


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stored(value: str) -> str:
    """The migrated store's time format for an old ``...Z`` value (as body_versions stores it)."""
    if not value.endswith("Z") or "." in value:
        return value
    return value[:-1] + ".000000+00:00"


def _fixture(tmp_path, monkeypatch, *, bad_time=False, non_full=False, non_full_index=0):
    old_path = tmp_path / "old.sqlite3"
    migrated_path = tmp_path / "migrated.sqlite3"
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    rows = []
    for asset, ticker, eligible, chunks in (
        ("asset-a", "AAA", "2026-01-01T00:00:00Z", ["a first", "a second", "a third"]),
        ("asset-b", "AAA", "2026-01-01T00:00:00Z", ["b first", "b second"]),
        ("asset-c", "BBB", "2026-01-02T00:00:00Z", ["c first"]),
    ):
        for ordinal, text in enumerate(chunks, 1):
            rows.append({
                "asset_id": asset, "ordinal": ordinal, "chunk_id": f"{asset}:chunk:{ordinal}",
                "text": text, "content_hash": _sha(text.encode()), "body_sha256": asset[6:7] * 64,
                "canonical_url": f"https://example.com/{asset}", "title": asset, "publisher": "Example",
                "source_class": "reported_news", "eligible_at": eligible,
                "fetched_at": "2026-01-03T12:00:00Z", "capture_mode": "LIVE_REFETCH", "ticker": ticker,
            })
    if bad_time:
        rows[0]["eligible_at"] = "2026-01-01T00:00:00.000001Z"
    with sqlite3.connect(old_path) as conn:
        conn.executescript(web_bodies.SCHEMA)
        for row in rows:
            conn.execute("INSERT INTO web_body_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(
                row[k] for k in ("asset_id", "ordinal", "chunk_id", "text", "content_hash", "body_sha256",
                                 "canonical_url", "title", "publisher", "source_class", "eligible_at",
                                 "fetched_at", "capture_mode")))
            conn.execute("INSERT OR IGNORE INTO web_headlines VALUES (?,?,?)",
                         (row["asset_id"], row["ticker"], row["eligible_at"]))
    with sqlite3.connect(migrated_path) as conn:
        conn.executescript("""
            CREATE TABLE body_versions (version_id TEXT, source_id TEXT, document_id TEXT, title TEXT, body TEXT,
                body_sha256 TEXT, observed_at TEXT, published_at TEXT, provider_updated_at TEXT, provenance TEXT,
                legacy_cohort TEXT, legacy_eligible_at TEXT);
            CREATE TABLE legacy_version_metadata (version_id TEXT, cohort_id TEXT, cohort_manifest_sha256 TEXT,
                membership_sha256 TEXT, source_snapshot_sha256 TEXT, source_store_id TEXT, source_id TEXT,
                document_id TEXT, asset_id TEXT, chunk_id TEXT, ordinal INTEGER, representation TEXT,
                original_body_sha256 TEXT, eligibility_rule_id TEXT, eligibility_rule_version TEXT,
                correction_bindings_json TEXT);
            CREATE TABLE temporal_evidence_map (version_id TEXT, ticker TEXT, eligible_at TEXT,
                canonical_url TEXT, publisher TEXT, source_class TEXT, time_flags_json TEXT);
        """)
        for i, row in enumerate(rows):
            version = f"version-{i}"
            conn.execute("INSERT INTO body_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                version, "source", row["asset_id"], row["title"], row["text"], _sha(row["text"].encode()),
                row["fetched_at"], row["eligible_at"], None, "fixture", "cohort-1", _stored(row["eligible_at"])))
            conn.execute("INSERT INTO legacy_version_metadata VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                version, "cohort-1", "pending", "pending", "snapshot", "store", "source", row["asset_id"],
                row["asset_id"], row["chunk_id"], row["ordinal"], "chunk", row["body_sha256"], "rule", "1", "{}"))
            conn.execute("INSERT INTO temporal_evidence_map VALUES (?,?,?,?,?,?,?)", (
                version, row["ticker"], _stored(row["eligible_at"]), row["canonical_url"], row["publisher"],
                row["source_class"], "[]"))
    catalog_bytes = b'{"fixture":true}\n'
    membership = []
    for i, row in enumerate(rows):
        attrs = {"content_state": "TITLE_ONLY" if non_full and i == non_full_index else "FULL_TEXT",
                 "capture_mode": row["capture_mode"], "fetched_at": row["fetched_at"]}
        membership.append({"chunk_id": row["chunk_id"], "asset_id": row["asset_id"], "kind": "body_chunk",
                           "evidence_role": "body", "legacy_attributes": attrs})
    membership_bytes = b"".join((json.dumps(item, sort_keys=True) + "\n").encode() for item in membership)
    (catalog_dir / "catalog.json").write_bytes(catalog_bytes)
    (catalog_dir / "membership.jsonl").write_bytes(membership_bytes)
    with sqlite3.connect(migrated_path) as conn:
        conn.execute("UPDATE legacy_version_metadata SET cohort_manifest_sha256=?, membership_sha256=?",
                     (_sha(catalog_bytes), _sha(membership_bytes)))
    monkeypatch.setenv(ENV[0], str(migrated_path))
    monkeypatch.setenv(ENV[1], str(catalog_dir))
    monkeypatch.setenv(ENV[2], "cohort-1")
    monkeypatch.setenv(ENV[3], _sha(migrated_path.read_bytes()))
    return old_path, migrated_path, catalog_dir, rows


def _old_chunks(path, ids, maximum):
    unique = list(dict.fromkeys(ids))
    out = {}
    with sqlite3.connect(path) as conn:
        cols = [column[1] for column in conn.execute("PRAGMA table_info(web_body_chunks)")]
        for start in range(0, len(unique), 400):
            batch = unique[start:start + 400]
            marks = ",".join("?" for _ in batch)
            for row in conn.execute(f"SELECT * FROM web_body_chunks WHERE asset_id IN ({marks}) "
                                    "AND ordinal <= ? ORDER BY asset_id, ordinal", (*batch, maximum)):
                out.setdefault(row[0], []).append(dict(zip(cols, row)))
    return out


def test_reconstructed_chunks_match_old_rows_and_order(tmp_path, monkeypatch):
    old, _, _, _ = _fixture(tmp_path, monkeypatch)
    cohort = legacy_read.configured_reader()
    ids = ["asset-b", "asset-a", "asset-a", "missing"]
    assert cohort.chunks_for_assets(ids, 2) == _old_chunks(old, ids, 2)
    assert list(cohort.chunks_for_assets(ids, 2)) == ["asset-a", "asset-b"]
    assert list(cohort.chunks_for_assets(["asset-a"], 3)["asset-a"][0]) == [
        "asset_id", "ordinal", "chunk_id", "text", "content_hash", "body_sha256", "canonical_url", "title",
        "publisher", "source_class", "eligible_at", "fetched_at", "capture_mode"]


@pytest.mark.parametrize("include_start", [False, True])
def test_session_assets_and_bodies_match_old_sql(tmp_path, monkeypatch, include_start):
    old, _, _, _ = _fixture(tmp_path, monkeypatch)
    start, cutoff = "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
    with sqlite3.connect(old) as conn:
        assets = [r[0] for r in conn.execute(
            "SELECT asset_id, MAX(eligible_at) AS t FROM web_headlines WHERE ticker IN (?) "
            f"AND eligible_at {'>=' if include_start else '>'} ? AND eligible_at <= ? "
            "GROUP BY asset_id ORDER BY t DESC", ("AAA", start, cutoff))]
    cohort = legacy_read.configured_reader()
    assert cohort.session_assets(["AAA"], start, cutoff, include_window_start=include_start) == assets
    assert web_bodies.session_bodies(["AAA"], start, cutoff, include_window_start=include_start) == _old_chunks(old, assets, 2)


def test_window_rows_match_old_sql_inclusive_bounds_and_ordinal_text(tmp_path, monkeypatch):
    old, _, _, _ = _fixture(tmp_path, monkeypatch)
    bounds = ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
    with sqlite3.connect(old) as conn:
        expected = conn.execute("SELECT asset_id, title, publisher, canonical_url, source_class, eligible_at, "
            "group_concat(text, ' ') FROM (SELECT * FROM web_body_chunks WHERE eligible_at BETWEEN ? AND ? "
            "ORDER BY asset_id, ordinal) GROUP BY asset_id", bounds).fetchall()
    assert legacy_read.configured_reader().window_asset_rows(*bounds) == expected


def test_partial_environment_and_hash_mismatches_raise(tmp_path, monkeypatch):
    _, migrated, catalog, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.delenv(ENV[3])
    with pytest.raises(legacy_read.LegacyReadError):
        legacy_read.configured_reader()
    with pytest.raises(legacy_read.LegacyReadError):
        legacy_read.open_cohort(migrated, catalog, "cohort-1", store_sha256="0" * 64)
    (catalog / "catalog.json").write_bytes(b"{}\n")
    with pytest.raises(legacy_read.LegacyReadError):
        legacy_read.open_cohort(migrated, catalog, "cohort-1", store_sha256=_sha(migrated.read_bytes()))


def test_non_integer_second_timestamp_raises_at_open(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch, bad_time=True)
    with pytest.raises(legacy_read.LegacyReadError, match="whole-second"):
        legacy_read.configured_reader()


@pytest.mark.parametrize("state", ["TITLE_ONLY", "METADATA_ONLY"])
def test_a_whole_non_full_text_asset_is_skipped_in_every_read(tmp_path, monkeypatch, state):
    _, migrated, catalog, _ = _fixture(tmp_path, monkeypatch)
    lines = [json.loads(line) for line in (catalog / "membership.jsonl").read_text().splitlines()]
    lines[5]["legacy_attributes"]["content_state"] = state  # asset-c, its only chunk
    data = b"".join((json.dumps(item, sort_keys=True) + "\n").encode() for item in lines)
    (catalog / "membership.jsonl").write_bytes(data)
    with sqlite3.connect(migrated) as conn:
        conn.execute("UPDATE legacy_version_metadata SET membership_sha256=?", (_sha(data),))
    _rehash(monkeypatch, migrated)
    cohort = legacy_read.configured_reader()
    assert cohort.window_asset_rows("2026-01-01T12:00:00Z", "2026-01-02T12:00:00Z") == []
    assert cohort.session_assets(["BBB"], "2026-01-01T12:00:00Z", "2026-01-02T12:00:00Z",
                                 include_window_start=False) == ["asset-c"]
    assert cohort.chunks_for_assets(["asset-c"], 40) == {}
    assert cohort.titles_for_chunks(["asset-c:chunk:1"]) == {}


def test_a_whole_non_full_text_asset_is_skipped_and_counted(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch, non_full=True, non_full_index=5)  # asset-c has one chunk
    cohort = legacy_read.configured_reader()
    assert "asset-c" not in cohort.chunks_for_assets(["asset-c"], 40)
    assert cohort.window_asset_rows("2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z") == []
    assert cohort.titles_for_chunks(["asset-c:chunk:1"]) == {}
    assert cohort.audit()["skipped_non_full_text"] == 1


def test_an_asset_mixing_full_text_and_other_chunks_fails_at_open(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch, non_full=True, non_full_index=0)
    with pytest.raises(legacy_read.LegacyReadError, match="mixes"):
        legacy_read.configured_reader()


def test_titles_for_chunks_match_old_sql(tmp_path, monkeypatch):
    old, _, _, _ = _fixture(tmp_path, monkeypatch)
    ids = ["asset-a:chunk:2", "missing", "asset-c:chunk:1", "asset-a:chunk:2"]
    with sqlite3.connect(old) as conn:
        expected = dict(conn.execute(
            "SELECT chunk_id, title FROM web_body_chunks WHERE chunk_id IN (?,?,?,?)", ids).fetchall())
    assert legacy_read.configured_reader().titles_for_chunks(ids) == expected


def test_window_rows_reject_inconsistent_grouped_columns_and_bad_bounds(tmp_path, monkeypatch):
    _, migrated, _, _ = _fixture(tmp_path, monkeypatch)
    with sqlite3.connect(migrated) as conn:
        conn.execute("UPDATE body_versions SET title='other' WHERE version_id='version-1'")
    monkeypatch.setenv(ENV[3], _sha(migrated.read_bytes()))
    cohort = legacy_read.configured_reader()
    with pytest.raises(legacy_read.LegacyReadError, match="inconsistent grouped columns"):
        cohort.window_asset_rows("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    with pytest.raises(legacy_read.LegacyReadError, match="window start must be"):
        cohort.window_asset_rows("2026-01-01", "2026-01-02T00:00:00Z")


def test_session_query_plan_matches_the_old_store(tmp_path, monkeypatch):
    old, _, _, _ = _fixture(tmp_path, monkeypatch)
    sql = legacy_read.SESSION_SQL.format(marks="?", op=">")
    with sqlite3.connect(old) as conn:
        expected = legacy_read.query_plan(conn, sql, ("AAA", "", ""))
    assert legacy_read.configured_reader().session_query_plan(["AAA"], include_window_start=False) == expected


def _rehash(monkeypatch, migrated):
    monkeypatch.setenv(ENV[3], _sha(migrated.read_bytes()))


@pytest.mark.parametrize("stored", ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00", "2026-01-01 00:00:00.000000+00:00"])
def test_any_time_outside_the_migrated_format_fails_at_open(tmp_path, monkeypatch, stored):
    _, migrated, _, _ = _fixture(tmp_path, monkeypatch)
    with sqlite3.connect(migrated) as conn:
        conn.execute("UPDATE body_versions SET legacy_eligible_at=? WHERE version_id='version-0'", (stored,))
    _rehash(monkeypatch, migrated)
    with pytest.raises(legacy_read.LegacyReadError, match="migrated format"):
        legacy_read.configured_reader()


@pytest.mark.parametrize("column, value, message", [
    ("capture_mode", None, "capture_mode"),
    ("content_state", None, "content_state"),
    ("content_state", "full_text", "is not one of"),
    ("content_state", "FULL TEXT", "is not one of"),
    ("content_state", "FULL_TEXT ", "is not one of"),
])
def test_missing_or_misspelled_catalog_attributes_fail_at_open(tmp_path, monkeypatch, column, value, message):
    _, migrated, catalog, _ = _fixture(tmp_path, monkeypatch)
    lines = [json.loads(line) for line in (catalog / "membership.jsonl").read_text().splitlines()]
    lines[0]["legacy_attributes"][column] = value
    data = b"".join((json.dumps(item, sort_keys=True) + "\n").encode() for item in lines)
    (catalog / "membership.jsonl").write_bytes(data)
    with sqlite3.connect(migrated) as conn:
        conn.execute("UPDATE legacy_version_metadata SET membership_sha256=?", (_sha(data),))
    _rehash(monkeypatch, migrated)
    with pytest.raises(legacy_read.LegacyReadError, match=message):
        legacy_read.configured_reader()


def test_missing_store_column_and_duplicate_position_fail_at_open(tmp_path, monkeypatch):
    _, migrated, _, _ = _fixture(tmp_path, monkeypatch)
    with sqlite3.connect(migrated) as conn:
        conn.execute("UPDATE legacy_version_metadata SET original_body_sha256=NULL WHERE version_id='version-0'")
    _rehash(monkeypatch, migrated)
    with pytest.raises(legacy_read.LegacyReadError, match="body_sha256"):
        legacy_read.configured_reader()
    with sqlite3.connect(migrated) as conn:
        conn.execute("UPDATE legacy_version_metadata SET original_body_sha256='a' WHERE version_id='version-0'")
        conn.execute("UPDATE legacy_version_metadata SET ordinal=1 WHERE version_id='version-1'")
    _rehash(monkeypatch, migrated)
    with pytest.raises(legacy_read.LegacyReadError, match="duplicate"):
        legacy_read.configured_reader()


def test_catalog_and_store_must_name_the_same_chunks(tmp_path, monkeypatch):
    _, migrated, _, _ = _fixture(tmp_path, monkeypatch)
    with sqlite3.connect(migrated) as conn:
        conn.execute("DELETE FROM legacy_version_metadata WHERE version_id='version-5'")
    _rehash(monkeypatch, migrated)
    with pytest.raises(legacy_read.LegacyReadError, match="different chunks"):
        legacy_read.configured_reader()


def test_ties_keep_the_old_order_when_old_insertion_order_differs(tmp_path, monkeypatch):
    old, _, _, _ = _fixture(tmp_path, monkeypatch)
    # Re-create the old headline table with the tied assets inserted in reverse id order.
    with sqlite3.connect(old) as conn:
        rows = conn.execute("SELECT * FROM web_headlines ORDER BY asset_id DESC").fetchall()
        conn.execute("DELETE FROM web_headlines")
        conn.executemany("INSERT INTO web_headlines VALUES (?,?,?)", rows)
        sql = legacy_read.SESSION_SQL.format(marks="?,?", op=">=")
        args = ("AAA", "BBB", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        expected = [r[0] for r in conn.execute(sql, args)]
    got = legacy_read.configured_reader().session_assets(["AAA", "BBB"], *args[2:], include_window_start=True)
    assert got == expected and len(got) == 3


def test_partial_window_asset_and_session_dict_order(tmp_path, monkeypatch):
    old, migrated, _, _ = _fixture(tmp_path, monkeypatch)
    for path, value in ((old, "2026-01-01T06:00:00Z"), (migrated, "2026-01-01T06:00:00.000000+00:00")):
        with sqlite3.connect(path) as conn:
            if path == old:
                conn.execute("UPDATE web_body_chunks SET eligible_at=? WHERE chunk_id='asset-a:chunk:3'", (value,))
            else:
                conn.execute("UPDATE body_versions SET legacy_eligible_at=? WHERE version_id='version-2'", (value,))
    _rehash(monkeypatch, migrated)
    bounds = ("2026-01-01T00:00:00Z", "2026-01-01T05:00:00Z")
    with sqlite3.connect(old) as conn:
        expected = conn.execute("SELECT asset_id, title, publisher, canonical_url, source_class, eligible_at, "
            "group_concat(text, ' ') FROM (SELECT * FROM web_body_chunks WHERE eligible_at BETWEEN ? AND ? "
            "ORDER BY asset_id, ordinal) GROUP BY asset_id", bounds).fetchall()
    assert legacy_read.configured_reader().window_asset_rows(*bounds) == expected
    start, cutoff = "2025-12-31T00:00:00Z", "2026-01-02T00:00:00Z"
    got = web_bodies.session_bodies(["AAA"], start, cutoff)
    with sqlite3.connect(old) as conn:
        assets = [r[0] for r in conn.execute(legacy_read.SESSION_SQL.format(marks="?", op=">"), ("AAA", start, cutoff))]
    assert list(got) == assets


def test_batches_of_400_keep_the_old_order(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    ids = [f"missing-{i:03d}" for i in range(399)] + ["asset-b", "asset-a"]
    assert list(legacy_read.configured_reader().chunks_for_assets(ids, 1)) == ["asset-b", "asset-a"]


def test_concurrent_reads_share_one_cohort(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    _fixture(tmp_path, monkeypatch)
    legacy_read._CACHE.clear()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: web_bodies.session_bodies(
            ["AAA"], "2025-12-31T00:00:00Z", "2026-01-02T00:00:00Z"), range(8)))
    assert all(r == results[0] for r in results) and results[0]
    assert len(legacy_read._CACHE) == 1


def test_session_query_plan_matches_the_old_store_with_window_start_included(tmp_path, monkeypatch):
    old, _, _, _ = _fixture(tmp_path, monkeypatch)
    sql = legacy_read.SESSION_SQL.format(marks="?,?", op=">=")
    with sqlite3.connect(old) as conn:
        expected = legacy_read.query_plan(conn, sql, ("AAA", "BBB", "", ""))
    assert legacy_read.configured_reader().session_query_plan(["AAA", "BBB"], include_window_start=True) == expected


def test_a_calendar_impossible_time_fails_at_open(tmp_path, monkeypatch):
    _, migrated, _, _ = _fixture(tmp_path, monkeypatch)
    with sqlite3.connect(migrated) as conn:
        conn.execute("UPDATE body_versions SET legacy_eligible_at='2026-02-31T09:00:00.000000+00:00' "
                     "WHERE version_id='version-0'")
    _rehash(monkeypatch, migrated)
    with pytest.raises(legacy_read.LegacyReadError, match="calendar"):
        legacy_read.configured_reader()


def test_ordinal_gaps_fail_at_open(tmp_path, monkeypatch):
    _, migrated, _, _ = _fixture(tmp_path, monkeypatch)
    with sqlite3.connect(migrated) as conn:
        conn.execute("UPDATE legacy_version_metadata SET ordinal=4 WHERE version_id='version-2'")
    _rehash(monkeypatch, migrated)
    with pytest.raises(legacy_read.LegacyReadError, match="1..n"):
        legacy_read.configured_reader()


def test_a_non_empty_wal_sidecar_fails(tmp_path, monkeypatch):
    _, migrated, _, _ = _fixture(tmp_path, monkeypatch)
    Path(str(migrated) + "-wal").write_bytes(b"x")
    with pytest.raises(legacy_read.LegacyReadError, match="WAL"):
        legacy_read.configured_reader()


def test_name_map_is_found_beside_the_configured_store_even_without_the_store(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "sec_company_tickers.json").write_text(json.dumps({"0": {"ticker": "NVDA", "title": "NVIDIA CORP"}}))
    monkeypatch.setenv(web_bodies.ENV_VAR, str(root / "news_bodies_v2.sqlite3"))  # not created
    assert web_bodies.store_path() is None
    assert web_bodies.name_map_path() == root / "sec_company_tickers.json"
    assert web_bodies.subject_matcher("NVDA")("Nvidia unveils a new chip")
    monkeypatch.delenv(web_bodies.ENV_VAR)
    assert web_bodies.name_map_path() is None


@pytest.mark.parametrize("update, message", [
    ("UPDATE legacy_version_metadata SET version_id='version-0' WHERE version_id='version-1'", "more than one chunk"),
    ("UPDATE legacy_version_metadata SET ordinal=0 WHERE version_id='version-0'", "positive integer"),
])
def test_shared_versions_and_non_positive_ordinals_fail_at_open(tmp_path, monkeypatch, update, message):
    _, migrated, _, _ = _fixture(tmp_path, monkeypatch)
    with sqlite3.connect(migrated) as conn:
        conn.execute(update)
    _rehash(monkeypatch, migrated)
    with pytest.raises(legacy_read.LegacyReadError, match=message):
        legacy_read.configured_reader()
