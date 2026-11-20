"""Verified, read-only access to pinned SQLite store snapshots."""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote

from catalyst_data.ingestion import input_manifest


@dataclass(frozen=True)
class PinnedStore:
    store_id: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class VerifiedStores:
    manifest_path: Path
    manifest_sha256: str
    invocation: tuple[str, ...]
    required_store_ids: tuple[str, ...]
    required_store_paths: tuple[tuple[str, Path], ...]
    optional_store_ids: tuple[str, ...]
    optional_store_paths: tuple[tuple[str, Path], ...]
    stores: Mapping[str, PinnedStore]
    missing_optional_store_ids: tuple[str, ...]
    required_env: tuple[str, ...]
    environ_override: Mapping[str, str] | None = field(default=None, repr=False)


def _declarations(value: Mapping[str, str | Path] | Sequence[tuple[str, str | Path]], name: str):
    if isinstance(value, Mapping):
        rows = list(value.items())
    else:
        if isinstance(value, (str, bytes)):
            raise ValueError(f"{name} must map store IDs to paths")
        try:
            rows = list(value)
        except TypeError:
            raise ValueError(f"{name} must map store IDs to paths") from None
    result = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError(f"{name} entries must be (store_id, path) pairs")
        store_id, raw_path = row
        if not isinstance(store_id, str) or not store_id.strip() or store_id != store_id.strip():
            raise ValueError("store_id must be a nonempty trimmed string")
        if store_id in seen:
            raise ValueError(f"duplicate store_id: {store_id}")
        seen.add(store_id)
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise input_manifest.PreflightError("input paths must be absolute")
        if path.is_symlink():
            raise input_manifest.PreflightError("symlink input is not a pinned snapshot")
        path = path.resolve(strict=False)
        result.append((store_id, path))
    return result


def _manifest_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise input_manifest.PreflightError("input paths must be absolute")
    if path.is_symlink():
        raise input_manifest.PreflightError("symlink input is not a pinned snapshot")
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise input_manifest.PreflightError("manifest is missing or unreadable") from None


def _sha256(path: Path) -> str:
    return input_manifest.fingerprint(path)


