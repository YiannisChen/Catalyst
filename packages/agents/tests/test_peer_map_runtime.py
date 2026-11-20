from __future__ import annotations

import json
from pathlib import Path

import pytest
from catalyst_agents.runtime.peer_map import (
    PeerMapUnavailableError,
    load_peer_map,
    load_peer_map_from_env,
)


def test_missing_configured_peer_map_fails_context_creation(tmp_path, monkeypatch):
    missing = tmp_path / "missing-peer-map.json"
    monkeypatch.setenv("CATALYST_PEER_MAP", str(missing))

    with pytest.raises(PeerMapUnavailableError, match="peer_map_unavailable"):
        load_peer_map_from_env()


def test_unreadable_peer_map_has_explicit_failure_reason(tmp_path, monkeypatch):
    path = tmp_path / "peer-map.json"
    path.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic unreadable peer map")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    with pytest.raises(PeerMapUnavailableError, match="PermissionError"):
        load_peer_map(path)


def test_present_peer_map_preserves_mapped_sector_and_peers(tmp_path, monkeypatch):
    path = tmp_path / "peer-map.json"
    value = {
        "benchmark": "SPY",
        "targets": {"ABC": {"sector_etf": "XLK", "direct_peers": ["XYZ"]}},
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setenv("CATALYST_PEER_MAP", str(path))

    from catalyst_agents.runtime.context import SQLiteContextProvider

    provider = SQLiteContextProvider(tmp_path / "snapshot.sqlite")

    assert provider.peer_map == value
    assert provider._resolve_context(None, "ABC", "2026-01-02") == ("SPY", "XLK", ("XYZ",))


def test_unconfigured_peer_map_uses_the_repository_default():
    default_path = Path(__file__).resolve().parents[3] / "data/manifests/peer_map_v1.json"

    peer_map = load_peer_map_from_env({})

    assert peer_map == json.loads(default_path.read_text(encoding="utf-8"))
    assert peer_map["targets"]


def test_unconfigured_peer_map_uses_repository_default_from_other_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    peer_map = load_peer_map_from_env({})

    assert peer_map["benchmark"] == "SPY"
    assert peer_map["targets"]


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"benchmark": "SPY", "targets": {}},
        {"targets": {"ABC": {"sector_etf": "XLK", "direct_peers": ["XYZ"]}}},
    ],
    ids=["empty-object", "empty-targets", "missing-benchmark"],
)
def test_invalid_peer_map_shape_is_unavailable(tmp_path, value):
    path = tmp_path / "peer-map.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(PeerMapUnavailableError, match="peer_map_unavailable"):
        load_peer_map(path)




def test_market_store_falls_back_to_the_runtime_data_root(tmp_path, monkeypatch):
    from catalyst_agents.runtime.context import SQLiteContextProvider

    # The earnings node and health check find the market store under CATALYST_RUNTIME_DATA_DIR; the session
    # context reader must find the same file, or every run loses its market and sector returns.
    monkeypatch.delenv("CATALYST_MARKET_CONTEXT_DB", raising=False)
    monkeypatch.setenv("CATALYST_RUNTIME_DATA_DIR", str(tmp_path))
    assert SQLiteContextProvider(tmp_path / "corpus.sqlite3").market_db is None
    (tmp_path / "market_context.sqlite3").write_bytes(b"")
    assert SQLiteContextProvider(tmp_path / "corpus.sqlite3").market_db == tmp_path / "market_context.sqlite3"
