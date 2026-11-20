from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.input_manifest import PreflightError
from catalyst_data.storage.snapshots import (
    PinnedStore,
    load_verified_stores,
    readonly_snapshot,
    validate_verified_stores,
)

INVOCATION = ["python", "-m", "catalyst_data.ingestion.coverage_report"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(tmp_path, files: list[Path], *, env=None, invocation=None) -> Path:
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "invocation": INVOCATION if invocation is None else invocation,
        "files": [{"path": str(item), "sha256": _sha(item)} for item in files],
        "environment": {} if env is None else env,
    }))
    return path


def _db(path: Path, *, attempt="attempt-1", coverage_id="coverage-1", status="OK") -> Path:
    conn = sqlite3.connect(path)
    coverage.ensure_schema(conn)
    item = coverage.start_attempt(
        conn, logical_task_id="task", attempt_id=attempt,
        source_id="daily:finnhub", product="headlines",
        subject_kind="ticker", subject_id="ABC", query_spec={},
        coverage_policy_version="policy-v1",
        requested_from="2026-10-23T13:30:00Z", requested_until="2026-10-23T20:00:00Z",
    )
    coverage.record_coverage(
        conn, coverage_id=coverage_id, attempt_id=item.attempt_id, status=status,
        reason="ERROR" if status == "ERROR" else None,
        recorded_at="2026-10-24T00:00:00Z",
        coverage_from="2026-10-23T13:30:00Z", coverage_until="2026-10-23T20:00:00Z",
        enumeration_complete=True,
    )
    conn.commit()
    conn.close()
    return path


def _load(tmp_path, stores, *, optional=(), files=None, env=None, required_env=(), environ=None):
    paths = [Path(path) for _, path in (stores.items() if hasattr(stores, "items") else stores)]
    optional_paths = [Path(path) for _, path in (optional.items() if hasattr(optional, "items") else optional)]
    manifest = _manifest(tmp_path, paths + [path for path in optional_paths if path.exists()] if files is None else files, env=env)
    return load_verified_stores(
        manifest, invocation=INVOCATION, required_stores=stores, optional_stores=optional,
        required_env=required_env, environ={} if environ is None else environ,
    )


def test_load_verifies_manifest_paths_and_derives_actual_sha(tmp_path):
    store_path = _db(tmp_path / "daily.sqlite3")
    manifest = _manifest(tmp_path, [store_path])
    verified = load_verified_stores(
        manifest, invocation=INVOCATION, required_stores={"daily": store_path}, environ={},
    )

    store = verified.stores["daily"]
    assert isinstance(store, PinnedStore)
    assert store.path == store_path.resolve()
    assert store.sha256 == _sha(store_path)
    assert verified.manifest_sha256 == _sha(manifest)
    assert verified.invocation == tuple(INVOCATION)
    assert verified.missing_optional_store_ids == ()


def test_missing_required_unlisted_path_unlisted_environment_and_invocation_fail_closed(tmp_path):
    existing = _db(tmp_path / "listed.sqlite3")
    absent = tmp_path / "absent.sqlite3"
    manifest = _manifest(tmp_path, [existing])
    with pytest.raises(PreflightError, match="missing required store"):
        load_verified_stores(manifest, invocation=INVOCATION,
                             required_stores={"daily": absent}, environ={})

    unlisted = _db(tmp_path / "unlisted.sqlite3")
    with pytest.raises(PreflightError, match="unlisted input"):
        load_verified_stores(manifest, invocation=INVOCATION,
                             required_stores={"daily": unlisted}, environ={})

    env_manifest = _manifest(tmp_path, [existing], env={"PYTHONPATH": "expected"})
    with pytest.raises(PreflightError, match="environment mismatch"):
        load_verified_stores(env_manifest, invocation=INVOCATION,
                             required_stores={"daily": existing},
                             required_env=("PYTHONPATH",), environ={"PYTHONPATH": "actual"})
    unlisted_env_manifest = _manifest(tmp_path, [existing])
    with pytest.raises(PreflightError, match="unlisted environment"):
        load_verified_stores(unlisted_env_manifest, invocation=INVOCATION,
                             required_stores={"daily": existing},
                             required_env=("PYTHONPATH",), environ={"PYTHONPATH": "x"})
    with pytest.raises(PreflightError, match="invocation mismatch"):
        load_verified_stores(manifest, invocation=["different"],
                             required_stores={"daily": existing}, environ={})


