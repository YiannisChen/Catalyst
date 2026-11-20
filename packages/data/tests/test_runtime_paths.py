from __future__ import annotations

from pathlib import Path

import pytest
from catalyst_data.ingestion import categorize_llm, corp_actions, media
from catalyst_data.ingestion.collectors import (
    sec,
    sec_earnings,
    sec_filings,
    web_search,
)
from catalyst_data.ingestion.ops import daily_config
from catalyst_data.storage import runtime_paths
from catalyst_data.storage.runtime_paths import (
    LEGACY_DATA_MARKERS,
    default_sec_tickers_path,
    runtime_data_dir,
    runtime_store_path,
)


def test_core_store_defaults_are_repo_anchored_and_do_not_create_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CATALYST_DATA_DIR", raising=False)
    monkeypatch.delenv("CATALYST_RUNTIME_DATA_DIR", raising=False)
    paths = {
        name: runtime_store_path(name, repo=tmp_path, environ={})
        for name in ("market_db", "bodies_db", "jobs_db", "archive_db")
    }

    assert paths == {
        "market_db": tmp_path / ".local/data/ingestion/market_context.sqlite3",
        "bodies_db": tmp_path / ".local/data/ingestion/news_bodies_v2.sqlite3",
        "jobs_db": tmp_path / ".local/data/ingestion/update_jobs.sqlite3",
        "archive_db": tmp_path / ".local/data/ingestion/headline_archive.sqlite3",
    }
    assert runtime_data_dir(tmp_path) == tmp_path / ".local/data/ingestion"
    assert not (tmp_path / ".local").exists()


@pytest.mark.parametrize("marker", LEGACY_DATA_MARKERS)
def test_any_canonical_legacy_marker_keeps_the_whole_family_on_legacy_root(tmp_path, marker):
    repo = tmp_path / marker.replace(".", "-")
    legacy_root = repo / "data/m9"
    legacy_root.mkdir(parents=True)
    marker_path = legacy_root / marker
    if marker in {"sec_submissions", "sec_earnings"}:
        marker_path.mkdir()
    else:
        marker_path.touch()

    assert runtime_data_dir(repo, {}) == legacy_root
    assert runtime_store_path("market_db", repo, {}) == legacy_root / "market_context.sqlite3"
    assert runtime_store_path("bodies_db", repo, {}) == legacy_root / "news_bodies_v2.sqlite3"


def test_runtime_root_override_wins_over_legacy_markers(tmp_path):
    repo = tmp_path / "repo"
    legacy_root = repo / "data/m9"
    legacy_root.mkdir(parents=True)
    (legacy_root / "news_bodies_v2.sqlite3").touch()

    assert runtime_data_dir(repo, {"CATALYST_RUNTIME_DATA_DIR": "custom-root"}) == repo / "custom-root"


def test_individual_store_override_wins_without_mutating_environment(tmp_path):
    env = {"CATALYST_DATA_DIR": "/status-only", "CATALYST_NEWS_BODIES_DB": "~/body.sqlite3"}
    before = dict(env)

    assert runtime_store_path("bodies_db", repo=tmp_path, environ=env) == Path("~/body.sqlite3").expanduser()
    assert runtime_store_path("jobs_db", repo=tmp_path, environ=env) == \
        tmp_path / ".local/data/ingestion/update_jobs.sqlite3"
    assert env == before


def test_web_search_default_is_repository_rooted_and_side_effect_free(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_paths, "repository_root", lambda: tmp_path)
    monkeypatch.delenv("CATALYST_WEB_SEARCH_DB", raising=False)

    expected = tmp_path / ".local/data/ingestion/web_search.sqlite3"
    assert runtime_store_path("web_search_db") == expected
    assert web_search._db_path() == expected
    assert not (tmp_path / "data").exists()


def test_web_search_override_keeps_relative_path_semantics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CATALYST_WEB_SEARCH_DB", "custom/store.sqlite3")

    assert runtime_store_path("web_search_db") == Path("custom/store.sqlite3")
    assert web_search._db_path() == Path("custom/store.sqlite3")
    assert not (tmp_path / "custom").exists()


def test_runtime_root_override_is_repo_anchored_and_per_store_override_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    env = {"CATALYST_RUNTIME_DATA_DIR": "runtime-state",
           "CATALYST_NEWS_BODIES_DB": "custom/body.sqlite3"}

    assert runtime_data_dir(repo, env) == repo / "runtime-state"
    assert runtime_store_path("market_db", repo, env) == repo / "runtime-state/market_context.sqlite3"
    assert runtime_store_path("bodies_db", repo, env) == Path("custom/body.sqlite3")
    assert default_sec_tickers_path(repo, env) == repo / "runtime-state/sec_company_tickers.json"
    assert not (repo / "runtime-state").exists()

    absolute_root = tmp_path / "absolute-state"
    assert runtime_data_dir(repo, {"CATALYST_RUNTIME_DATA_DIR": str(absolute_root)}) == absolute_root
    assert not absolute_root.exists()

