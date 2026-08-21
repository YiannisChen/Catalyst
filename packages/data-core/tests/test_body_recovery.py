"""M3-5: news full-body recovery + state-bound content hash tests (§F/§A.2)."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from catalyst_data.articles.body_recovery import (
    BODY_NORMALIZER_VERSION,
    BodyRecoveryResult,
    persist_article_content_repair,
    recover_body,
)
from catalyst_data.canonical.ids import canonical_json_bytes

FULL_BODY = (
    "Apple Inc. announced new AI features during its product event. The "
    "company said the updates will roll out to customers starting next month. "
    "Analysts expect the changes to improve device performance and battery "
    "life across the lineup. This paragraph is deliberately long enough to "
    "clear the minimum material body threshold."
)


def _article_row(**overrides):
    row = {
        "article_id": "finnhub:full-1",
        "title": "Apple announces new AI features",
        "description": FULL_BODY,
        "article_url": "https://example.com/apple-ai",
    }
    row.update(overrides)
    return row


def _expected_state_hash(content_state: str, **fields) -> str:
    payload = {"content_state": content_state}
    payload.update(fields)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def test_full_text_material_body():
    result = recover_body(_article_row(), raw_payload={})
    assert isinstance(result, BodyRecoveryResult)
    assert result.content_state == "FULL_TEXT"
    assert result.body_text is not None
    assert len(result.body_text.strip()) >= 200
    assert result.content_hash is not None
    assert result.content_hash == _expected_state_hash(
        "FULL_TEXT", normalized_body=result.body_text
    )


def test_snippet_below_threshold_metadata_only():
    result = recover_body(
        _article_row(description="Short snippet only."), raw_payload={}
    )
    assert result.content_state == "METADATA_ONLY"
    assert result.body_text is None
    assert result.content_hash is not None
    assert result.content_hash == _expected_state_hash(
        "METADATA_ONLY",
        normalized_title="Apple announces new AI features",
        normalized_description="Short snippet only.",
        canonical_url="https://example.com/apple-ai",
    )


def test_title_only_no_body():
    result = recover_body(_article_row(description=None), raw_payload={})
    assert result.content_state == "TITLE_ONLY"
    assert result.content_hash == _expected_state_hash(
        "TITLE_ONLY", normalized_title="Apple announces new AI features"
    )


def test_whitespace_only_empty():
    result = recover_body(_article_row(description="   \n\t  "), raw_payload={})
    assert result.content_state == "EMPTY"
    assert result.content_hash is None
    assert result.body_text is None


def test_terminal_failure_failed():
    result = recover_body(_article_row(), raw_payload={"content_failed": True})
    assert result.content_state == "FAILED"
    assert result.content_hash is None
    assert result.body_text is None


def test_content_hash_state_bound():
    full = recover_body(_article_row(), raw_payload={})
    meta = recover_body(
        _article_row(description=FULL_BODY[:50]), raw_payload={}
    )
    assert full.content_state == "FULL_TEXT"
    assert meta.content_state == "METADATA_ONLY"
    assert full.content_hash != meta.content_hash


def _db_with_articles() -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    for raw_id in ("raw:test-a1", "raw:test-a2"):
        conn.execute(
            """INSERT INTO raw_assets
               (asset_id, ticker, source_type, reference_date, fetched_at,
                data_version, content_raw, metadata_json)
               VALUES (?, 'AAPL', 'finnhub_company_news', '2026-01-05',
                       '2026-01-05T10:00:00Z', 'v1', ?, '{}')""",
            (raw_id, b"raw-payload"),
        )
    conn.commit()
    from catalyst_data.articles import upsert_article

    upsert_article(conn, article={
        "article_id": "finnhub:full-1",
        "raw_asset_id": "raw:test-a1",
        "provider": "finnhub",
        "source_type": "finnhub_company_news",
        "ticker": "AAPL",
        "reference_date": "2026-01-05",
        "published_utc": "2026-01-05T15:30:00Z",
        "title": "Apple announces new AI features",
        "description": FULL_BODY,
        "article_url": "https://example.com/apple-ai",
        "publisher_name": "Example News",
    })
    upsert_article(conn, article={
        "article_id": "finnhub:meta-1",
        "raw_asset_id": "raw:test-a2",
        "provider": "finnhub",
        "source_type": "finnhub_company_news",
        "ticker": "AAPL",
        "reference_date": "2026-01-05",
        "published_utc": "2026-01-05T16:00:00Z",
        "title": "Apple stock watch",
        "description": "Short snippet only.",
        "article_url": "https://example.com/apple-watch",
        "publisher_name": "Example News",
    })
    return conn


def test_persist_article_content_repair_writes_repair_columns():
    conn = _db_with_articles()
    result = recover_body(_article_row(), raw_payload={})
    persist_article_content_repair(
        conn,
        article_id="finnhub:full-1",
        normalized_url=None,
        result=result,
    )
    row = conn.execute(
        "SELECT * FROM articles WHERE article_id='finnhub:full-1'"
    ).fetchone()
    assert row["recovered_content_state"] == "FULL_TEXT"
    assert row["recovered_body_text"] == result.body_text
    assert row["recovered_content_hash"] == result.content_hash
    assert row["body_normalizer_version"] == BODY_NORMALIZER_VERSION
    # M3-5B can read the persisted repair values.
    readback = conn.execute(
        "SELECT recovered_content_state, recovered_content_hash FROM articles "
        "WHERE article_id='finnhub:full-1'"
    ).fetchone()
    assert readback["recovered_content_state"] == "FULL_TEXT"
    conn.close()


def test_persist_article_content_repair_idempotent():
    conn = _db_with_articles()
    result = recover_body(_article_row(), raw_payload={})
    persist_article_content_repair(
        conn, article_id="finnhub:full-1", normalized_url=None, result=result
    )
    persist_article_content_repair(
        conn, article_id="finnhub:full-1", normalized_url=None, result=result
    )
    rows = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE recovered_content_state='FULL_TEXT'"
    ).fetchone()[0]
    assert rows == 1
    conn.close()


def test_persist_article_content_repair_targets_only_requested_article():
    conn = _db_with_articles()
    result = recover_body(_article_row(), raw_payload={})
    persist_article_content_repair(
        conn, article_id="finnhub:full-1", normalized_url=None, result=result
    )
    other = conn.execute(
        "SELECT recovered_content_state FROM articles WHERE article_id='finnhub:meta-1'"
    ).fetchone()
    assert other["recovered_content_state"] is None
    conn.close()


def test_persist_unknown_article_fails_closed():
    conn = _db_with_articles()
    result = recover_body(_article_row(), raw_payload={})
    with pytest.raises(ValueError, match="article"):
        persist_article_content_repair(
            conn, article_id="finnhub:missing", normalized_url=None, result=result
        )
    conn.close()
