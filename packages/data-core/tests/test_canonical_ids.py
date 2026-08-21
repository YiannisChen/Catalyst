"""M3-1: literal golden-vector tests for canonical identity builders.

Execution-lock §A (exact): canonical_json_bytes encoding, golden
asset_id / canonical_content_version_id / corpus_document_id / fact_id values,
non-finite (NaN/Inf) rejection, exact stored-PK sensitivity, idempotency, and
the closed 26-key canonical projection digest (A.6).
"""
from __future__ import annotations

import pytest

from catalyst_data.canonical.ids import (
    asset_id,
    canonical_content_version_id,
    canonical_json_bytes,
    canonical_projection_row,
    compute_canonical_projection_digest,
    corpus_document_id,
    fact_id,
    source_pk_json,
)


def _projection_row(**overrides: object) -> dict[str, object]:
    row = {
        "asset_id": "v1:asset:aaaa",
        "canonical_content_version_id": "v1:content:bbbb",
        "content_hash": "00ebb9b7daf865ce736aec5cf250d5f109d3d40c401265f13b9d36d72c178ee5",
        "normalizer_version": "news_body_v1",
        "materiality_version": "materiality_v1",
        "payload_ref": None,
        "subtype_table": "articles",
        "subtype_pk": "article_id",
        "subtype_pk_value": "finnhub:a",
        "issuer_id": "issuer:AAPL",
        "tickers": ["AAPL", "MSFT"],
        "provider": "finnhub",
        "publisher": "Example News",
        "canonical_url": "https://example.com/a",
        "source_class": "reported_news",
        "source_published_at": "2026-01-05T10:00:00Z",
        "eligible_at": "2026-01-05T10:00:00Z",
        "temporal_precision": "publication_time",
        "content_state": "FULL_TEXT",
        "serving_status": "body_candidate",
        "title": "Title A",
        "content_ref": "ref-a",
        "dedup_cluster_id": None,
        "independence_group_id": None,
        "parse_quality": "not_applicable",
        "subtype_metadata": {"publisher": "Example News"},
    }
    row.update(overrides)
    return row


# --- canonical_json_bytes -------------------------------------------------

def test_canonical_json_bytes_exact_encoding():
    assert canonical_json_bytes({"b": 1, "a": [2, 3]}) == b'{"a":[2,3],"b":1}'


def test_canonical_json_bytes_utf8_ensure_ascii_false():
    assert canonical_json_bytes({"title": "中文"}) == b'{"title":"\xe4\xb8\xad\xe6\x96\x87"}'


def test_canonical_json_bytes_null_not_empty_string():
    assert canonical_json_bytes({"a": None, "b": ""}) == b'{"a":null,"b":""}'


def test_canonical_json_bytes_rejects_non_finite():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_json_bytes({"v": bad})


# --- asset_id -------------------------------------------------------------

def test_asset_id_news_golden():
    assert asset_id(
        asset_type="NEWS", source_table="articles", source_pk="finnhub:abc123"
    ) == "v1:asset:d2e29ff539c34e4f8c2ad645001c4e9720872d57f57999cdfc6b3c5407d7d354"


def test_asset_id_filing_golden():
    assert asset_id(
        asset_type="FILING", source_table="filings",
        source_pk="filing-8k-0000320193-26-000001",
    ) == "v1:asset:ac1ca22844bd36aacf153e4e69224b2cd43c33ce20d5fc987f05a121b1952157"


def test_asset_id_stored_pk_sensitivity_no_reconstruction():
    # Exact stored PK is the identity input: no re-lowercasing/reconstruction.
    upper = asset_id(asset_type="NEWS", source_table="articles", source_pk="finnhub:ABC")
    lower = asset_id(asset_type="NEWS", source_table="articles", source_pk="FINNHUB:abc")
    assert upper != lower
    reconstructed = asset_id(
        asset_type="NEWS", source_table="articles", source_pk="finnhub:abc123"
    )
    assert reconstructed == "v1:asset:d2e29ff539c34e4f8c2ad645001c4e9720872d57f57999cdfc6b3c5407d7d354"


def test_asset_id_idempotent():
    first = asset_id(asset_type="NEWS", source_table="articles", source_pk="finnhub:abc123")
    second = asset_id(asset_type="NEWS", source_table="articles", source_pk="finnhub:abc123")
    assert first == second


# --- canonical_content_version_id ----------------------------------------

NEWS_ASSET_ID = "v1:asset:d2e29ff539c34e4f8c2ad645001c4e9720872d57f57999cdfc6b3c5407d7d354"
CONTENT_HASH = "00ebb9b7daf865ce736aec5cf250d5f109d3d40c401265f13b9d36d72c178ee5"


