"""Manual backfills refuse to open a target store without verified inputs."""

import pytest
from catalyst_data.ingestion.collectors import alpaca


@pytest.mark.parametrize("module,arguments", [
    (alpaca, ["news-backfill", "--dates", "dates.txt"]),
])
def test_backfill_refuses_before_opening_store(module, arguments, tmp_path, monkeypatch):
    from catalyst_data.ingestion.input_manifest import PreflightError
    monkeypatch.delenv("CATALYST_INPUT_MANIFEST", raising=False)
    monkeypatch.setattr(module, "connect", lambda *_: pytest.fail("opened DB before preflight"))
    with pytest.raises(PreflightError):
        module.main(arguments + ["--db", str(tmp_path / "output.sqlite3")])
    assert list(tmp_path.iterdir()) == []


def test_daily_backfill_manifest_binds_runtime_data_root(tmp_path, monkeypatch):
    from catalyst_data.ingestion import input_manifest
    from catalyst_data.ingestion.ops import daily

    monkeypatch.setattr(daily.headline_archive, "REPO", tmp_path)
    monkeypatch.setenv("CATALYST_RUNTIME_DATA_DIR", "runtime-state")
    monkeypatch.delenv("CATALYST_PEER_MAP", raising=False)

    def checked(_module_file, _arguments, *, inputs, environment):
        assert "CATALYST_RUNTIME_DATA_DIR" in environment
        assert inputs == [tmp_path / "data/manifests/peer_map_v1.json", tmp_path / "data/manifests"]
        raise input_manifest.PreflightError("test stops before execution")

    monkeypatch.setattr(input_manifest, "require_backfill", checked)
    with pytest.raises(input_manifest.PreflightError, match="test stops"):
        daily.main(["run", "--start", "2026-10-22"])




def test_backfill_manifest_requires_runtime_root_setting(monkeypatch):
    from catalyst_data.ingestion import input_manifest

    def capture(*, required_env, **_kwargs):
        assert "CATALYST_RUNTIME_DATA_DIR" in required_env
        raise input_manifest.PreflightError("test stops")

    monkeypatch.setattr(input_manifest, "require_manifest", capture)
    with pytest.raises(input_manifest.PreflightError, match="test stops"):
        input_manifest.require_backfill("task.py", [], inputs=[])
