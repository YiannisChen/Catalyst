"""Pure data/index identity readers for the M1 baseline seal (M1-3).

Reads the frozen snapshot DB and the LanceDB pointer/manifest with
connection-per-operation read-only SQLite connections. Missing identity
sources return ``None`` facts so the baseline report can be explicitly
NON-COMPARABLE; this module never modifies either reproduction runner.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SNAPSHOT_IDENTITY_SCHEMA = "snapshot_identity_v1"
LANCEDB_IDENTITY_SCHEMA = "lancedb_identity_v1"

_SNAPSHOT_EMPTY: dict[str, Any] = {
    "schema_version": SNAPSHOT_IDENTITY_SCHEMA,
    "corpus_manifest_id": None,
    "snapshot_id": None,
    "inventory_row_count": None,
    "tokenizer_model_id": None,
    "tokenizer_revision": None,
    "lexical_corpus_manifest_id": None,
    "lexical_mode_served": None,
    "lexical_row_count": None,
    "lexical_generation_id": None,
    "lexical_digest": None,
    "corpus_served_chunks_count": None,
    "corpus_build_chunks_fts_count": None,
    "articles_count": None,
    "filings_count": None,
}

_LANCEDB_EMPTY: dict[str, Any] = {
    "schema_version": LANCEDB_IDENTITY_SCHEMA,
    "lancedb_table_name": None,
    "snapshot_id": None,
    "corpus_manifest_id": None,
    "index_manifest_id": None,
    "source_bundle_id": None,
    "probe_report_id": None,
    "postbuild_readiness_id": None,
    "embedding_model": None,
    "embedding_dim": None,
    "chunk_count": None,
    "vector_count": None,
    "index_manifest_path": None,
}


def _read_json_pointer(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.OperationalError:
        return None


def _manifest_facts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Facts from the current corpus_manifest row (is_current = 1)."""
    try:
        row = conn.execute(
            "SELECT manifest_id, manifest_json FROM corpus_manifest WHERE is_current = 1 LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None:
        return {}
    manifest_id, manifest_json = row
    try:
        parsed = json.loads(manifest_json) if manifest_json else {}
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return {
        "manifest_id": manifest_id,
        "certified_snapshot_id": parsed.get("certified_snapshot_identity"),
        "inventory_row_count": parsed.get("inventory_row_count"),
        "tokenizer_model_id": parsed.get("tokenizer_model_id"),
        "tokenizer_revision": parsed.get("tokenizer_revision"),
    }


def _lexical_facts(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = conn.execute(
            "SELECT corpus_manifest_id, mode_served, row_count, lexical_generation_id, "
            "lexical_digest FROM lexical_index_state LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None:
        return {}
    return {
        "lexical_corpus_manifest_id": row[0],
        "lexical_mode_served": row[1],
        "lexical_row_count": row[2],
        "lexical_generation_id": row[3],
        "lexical_digest": row[4],
    }


def snapshot_identity(db_path: str | Path | None) -> dict[str, Any]:
    """Read frozen-snapshot identity facts through a read-only connection.

    Missing DB/file yields all-``None`` facts (explicitly NON-COMPARABLE),
    never a guessed identity. Each call opens and closes its own connection.
    """
    if db_path is None:
        return dict(_SNAPSHOT_EMPTY)
    path = Path(db_path)
    if not path.is_file():
        return dict(_SNAPSHOT_EMPTY)

    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        manifest = _manifest_facts(conn)
        lexical = _lexical_facts(conn)
        return {
            "schema_version": SNAPSHOT_IDENTITY_SCHEMA,
            "corpus_manifest_id": manifest.get("manifest_id"),
            "snapshot_id": manifest.get("certified_snapshot_id"),
            "inventory_row_count": manifest.get("inventory_row_count"),
            "tokenizer_model_id": manifest.get("tokenizer_model_id"),
            "tokenizer_revision": manifest.get("tokenizer_revision"),
            "lexical_corpus_manifest_id": lexical.get("lexical_corpus_manifest_id"),
            "lexical_mode_served": lexical.get("lexical_mode_served"),
            "lexical_row_count": lexical.get("lexical_row_count"),
            "lexical_generation_id": lexical.get("lexical_generation_id"),
            "lexical_digest": lexical.get("lexical_digest"),
            "corpus_served_chunks_count": _count(conn, "corpus_served_chunks"),
            "corpus_build_chunks_fts_count": _count(conn, "corpus_build_chunks_fts"),
            "articles_count": _count(conn, "articles"),
            "filings_count": _count(conn, "filings"),
        }
    finally:
        conn.close()


def lancedb_identity(
    lancedb_dir: str | Path | None,
    *,
    index_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read the active LanceDB pointer and clean-import index manifest.

    ``index_manifest_path`` wins when supplied; otherwise falls back to
    ``<lancedb_dir>/index_manifest.json``. Missing files yield ``None`` facts.
    """
    if lancedb_dir is None:
        return dict(_LANCEDB_EMPTY)
    base = Path(lancedb_dir)
    if not base.is_dir():
        return dict(_LANCEDB_EMPTY)

    pointer = _read_json_pointer(base / "active_generation.json")
    manifest_file = (
        Path(index_manifest_path)
        if index_manifest_path is not None
        else base / "index_manifest.json"
    )
    manifest = _read_json_pointer(manifest_file)

    def _value(payload: dict, key: str) -> Any:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None

    dimension = _value(manifest, "dimension")
    return {
        "schema_version": LANCEDB_IDENTITY_SCHEMA,
        "lancedb_table_name": _value(pointer, "table_name") or _value(manifest, "table_name"),
        "snapshot_id": _value(pointer, "snapshot_id") or _value(manifest, "snapshot_id"),
        "corpus_manifest_id": (
            _value(pointer, "corpus_manifest_id") or _value(manifest, "corpus_manifest_id")
        ),
        "index_manifest_id": (
            _value(pointer, "index_manifest_id") or _value(manifest, "index_manifest_id")
        ),
        "source_bundle_id": (
            _value(pointer, "source_bundle_id") or _value(manifest, "source_bundle_id")
        ),
        "probe_report_id": _value(manifest, "probe_report_id"),
        "postbuild_readiness_id": _value(manifest, "postbuild_readiness_id"),
        "embedding_model": _value(manifest, "model_name"),
        "embedding_dim": str(dimension) if dimension is not None else None,
        "chunk_count": _value(pointer, "chunk_count"),
        "vector_count": _value(manifest, "vector_count"),
        "index_manifest_path": str(manifest_file) if manifest_file.is_file() else None,
    }


__all__ = [
    "LANCEDB_IDENTITY_SCHEMA",
    "SNAPSHOT_IDENTITY_SCHEMA",
    "lancedb_identity",
    "snapshot_identity",
]
