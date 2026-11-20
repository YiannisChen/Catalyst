from __future__ import annotations

from pathlib import Path

from catalyst_data.ingestion.ops import daily_config


def test_deepseek_api_key_reads_and_trims_the_current_environment_value():
    environ = {}
    assert daily_config.deepseek_api_key(environ) == ""
    environ["DEEPSEEK_API_KEY"] = "  test-key  "
    assert daily_config.deepseek_api_key(environ) == "test-key"
    environ["DEEPSEEK_API_KEY"] = ""
    assert daily_config.deepseek_api_key(environ) == ""


def test_backfill_inputs_preserve_overrides_filtering_and_environment_names(tmp_path: Path):
    repo = tmp_path / "repo"
    manifest_dir = repo / "data/manifests"
    manifest_dir.mkdir(parents=True)

    existing = {
        "CATALYST_MARKET_CONTEXT_DB": tmp_path / "market.sqlite3",
        "CATALYST_UPDATE_JOBS_DB": tmp_path / "jobs.sqlite3",
    }
    for path in existing.values():
        path.touch()
    peer_map = tmp_path / "custom-peers.json"
    snapshot = tmp_path / "missing-snapshot.db"
    environ = {
        **{name: str(path) for name, path in existing.items()},
        "CATALYST_NEWS_BODIES_DB": str(tmp_path / "missing-bodies.sqlite3"),
        "CATALYST_HEADLINE_ARCHIVE_DB": str(tmp_path / "missing-archive.sqlite3"),
        "CATALYST_SNAPSHOT_DB": str(snapshot),
        "CATALYST_PEER_MAP": str(peer_map),
    }

    inputs, environment = daily_config.resolve_backfill_inputs(repo, environ)

    assert inputs == [existing["CATALYST_MARKET_CONTEXT_DB"], existing["CATALYST_UPDATE_JOBS_DB"],
                      peer_map, manifest_dir]
    assert environment == [
        "CATALYST_MARKET_CONTEXT_DB", "CATALYST_NEWS_BODIES_DB",
        "CATALYST_UPDATE_JOBS_DB", "CATALYST_HEADLINE_ARCHIVE_DB",
        "CATALYST_SNAPSHOT_DB", "CATALYST_RUNTIME_DATA_DIR", "CATALYST_PEER_MAP",
        "CATALYST_PREFER_IPV4",
    ]


def test_backfill_inputs_use_repository_defaults_when_environment_is_unset(tmp_path: Path):
    repo = tmp_path / "repo"
    manifest_dir = repo / "data/manifests"
    manifest_dir.mkdir(parents=True)
    peer_map = manifest_dir / "peer_map_v1.json"
    snapshot = repo / "data/snapshots/catalyst_m8a_derivative_7a004acc.db"
    snapshot.parent.mkdir(parents=True)
    snapshot.touch()
    (repo / ".local/data/ingestion").mkdir(parents=True)
    runtime_dir = repo / ".local/data/ingestion"
    core_paths = [
        runtime_dir / "market_context.sqlite3",
        runtime_dir / "news_bodies_v2.sqlite3",
        runtime_dir / "update_jobs.sqlite3",
        runtime_dir / "headline_archive.sqlite3",
    ]
    for path in core_paths:
        path.touch()

    inputs, _environment = daily_config.resolve_backfill_inputs(repo, {})

    assert inputs == [*core_paths, snapshot, peer_map, manifest_dir]