def test_duplicate_store_ids_fail_but_same_file_hash_for_distinct_ids_is_allowed(tmp_path):
    path = _db(tmp_path / "same.sqlite3")
    manifest = _manifest(tmp_path, [path])
    with pytest.raises(ValueError, match="duplicate store_id"):
        load_verified_stores(manifest, invocation=INVOCATION,
                             required_stores=[("same", path), ("same", path)], environ={})

    verified = load_verified_stores(
        manifest, invocation=INVOCATION,
        required_stores=[("first", path), ("second", path)], environ={},
    )
    assert verified.stores["first"].sha256 == verified.stores["second"].sha256


def test_optional_missing_store_is_audited_and_not_treated_as_an_empty_database(tmp_path):
    required = _db(tmp_path / "required.sqlite3")
    optional = tmp_path / "not-present.sqlite3"
    manifest = _manifest(tmp_path, [required])

    verified = load_verified_stores(
        manifest, invocation=INVOCATION, required_stores={"daily": required},
        optional_stores={"archive": optional}, environ={},
    )

    assert verified.missing_optional_store_ids == ("archive",)
    assert "archive" not in verified.stores


def test_revalidates_manifest_database_and_forged_pins_before_each_read(tmp_path):
    store_path = _db(tmp_path / "daily.sqlite3")
    manifest = _manifest(tmp_path, [store_path])
    verified = load_verified_stores(manifest, invocation=INVOCATION,
                                    required_stores={"daily": store_path}, environ={})

    forged_store = replace(verified.stores["daily"], sha256="0" * 64)
    forged = replace(verified, stores={"daily": forged_store})
    with pytest.raises(PreflightError):
        with readonly_snapshot(forged, forged_store):
            pass

    manifest.write_text(manifest.read_text() + " ")
    with pytest.raises(PreflightError):
        with readonly_snapshot(verified, verified.stores["daily"]):
            pass

    manifest = _manifest(tmp_path, [store_path])
    verified = load_verified_stores(manifest, invocation=INVOCATION,
                                    required_stores={"daily": store_path}, environ={})
    with store_path.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(PreflightError):
        with readonly_snapshot(verified, verified.stores["daily"]):
            pass


def test_rejects_symlinks_and_live_wal_before_reading(tmp_path):
    store_path = _db(tmp_path / "daily.sqlite3")
    link = tmp_path / "alias.sqlite3"
    link.symlink_to(store_path)
    manifest = _manifest(tmp_path, [store_path])
    with pytest.raises(PreflightError, match="symlink"):
        load_verified_stores(manifest, invocation=INVOCATION,
                             required_stores={"daily": link}, environ={})

    manifest = _manifest(tmp_path, [store_path])
    wal = Path(str(store_path) + "-wal")
    wal.write_bytes(b"live")
    with pytest.raises(PreflightError, match="WAL"):
        load_verified_stores(manifest, invocation=INVOCATION,
                             required_stores={"daily": store_path}, environ={})


def test_readonly_snapshot_uses_query_only_transaction_and_does_not_change_input(tmp_path):
    store_path = _db(tmp_path / "daily.sqlite3")
    manifest = _manifest(tmp_path, [store_path])
    verified = load_verified_stores(manifest, invocation=INVOCATION,
                                    required_stores={"daily": store_path}, environ={})
    before = _sha(store_path)

    with readonly_snapshot(verified, verified.stores["daily"]) as conn:
        assert conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM source_coverage_attempts").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO source_coverage_attempts VALUES (" + ",".join("?" for _ in range(13)) + ")",
                         ("x", "x", "x", 1, "x", "headlines", "ticker", "x", "x", "{}", "x", "2026-10-23T13:30:00Z", "2026-10-23T20:00:00Z"))

    assert _sha(store_path) == before


