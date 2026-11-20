"""Strict loading for the configured peer map used by publication contexts."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from catalyst_data.storage.runtime_paths import repository_root

DEFAULT_PEER_MAP_PATH = repository_root() / "data/manifests/peer_map_v1.json"


class PeerMapUnavailableError(RuntimeError):
    """The configured peer map could not be read as a JSON object."""


def load_peer_map(path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path).expanduser() if path is not None else DEFAULT_PEER_MAP_PATH
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PeerMapUnavailableError(
            f"peer_map_unavailable: cannot read {source} ({type(exc).__name__})"
        ) from exc
    if not isinstance(value, dict):
        raise PeerMapUnavailableError(
            f"peer_map_unavailable: expected a JSON object in {source}"
        )
    targets = value.get("targets")
    benchmark = value.get("benchmark")
    if not isinstance(targets, dict) or not targets:
        raise PeerMapUnavailableError(
            f"peer_map_unavailable: expected non-empty targets object in {source}"
        )
    if not isinstance(benchmark, str) or not benchmark.strip():
        raise PeerMapUnavailableError(
            f"peer_map_unavailable: expected non-empty benchmark string in {source}"
        )
    return value


def load_peer_map_from_env(env: Any = None) -> dict[str, Any]:
    env = os.environ if env is None else env
    configured = env.get("CATALYST_PEER_MAP")
    path = configured.strip() if isinstance(configured, str) else ""
    return load_peer_map(path or None)


__all__ = [
    "DEFAULT_PEER_MAP_PATH",
    "PeerMapUnavailableError",
    "load_peer_map",
    "load_peer_map_from_env",
]
