"""AMEND-3: public persisted LanceDB table validator contract.

``catalyst_data.retrieval.gpu_contract.validate_persisted_lancedb_table`` is the
public wrapper around the production bounded-batch persisted-table validator.
It must reuse the existing hash algorithm, read only in bounded batches, and
fail closed on any persisted drift (row count, chunk order, identity columns,
dimension, vector finiteness, artifact hash).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from catalyst_data.retrieval.gpu_contract import (
    MAX_PERSISTED_VALIDATION_BATCH,
    production_lancedb_schema,
    validate_persisted_lancedb_table,
)

SNAPSHOT_ID = "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49"
CORPUS_MANIFEST_ID = "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc"
SOURCE_BUNDLE_ID = "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2"
PROBE_REPORT_ID = "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23"
POSTBUILD_READINESS_ID = "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b"
INDEX_MANIFEST_ID = "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083"
IDENTITY_COLUMNS = {
    "corpus_manifest_id": CORPUS_MANIFEST_ID,
    "source_bundle_id": SOURCE_BUNDLE_ID,
    "snapshot_id": SNAPSHOT_ID,
    "probe_report_id": PROBE_REPORT_ID,
    "postbuild_readiness_id": POSTBUILD_READINESS_ID,
}


def _row(index: int, *, corpus_manifest_id: str = CORPUS_MANIFEST_ID) -> dict:
    chunk_id = f"c{index:04d}"
    text = f"fixture content {index}"
    return {
        "chunk_id": chunk_id,
        "document_id": f"doc:{index}",
        "content_text": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "metadata_hash": hashlib.sha256(f"meta:{index}".encode()).hexdigest(),
        "available_at": "2025-01-01T00:00:00Z",
        "ticker_associations": ["AAPL"],
        "source_class": "reported_news",
        "chunk_profile_version": "news_v2",
        "status": "active",
        "eligibility": "eligible",
        "dedup_cluster_id": None,
        "cluster_first_available_at": None,
        "representative_document_id": None,
        "corpus_manifest_id": corpus_manifest_id,
        "source_bundle_id": SOURCE_BUNDLE_ID,
        "snapshot_id": SNAPSHOT_ID,
        "probe_report_id": PROBE_REPORT_ID,
        "postbuild_readiness_id": POSTBUILD_READINESS_ID,
        "index_manifest_id": INDEX_MANIFEST_ID,
        "vector": [0.0] * 1024,
    }


def _canonical_digest(rows: list[dict]) -> str:
    """Fixture-side canonical digest for a valid persisted table artifact."""
    payload = [
        {key: value for key, value in row.items() if key != "index_manifest_id"}
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_chunk_ids(tmp_path: Path, chunk_ids: list[str]) -> Path:
    path = tmp_path / "chunk_ids.json"
    path.write_text(json.dumps(chunk_ids, separators=(",", ":")) + "\n")
    return path


def _build_table(tmp_path: Path, *, count: int = 4, rows=None) -> tuple[object, list[dict], Path, str]:
    import lancedb

    table_rows = rows if rows is not None else [_row(i) for i in range(count)]
    chunk_ids = [r["chunk_id"] for r in table_rows]
    chunk_ids_path = _write_chunk_ids(tmp_path, chunk_ids)
    expected_hash = _canonical_digest(table_rows)
    db = lancedb.connect(str(tmp_path / "lance"))
    table = db.create_table("staging", data=[], schema=production_lancedb_schema())
    table.add(table_rows)
    return table, table_rows, chunk_ids_path, expected_hash


def _call(table, *, chunk_ids_path, count=None, dimension=1024, index_manifest_id=INDEX_MANIFEST_ID,
          expected_identities=None, expected_hash=None, batch_size=None):
    kwargs = {
        "chunk_ids_path": chunk_ids_path,
        "chunk_count": count if count is not None else int(table.count_rows()),
        "dimension": dimension,
        "index_manifest_id": index_manifest_id,
        "expected_identities": expected_identities or IDENTITY_COLUMNS,
        "expected_hash": expected_hash,
    }
    if batch_size is not None:
        kwargs["batch_size"] = batch_size
    return validate_persisted_lancedb_table(table, **kwargs)


def test_public_validator_accepts_matching_persisted_table(tmp_path):
    table, _rows, chunk_ids_path, expected_hash = _build_table(tmp_path)
    _call(table, chunk_ids_path=chunk_ids_path, expected_hash=expected_hash)


def test_public_validator_rejects_row_count_mismatch(tmp_path):
    table, _rows, chunk_ids_path, expected_hash = _build_table(tmp_path)
    with pytest.raises(ValueError, match="row count"):
        _call(table, chunk_ids_path=chunk_ids_path, expected_hash=expected_hash, count=3)


def test_public_validator_rejects_chunk_id_order_drift(tmp_path):
    table, _rows, chunk_ids_path, expected_hash = _build_table(tmp_path)
    # Literal wrong order: c0001 before c0000.
    _write_chunk_ids(tmp_path, ["c0001", "c0000", "c0002", "c0003"])
    with pytest.raises(ValueError, match="order|drift|chunk_id"):
        _call(table, chunk_ids_path=tmp_path / "chunk_ids.json", expected_hash=expected_hash)


def test_public_validator_rejects_identity_column_mismatch(tmp_path):
    rows = [_row(i) for i in range(4)]
    rows[1] = dict(rows[1])
    rows[1]["corpus_manifest_id"] = "e" * 64
    table, _rows, chunk_ids_path, expected_hash = _build_table(tmp_path, rows=rows)
    with pytest.raises(ValueError, match="corpus_manifest_id"):
        _call(table, chunk_ids_path=chunk_ids_path, expected_hash=expected_hash)


def test_public_validator_rejects_hash_mismatch(tmp_path):
    table, _rows, chunk_ids_path, _hash = _build_table(tmp_path)
    with pytest.raises(ValueError, match="hash"):
        _call(table, chunk_ids_path=chunk_ids_path, expected_hash="f" * 64)


def test_public_validator_rejects_missing_chunk_ids_file(tmp_path):
    table, _rows, chunk_ids_path, expected_hash = _build_table(tmp_path)
    with pytest.raises((ValueError, OSError)):
        _call(table, chunk_ids_path=tmp_path / "missing_chunk_ids.json", expected_hash=expected_hash)


def test_public_validator_rejects_batch_size_over_max(tmp_path):
    table, _rows, chunk_ids_path, expected_hash = _build_table(tmp_path)
    with pytest.raises(ValueError, match="batch"):
        _call(table, chunk_ids_path=chunk_ids_path, expected_hash=expected_hash,
              batch_size=MAX_PERSISTED_VALIDATION_BATCH + 1)


class _BoundedFakeTable:
    """Minimal table exposing only bounded read APIs (no to_pylist/to_list)."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.name = "staging"

    def count_rows(self) -> int:
        return len(self._rows)

    def take_offsets(self, offsets):
        class Builder:
            def __init__(self, rows):
                self._rows = rows

            def to_arrow(self):
                import pyarrow as pa
                return pa.Table.from_pylist(self._rows)

        return Builder([self._rows[offset] for offset in offsets])


def test_public_validator_reads_only_bounded_batch_apis(tmp_path):
    rows = [_row(i) for i in range(4)]
    chunk_ids_path = _write_chunk_ids(tmp_path, [r["chunk_id"] for r in rows])
    table = _BoundedFakeTable(rows)
    assert not hasattr(table, "to_pylist")
    _call(table, chunk_ids_path=chunk_ids_path, expected_hash=_canonical_digest(rows))