def test_default_environment_is_rechecked_from_live_process_environment(tmp_path, monkeypatch):
    store_path = _db(tmp_path / "daily.sqlite3")
    monkeypatch.setenv("CATALYST_DATA_DIR", "/tmp/catalyst-a")
    manifest = _manifest(tmp_path, [store_path], env={"CATALYST_DATA_DIR": "/tmp/catalyst-a"})
    verified = load_verified_stores(
        manifest, invocation=INVOCATION, required_stores={"daily": store_path},
    )
    monkeypatch.setenv("CATALYST_DATA_DIR", "/tmp/catalyst-b")

    with pytest.raises(PreflightError, match="environment mismatch"):
        with readonly_snapshot(verified, verified.stores["daily"]):
            pass


def test_manifest_is_rechecked_after_read_before_context_returns(tmp_path):
    store_path = _db(tmp_path / "daily.sqlite3")
    manifest = _manifest(tmp_path, [store_path])
    verified = load_verified_stores(manifest, invocation=INVOCATION,
                                    required_stores={"daily": store_path}, environ={})

    with pytest.raises(PreflightError, match="manifest changed"):
        with readonly_snapshot(verified, verified.stores["daily"]) as conn:
            conn.execute("SELECT count(*) FROM source_coverage_attempts").fetchone()
            manifest.write_text(manifest.read_text() + " ")


def test_deleted_database_during_read_still_closes_connection(tmp_path):
    store_path = _db(tmp_path / "daily.sqlite3")
    manifest = _manifest(tmp_path, [store_path])
    verified = load_verified_stores(manifest, invocation=INVOCATION,
                                    required_stores={"daily": store_path}, environ={})
    opened = []

    with pytest.raises(PreflightError):
        with readonly_snapshot(verified, verified.stores["daily"]) as conn:
            opened.append(conn)
            store_path.unlink()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_empty_forged_store_set_is_rejected_instead_of_issuing_empty_audit(tmp_path):
    store_path = _db(tmp_path / "daily.sqlite3")
    verified = load_verified_stores(
        _manifest(tmp_path, [store_path]), invocation=INVOCATION,
        required_stores={"daily": store_path}, environ={},
    )
    forged = replace(verified, stores={})

    with pytest.raises(PreflightError):
        from catalyst_data.ingestion.coverage_readiness import (
            CoveragePlan,
            CoverageRequirement,
            PlannedPartition,
            SourceCapability,
            expected_partition_id,
        )
        from catalyst_data.ingestion.gap_readiness import evaluate_snapshots

        req = CoverageRequirement(
            requirement_id="req", session_date="2026-10-23", ticker="ABC",
            category="target_news", scope_kind="target", scope_id="ABC",
            subject_kind="ticker", subject_id="ABC", product="headlines",
            requested_from="2026-10-23T13:30:00Z", requested_until="2026-10-23T20:00:00Z",
            source_ids=("daily:finnhub",),
        )
        pending = PlannedPartition(
            partition_id="pending", source_id="daily:finnhub", product="headlines",
            subject_kind="ticker", subject_id="ABC", requested_from="2026-10-23T13:30:00Z",
            requested_until="2026-10-23T20:00:00Z", query_filters={}, coverage_policy_version="p",
        )
        part = PlannedPartition(**(pending.__dict__ | {"partition_id": expected_partition_id(pending)}))
        plan = CoveragePlan((part,), {"daily:finnhub": SourceCapability(
            frozenset({"headlines"}), frozenset({"ticker"}))}, {})
        evaluate_snapshots((req,), plan, forged)


def test_all_empty_forged_declarations_cannot_pass_verified_store_validation(tmp_path):
    store_path = _db(tmp_path / "daily.sqlite3")
    verified = load_verified_stores(
        _manifest(tmp_path, [store_path]), invocation=INVOCATION,
        required_stores={"daily": store_path}, environ={},
    )
    forged = replace(verified, required_store_ids=(), required_store_paths=(), stores={})

    with pytest.raises(PreflightError):
        validate_verified_stores(forged)


def test_deleted_manifest_during_read_is_reported_as_preflight_error_and_closes(tmp_path):
    store_path = _db(tmp_path / "daily.sqlite3")
    manifest = _manifest(tmp_path, [store_path])
    verified = load_verified_stores(manifest, invocation=INVOCATION,
                                    required_stores={"daily": store_path}, environ={})
    opened = []

    with pytest.raises(PreflightError):
        with readonly_snapshot(verified, verified.stores["daily"]) as conn:
            opened.append(conn)
            manifest.unlink()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
