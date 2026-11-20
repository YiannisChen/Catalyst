"""Identity and lookup helpers for completed anonymous public results."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from catalyst_data.storage.errors import StoreReadError

from catalyst_app.persistence.connect import open_readonly


def production_strategy_identity() -> str:
    """Return a stable digest of the production policy recorded in manifests.

    The policy version is the explicit bump point for strategy changes not
    represented by an attribution-policy field. The serialized policy binds
    the active rule, verifier, evidence-window, and investigation choices.
    """
    from catalyst_agents.attribution.policy import resolve_runtime_attribution_policy

    from catalyst_app.runtime.manifest_factory import PRODUCTION_POLICY_VERSION

    return compute_strategy_identity(
        policy_version=PRODUCTION_POLICY_VERSION,
        attribution_policy=resolve_runtime_attribution_policy().model_dump(mode="json"),
    )


def compute_strategy_identity(
    *, policy_version: str, attribution_policy: dict[str, Any]
) -> str:
    """Hash the versioned strategy fields shared with the run manifest."""
    payload = {
        "policy_version": policy_version,
        "attribution_policy": attribution_policy,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_public_cache_key(
    *,
    ticker: str,
    session_date: str,
    data_identity: str,
    provider: str,
    model_id: str,
    base_url: str,
    config_version: str,
    strategy_identity: str,
) -> str:
    """Hash normalized public-run identity without credentials or transport data.

    ``data_identity`` binds the prepared-data build so rebuilt sessions cannot
    reuse an answer produced from older data.
    """
    payload = {
        "ticker": ticker.strip().upper(),
        "session_date": session_date,
        "data_identity": data_identity,
        "provider": provider,
        "model_id": model_id,
        "base_url": base_url.rstrip("/"),
        "config_version": config_version,
        "strategy_identity": strategy_identity,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def find_completed_public_run(db_path: str | Path, cache_key: str) -> str | None:
    """Find only a completed row explicitly marked as public and cacheable."""
    path = Path(db_path)
    if not path.is_file():
        cause = FileNotFoundError(path)
        raise StoreReadError(path, "read public result cache", str(cause)) from cause
    try:
        with open_readonly(path) as conn:
            row = conn.execute(
                "SELECT run_id FROM runs"
                " WHERE public_cache_key = ? AND visibility = 'public'"
                " AND lifecycle_status = 'COMPLETED'"
                " ORDER BY updated_at DESC, run_id DESC LIMIT 1",
                (cache_key,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise StoreReadError(path, "read public result cache", str(exc)) from exc
    return str(row["run_id"]) if row is not None else None


__all__ = [
    "compute_public_cache_key",
    "compute_strategy_identity",
    "find_completed_public_run",
    "production_strategy_identity",
]
