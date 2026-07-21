"""Append-only raw response store for request-scoped payloads."""
from __future__ import annotations

import hashlib
import json
import sqlite3


class RawResponseIntegrityError(Exception):
    """Same request_id with different response bytes — integrity violation."""


def store_raw_response(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    response_bytes: bytes,
    content_encoding: str = "identity",
    response_sha256: str | None = None,
    page_no: int = 1,
    ticker: str = "",
    reference_date: str = "",
    source_type: str = "news",
    http_status: int | None = None,
) -> str:
    """Store one raw response as a v2 request-scoped row.

    Returns the raw_asset_id = 'raw:' + request_id.
    Uses real schema columns: content_raw BLOB, data_version='v2', metadata_json.
    """
    computed_sha = hashlib.sha256(response_bytes).hexdigest()

    if response_sha256 is not None and response_sha256 != computed_sha:
        raise RawResponseIntegrityError(
            f"Supplied SHA {response_sha256[:16]}... != computed SHA {computed_sha[:16]}..."
        )

    response_sha256 = computed_sha

    raw_asset_id = f"raw:{request_id}"

    # Check for existing
    existing = conn.execute(
        "SELECT response_sha256 FROM raw_assets WHERE asset_id = ?",
        (raw_asset_id,),
    ).fetchone()

    if existing:
        if existing["response_sha256"] == response_sha256:
            # Idempotent: same ID, same hash → no-op
            return raw_asset_id
        else:
            raise RawResponseIntegrityError(
                f"Request {request_id}: existing SHA {existing['response_sha256'][:16]}... != new SHA {response_sha256[:16]}..."
            )

    metadata = json.dumps({
        "content_encoding": content_encoding,
        "page_no": page_no,
        "response_sha256": response_sha256,
    })

    conn.execute(
        """INSERT INTO raw_assets
           (asset_id, request_id, response_sha256, content_encoding,
            page_no, ticker, reference_date, source_type,
            data_version, content_raw, http_status, metadata_json, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'v2', ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
        (
            raw_asset_id, request_id, response_sha256, content_encoding,
            page_no, ticker, reference_date, source_type,
            response_bytes, http_status, metadata,
        ),
    )
    return raw_asset_id
