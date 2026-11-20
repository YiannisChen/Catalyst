import sqlite3

import pytest
from catalyst_data.ingestion import corp_actions, scope


def _store(tmp_path, monkeypatch):
    db = tmp_path / "corp.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(corp_actions.SCHEMA)
    conn.execute("INSERT INTO dividends VALUES ('XOM', '2026-08-14', 1.0, 4)")
    conn.execute("INSERT INTO dividends VALUES ('BRK.B', '2026-08-14', 0.5, 4)")
    conn.execute("INSERT INTO splits VALUES ('NVDA', '2026-06-10', 10.0)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("CATALYST_CORP_ACTIONS_DB", str(db))


def test_lookup_and_total_return(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    assert corp_actions.dividend("XOM", "2026-08-14") == 1.0
    assert corp_actions.dividend("BRK-B", "2026-08-14") == 0.5
    assert corp_actions.dividend("XOM", "2026-08-15") is None
    assert corp_actions.split_ratio("NVDA", "2026-06-10") == 10.0
    assert abs(corp_actions.total_return_pct(99.0, 100.0, "XOM", "2026-08-14")) < 1e-9


def test_missing_store_applies_no_correction(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_CORP_ACTIONS_DB", str(tmp_path / "absent.sqlite3"))
    assert corp_actions.dividend("XOM", "2026-08-14") is None
    assert abs(corp_actions.total_return_pct(99.0, 100.0, "XOM", "2026-08-14") + 1.0) < 1e-9


def test_broken_existing_store_is_not_treated_as_no_correction(tmp_path, monkeypatch):
    from catalyst_data.storage.errors import StoreReadError

    path = tmp_path / "broken.sqlite3"
    path.write_text("not sqlite")
    monkeypatch.setenv("CATALYST_CORP_ACTIONS_DB", str(path))
    with pytest.raises(StoreReadError, match="read corporate actions"):
        corp_actions.dividend("XOM", "2026-08-14")


def test_session_return_adds_ex_day_cash(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    conn.executemany("INSERT INTO ohlcv VALUES (?,?,?)", [("XOM", "2026-08-13", 100.0), ("XOM", "2026-08-14", 98.0)])
    assert abs(scope.session_return(conn, "XOM", "2026-08-14") + 1.0) < 1e-9
