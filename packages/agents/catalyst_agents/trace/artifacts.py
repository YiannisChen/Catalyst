"""Persistence helpers for node-level runtime artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_node_artifact(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    event_seq: int,
    node: str,
    artifact_type: str,
    payload: dict[str, Any],
) -> None:
    """Insert or replace one artifact row after validating JSON serializability."""
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """
        INSERT OR REPLACE INTO node_artifacts
            (run_id, event_seq, node, artifact_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, event_seq, node, artifact_type, payload_json, _utc_now()),
    )
    conn.commit()


def read_node_artifacts(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    event_seq: int | None = None,
    artifact_type: str | None = None,
) -> list[dict[str, Any]]:
    """Read artifacts for one run, optionally filtered by event sequence and type."""
    query = """
        SELECT run_id, event_seq, node, artifact_type, payload_json, created_at
        FROM node_artifacts
        WHERE run_id = ?
    """
    params: list[Any] = [run_id]
    if event_seq is not None:
        query += " AND event_seq = ?"
        params.append(event_seq)
    if artifact_type is not None:
        query += " AND artifact_type = ?"
        params.append(artifact_type)
    query += " ORDER BY event_seq ASC, artifact_type ASC"

    rows = conn.execute(query, tuple(params)).fetchall()
    return [
        {
            "run_id": row[0],
            "event_seq": row[1],
            "node": row[2],
            "artifact_type": row[3],
            "payload_json": json.loads(row[4]),
            "created_at": row[5],
        }
        for row in rows
    ]
