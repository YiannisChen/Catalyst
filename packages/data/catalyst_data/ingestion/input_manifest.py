"""Fail-closed input provenance for explicit research/backfill invocations (stdlib only).

Manifests pin immutable file/directory snapshots, safe environment settings and the
exact invocation. They are supplied by the operator; verification never auto-pins
whatever inputs happen to be present. Mutable production SQLite stores must first
be copied with SQLite's backup API and checkpointed, then hashed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping


class PreflightError(ValueError):
    """The job is not permitted to proceed with these inputs."""


_SECRET = re.compile(r"key|token|secret|password|credential|cookie|user_agent", re.I)
_SAFE_ENV = re.compile(r"(?:PYTHONPATH|APCA_DATA_URL|CATALYST_PEER_MAP|CATALYST_PREFER_IPV4|[A-Z][A-Z0-9_]*(?:_DB|_DIR|_PATH|_ROOT|_MODE|_OFFLINE))\Z")
_LOCKED = {"Catalyst-prospective", "Catalyst-round2"}


def _path(value: str | Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise PreflightError("input paths must be absolute")
    if p.is_symlink():
        raise PreflightError("symlink input is not a pinned snapshot")
    p = p.resolve()
    if _LOCKED.intersection(p.parts):
        raise PreflightError("locked evaluation directory is not a data-task input")
    if p.name == ".env" or p.suffix in (".pem", ".key"):
        raise PreflightError("credential files are not input-manifest artifacts")
    return p


def _file_hash(path: Path) -> str:
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise PreflightError("input has a live WAL; create an immutable SQLite backup first")
    before = path.stat()
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise PreflightError("input changed during checksum verification")
    return h.hexdigest()


def fingerprint(value: str | Path) -> str:
    """File SHA-256, or SHA-256 of sorted JSON [relative path, file SHA-256] rows."""
    path = _path(value)
    try:
        if path.is_file():
            return _file_hash(path)
        if not path.is_dir():
            raise PreflightError("missing input snapshot")
        members = sorted(path.rglob("*"))
        rows = []
        for member in members:
            _path(member)
            if member.is_file():
                rows.append([member.relative_to(path).as_posix(), _file_hash(member)])
        if members != sorted(path.rglob("*")):
            raise PreflightError("directory changed during checksum verification")
        payload = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()
    except OSError:
        raise PreflightError("input snapshot is missing or unreadable") from None


def verify(manifest_path: str | Path, *, invocation: list[str], required_paths: Iterable[str | Path] = (),
           required_env: Iterable[str] = (), environ: Mapping[str, str] | None = None) -> dict:
    env = os.environ if environ is None else environ
    path = _path(manifest_path)
    try:
        raw = path.read_bytes()
        spec = json.loads(raw)
    except (OSError, ValueError):
        raise PreflightError("manifest is missing or invalid JSON") from None
    if not isinstance(spec, dict) or spec.get("schema_version") != 1:
        raise PreflightError("unsupported input manifest schema")
    if spec.get("invocation") != invocation:
        raise PreflightError("invocation mismatch")
    files, settings = spec.get("files"), spec.get("environment")
    if not isinstance(files, list) or not files or not isinstance(settings, dict):
        raise PreflightError("manifest requires nonempty files and an environment map")
    safe_settings = {}
    for name, expected in settings.items():
        if _SECRET.search(name) or not _SAFE_ENV.fullmatch(name) or (expected is not None and not isinstance(expected, str)):
            raise PreflightError("manifest contains a forbidden environment field")
        if name == "APCA_DATA_URL" and expected is not None:
            from urllib.parse import urlsplit
            endpoint = urlsplit(expected)
            if endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise PreflightError("endpoint must not contain credentials, query or fragment")
        if env.get(name) != expected:
            raise PreflightError(f"environment mismatch: {name}")
        safe_settings[name] = expected
    for name in required_env:
        if name not in settings:
            raise PreflightError(f"unlisted environment: {name}")
    checked = {}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise PreflightError("invalid input file entry")
        if not isinstance(entry["path"], str) or not isinstance(entry["sha256"], str):
            raise PreflightError("invalid input file field type")
        pinned = _path(entry["path"])
        if pinned in checked or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise PreflightError("duplicate input or invalid checksum")
        if fingerprint(pinned) != entry["sha256"]:
            raise PreflightError(f"input checksum mismatch: {pinned.name}")
        checked[pinned] = entry["sha256"]
    for required in required_paths:
        target = _path(required)
        if not any(target == p or (p.is_dir() and target.is_relative_to(p)) for p in checked):
            raise PreflightError(f"unlisted input: {target.name}")
    return {"schema_version": 1, "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "verified_at": datetime.now(timezone.utc).isoformat(), "invocation": invocation,
            "files": [{"path": str(p), "sha256": sha} for p, sha in checked.items()],
            "environment": safe_settings}


def require_manifest(*, required_paths: Iterable[str | Path] = (), required_env: Iterable[str] = (),
                     invocation: list[str] | None = None) -> dict:
    """Check before any job input is opened; write receipt sidecar and stdout header."""
    specified = os.environ.get("CATALYST_INPUT_MANIFEST")
    if not specified:
        raise PreflightError("CATALYST_INPUT_MANIFEST is required")
    receipt_path = os.environ.get("CATALYST_INPUT_RECEIPT")
    if not receipt_path:
        raise PreflightError("CATALYST_INPUT_RECEIPT is required")
    receipt = verify(specified, invocation=list(sys.argv) if invocation is None else invocation,
                     required_paths=required_paths, required_env=required_env)
    dest = _path(receipt_path)
    if dest == _path(specified) or any(dest == Path(f["path"]) or (Path(f["path"]).is_dir()
                                        and dest.is_relative_to(f["path"])) for f in receipt["files"]):
        raise PreflightError("receipt must not overwrite or change a pinned input")
    try:
        with dest.open("x", encoding="utf-8") as f:
            json.dump(receipt, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError:
        raise PreflightError("receipt must be a new writable output file") from None
    print(json.dumps({"input_manifest": receipt}, ensure_ascii=False), flush=True)
    return receipt


def require_backfill(module_file: str, arguments: list[str], *, inputs: Iterable[str | Path],
                     environment: Iterable[str] = ()) -> dict:
    """Bind manual CLI inputs and implementation; scheduled collectors are unaffected."""
    paths = [Path(module_file).resolve(), Path(__file__).resolve()]
    paths += [Path(p).expanduser().resolve() for p in inputs]
    module = "catalyst_data.ingestion." + Path(module_file).stem
    return require_manifest(invocation=["-m", module, *arguments], required_paths=paths,
                            required_env=["PYTHONPATH", "CATALYST_ARCHIVE_DIR", "CATALYST_DATA_DIR",
                                          "CATALYST_RUNTIME_DATA_DIR",
                                          "CATALYST_RATELIMIT_DIR", "CATALYST_RESILIENCE_DB", *environment])