def test_auxiliary_store_defaults_are_repo_rooted_and_overrides_do_not_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_paths, "repository_root", lambda: tmp_path)
    stores = (
        ("corp_actions_db", "CATALYST_CORP_ACTIONS_DB", "corp_actions.sqlite3", corp_actions.store_path),
        ("news_categories_db", "CATALYST_NEWS_CATEGORIES_DB", "news_categories.sqlite3", categorize_llm.default_db),
        ("media_db", "CATALYST_MEDIA_DB", "media_cache.sqlite3", media.cache_path),
    )
    for _, env_name, _, _ in stores:
        monkeypatch.delenv(env_name, raising=False)

    for store, _, filename, resolver in stores:
        expected = tmp_path / ".local/data/ingestion" / filename
        assert runtime_store_path(store) == expected
        assert resolver() == expected
    assert not (tmp_path / "data").exists()

    for _, env_name, _, resolver in stores:
        relative = f"custom/{env_name.lower()}.sqlite3"
        monkeypatch.setenv(env_name, relative)
        assert resolver() == Path(relative)
        assert not (tmp_path / "custom").exists()


def test_sec_cache_defaults_are_repo_rooted_and_overrides_do_not_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_paths, "repository_root", lambda: tmp_path)
    caches = (
        ("sec_submissions_dir", "CATALYST_SEC_SUBMISSIONS_DIR", "sec_submissions", sec_filings._dir),
        ("sec_earnings_cache", "CATALYST_SEC_EARNINGS_CACHE", "sec_earnings", sec_earnings._cache_dir),
    )
    for _, env_name, _, _ in caches:
        monkeypatch.delenv(env_name, raising=False)

    for store, _, dirname, resolver in caches:
        expected = tmp_path / ".local/data/ingestion" / dirname
        assert runtime_store_path(store) == expected
        assert resolver() == expected
    assert not (tmp_path / "data").exists()

    for _, env_name, _, resolver in caches:
        relative = f"custom/{env_name.lower()}"
        monkeypatch.setenv(env_name, relative)
        assert resolver() == Path(relative)
        assert not (tmp_path / "custom").exists()


def test_sec_ticker_map_default_is_repo_rooted_and_side_effect_free(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CATALYST_RUNTIME_DATA_DIR", str(tmp_path / ".local/data/ingestion"))
    expected = tmp_path / ".local/data/ingestion/sec_company_tickers.json"

    assert default_sec_tickers_path() == expected
    assert default_sec_tickers_path(tmp_path) == expected
    assert sec._tickers_path() == expected
    assert not (tmp_path / ".local").exists()






def test_daily_status_default_uses_runtime_data_dir_but_preserves_override_semantics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    peer_map = tmp_path / "peer.json"
    peer_map.write_text("{}")

    defaults = daily_config.resolve_path_values(repo, {"CATALYST_PEER_MAP": str(peer_map)})
    assert defaults["pipeline_status_path"] == repo / ".local/data/ingestion/daily_pipeline_status.json"

    relative_data = daily_config.resolve_path_values(repo, {
        "CATALYST_PEER_MAP": str(peer_map),
        "CATALYST_DATA_DIR": "relative-data",
        "CATALYST_DAILY_PIPELINE_STATUS": "status.json",
    })
    assert relative_data["pipeline_status_path"] == Path("status.json")

    relative_default = daily_config.resolve_path_values(repo, {
        "CATALYST_PEER_MAP": str(peer_map),
        "CATALYST_DATA_DIR": "relative-data",
    })
    assert relative_default["pipeline_status_path"] == Path("relative-data/daily_pipeline_status.json")
    assert not (repo / ".local").exists()


def test_daily_config_uses_injected_runtime_root_snapshot_consistently(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    peer_map = tmp_path / "peer.json"
    peer_map.write_text("{}")
    env = {
        "CATALYST_RUNTIME_DATA_DIR": "state",
        "CATALYST_PEER_MAP": str(peer_map),
        "CATALYST_NEWS_BODIES_DB": "custom/bodies.sqlite3",
    }

    paths = daily_config.resolve_path_values(repo, env)
    assert paths["market_db"] == repo / "state/market_context.sqlite3"
    assert paths["bodies_db"] == Path("custom/bodies.sqlite3")
    assert paths["jobs_db"] == repo / "state/update_jobs.sqlite3"
    assert paths["archive_db"] == repo / "state/headline_archive.sqlite3"
    assert paths["pipeline_status_path"] == repo / "state/daily_pipeline_status.json"
    assert default_sec_tickers_path(repo, env) == repo / "state/sec_company_tickers.json"
    assert not (repo / "state").exists()
