"""WP-B B7a §2.1: building, verifying and publishing the served projection."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from catalyst_data.retrieval import served_projection as sp
from catalyst_data.retrieval.result import RetrievalContractError
from catalyst_data.retrieval.served_projection import (
    FTS_MAP_TABLE,
    PROJECTION_TABLE,
    SCHEMA_VERSION,
    build_served_projection,
    open_or_build_served_projection,
)
from catalyst_data.storage.connect import make_readonly_factory
from served_projection_fixtures import (
    BUILD,
    MANIFEST,
    chunk,
    make_served_db,
    old_build_chunks,
    rich_chunks,
)


def _build(db: Path, out: Path, *, factory=None):
    out.mkdir(exist_ok=True)
    return build_served_projection(
        factory or make_readonly_factory(db),
        expected_manifest_id=MANIFEST,
        projection_dir=out,
    )


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return make_served_db(tmp_path / "served.db", rich_chunks(), old_chunks=old_build_chunks())


def _rw(db: Path, sql: str, params=()):
    conn = sqlite3.connect(db)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def test_build_publishes_a_verified_sidecar(db, tmp_path):
    handle = _build(db, tmp_path / "p")
    assert handle.path.name == f"served_projection_{handle.projection_sha256[:16]}.db"
    assert [p.name for p in (tmp_path / "p").iterdir()] == [handle.path.name]
    assert handle.build_id == BUILD
    assert handle.manifest_id == MANIFEST
    assert handle.meta["schema_version"] == SCHEMA_VERSION
    assert handle.meta["row_count"] == len(rich_chunks())
    conn = handle.connect()
    try:
        assert conn.execute(f"SELECT COUNT(*) FROM {PROJECTION_TABLE}").fetchone()[0] == len(
            rich_chunks()
        )
        assert conn.execute(
            f"SELECT COUNT(*) FROM {PROJECTION_TABLE} WHERE content_text IS NOT NULL"
        ).fetchone()[0] == 0
        builds = {r[0] for r in conn.execute(f"SELECT DISTINCT build_id FROM {FTS_MAP_TABLE}")}
        assert builds == {BUILD}
        stored = json.loads(conn.execute("SELECT meta_json FROM served_projection_meta").fetchone()[0])
        assert stored["projection_sha256"] == handle.projection_sha256
    finally:
        conn.close()
    assert handle.identity() == {
        "build_id": BUILD,
        "row_count": len(rich_chunks()),
        "projection_sha256": handle.projection_sha256,
        "schema_version": SCHEMA_VERSION,
    }


def test_build_is_deterministic(db, tmp_path):
    first = _build(db, tmp_path / "a")
    second = _build(db, tmp_path / "b")
    assert first.projection_sha256 == second.projection_sha256


def test_open_or_build_reuses_matching_sidecar(db, tmp_path):
    out = tmp_path / "projection"
    out.mkdir()
    first, reused = open_or_build_served_projection(
        make_readonly_factory(db), expected_manifest_id=MANIFEST, projection_dir=out
    )
    second, reused_again = open_or_build_served_projection(
        make_readonly_factory(db), expected_manifest_id=MANIFEST, projection_dir=out
    )
    assert reused is False
    assert reused_again is True
    assert second.path == first.path
    assert len(list(out.glob("served_projection_*.db"))) == 1


def test_open_or_build_rebuilds_mismatched_identity_and_prunes(db, tmp_path):
    out = tmp_path / "projection"
    out.mkdir()
    first = _build(db, out)
    conn = sqlite3.connect(first.path)
    meta = json.loads(conn.execute("SELECT meta_json FROM served_projection_meta").fetchone()[0])
    meta["fts_index_version"] = "wrong"
    conn.execute("UPDATE served_projection_meta SET meta_json=?", (json.dumps(meta),))
    conn.commit()
    conn.close()
    (out / "served_projection_old1.db").write_bytes(b"old")
    (out / "served_projection_old2.db").write_bytes(b"old")

    rebuilt, reused = open_or_build_served_projection(
        make_readonly_factory(db), expected_manifest_id=MANIFEST, projection_dir=out
    )
    assert reused is False
    assert rebuilt.meta["fts_index_version"] != "wrong"
    sidecars = list(out.glob("served_projection_*.db"))
    assert len(sidecars) == 2
    assert rebuilt.path in sidecars


def test_sidecar_main_schema_is_read_only_but_temp_is_writable(db, tmp_path):
    handle = _build(db, tmp_path / "p")
    conn = handle.connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(f"DELETE FROM {PROJECTION_TABLE}")
        conn.execute("CREATE TEMP TABLE t (x INTEGER)")
        conn.execute("INSERT INTO temp.t VALUES (1)")
        assert conn.execute("SELECT x FROM temp.t").fetchone() == (1,)
    finally:
        conn.close()


def test_wrong_manifest_fails_closed(db, tmp_path):
    (tmp_path / "p").mkdir()
    with pytest.raises(RetrievalContractError):
        build_served_projection(
            make_readonly_factory(db), expected_manifest_id="f" * 64, projection_dir=tmp_path / "p"
        )
    assert list((tmp_path / "p").iterdir()) == []


def test_missing_projection_dir_fails_closed(db, tmp_path):
    with pytest.raises(RetrievalContractError) as err:
        build_served_projection(
            make_readonly_factory(db), expected_manifest_id=MANIFEST,
            projection_dir=tmp_path / "absent",
        )
    assert err.value.code == "served_projection_dir_missing"


class _Proxy:
    """A served connection whose selected statements are rewritten."""

    def __init__(self, conn: sqlite3.Connection, rewrite):
        self._conn = conn
        self._rewrite = rewrite

    def execute(self, sql, params=()):
        sql, params = self._rewrite(sql, params)
        return self._conn.execute(sql, params)

    def close(self):
        self._conn.close()


def _factory_with(db: Path, rewrite):
    base = make_readonly_factory(db)
    return lambda: _Proxy(base(), rewrite)


def _expect_failure(db, tmp_path, rewrite, code):
    out = tmp_path / "p"
    out.mkdir(exist_ok=True)
    with pytest.raises(RetrievalContractError) as err:
        build_served_projection(
            _factory_with(db, rewrite), expected_manifest_id=MANIFEST, projection_dir=out
        )
    assert err.value.code == code
    assert list(out.iterdir()) == []  # nothing published, temp file removed


def test_check2_build_count_mismatch_fails_closed(db, tmp_path):
    def rewrite(sql, params):
        if sql == "SELECT COUNT(*) FROM corpus_build_chunks WHERE build_id = ?":
            return "SELECT COUNT(*) + 1 FROM corpus_build_chunks WHERE build_id = ?", params
        return sql, params

    _expect_failure(db, tmp_path, rewrite, "served_projection_view_not_build")


def test_check2_second_published_build_of_current_manifest_fails_closed(db, tmp_path):
    def rewrite(sql, params):
        if "published builds" in sql or (
            "FROM corpus_publication_builds b JOIN corpus_manifest m" in sql
            and "is_current = 1 WHERE b.status = 'published'" in sql
        ):
            return sql.replace("SELECT COUNT(*)", "SELECT COUNT(*) + 1"), params
        return sql, params

    _expect_failure(db, tmp_path, rewrite, "served_projection_view_not_build")


def test_check2_legacy_branch_rows_fail_closed(db, tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_legacy_branch_rows", lambda conn, manifest_id: 3)
    _expect_failure(db, tmp_path, lambda s, p: (s, p), "served_projection_legacy_branch_active")


def test_check3_row_digest_mismatch_fails_closed(db, tmp_path, monkeypatch):
    """The projection's own re-read must reproduce the view's row digest."""
    monkeypatch.setattr(
        sp,
        "_PROJ_SELECT",
        sp._PROJ_SELECT.replace(
            "SELECT chunk_id, document_id,",
            "SELECT chunk_id, CASE WHEN chunk_id='chunk-005' THEN 'x' "
            "ELSE document_id END AS document_id,",
        ),
    )
    _expect_failure(db, tmp_path, lambda s, p: (s, p), "served_projection_row_digest_mismatch")


def test_check4_non_text_storage_fails_closed(tmp_path):
    db = make_served_db(tmp_path / "s.db", [chunk("c1", "AAPL text", "2026-08-01T12:00:00Z")])
    # A NULL ``available_at`` is not TEXT (TEXT affinity would coerce a
    # number to text, which check 3 already catches as a digest mismatch).
    def rewrite(sql, params):
        if sql == sp._VIEW_SELECT:
            return sql.replace("available_at,", "NULL AS available_at,", 1), params
        return sql, params

    _expect_failure(db, tmp_path, rewrite, "served_projection_storage_class")


def test_check5_fts_map_count_mismatch_fails_closed(db, tmp_path):
    def rewrite(sql, params):
        if "FROM corpus_build_chunks_fts_content" in sql and "WHERE c0 = ?" in sql:
            return sql.replace("ORDER BY id", "AND id <> (SELECT MIN(id) FROM corpus_build_chunks_fts_content WHERE c0 = ?) ORDER BY id"), (*params, *params)
        return sql, params

    _expect_failure(db, tmp_path, rewrite, "served_projection_fts_map_count")


def test_check5_fts_map_vtable_mismatch_fails_closed(db, tmp_path):
    """Same count, wrong chunk id: the map must match the FTS table itself."""
    def rewrite(sql, params):
        if "FROM corpus_build_chunks_fts_content" in sql and "WHERE c0 = ?" in sql:
            return (
                sql.replace(
                    "SELECT id, c0, c1",
                    "SELECT id, c0, CASE WHEN id = (SELECT MIN(id) FROM "
                    "corpus_build_chunks_fts_content WHERE c0 = ?) THEN 'wrong' "
                    "ELSE c1 END",
                ),
                (*params, *params),
            )
        return sql, params

    _expect_failure(db, tmp_path, rewrite, "served_projection_fts_map_vtable_mismatch")


def test_check5_fts_layout_fails_closed(db, tmp_path):
    def rewrite(sql, params):
        if sql == "PRAGMA table_info(corpus_build_chunks_fts_content)":
            return "SELECT 0, 'id' UNION ALL SELECT 1, 'c9'", params
        return sql, params

    _expect_failure(db, tmp_path, rewrite, "served_projection_fts_content_layout")


def test_check6_digest_binds_view_definition(db, tmp_path):
    first = _build(db, tmp_path / "a")

    def rewrite(sql, params):
        if "sqlite_master WHERE type='view' AND name='corpus_served_chunks'" in sql:
            return "SELECT sql || ' ' FROM sqlite_master WHERE type='view' AND name='corpus_served_chunks'", params
        return sql, params

    (tmp_path / "b").mkdir()
    second = build_served_projection(
        _factory_with(db, rewrite), expected_manifest_id=MANIFEST, projection_dir=tmp_path / "b"
    )
    assert second.projection_sha256 != first.projection_sha256
    assert second.meta["row_digest"] == first.meta["row_digest"]


def test_republish_does_not_disturb_an_open_reader(db, tmp_path):
    first = _build(db, tmp_path / "p")
    reader = first.connect()
    try:
        _build(db, tmp_path / "p")  # same content: atomic replace of the same name
        assert reader.execute(f"SELECT COUNT(*) FROM {PROJECTION_TABLE}").fetchone()[0] == len(
            rich_chunks()
        )
    finally:
        reader.close()
    assert not [p for p in (tmp_path / "p").iterdir() if p.name.startswith(".served_projection.tmp")]


def test_duplicate_fts_row_in_the_served_build_fails_closed(db, tmp_path):
    """A second FTS row for a served chunk breaks the persisted FTS row count."""
    rw = sqlite3.connect(db)
    rw.execute(
        "INSERT INTO corpus_build_chunks_fts (build_id, chunk_id, content_text) "
        "SELECT build_id, chunk_id, content_text FROM corpus_build_chunks_fts "
        "WHERE build_id = ? LIMIT 1",
        (BUILD,),
    )
    rw.commit()
    rw.close()
    _expect_failure(db, tmp_path, lambda s, p: (s, p), "served_projection_fts_map_count")
