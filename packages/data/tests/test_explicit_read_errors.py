from __future__ import annotations

import json
import sqlite3

import pytest
from catalyst_data.ingestion.collectors import sec_structure
from catalyst_data.ingestion.ops import health
from catalyst_data.storage.connect import SQLiteReadTimeoutError
from catalyst_data.storage.errors import StoreReadError


def _body_store(path, *, table=True, row=True):
    with sqlite3.connect(path) as conn:
        if table:
            conn.execute(
                "CREATE TABLE sec_exhibit_units (url TEXT, accession TEXT, units_json TEXT, parsed_at TEXT)"
            )
            if row:
                conn.execute(
                    "INSERT INTO sec_exhibit_units VALUES ('u', 'a', ?, 't')",
                    (json.dumps({"units": [{"kind": "title", "text": "Example"}]}),),
                )




def test_units_for_returns_rows_and_none_for_normal_absence(tmp_path, monkeypatch):
    path = tmp_path / "body.sqlite3"
    _body_store(path)
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(path))
    assert sec_structure.units_for("u") == ({"kind": "title", "text": "Example"},)
    assert sec_structure.units_for("missing") is None


@pytest.mark.parametrize("corrupt", [False, True], ids=["missing-table", "corrupt-db"])
def test_units_for_reports_store_failures(tmp_path, monkeypatch, corrupt):
    path = tmp_path / "body.sqlite3"
    if corrupt:
        path.write_bytes(b"not sqlite")
    else:
        _body_store(path, table=False)
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(path))
    with pytest.raises(StoreReadError) as caught:
        sec_structure.units_for("u")
    assert caught.value.store_path == str(path)
    assert caught.value.operation == "read sec_exhibit_units"
    assert caught.value.__cause__ is not None


def test_units_for_reports_missing_configured_store(tmp_path, monkeypatch):
    path = tmp_path / "missing.sqlite3"
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(path))
    with pytest.raises(StoreReadError) as caught:
        sec_structure.units_for("u")
    assert isinstance(caught.value.__cause__, FileNotFoundError)












def test_missing_units_result_is_not_cached_across_store_damage(tmp_path, monkeypatch):
    path = tmp_path / "body.sqlite3"
    _body_store(path, row=False)
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(path))
    assert sec_structure.units_for("not-present") is None
    path.write_bytes(b"not sqlite")
    with pytest.raises(StoreReadError):
        sec_structure.units_for("not-present")




def test_health_sec_units_reports_missing_corrupt_and_readable(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(health, "DATA", data)
    path = data / "news_bodies_v2.sqlite3"
    assert health.sec_exhibit_units()["status"] == "error"
    path.write_bytes(b"not sqlite")
    assert health.sec_exhibit_units()["status"] == "error"
    path.unlink()
    _body_store(path)
    assert health.sec_exhibit_units()["status"] == "ok"


def test_health_sec_units_missing_table_is_error(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(health, "DATA", data)
    path = data / "news_bodies_v2.sqlite3"
    _body_store(path, table=False)
    assert health.sec_exhibit_units()["status"] == "error"




@pytest.mark.parametrize("failure", ["missing-file", "missing-table", "corrupt-db"])
def test_units_count_reports_store_failure_instead_of_no_data(tmp_path, failure):
    path = tmp_path / "body.sqlite3"
    if failure == "missing-table":
        sqlite3.connect(path).close()
    elif failure == "corrupt-db":
        path.write_bytes(b"not sqlite")
    with pytest.raises(StoreReadError):
        sec_structure.units_count(path)

def test_explicit_filing_units_waiver_returns_no_data_without_opening_store(monkeypatch):
    from catalyst_data.ingestion.collectors import sec_structure
    from catalyst_data.read import sec_units

    monkeypatch.setenv("CATALYST_FILING_UNITS", "off")

    def unexpected_store_access(*_args, **_kwargs):
        raise AssertionError("filing-units waiver must not touch the body store")

    monkeypatch.setattr(sec_units, "store_path", unexpected_store_access)
    monkeypatch.setattr(sec_structure.store_connect, "connect_readonly", unexpected_store_access)

    assert sec_structure.units_count("missing.sqlite3") is None
    assert sec_structure.units_for("https://www.sec.gov/Archives/edgar/data/1/1/ex99.htm") is None
    assert sec_structure.title_for("https://www.sec.gov/Archives/edgar/data/1/1/ex99.htm") is None

def test_read_timeouts_keep_their_distinct_data_core_type(tmp_path, monkeypatch):
    path = tmp_path / "body.sqlite3"
    path.touch()
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(path))
    from catalyst_data.storage import connect
    monkeypatch.setattr(connect, "connect_readonly", lambda *_a, **_k: (_ for _ in ()).throw(
        SQLiteReadTimeoutError("locked")))
    with pytest.raises(SQLiteReadTimeoutError):
        sec_structure.units_for("u")
    assert not isinstance(SQLiteReadTimeoutError("locked"), StoreReadError)

def test_live_read_lock_timeout_raises_sqlite_read_timeout_error(tmp_path, monkeypatch):
    from catalyst_data.storage.connect import connect_readonly

    path = tmp_path / "locked.sqlite3"
    writer = sqlite3.connect(path, timeout=0.01)
    writer.execute("CREATE TABLE sample (value INTEGER)")
    writer.commit()
    writer.execute("BEGIN EXCLUSIVE")
    monkeypatch.setenv("CATALYST_SQLITE_LIVE_READ", "1")
    monkeypatch.setenv("CATALYST_SQLITE_READ_TIMEOUT_S", "0.02")
    monkeypatch.setenv("CATALYST_SQLITE_READ_RETRIES", "0")
    reader = connect_readonly(path, timeout_ms=20, immutable=False)
    try:
        with pytest.raises(SQLiteReadTimeoutError):
            reader.execute("SELECT * FROM sample").fetchall()
    finally:
        reader.close()
        writer.rollback()
        writer.close()




def test_sec_reader_and_health_check_use_the_configured_store_path(tmp_path, monkeypatch):
    body_store = tmp_path / "configured-body.sqlite3"
    _body_store(body_store)
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(body_store))
    assert sec_structure.units_for("u") is not None
    assert health.sec_exhibit_units()["status"] == "ok"
