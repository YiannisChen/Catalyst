"""Side-effect-free environment resolution for daily ingestion paths."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from catalyst_data.storage.runtime_paths import runtime_data_dir, runtime_store_path


def resolve_path_values(repo: Path, environ: Mapping[str, str]) -> dict:
    """Resolve daily paths and flags from an environment snapshot without mutating it."""
    def env_path(name: str, default: str) -> Path:
        return Path(environ.get(name) or repo / default)

    data_dir = Path(environ.get("CATALYST_DATA_DIR") or runtime_data_dir(repo, environ))
    try:
        min_free_gb = float(environ.get("CATALYST_CORPUS_MIN_FREE_GB", "10"))
        if not math.isfinite(min_free_gb) or min_free_gb < 0:
            min_free_gb = 10.0
    except ValueError:
        min_free_gb = 10.0

    rebuild_flag = environ.get("CATALYST_CORPUS_FTS_REBUILD", "").strip().lower()
    vacuum_flag = environ.get("CATALYST_CORPUS_VACUUM", "").strip().lower()
    peer_map_path = env_path("CATALYST_PEER_MAP", "data/manifests/peer_map_v1.json")
    return {
        "market_db": runtime_store_path("market_db", repo, environ),
        "bodies_db": runtime_store_path("bodies_db", repo, environ),
        "peer_map": json.loads(peer_map_path.read_text()),
        "peer_map_path": peer_map_path,
        "snapshot_db": resolve_optional_snapshot(repo, environ),
        "jobs_db": runtime_store_path("jobs_db", repo, environ),
        "archive_db": runtime_store_path("archive_db", repo, environ),
        "corpus_db": Path(environ["CATALYST_CORPUS_DB"])
        if environ.get("CATALYST_CORPUS_DB") else None,
        "alpaca_news_db": Path(environ["CATALYST_ALPACA_NEWS_DB"])
        if environ.get("CATALYST_ALPACA_NEWS_DB") else None,
        "corpus_fts_rebuild_enabled": rebuild_flag in {"1", "true", "yes", "on"},
        "corpus_vacuum_after_prune": vacuum_flag in {"1", "true", "yes", "on"},
        "pipeline_status_path": Path(environ.get("CATALYST_DAILY_PIPELINE_STATUS")
                                      or data_dir / "daily_pipeline_status.json"),
        "corpus_min_free_gb": min_free_gb,
    }


def resolve_optional_snapshot(repo: Path, environ: Mapping[str, str]) -> Path | None:
    """Return the configured snapshot only when it exists; never rewrite the environment."""
    value = Path(environ.get("CATALYST_SNAPSHOT_DB")
                 or repo / "data/snapshots/catalyst_m8a_derivative_7a004acc.db")
    return value if value.exists() else None


def deepseek_api_key(environ: Mapping[str, str]) -> str:
    """Read the scheduled classifier key at the point the schedule needs it."""
    return environ.get("DEEPSEEK_API_KEY", "").strip()


def resolve_backfill_inputs(repo: Path, environ: Mapping[str, str]) -> tuple[list[Path], list[str]]:
    """Resolve the input manifest paths and environment names for a CLI backfill."""
    core_stores = (
        ("market_db", "CATALYST_MARKET_CONTEXT_DB"),
        ("bodies_db", "CATALYST_NEWS_BODIES_DB"),
        ("jobs_db", "CATALYST_UPDATE_JOBS_DB"),
        ("archive_db", "CATALYST_HEADLINE_ARCHIVE_DB"),
    )
    snapshot_env = {"CATALYST_SNAPSHOT_DB": "data/snapshots/catalyst_m8a_derivative_7a004acc.db"}
    inputs = [runtime_store_path(store, repo, environ) for store, _ in core_stores]
    inputs.extend(Path(environ.get(key) or repo / default)
                  for key, default in snapshot_env.items())
    inputs = [path for path in inputs if path.exists()]
    inputs.extend((
        Path(environ.get("CATALYST_PEER_MAP") or repo / "data/manifests/peer_map_v1.json"),
        repo / "data/manifests",
    ))
    environment = [*(name for _, name in core_stores), *snapshot_env,
                   "CATALYST_RUNTIME_DATA_DIR", "CATALYST_PEER_MAP", "CATALYST_PREFER_IPV4"]
    return inputs, environment


def publish_paths_environment(paths, environ: MutableMapping[str, str], repo: Path) -> None:
    """Expose resolved stores for legacy downstream helpers at an operation boundary."""
    for name, value in (
        ("CATALYST_MARKET_CONTEXT_DB", paths.market_db),
        ("CATALYST_NEWS_BODIES_DB", paths.bodies_db),
        ("CATALYST_PEER_MAP", paths.peer_map_path
         or Path(environ.get("CATALYST_PEER_MAP") or repo / "data/manifests/peer_map_v1.json")),
        ("CATALYST_UPDATE_JOBS_DB", paths.jobs_db),
        ("CATALYST_HEADLINE_ARCHIVE_DB", paths.archive_db),
    ):
        environ[name] = str(value)
    if paths.snapshot_db is None:
        environ.pop("CATALYST_SNAPSHOT_DB", None)
    else:
        environ["CATALYST_SNAPSHOT_DB"] = str(paths.snapshot_db)
