"""V1.1 canonical identity builders (M3-1).

Execution-lock §A owns every stable V1.1 identity: the shared locked serializer
``canonical_json_bytes``, ``asset_id``, ``canonical_content_version_id``,
``corpus_document_id``, ``fact_id``, and the closed 26-key canonical projection
digest (A.6). No competing ID system is created; legacy baseline serializers are
not the V1.1 identity serializer.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

# Locked projection row schema (execution-lock §A.6). Every key is always
# present; unknown/missing values serialize as JSON null; extra keys fail closed.
PROJECTION_ROW_KEYS: frozenset[str] = frozenset({
    "asset_id",
    "canonical_content_version_id",
    "content_hash",
    "normalizer_version",
    "materiality_version",
    "payload_ref",
    "subtype_table",
    "subtype_pk",
    "subtype_pk_value",
    "issuer_id",
    "tickers",
    "provider",
    "publisher",
    "canonical_url",
    "source_class",
    "source_published_at",
    "eligible_at",
    "temporal_precision",
    "content_state",
    "serving_status",
    "title",
    "content_ref",
    "dedup_cluster_id",
    "independence_group_id",
    "parse_quality",
    "subtype_metadata",
})

_PROJECTION_DIGEST_SCHEMA_VERSION = "canonical_projection_digest_v1"

# Fact types / source tables are validated at the writer layer; the builders
# remain pure and cheap here.
_NEWS_SOURCE_TABLE = "articles"
_FILING_SOURCE_TABLE = "filings"


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a JSON-native value with the locked V1.1 canonical encoding.

    UTF-8, ``sort_keys``, ``(",",":")`` separators, ``ensure_ascii=False``,
    ``allow_nan=False`` (execution-lock §1). NaN/Infinity are rejected so they
    can never enter an identity payload.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_identity(value: object) -> str:
    """Lowercase 64-hex SHA-256 over ``canonical_json_bytes(value)``."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def asset_id(*, asset_type: str, source_table: str, source_pk: str) -> str:
    """Stable namespaced subtype identity (execution-lock §A.1).

    ``source_pk`` is the exact stored subtype primary key, used verbatim (no
    case folding, no reconstruction).
    """
    payload = {
        "schema_version": "canonical_asset_id_v1",
        "asset_type": asset_type,
        "source_table": source_table,
        "source_pk": source_pk,
    }
    return f"v1:asset:{sha256_identity(payload)}"


def canonical_content_version_id(
    *,
    asset_id: str,
    content_hash: str,
    normalizer_version: str,
    materiality_version: str,
) -> str:
    """Content-version identity (execution-lock §A.2).

    ``version_ordinal`` is a storage column and never part of the hash payload.
    """
    payload = {
        "namespace": "v1:content",
        "asset_id": asset_id,
        "content_hash": content_hash,
        "normalizer_version": normalizer_version,
        "materiality_version": materiality_version,
    }
    return f"v1:content:{sha256_identity(payload)}"


def corpus_document_id(
    *, canonical_content_version_id: str, chunk_profile_version: str
) -> str:
    """64-hex corpus document identity (execution-lock §A.3)."""
    payload = {
        "corpus_document": "v1",
        "content_version_id": canonical_content_version_id,
        "chunk_profile_version": chunk_profile_version,
    }
    return sha256_identity(payload)


def fact_id(*, fact_type: str, source_table: str, natural_key: Mapping[str, object]) -> str:
    """Structured-fact identity (execution-lock §A.5)."""
    payload = {
        "schema_version": "structured_fact_id_v1",
        "fact_type": fact_type,
        "source_table": source_table,
        "natural_key": dict(natural_key),
    }
    return f"v1:fact:{sha256_identity(payload)}"


def source_pk_json(natural_key: Mapping[str, object]) -> str:
    """Canonical ``source_pk_json`` string for a fact natural key (§A.5)."""
    return canonical_json_bytes(dict(natural_key)).decode("utf-8")


def _is_json_native(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        import math

        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(_is_json_native(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) for key in value) and all(
            _is_json_native(item) for item in value.values()
        )
    return False


def canonical_projection_row(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate a projection row against the closed 26-key schema (A.6).

    Missing or extra keys fail closed; values must already be JSON-native
    (NaN/Infinity rejected). Returns the validated dict unchanged.
    """
    if set(payload) != PROJECTION_ROW_KEYS:
        missing = PROJECTION_ROW_KEYS - set(payload)
        extra = set(payload) - PROJECTION_ROW_KEYS
        raise ValueError(
            f"canonical projection row must have exactly the 26 locked keys; "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
    row = dict(payload)
    if not _is_json_native(row):
        raise ValueError("canonical projection row values must be JSON-native")
    # allow_nan=False double-checks non-finite rejection for every value.
    canonical_json_bytes(row)
    return row


def compute_canonical_projection_digest(
    rows: Sequence[Mapping[str, object]],
) -> str:
    """Compute the locked canonical projection digest (execution-lock §A.6).

    Rows are sorted by ``(asset_id, canonical_content_version_id, subtype_table,
    subtype_pk_value)``; ``tickers`` is sorted lexicographically before
    serialization so reordered input tickers produce the same digest.
    """
    normalized: list[dict[str, object]] = []
    for payload in rows:
        row = canonical_projection_row(payload)
        tickers = row["tickers"]
        if not isinstance(tickers, (list, tuple)) or not all(
            isinstance(item, str) for item in tickers
        ):
            raise ValueError("projection row tickers must be a list of strings")
        row["tickers"] = sorted(tickers)
        normalized.append(row)

    normalized.sort(
        key=lambda row: (
            row["asset_id"],
            row["canonical_content_version_id"],
            row["subtype_table"],
            row["subtype_pk_value"],
        )
    )

    hasher = hashlib.sha256()
    hasher.update(f"{_PROJECTION_DIGEST_SCHEMA_VERSION}\n".encode("utf-8"))
    for row in normalized:
        hasher.update(canonical_json_bytes(row))
        hasher.update(b"\n")
    return hasher.hexdigest()


__all__ = [
    "PROJECTION_ROW_KEYS",
    "asset_id",
    "canonical_content_version_id",
    "canonical_json_bytes",
    "canonical_projection_row",
    "compute_canonical_projection_digest",
    "corpus_document_id",
    "fact_id",
    "sha256_identity",
    "source_pk_json",
]
