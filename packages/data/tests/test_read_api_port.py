from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from catalyst_data import read_api

FIXTURE = Path(__file__).parent / "fixtures" / "ready_range_data_main_ccd5800.json"
# A real ready_range.json from the data writer, if one is supplied locally; the test skips otherwise.
REAL_DATA_MAIN_SAMPLE = Path(os.environ.get("CATALYST_READY_RANGE_SAMPLE", "/nonexistent/ready_range.json"))


def test_read_api_accepts_snapshot_emitted_by_data_main_writer():
    # Provenance: fixture emitted by read-only data/main checkout commit ccd5800
    # via catalyst_data.ingestion.ops.ready_snapshot.build from tests/ingestion/test_ready_snapshot.py's
    # real_update_jobs_slice.sqlite3 fixture.
    snapshot = read_api.ready_snapshot(FIXTURE)

    assert snapshot["schema_version"] == 2
    assert snapshot["generated_at"] == "2026-10-30T15:00:00Z"
    assert snapshot["last_completed_session_date"] == "2026-10-29"
    assert {pair["session_date"] for pair in snapshot["pairs"]} == {
        "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29",
    }
    assert {"ticker": "TSLA", "session_date": "2026-10-26"} in snapshot["pairs"]
    assert len(snapshot["pairs"]) > 1


@pytest.mark.skipif(
    not REAL_DATA_MAIN_SAMPLE.is_file(),
    reason="optional real data/main ready-range sample is not available",
)
def test_read_api_validates_real_data_main_snapshot_pairs_hash():
    snapshot = read_api.ready_snapshot(REAL_DATA_MAIN_SAMPLE)
    canonical_pairs = json.dumps(
        snapshot["pairs"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    assert set(snapshot) == {
        "schema_version",
        "generated_at",
        "daily_last_completed_at",
        "last_completed_session_date",
        "pairs",
        "pairs_sha256",
    }
    assert snapshot["schema_version"] == 2
    assert snapshot["generated_at"] == "2026-11-04T18:57:27Z"
    assert snapshot["last_completed_session_date"] == "2026-11-03"
    assert len(snapshot["pairs"]) == 6492
    assert snapshot["pairs_sha256"] == hashlib.sha256(canonical_pairs).hexdigest()


@pytest.mark.parametrize("contents", ["{", "[]", json.dumps({"schema_version": 9})])
def test_ready_snapshot_rejects_malformed_or_unsupported_documents(tmp_path, contents):
    path = tmp_path / "invalid.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(read_api.ReadySnapshotError):
        read_api.ready_snapshot(path)


def test_ready_snapshot_rejects_missing_files(tmp_path):
    with pytest.raises(read_api.ReadySnapshotError):
        read_api.ready_snapshot(tmp_path / "missing.json")


def test_ready_snapshot_rejects_pair_hash_mismatch(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["pairs_sha256"] = "0" * 64
    path = tmp_path / "bad-hash.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(read_api.ReadySnapshotError, match="pairs_sha256 mismatch"):
        read_api.ready_snapshot(path)


@pytest.mark.parametrize("field_change", ["missing", "extra"])
def test_ready_snapshot_rejects_non_exact_field_set(tmp_path, field_change):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if field_change == "missing":
        payload.pop("daily_last_completed_at")
    else:
        payload["unexpected"] = True
    path = tmp_path / f"{field_change}-field.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(read_api.ReadySnapshotError, match="invalid ready snapshot schema"):
        read_api.ready_snapshot(path)