def test_canonical_content_version_id_golden():
    assert canonical_content_version_id(
        asset_id=NEWS_ASSET_ID,
        content_hash=CONTENT_HASH,
        normalizer_version="news_body_v1",
        materiality_version="materiality_v1",
    ) == "v1:content:dd87141ddf252e97b35b5ee505d827cbf412bf2c32e8be10bc1ce1ac42a93fd2"


def test_canonical_content_version_id_no_ordinal_in_hash():
    # version_ordinal is a storage column, never a hash input.
    a = canonical_content_version_id(
        asset_id=NEWS_ASSET_ID, content_hash=CONTENT_HASH,
        normalizer_version="news_body_v1", materiality_version="materiality_v1",
    )
    b = canonical_content_version_id(
        asset_id=NEWS_ASSET_ID, content_hash=CONTENT_HASH,
        normalizer_version="news_body_v1", materiality_version="materiality_v1",
    )
    assert a == b


def test_canonical_content_version_id_state_bound_hash_changes_id():
    other_hash = "11ebb9b7daf865ce736aec5cf250d5f109d3d40c401265f13b9d36d72c178ee5"
    assert canonical_content_version_id(
        asset_id=NEWS_ASSET_ID, content_hash=CONTENT_HASH,
        normalizer_version="news_body_v1", materiality_version="materiality_v1",
    ) != canonical_content_version_id(
        asset_id=NEWS_ASSET_ID, content_hash=other_hash,
        normalizer_version="news_body_v1", materiality_version="materiality_v1",
    )


# --- corpus_document_id ---------------------------------------------------

def test_corpus_document_id_golden_64_hex():
    cid = corpus_document_id(
        canonical_content_version_id="v1:content:dd87141ddf252e97b35b5ee505d827cbf412bf2c32e8be10bc1ce1ac42a93fd2",
        chunk_profile_version="news_v2",
    )
    assert cid == "fbf0e838e63c71faab02e1fea50e4baa036c702ddb29f0dfdfe0e4570254436c"
    assert len(cid) == 64
    assert cid.islower()
    assert all(c in "0123456789abcdef" for c in cid)


# --- fact_id / source_pk_json --------------------------------------------

def test_fact_id_golden():
    assert fact_id(
        fact_type="ohlcv",
        source_table="ohlcv",
        natural_key={"symbol": "AAPL", "date": "2026-01-05"},
    ) == "v1:fact:13a43968f2a0408546ae37b387c42ad26df1fde0642695423eece1cac9ade788"


def test_source_pk_json_is_locked_serializer_output():
    assert source_pk_json({"symbol": "AAPL", "date": "2026-01-05"}) == (
        '{"date":"2026-01-05","symbol":"AAPL"}'
    )


# --- canonical projection row / digest (A.6) ------------------------------

def test_projection_row_rejects_missing_key():
    row = _projection_row()
    del row["title"]
    with pytest.raises(ValueError):
        canonical_projection_row(row)


def test_projection_row_rejects_extra_key():
    row = _projection_row()
    row["extra"] = "boom"
    with pytest.raises(ValueError):
        canonical_projection_row(row)


def test_projection_digest_golden():
    rows = [_projection_row(), _projection_row()]
    assert compute_canonical_projection_digest(rows) == (
        "c81dc064a51d8d5d58fcb3e5d1eb1710ae3215b2a35fe707d5c666dc9878e129"
    )


def test_projection_digest_sorts_tickers():
    reordered = [_projection_row(tickers=["MSFT", "AAPL"])]
    sorted_row = [_projection_row(tickers=["AAPL", "MSFT"])]
    assert compute_canonical_projection_digest(reordered) == compute_canonical_projection_digest(sorted_row)
    different = [_projection_row(tickers=["AAPL", "NVDA"])]
    assert compute_canonical_projection_digest(reordered) != compute_canonical_projection_digest(different)


def test_projection_digest_reordered_object_keys_identical():
    a = [_projection_row(subtype_metadata={"publisher": "Example News", "category": "tech"})]
    b = [_projection_row(subtype_metadata={"category": "tech", "publisher": "Example News"})]
    assert compute_canonical_projection_digest(a) == compute_canonical_projection_digest(b)


def test_projection_digest_null_vs_empty_distinct():
    null_row = [_projection_row(publisher=None)]
    empty_row = [_projection_row(publisher="")]
    assert compute_canonical_projection_digest(null_row) != compute_canonical_projection_digest(empty_row)


def test_projection_digest_rejects_non_finite_nested():
    with pytest.raises(ValueError):
        compute_canonical_projection_digest(
            [_projection_row(subtype_metadata={"score": float("nan")})]
        )


def test_projection_digest_rejects_extra_keys():
    row = _projection_row()
    row["extra"] = "boom"
    with pytest.raises(ValueError):
        compute_canonical_projection_digest([row])
