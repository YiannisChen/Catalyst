from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest


def test_daily_console_script_has_its_runtime_dependency_and_starts(monkeypatch, capsys):
    project_root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((project_root / "pyproject.toml").read_text())["project"]

    assert "requests>=2.31" in project["dependencies"]
    assert project["scripts"]["catalyst-daily"] == (
        "catalyst_data.ingestion.ops.daily:main"
    )

    monkeypatch.setattr(sys, "argv", ["catalyst-daily", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        from catalyst_data.ingestion.ops.daily import main

        main()

    assert exit_info.value.code == 0
    assert "usage: catalyst-daily" in capsys.readouterr().out
