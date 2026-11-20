"""Canonical corpus projection record builders."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from catalyst_data.canonical.ids import canonical_projection_row, sha256_identity
from catalyst_data.corpus.news_v2 import _normalize_text


def build_canonical_corpus_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read canonical projection tables only (M3-9 entrypoint).

    Returns only closed 26-key projection rows. ``asset_type`` is used
    internally for profile routing and is never emitted as a 27th projection
    field. Existing ``build_article_records`` / ``build_filing_records`` stay
    baseline subtype builders and are not used here.
    """
    if not _table_exists(conn, "canonical_assets"):
        return []
    tickers_by_asset: dict[str, list[str]] = {}
    if _table_exists(conn, "canonical_asset_tickers"):
        for asset_id, ticker in conn.execute(
            "SELECT asset_id, ticker FROM canonical_asset_tickers ORDER BY asset_id, ticker"
        ):
            tickers_by_asset.setdefault(str(asset_id), []).append(str(ticker))

    rows = conn.execute(
        """
        SELECT
            a.asset_id, a.asset_type, a.issuer_id, a.provider, a.publisher,
            a.canonical_url, a.source_class, a.source_published_at, a.eligible_at,
            a.temporal_precision, a.content_state, a.serving_status, a.title,
            a.content_ref, a.dedup_cluster_id, a.independence_group_id,
            a.parse_quality, a.subtype_metadata,
            v.canonical_content_version_id, v.content_hash, v.normalizer_version,
            v.materiality_version, v.payload_ref,
            s.subtype_table, s.subtype_pk, s.subtype_pk_value
        FROM canonical_subtype_assoc s
        JOIN canonical_assets a ON a.asset_id = s.asset_id
        JOIN canonical_content_versions v
          ON v.canonical_content_version_id = s.canonical_content_version_id
        ORDER BY a.asset_id, v.canonical_content_version_id,
                 s.subtype_table, s.subtype_pk_value
        """
    )
    records: list[dict[str, Any]] = []
    filing_record_index: dict[tuple[str, str], int] = {}
    for row in rows:
        asset_type = str(row[1])
        metadata = row[17]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        if metadata is None:
            metadata = {}
        payload = {
            "asset_id": row[0],
            "canonical_content_version_id": row[18],
            "content_hash": row[19],
            "normalizer_version": row[20],
            "materiality_version": row[21],
            "payload_ref": row[22],
            "subtype_table": row[23],
            "subtype_pk": row[24],
            "subtype_pk_value": row[25],
            "issuer_id": row[2],
            "tickers": list(tickers_by_asset.get(str(row[0]), [])),
            "provider": row[3],
            "publisher": row[4],
            "canonical_url": row[5],
            "source_class": row[6],
            "source_published_at": row[7],
            "eligible_at": row[8],
            "temporal_precision": row[9],
            "content_state": row[10],
            "serving_status": row[11],
            "title": row[12],
            "content_ref": row[13],
            "dedup_cluster_id": row[14],
            "independence_group_id": row[15],
            "parse_quality": row[16],
            "subtype_metadata": metadata,
        }
        if asset_type == "FILING":
            key = (str(row[0]), str(row[18]))
            existing_index = filing_record_index.get(key)
            if existing_index is None:
                filing_record_index[key] = len(records)
                records.append(canonical_projection_row(payload))
                continue
            # One corpus record per (asset_id, canonical_content_version_id,
            # filing profile). The filing_documents association wins because it
            # owns the exact payload provenance and document_id; the filings
            # association is asset-level provenance only.
            existing = records[existing_index]
            if (
                existing["subtype_table"] != "filing_documents"
                and payload["subtype_table"] == "filing_documents"
            ):
                records[existing_index] = canonical_projection_row(payload)
            continue
        records.append(canonical_projection_row(payload))
    return records


def resolve_filing_body_text(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    canonical_content_version_id: str,
    content_hash: str,
) -> str:
    """Resolve a filing content version's body from its bound document row.

    M3 corpus-evidence corrective pass: the FULL_TEXT body lives in
    ``filing_documents.text`` (never in canonical ``subtype_metadata``). The
    binding is the ``canonical_subtype_assoc`` row with
    subtype_table='filing_documents' whose ``subtype_pk_value`` is the
    document_id.

    Validates association ownership, document identity, content-version
    binding, and the state-bound normalized content hash. Raises ValueError
    (fail closed) on a missing association, missing/empty text, or hash
    mismatch.
    """
    rows = conn.execute(
        """SELECT s.asset_id, s.canonical_content_version_id,
                  s.subtype_pk_value, fd.text
           FROM canonical_subtype_assoc s
           LEFT JOIN filing_documents fd
                  ON fd.document_id = s.subtype_pk_value
           WHERE s.asset_id = ?
             AND s.canonical_content_version_id = ?
             AND s.subtype_table = 'filing_documents'
           ORDER BY s.subtype_pk_value""",
        (asset_id, canonical_content_version_id),
    ).fetchall()
    if not rows:
        raise ValueError(
            f"filing content version {canonical_content_version_id} for asset "
            f"{asset_id} has no bound filing_documents association; cannot "
            f"resolve filing body"
        )
    if len(rows) != 1:
        raise ValueError(
            f"filing content version {canonical_content_version_id} for asset "
            f"{asset_id} has ambiguous filing_documents bindings"
        )
    candidates: list[tuple[str, str]] = []
    for row in rows:
        if str(row["asset_id"]) != asset_id:
            raise ValueError(
                f"canonical_subtype_assoc ownership mismatch for filing "
                f"document {row['subtype_pk_value']}: asset {row['asset_id']} "
                f"does not own {asset_id}"
            )
        if str(row["canonical_content_version_id"]) != canonical_content_version_id:
            raise ValueError(
                f"canonical_subtype_assoc content-version mismatch for filing "
                f"document {row['subtype_pk_value']}: "
                f"{row['canonical_content_version_id']} != "
                f"{canonical_content_version_id}"
            )
        document_id = str(row["subtype_pk_value"])
        text = row["text"]
        if text is None or not str(text).strip():
            raise ValueError(
                f"filing_documents row {document_id} has missing/empty text; "
                f"cannot resolve filing body for content version "
                f"{canonical_content_version_id}"
            )
        normalized = _normalize_text(str(text))
        computed = sha256_identity(
            {"content_state": "FULL_TEXT", "normalized_body": normalized}
        )
        if computed != content_hash:
            raise ValueError(
                f"filing_documents row {document_id} normalized body hash "
                f"{computed} does not match content version content_hash "
                f"{content_hash} for asset {asset_id}"
            )
        candidates.append((document_id, normalized))
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )
