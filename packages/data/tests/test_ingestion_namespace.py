"""The canonical ingestion namespace exposes its supported modules and CLIs."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest


def test_canonical_ingestion_modules_import_directly() -> None:
    for module in (
        "catalyst_data.ingestion.ops.daily",
        "catalyst_data.ingestion.collectors.web_discovery_v2",
        "catalyst_data.ingestion.collectors.alpaca",
    ):
        assert importlib.import_module(module).__name__ == module


def test_active_forwarding_files_are_absent() -> None:
    from catalyst_data import ingestion

    root = Path(ingestion.__file__).parent
    active = (
        "alpaca", "benzinga", "body_migration", "cnbc_roundups", "daily", "fetch",
        "headline_archive", "headlines", "health", "kitco", "market", "page_versions",
        "readiness", "ready_snapshot", "refresh", "relay", "sec", "sec_earnings",
        "sec_filings", "sec_structure", "status", "store", "timefields", "times",
        "web_discovery_v2", "web_search",
    )
    assert not [name for name in active if (root / f"{name}.py").exists()]


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("catalyst_data.ingestion.ops.daily", ("plan", "scheduled")),
        ("catalyst_data.ingestion.collectors.web_discovery_v2", ("run", "dump")),
        ("catalyst_data.ingestion.collectors.alpaca", ("news-poll", "news-backfill")),
    ],
)
def test_canonical_cli_exposes_help(module: str, expected: tuple[str, ...]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    for command in expected:
        assert command in result.stdout
