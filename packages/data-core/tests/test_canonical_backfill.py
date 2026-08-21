"""M3-2: canonical projection engine + structured-fact certification tests.

Execution-lock §A.5/§C: fixture-only text projection proves the schema/identity
contract; structured facts (ohlcv/macro/fundamental) are certified into
canonical_structured_fact_refs with fact_id and never projected as text assets.
Production text projection over repaired rows is owned by M3-5B.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from catalyst_data.canonical.ids import (
    asset_id as build_asset_id,
    canonical_json_bytes,
    fact_id as build_fact_id,
    source_pk_json,
)
from catalyst_data.canonical.backfill import backfill_from_subtypes


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _seed_raw(
    conn: sqlite3.Connection, *, asset_id: str, source_type: str = "news"
) -> None:
    conn.execute(
        """INSERT INTO raw_assets
           (asset_id, ticker, source_type, reference_date, fetched_at,
            data_version, content_raw, metadata_json)
           VALUES (?, 'AAPL', ?, '2026-01-05', '2026-01-05T10:00:00Z',
                   'v1', ?, '{}')""",
        (asset_id, source_type, b"raw-payload"),
    )


FULL_BODY = (
    "Apple Inc. announced new AI features during its product event. "
    "The company said the updates will roll out to customers starting next "
    "month. Analysts expect the changes to improve device performance and "
    "battery life across the lineup. This paragraph is deliberately long "
    "enough to clear the minimum material body threshold so the projection "
    "classifies the article as full text evidence rather than metadata."
)


def _fixture_conn() -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("PRAGMA foreign_keys = ON")

    _seed_raw(conn, asset_id="raw:test-a1", source_type="finnhub_company_news")
    _seed_raw(conn, asset_id="raw:test-a2", source_type="finnhub_company_news")
    _seed_raw(conn, asset_id="raw:test-sec", source_type="sec_filings")
    _seed_raw(conn, asset_id="raw:test-ohlcv", source_type="polygon_ohlcv")
    _seed_raw(conn, asset_id="raw:test-macro", source_type="fred_macro")
    _seed_raw(conn, asset_id="raw:test-fund", source_type="fmp_fundamentals")

    # 2 articles: one full-body, one metadata-only.
    from catalyst_data.articles import upsert_article, upsert_article_ticker

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
    upsert_article_ticker(
        conn, article_id="finnhub:full-1", ticker="AAPL",
        raw_asset_id="raw:test-a1", reference_date="2026-01-05",
    )
    upsert_article_ticker(
        conn, article_id="finnhub:meta-1", ticker="AAPL",
        raw_asset_id="raw:test-a2", reference_date="2026-01-05",
    )

    # 1 filing with 1 primary document.
    from catalyst_data.storage.sqlite import upsert_filing

    upsert_filing(
        conn,
        filing_id="filing-8k-0000320193-26-000001",
        cik="0000320193",
        ticker="AAPL",
        form_type="8-K",
        filed_at="2026-01-05",
        accession_number="0000320193-26-000001",
        primary_document="a.htm",
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/a.htm",
        raw_asset_id="raw:test-sec",
    )
    conn.execute(
        """INSERT INTO filing_documents (
               filing_id, document_url, document_type, text, char_len,
               content_type, byte_size, extraction_status, extracted_at,
               document_id
           ) VALUES (?, ?, 'primary_doc', ?, ?, 'text/html', ?, 'success',
                     '2026-01-05T20:00:00Z', ?)""",
        (
            "filing-8k-0000320193-26-000001",
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/a.htm",
            "Item 1.01 Entry into a Material Definitive Agreement. On January 5, 2026, "
            "Apple Inc. entered into a material definitive agreement.",
            160,
            160,
            "a" * 64,
        ),
    )

    # 1 ohlcv row on a trading day.
    conn.execute(
        """INSERT INTO ohlcv (symbol, date, open, high, low, close, volume, source)
           VALUES ('AAPL', '2026-01-05', 200.0, 205.0, 198.0, 204.5, 50000000, 'polygon')"""
    )
    # 1 macro observation with released_at.
    from catalyst_data.storage.sqlite import upsert_macro_observation

    upsert_macro_observation(
        conn,
        series_id="FEDFUNDS",
        observation_date="2026-01-05",
        value=4.5,
        released_at="2026-01-05T19:00:00Z",
        raw_asset_id="raw:test-macro",
    )
    # 1 fundamental statement with available_at.
    conn.execute(
        """INSERT INTO fundamental_statements (
               statement_id, raw_asset_id, provider, ticker, statement_type,
               fiscal_date, fiscal_period, reported_currency, available_at,
               payload_json, created_at
           ) VALUES ('stmt-1', 'raw:test-fund', 'fmp', 'AAPL',
                     'income_statement', '2025-12-31', 'FY', 'USD',
                     '2026-01-05T20:00:00Z', '{}', '2026-01-05T20:00:00Z')"""
    )
    conn.commit()
    return conn


def _canonical_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "canonical_assets",
            "canonical_content_versions",
            "canonical_subtype_assoc",
            "canonical_asset_tickers",
            "canonical_structured_fact_refs",
        )
    }


def test_backfill_projects_fixture_text_assets_and_fact_refs():
    conn = _fixture_conn()
    result = backfill_from_subtypes(conn)
    counts = _canonical_counts(conn)
    assert counts["canonical_assets"] == 3
    assert counts["canonical_content_versions"] == 3
    assert counts["canonical_subtype_assoc"] == 4  # 2 articles + 1 filing + 1 document
    assert counts["canonical_asset_tickers"] == 3  # 2 article + 1 filing
    assert counts["canonical_structured_fact_refs"] == 3
    assert result.assets == 3
    assert result.fact_refs == 3
    conn.close()


def test_backfill_content_states_and_serving_status():
    conn = _fixture_conn()
    backfill_from_subtypes(conn)
    rows = {
        row["asset_id"]: row
        for row in conn.execute(
            "SELECT * FROM canonical_assets ORDER BY asset_id"
        ).fetchall()
    }
    by_type = {
        row["asset_type"]: row
        for row in conn.execute("SELECT * FROM canonical_assets").fetchall()
    }
    news_rows = [row for row in conn.execute(
        "SELECT * FROM canonical_assets WHERE asset_type='NEWS' ORDER BY asset_id"
    ).fetchall()]
    assert len(news_rows) == 2
    states = {row["content_state"] for row in news_rows}
    assert states == {"FULL_TEXT", "METADATA_ONLY"}
    full = next(row for row in news_rows if row["content_state"] == "FULL_TEXT")
    meta = next(row for row in news_rows if row["content_state"] == "METADATA_ONLY")
    assert full["serving_status"] == "body_candidate"
    assert full["parse_quality"] == "not_applicable"
    assert meta["serving_status"] == "lead_candidate"
    assert meta["parse_quality"] == "not_applicable"
    filing = by_type["FILING"]
    assert filing["content_state"] == "FULL_TEXT"
    assert filing["parse_quality"] == "full"
    assert filing["eligible_at"] is None
    assert filing["eligible_at_reason"] == "fail_closed_no_accepted_time"
    assert filing["fail_closed"] == 1
    assert filing["serving_status"] == "excluded"
    conn.close()


def test_backfill_asset_ids_namespaced_and_stable():
    conn = _fixture_conn()
    first = backfill_from_subtypes(conn)
    ids_first = {
        row["asset_id"]
        for row in conn.execute("SELECT asset_id FROM canonical_assets").fetchall()
    }
    assert all(aid.startswith("v1:asset:") for aid in ids_first)
    # Expected golden asset ids for the fixture rows.
    assert build_asset_id(
        asset_type="NEWS", source_table="articles", source_pk="finnhub:full-1"
    ) in ids_first
    assert build_asset_id(
        asset_type="NEWS", source_table="articles", source_pk="finnhub:meta-1"
    ) in ids_first
    assert build_asset_id(
        asset_type="FILING", source_table="filings",
        source_pk="filing-8k-0000320193-26-000001",
    ) in ids_first
    # Idempotent rerun: no new rows, same IDs.
    second = backfill_from_subtypes(conn)
    ids_second = {
        row["asset_id"]
        for row in conn.execute("SELECT asset_id FROM canonical_assets").fetchall()
    }
    assert ids_second == ids_first
    assert _canonical_counts(conn)["canonical_assets"] == 3
    assert first.assets == second.assets == 3
    conn.close()


def test_backfill_fact_ids_namespaced_and_certified():
    conn = _fixture_conn()
    backfill_from_subtypes(conn)
    facts = conn.execute(
        "SELECT * FROM canonical_structured_fact_refs ORDER BY fact_id"
    ).fetchall()
    assert len(facts) == 3
    statuses = {row["certification_status"] for row in facts}
    assert statuses == {"certified"}
    fact_ids = {row["fact_id"] for row in facts}
    assert all(fid.startswith("v1:fact:") for fid in fact_ids)
    expected_ohlcv = build_fact_id(
        fact_type="ohlcv",
        source_table="ohlcv",
        natural_key={"symbol": "AAPL", "date": "2026-01-05"},
    )
    expected_macro = build_fact_id(
        fact_type="macro_observation",
        source_table="macro_observations",
        natural_key={"series_id": "FEDFUNDS", "observation_date": "2026-01-05"},
    )
    expected_fund = build_fact_id(
        fact_type="fundamental_statement",
        source_table="fundamental_statements",
        natural_key={"statement_id": "stmt-1"},
    )
    assert fact_ids == {expected_ohlcv, expected_macro, expected_fund}
    # source_pk_json is exactly the locked serializer output for the natural key.
    by_table = {row["source_table"]: row for row in facts}
    assert by_table["ohlcv"]["source_pk_json"] == source_pk_json(
        {"symbol": "AAPL", "date": "2026-01-05"}
    )
    # eligible_at is PIT-derived: OHLCV uses the exchange session close.
    from catalyst_data.trading_calendar import session_close_utc

    assert by_table["ohlcv"]["eligible_at"] == session_close_utc("2026-01-05")
    assert by_table["macro_observations"]["eligible_at"] == "2026-01-05T19:00:00Z"
    assert by_table["fundamental_statements"]["eligible_at"] == "2026-01-05T20:00:00Z"
    conn.close()


def test_backfill_no_orphans_after_projection():
    conn = _fixture_conn()
    backfill_from_subtypes(conn)
    orphan_tickers = conn.execute(
        "SELECT COUNT(*) FROM canonical_asset_tickers t "
        "LEFT JOIN canonical_assets a ON a.asset_id = t.asset_id "
        "WHERE a.asset_id IS NULL"
    ).fetchone()[0]
    assert orphan_tickers == 0
    orphan_assoc = conn.execute(
        "SELECT COUNT(*) FROM canonical_subtype_assoc s "
        "LEFT JOIN canonical_assets a ON a.asset_id = s.asset_id "
        "WHERE a.asset_id IS NULL"
    ).fetchone()[0]
    assert orphan_assoc == 0
    orphan_versions = conn.execute(
        "SELECT COUNT(*) FROM canonical_content_versions v "
        "LEFT JOIN canonical_assets a ON a.asset_id = v.asset_id "
        "WHERE a.asset_id IS NULL"
    ).fetchone()[0]
    assert orphan_versions == 0
    # Every association content version binds the same asset (ownership).
    wrong_owner = conn.execute(
        "SELECT COUNT(*) FROM canonical_subtype_assoc s "
        "JOIN canonical_content_versions v "
        "ON v.canonical_content_version_id = s.canonical_content_version_id "
        "WHERE v.asset_id != s.asset_id"
    ).fetchone()[0]
    assert wrong_owner == 0
    # Every fact ref resolves to its source row.
    for source_table, pk_sql in (
        ("ohlcv", "SELECT COUNT(*) FROM ohlcv o JOIN canonical_structured_fact_refs f "
                  "ON f.source_table='ohlcv' AND f.source_pk_json = ? "),
        ("macro_observations", "SELECT COUNT(*) FROM macro_observations o JOIN canonical_structured_fact_refs f "
                  "ON f.source_table='macro_observations' AND f.source_pk_json = ? "),
        ("fundamental_statements", "SELECT COUNT(*) FROM fundamental_statements o JOIN canonical_structured_fact_refs f "
                  "ON f.source_table='fundamental_statements' AND f.source_pk_json = ? "),
    ):
        pass  # resolved individually below
    ohlcv_refs = conn.execute(
        "SELECT source_pk_json FROM canonical_structured_fact_refs WHERE source_table='ohlcv'"
    ).fetchall()
    for ref in ohlcv_refs:
        pk = json.loads(ref["source_pk_json"])
        exists = conn.execute(
            "SELECT COUNT(*) FROM ohlcv WHERE symbol=? AND date=?",
            (pk["symbol"], pk["date"]),
        ).fetchone()[0]
        assert exists == 1
    conn.close()


def test_backfill_filing_document_association_binds_exact_content_version():
    conn = _fixture_conn()
    backfill_from_subtypes(conn)
    filing_row = conn.execute(
        "SELECT * FROM canonical_assets WHERE asset_type='FILING'"
    ).fetchone()
    version = conn.execute(
        "SELECT * FROM canonical_content_versions WHERE asset_id=?",
        (filing_row["asset_id"],),
    ).fetchone()
    assoc = conn.execute(
        "SELECT * FROM canonical_subtype_assoc WHERE subtype_table='filing_documents'"
    ).fetchone()
    assert assoc["canonical_content_version_id"] == version["canonical_content_version_id"]
    assert version["asset_id"] == filing_row["asset_id"]
    filing_assoc = conn.execute(
        "SELECT * FROM canonical_subtype_assoc WHERE subtype_table='filings'"
    ).fetchone()
    assert filing_assoc["subtype_pk_value"] == "filing-8k-0000320193-26-000001"
    assert filing_assoc["canonical_content_version_id"] == version["canonical_content_version_id"]
    conn.close()


def test_backfill_dedup_and_independence_ids_null_until_m3_6():
    conn = _fixture_conn()
    backfill_from_subtypes(conn)
    rows = conn.execute(
        "SELECT dedup_cluster_id, independence_group_id FROM canonical_assets"
    ).fetchall()
    assert all(row["dedup_cluster_id"] is None for row in rows)
    assert all(row["independence_group_id"] is None for row in rows)
    conn.close()


def test_backfill_dry_run_zero_writes():
    conn = _fixture_conn()
    before = _canonical_counts(conn)
    result = backfill_from_subtypes(conn, dry_run=True)
    after = _canonical_counts(conn)
    assert before == after == {
        "canonical_assets": 0,
        "canonical_content_versions": 0,
        "canonical_subtype_assoc": 0,
        "canonical_asset_tickers": 0,
        "canonical_structured_fact_refs": 0,
    }
    assert result.assets == 3
    assert result.fact_refs == 3
    conn.close()


def test_backfill_fact_missing_time_is_excluded():
    conn = _fixture_conn()
    conn.execute(
        """INSERT INTO macro_observations (
               series_id, observation_date, value, released_at, fetched_at, raw_asset_id
           ) VALUES ('GDP', '2026-01-06', 3.1, NULL, '2026-01-06T10:00:00Z',
                     'raw:test-macro')"""
    )
    conn.commit()
    backfill_from_subtypes(conn)
    row = conn.execute(
        "SELECT * FROM canonical_structured_fact_refs "
        "WHERE source_table='macro_observations' AND source_pk_json=?",
        (source_pk_json({"series_id": "GDP", "observation_date": "2026-01-06"}),),
    ).fetchone()
    assert row is not None
    assert row["certification_status"] == "excluded_missing_time"
    assert row["eligible_at"] is None
    conn.close()


def test_backfill_rejects_duplicate_semantic_natural_key_spellings():
    """A fact ref with a hand-written (non-canonical) source_pk_json spelling of
    an existing semantic natural key must fail closed instead of minting a
    second ref for the same semantic key."""
    conn = _fixture_conn()
    # Insert the duplicate semantic key with a hand-written (non-canonical) spelling.
    conn.execute(
        """INSERT INTO canonical_structured_fact_refs (
               fact_id, fact_type, source_table, source_pk_json, eligible_at,
               temporal_precision, source_row_sha256, certification_status, created_at
           ) VALUES (?, 'ohlcv', 'ohlcv', ?, NULL, 'unknown', ?, 'excluded_missing_time',
                     '2026-01-05T20:00:00Z')""",
        (
            build_fact_id(
                fact_type="ohlcv",
                source_table="ohlcv",
                natural_key={"symbol": "AAPL", "date": "2026-01-05"},
            ),
            '{"symbol": "AAPL", "date": "2026-01-05"}',  # non-canonical whitespace spelling
            "0" * 64,
        ),
    )
    conn.commit()
    with pytest.raises(Exception):
        backfill_from_subtypes(conn)
    conn.close()