def load_verified_stores(
    manifest_path: str | Path,
    *,
    invocation: Sequence[str],
    required_stores: Mapping[str, str | Path] | Sequence[tuple[str, str | Path]],
    optional_stores: Mapping[str, str | Path] | Sequence[tuple[str, str | Path]] = (),
    required_env: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> VerifiedStores:
    """Verify manifest, environment, and declared SQLite files before opening any DB."""
    if isinstance(invocation, (str, bytes)) or not all(isinstance(part, str) for part in invocation):
        raise ValueError("invocation must be a sequence of strings")
    invocation = tuple(invocation)
    required = _declarations(required_stores, "required_stores")
    optional = _declarations(optional_stores, "optional_stores")
    required_ids = {store_id for store_id, _ in required}
    if required_ids.intersection(store_id for store_id, _ in optional):
        raise ValueError("store_id cannot be both required and optional")
    if not required:
        raise ValueError("at least one required store is needed")
    if not isinstance(required_env, (tuple, list, set, frozenset)):
        required_env = tuple(required_env)
    required_env = tuple(required_env)
    if any(not isinstance(name, str) or not name for name in required_env):
        raise ValueError("required_env entries must be nonempty strings")

    missing_required = [store_id for store_id, path in required if not path.is_file()]
    if missing_required:
        raise input_manifest.PreflightError("missing required store snapshot")
    present_optional = [(store_id, path) for store_id, path in optional if path.exists()]
    bad_optional = [path for _, path in present_optional if not path.is_file()]
    if bad_optional:
        raise input_manifest.PreflightError("optional store path is not a file")
    declarations = required + present_optional
    paths = [path for _, path in declarations]

    manifest = _manifest_path(manifest_path)
    safe_environment = os.environ if environ is None else environ
    receipt = input_manifest.verify(
        manifest, invocation=list(invocation), required_paths=paths,
        required_env=required_env, environ=safe_environment,
    )
    receipt_by_path = {Path(row["path"]): row["sha256"] for row in receipt["files"]}
    stores = {}
    for store_id, path in declarations:
        # Require each database to be an explicitly hashed manifest artifact.
        digest = receipt_by_path.get(path)
        if digest is None or _sha256(path) != digest:
            raise input_manifest.PreflightError("store is not an explicitly pinned manifest file")
        if Path(str(path) + "-wal").exists():
            raise input_manifest.PreflightError("SQLite snapshot has a WAL sidecar")
        stores[store_id] = PinnedStore(store_id, path, digest)
    return VerifiedStores(
        manifest_path=manifest,
        manifest_sha256=receipt["manifest_sha256"],
        invocation=invocation,
        required_store_ids=tuple(store_id for store_id, _ in required),
        required_store_paths=tuple(required),
        optional_store_ids=tuple(store_id for store_id, _ in optional),
        optional_store_paths=tuple(optional),
        stores=MappingProxyType(stores),
        missing_optional_store_ids=tuple(store_id for store_id, path in optional if not path.exists()),
        required_env=required_env,
        environ_override=environ,
    )


def _verified_receipt(verified: VerifiedStores):
    if not isinstance(verified, VerifiedStores):
        raise input_manifest.PreflightError("unverified store set")
    required = dict(verified.required_store_paths)
    optional = dict(verified.optional_store_paths)
    if not required:
        raise input_manifest.PreflightError("at least one required store pin is necessary")
    if any(not isinstance(store_id, str) or not store_id.strip() or store_id != store_id.strip()
           for store_id in (*verified.required_store_ids, *verified.optional_store_ids)):
        raise input_manifest.PreflightError("invalid verified store identity")
    if (len(required) != len(verified.required_store_paths)
            or len(optional) != len(verified.optional_store_paths)
            or set(required) != set(verified.required_store_ids)
            or set(optional) != set(verified.optional_store_ids)
            or set(required).intersection(optional)):
        raise input_manifest.PreflightError("invalid verified store declarations")
    present_optional = {store_id for store_id, path in optional.items() if path.exists()}
    expected_ids = set(required).union(present_optional)
    if set(verified.stores) != expected_ids:
        raise input_manifest.PreflightError("verified store set is incomplete or contains undeclared pins")
    expected_missing = tuple(store_id for store_id, path in verified.optional_store_paths if not path.exists())
    if tuple(verified.missing_optional_store_ids) != expected_missing:
        raise input_manifest.PreflightError("optional store audit does not match declarations")
    declarations = required | {key: value for key, value in optional.items() if key in present_optional}
    for store_id, path in declarations.items():
        pin = verified.stores[store_id]
        if (not isinstance(pin, PinnedStore) or pin.store_id != store_id
                or pin.path != path or not re.fullmatch(r"[0-9a-f]{64}", pin.sha256)):
            raise input_manifest.PreflightError("store pin does not match its declared path")

    manifest = _manifest_path(verified.manifest_path)
    current_environment = os.environ if verified.environ_override is None else verified.environ_override
    receipt = input_manifest.verify(
        manifest, invocation=list(verified.invocation),
        required_paths=list(declarations.values()),
        required_env=verified.required_env,
        environ=current_environment,
    )
    if receipt["manifest_sha256"] != verified.manifest_sha256:
        raise input_manifest.PreflightError("input manifest changed after store verification")
    current = {Path(row["path"]): row["sha256"] for row in receipt["files"]}
    for store_id, path in declarations.items():
        pin = verified.stores[store_id]
        if current.get(path) != pin.sha256 or _sha256(path) != pin.sha256:
            raise input_manifest.PreflightError("store snapshot changed after verification")
        if Path(str(path) + "-wal").exists():
            raise input_manifest.PreflightError("SQLite snapshot has a WAL sidecar")
    return receipt


def validate_verified_stores(verified: VerifiedStores) -> None:
    """Revalidate all declared stores, including a nonempty required pin set."""
    _verified_receipt(verified)


def _revalidate(verified: VerifiedStores, store: PinnedStore) -> Path:
    if not isinstance(store, PinnedStore):
        raise input_manifest.PreflightError("unverified store pin")
    _verified_receipt(verified)
    if verified.stores.get(store.store_id) != store:
        raise input_manifest.PreflightError("store pin does not belong to this verified manifest")
    return store.path


@contextmanager
def readonly_snapshot(verified: VerifiedStores, store: PinnedStore) -> Iterator[sqlite3.Connection]:
    """Open a revalidated immutable snapshot in a query-only read transaction."""
    path = _revalidate(verified, store)
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise input_manifest.PreflightError("store snapshot cannot be opened read-only") from exc
    before = store.sha256
    try:
        yield conn
    finally:
        try:
            if _sha256(path) != before:
                raise input_manifest.PreflightError("store snapshot changed during read")
            _verified_receipt(verified)
        finally:
            try:
                if conn.in_transaction:
                    conn.rollback()
            finally:
                conn.close()


__all__ = ["PinnedStore", "VerifiedStores", "load_verified_stores", "readonly_snapshot", "validate_verified_stores"]
