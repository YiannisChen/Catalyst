"""Input provenance must fail before a research/backfill job can read its inputs."""
import hashlib
import json
from pathlib import Path

import pytest
from catalyst_data.ingestion import input_manifest as M


def manifest(tmp_path, **changes):
    data = tmp_path / "snapshot.json"
    data.write_text('{"rows": 1}')
    spec = {"schema_version": 1, "invocation": ["study.py", "--samples", str(data)],
            "files": [{"path": str(data), "sha256": hashlib.sha256(data.read_bytes()).hexdigest()}],
            "environment": {"PEER_SUBS_DIR": None}}
    spec.update(changes)
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(spec))
    return path, data, spec


def test_valid_receipt_binds_input_paths_hashes_and_invocation(tmp_path):
    path, data, spec = manifest(tmp_path)
    receipt = M.verify(path, invocation=spec["invocation"], required_paths=[data],
                       required_env=["PEER_SUBS_DIR"], environ={})
    assert receipt["files"] == spec["files"]
    assert receipt["manifest_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert receipt["environment"] == {"PEER_SUBS_DIR": None}


def test_changed_snapshot_fails(tmp_path):
    path, data, spec = manifest(tmp_path)
    data.write_text("changed")
    with pytest.raises(M.PreflightError, match="checksum"):
        M.verify(path, invocation=spec["invocation"], environ={})


def test_manifest_cannot_omit_an_actual_input(tmp_path):
    path, _, spec = manifest(tmp_path)
    with pytest.raises(M.PreflightError, match="unlisted input"):
        M.verify(path, invocation=spec["invocation"], required_paths=[tmp_path / "other"], environ={})


def test_environment_mismatch_never_prints_actual_value(tmp_path):
    path, _, spec = manifest(tmp_path)
    with pytest.raises(M.PreflightError, match="environment mismatch: PEER_SUBS_DIR") as exc:
        M.verify(path, invocation=spec["invocation"], environ={"PEER_SUBS_DIR": "PRIVATE_ACTUAL_VALUE"})
    assert "PRIVATE_ACTUAL_VALUE" not in str(exc.value)


def test_required_environment_cannot_be_omitted(tmp_path):
    path, _, spec = manifest(tmp_path)
    with pytest.raises(M.PreflightError, match="unlisted environment"):
        M.verify(path, invocation=spec["invocation"], required_env=["PEER_MARKET_DB"], environ={})


def test_changed_invocation_is_not_the_same_study(tmp_path):
    path, _, _ = manifest(tmp_path)
    with pytest.raises(M.PreflightError, match="invocation mismatch"):
        M.verify(path, invocation=["study.py", "different"], environ={})


@pytest.mark.parametrize("name", ["API_KEY", "SEC_USER_AGENT", "ACCESS_TOKEN"])
def test_secret_values_are_never_accepted_in_manifest(tmp_path, name):
    path, _, spec = manifest(tmp_path, environment={name: "DO_NOT_PRINT"})
    with pytest.raises(M.PreflightError) as exc:
        M.verify(path, invocation=spec["invocation"], environ={})
    assert "DO_NOT_PRINT" not in str(exc.value)


def test_endpoint_drift_is_rejected(tmp_path):
    path, _, spec = manifest(tmp_path, environment={"APCA_DATA_URL": None})
    with pytest.raises(M.PreflightError, match="environment mismatch: APCA_DATA_URL"):
        M.verify(path, invocation=spec["invocation"], environ={"APCA_DATA_URL": "https://other.example"})


def test_endpoint_receipt_cannot_contain_credentials(tmp_path):
    value = "https://user:DO_NOT_PRINT@example.com"
    path, _, spec = manifest(tmp_path, environment={"APCA_DATA_URL": value})
    with pytest.raises(M.PreflightError) as exc:
        M.verify(path, invocation=spec["invocation"], environ={"APCA_DATA_URL": value})
    assert "DO_NOT_PRINT" not in str(exc.value)


def test_directory_snapshot_hash_changes_when_member_changes(tmp_path):
    folder = tmp_path / "subs"
    folder.mkdir()
    (folder / "a.json").write_text("a")
    digest = M.fingerprint(folder)
    path, _, spec = manifest(tmp_path, files=[{"path": str(folder), "sha256": digest}])
    M.verify(path, invocation=spec["invocation"], required_paths=[folder / "a.json"], environ={})
    (folder / "b.json").write_text("b")
    with pytest.raises(M.PreflightError, match="checksum"):
        M.verify(path, invocation=spec["invocation"], environ={})


def test_sqlite_wal_is_not_an_immutable_snapshot(tmp_path):
    path, data, spec = manifest(tmp_path)
    Path(str(data) + "-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(M.PreflightError, match="WAL"):
        M.verify(path, invocation=spec["invocation"], environ={})


def test_missing_manifest_fails_before_creating_receipt(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALYST_INPUT_MANIFEST", raising=False)
    with pytest.raises(M.PreflightError, match="CATALYST_INPUT_MANIFEST"):
        M.require_manifest()


def test_receipt_is_written_and_printed_before_job_results(tmp_path, monkeypatch, capsys):
    path, data, spec = manifest(tmp_path)
    out = tmp_path / "receipt.json"
    monkeypatch.setenv("CATALYST_INPUT_MANIFEST", str(path))
    monkeypatch.setenv("CATALYST_INPUT_RECEIPT", str(out))
    monkeypatch.delenv("PEER_SUBS_DIR", raising=False)
    receipt = M.require_manifest(invocation=spec["invocation"], required_paths=[data])
    assert json.loads(out.read_text()) == receipt
    assert json.loads(capsys.readouterr().out)["input_manifest"] == receipt


def test_forbidden_directory_is_rejected_without_opening_it(tmp_path):
    with pytest.raises(M.PreflightError, match="locked"):
        M.fingerprint(tmp_path / "Catalyst-round2" / "anything")


def test_receipt_cannot_overwrite_snapshot(tmp_path, monkeypatch):
    path, data, spec = manifest(tmp_path)
    monkeypatch.setenv("CATALYST_INPUT_MANIFEST", str(path))
    monkeypatch.setenv("CATALYST_INPUT_RECEIPT", str(data))
    monkeypatch.delenv("PEER_SUBS_DIR", raising=False)
    before = data.read_bytes()
    with pytest.raises(M.PreflightError, match="overwrite"):
        M.require_manifest(invocation=spec["invocation"])
    assert data.read_bytes() == before
