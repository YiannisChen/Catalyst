from __future__ import annotations

import sqlite3

from catalyst_data.storage import connect


def test_q001_readonly_connection_is_immutable(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeConnection:
        def execute(self, sql):
            return None

    def fake_connect(database, **kwargs):
        captured["uri"] = database
        captured["kwargs"] = kwargs
        return FakeConnection()

    monkeypatch.setattr(connect.sqlite3, "connect", fake_connect)
    connection = connect._connect_readonly(tmp_path / "q001.sqlite3")
    assert connection is not None
    assert "mode=ro&immutable=1" in str(captured["uri"])
    assert captured["kwargs"]["uri"] is True
