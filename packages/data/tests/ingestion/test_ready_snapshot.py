import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from catalyst_data import read_api
from catalyst_data.ingestion.ops import ready_snapshot

FIXTURE = Path(__file__).parent.parent / "fixtures" / "real_update_jobs_slice.sqlite3"


def test_build_read_roundtrip_and_only_prepared_completed_sessions(tmp_path):
    out = tmp_path / "ready.json"
    generated = datetime(2026, 11, 4, tzinfo=timezone.utc)
    written = ready_snapshot.build(FIXTURE, out, generated_at=generated)
    assert read_api.ready_snapshot(out) == written
    assert written["last_completed_session_date"] == "2026-10-29"
    assert {p["session_date"] for p in written["pairs"]} == {
        "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"
    }
    assert ready_snapshot.build(FIXTURE, tmp_path / "again.json", generated_at=generated) == written
    assert len(written["pairs_sha256"]) == 64


def test_atomic_write_leaves_no_temp_file(tmp_path):
    target = tmp_path / "ready.json"
    ready_snapshot.build(FIXTURE, target)
    assert target.is_file()
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("contents", ["{", json.dumps({"schema_version": 9}), "[]"])
def test_bad_snapshot_raises_ready_snapshot_error(tmp_path, contents):
    path = tmp_path / "bad.json"
    path.write_text(contents)
    with pytest.raises(read_api.ReadySnapshotError):
        read_api.ready_snapshot(path)


def test_missing_snapshot_raises_ready_snapshot_error(tmp_path):
    with pytest.raises(read_api.ReadySnapshotError):
        read_api.ready_snapshot(tmp_path / "missing.json")


def test_pairs_digest_mismatch_raises(tmp_path):
    path = tmp_path / "bad-digest.json"
    value = ready_snapshot.build(FIXTURE, path)
    value["pairs"][0]["ticker"] = "WRONG"
    path.write_text(json.dumps(value))
    with pytest.raises(read_api.ReadySnapshotError, match="pairs_sha256"):
        read_api.ready_snapshot(path)


def test_default_path_is_shared_by_build_and_read(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.sqlite3"
    target = tmp_path / "custom.json"
    monkeypatch.setenv("CATALYST_READY_SNAPSHOT", str(target))
    assert ready_snapshot.default_path(jobs) == target
    ready_snapshot.build(FIXTURE, generated_at=datetime(2026, 11, 4, tzinfo=timezone.utc))
    assert read_api.ready_snapshot() == read_api.ready_snapshot(target)


def test_default_path_uses_jobs_directory_without_override(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALYST_READY_SNAPSHOT", raising=False)
    assert ready_snapshot.default_path(tmp_path / "jobs.sqlite3") == tmp_path / "ready_range.json"


def test_daily_snapshot_hook_failure_is_logged_and_absorbed(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from catalyst_data.ingestion.ops import daily

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ready_snapshot, "build", fail)
    messages = []
    daily._build_ready_snapshot_best_effort(
        SimpleNamespace(jobs_db=tmp_path / "jobs.sqlite3"), messages.append
    )
    assert messages and "OSError" in messages[0]
