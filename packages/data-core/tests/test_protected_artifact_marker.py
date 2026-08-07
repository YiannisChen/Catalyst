"""Regression tests for the ``protected_artifact`` marker and strict mode.

The marker makes protected-DB tests skip with the missing path when the
artifact DB is absent, and fail (not skip) when
``CATALYST_REQUIRE_PROTECTED_DB=1``. We never fake databases or reset SHAs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


class _FakeMark:
    """Duck-typed marker matching the pytest attribute surface we consume."""

    def __init__(self, name: str, args: tuple):
        self.name = name
        self.args = args


def _item(marker: _FakeMark):
    class _FakeItem:
        def get_closest_marker(self, name):
            return marker if marker.name == name else None

    return _FakeItem()


def _marker(*args):
    return _FakeMark("protected_artifact", tuple(args))


def test_marker_is_registered_in_conftest():
    conftest_source = (
        REPO_ROOT / "packages" / "data-core" / "tests" / "conftest.py"
    ).read_text(encoding="utf-8")
    assert "protected_artifact" in conftest_source
    assert "CATALYST_REQUIRE_PROTECTED_DB" in conftest_source


def test_missing_artifact_paths_resolve_to_repo_root():
    from conftest import _protected_artifact_missing

    item = _item(_marker("data/catalyst_dev_ws4b.db"))
    paths = _protected_artifact_missing(item)
    assert paths == [REPO_ROOT / "data" / "catalyst_dev_ws4b.db"]


def test_non_marked_item_has_no_protected_paths():
    from conftest import _protected_artifact_missing

    item = _item(_FakeMark("other", ()))
    assert _protected_artifact_missing(item) == []


def test_missing_db_skips_with_missing_path_by_default(monkeypatch):
    from conftest import pytest_runtest_setup

    monkeypatch.delenv("CATALYST_REQUIRE_PROTECTED_DB", raising=False)
    item = _item(_marker("data/catalyst_dev_ws4b.db"))
    with pytest.raises(pytest.skip.Exception) as excinfo:
        pytest_runtest_setup(item)
    message = str(excinfo.value)
    assert "missing" in message
    assert "catalyst_dev_ws4b.db" in message


def test_missing_db_fails_in_strict_mode(monkeypatch):
    from conftest import pytest_runtest_setup

    monkeypatch.setenv("CATALYST_REQUIRE_PROTECTED_DB", "1")
    item = _item(_marker("data/catalyst_dev_ws4b.db"))
    with pytest.raises(pytest.fail.Exception) as excinfo:
        pytest_runtest_setup(item)
    message = str(excinfo.value)
    assert "missing" in message
    assert "catalyst_dev_ws4b.db" in message


def test_present_artifact_does_not_skip_or_fail(monkeypatch):
    from conftest import pytest_runtest_setup

    monkeypatch.delenv("CATALYST_REQUIRE_PROTECTED_DB", raising=False)
    item = _item(_marker("README.md"))
    pytest_runtest_setup(item)  # must not raise


def test_s3_protected_test_files_declare_marker():
    for name in (
        "test_s3_corpus_items.py",
        "test_s3_frozen_db_readonly.py",
        "test_s3_timestamp_canonical.py",
        "test_s3_watermark.py",
    ):
        source = (REPO_ROOT / "packages" / "data-core" / "tests" / name).read_text(
            encoding="utf-8"
        )
        assert "protected_artifact(" in source, f"{name} missing marker"
