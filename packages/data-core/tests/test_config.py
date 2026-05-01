from __future__ import annotations

from pathlib import Path

from catalyst_data.config import db_path


def test_db_path_uses_env_override(monkeypatch):
    monkeypatch.setenv("CATALYST_DB_PATH", "/tmp/test_catalyst.db")

    assert db_path() == Path("/tmp/test_catalyst.db")


def test_db_path_defaults_to_repo_data_dir(monkeypatch):
    monkeypatch.delenv("CATALYST_DB_PATH", raising=False)

    expected = Path(__file__).resolve().parents[3] / "data" / "catalyst_dev.db"
    assert db_path() == expected
