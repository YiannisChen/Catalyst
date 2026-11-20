import json
import sqlite3
import threading
import time

from catalyst_data import read_api
from catalyst_data.ingestion.collectors import sec_structure
from catalyst_data.retrieval import web_bodies
from catalyst_data.storage import connect


def _lock_then_release(path, delay=0.3):
    writer = sqlite3.connect(path, timeout=0, check_same_thread=False)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("UPDATE lock_table SET value=value+1")
    thread = threading.Thread(target=lambda: (time.sleep(delay), writer.commit(), writer.close()))
    thread.start()
    return thread


def _track_backoff(monkeypatch):
    calls = []
    real_sleep = time.sleep
    monkeypatch.setattr(connect.time, "sleep", lambda seconds: (calls.append(seconds), real_sleep(seconds))[1])
    return calls


def _lock_db(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE lock_table (value INTEGER)")
        db.execute("INSERT INTO lock_table VALUES (0)")


def test_web_bodies_waits_on_query_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_SQLITE_READ_TIMEOUT_S", "2")
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(tmp_path / "body.db"))
    body = tmp_path / "body.db"
    with sqlite3.connect(body) as db:
        db.execute("CREATE TABLE web_body_chunks (asset_id TEXT, ordinal INTEGER)")
        db.execute("INSERT INTO web_body_chunks VALUES ('a', 1)")
        db.execute("CREATE TABLE lock_table (value INTEGER)")
        db.execute("INSERT INTO lock_table VALUES (0)")
    calls = _track_backoff(monkeypatch)
    lock = _lock_then_release(body)
    assert web_bodies.bodies_for_assets(["a"]) == {"a": [{"asset_id": "a", "ordinal": 1}]}
    lock.join()
    assert calls


def test_sec_structure_waits_on_query_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_SQLITE_READ_TIMEOUT_S", "2")
    body = tmp_path / "body.db"
    with sqlite3.connect(body) as db:
        db.execute("CREATE TABLE sec_exhibit_units (url TEXT, accession TEXT, units_json TEXT, parsed_at TEXT)")
        db.execute("INSERT INTO sec_exhibit_units VALUES ('u', 'a', ?, 't')", (json.dumps({"units": []}),))
        db.execute("CREATE TABLE lock_table (value INTEGER)")
        db.execute("INSERT INTO lock_table VALUES (0)")
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(body))
    calls = _track_backoff(monkeypatch)
    lock = _lock_then_release(body)
    assert sec_structure.units_for("u") == ()
    lock.join()
    assert calls


def test_sec_acceptance_table_waits_on_query_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_SQLITE_READ_TIMEOUT_S", "2")
    body = tmp_path / "body.db"
    with sqlite3.connect(body) as db:
        db.executescript("""
        CREATE TABLE body_versions (version_id TEXT, source_id TEXT, document_id TEXT, title TEXT, published_at TEXT);
        CREATE TABLE temporal_evidence_map (version_id TEXT, ticker TEXT, kind TEXT, eligible_at TEXT, url TEXT, sec_accession TEXT);
        CREATE TABLE temporal_evidence_versions (version_id TEXT, published_at_source TEXT);
        CREATE TABLE lock_table (value INTEGER);
        INSERT INTO lock_table VALUES (0);
        INSERT INTO body_versions VALUES ('v','daily:sec','x','AAA EX-99.1','2026-10-30T20:00:00Z');
        INSERT INTO temporal_evidence_map VALUES ('v','AAA','filing','2026-10-30T20:00:00Z','https://www.sec.gov/Archives/edgar/data/1/x.htm','0000000001-26-000001');
        INSERT INTO temporal_evidence_versions VALUES ('v','sec_index');
        """)
    correction = tmp_path / "corrections.csv"
    correction.write_text("accession,cik,form,items,json_acceptance_utc,edgar_accepted_et,edgar_accepted_utc,json_minus_edgar_h,in_stores\n")
    calls = _track_backoff(monkeypatch)
    lock = _lock_then_release(body)
    result = read_api.sec_8k_acceptance_table(body, accessions=["0000000001-26-000001"], store_id="test-store",
                                               correction_table_path=correction)
    lock.join()
    assert result and calls


def test_original_plain_ro_entrypoints_never_add_immutable(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALYST_SQLITE_LIVE_READ", raising=False)
    body = tmp_path / "body.db"
    with sqlite3.connect(body) as db:
        db.execute("CREATE TABLE web_body_chunks (asset_id TEXT, ordinal INTEGER)")
    uris = []
    real_connect = sqlite3.connect
    def record(database, *args, **kwargs):
        uris.append(str(database))
        return real_connect(database, *args, **kwargs)
    monkeypatch.setattr(connect.sqlite3, "connect", record)
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(body))
    web_bodies.bodies_for_assets(["none"])
    correction = tmp_path / "corrections.csv"
    correction.write_text("accession,cik,form,items,json_acceptance_utc,edgar_accepted_et,edgar_accepted_utc,json_minus_edgar_h,in_stores\n")
    try:
        read_api.sec_8k_acceptance_table(body, accessions=["0000000001-26-000001"],
                                         correction_table_path=correction)
    except read_api.SecFilingDataError:
        pass
    assert len(uris) >= 2 and all("immutable" not in uri for uri in uris)


def test_wal_read_is_not_blocked_by_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_SQLITE_READ_TIMEOUT_S", "0.5")
    path = tmp_path / "wal.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE data (value INTEGER)")
        db.execute("INSERT INTO data VALUES (1)")
    writer = sqlite3.connect(path, timeout=0, check_same_thread=False)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO data VALUES (2)")
    try:
        with connect.open_readonly(path, immutable=False) as reader:
            assert [tuple(row) for row in reader.execute("SELECT value FROM data").fetchall()] == [(1,)]
    finally:
        writer.rollback()
        writer.close()


def test_sec_structure_does_not_swallow_lock_timeout(tmp_path, monkeypatch):
    from catalyst_data.storage import connect as connections

    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(tmp_path / "body.db"))
    (tmp_path / "body.db").touch()
    monkeypatch.setattr(connections, "connect_readonly", lambda *a, **k: (_ for _ in ()).throw(
        connections.SQLiteReadTimeoutError("locked")))
    import pytest
    with pytest.raises(connections.SQLiteReadTimeoutError):
        sec_structure.units_for("url")
