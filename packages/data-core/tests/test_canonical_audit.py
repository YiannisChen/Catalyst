"""M3-7: canonical temporal/materiality/source audit gate tests.

Execution-lock §H: one shared serving predicate S = serving_status IN
('body_candidate','lead_candidate'); every ratio denominator must be > 0 or
the audit fails closed; structured-fact reconciliation gates and provenance
integrity are part of the no-orphan gate.
"""
from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.canonical.audit import audit_canonical

FULL_BODY = (
    "Apple Inc. announced new AI features during its product event. The "
    "company said the updates will roll out to customers starting next month. "
    "Analysts expect the changes to improve device performance and battery "
    "life across the lineup. This paragraph is deliberately long enough to "
    "clear the minimum material body threshold."
)


def _seed_raw(conn, asset_id: str) -> None:
    conn.execute(
        """INSERT INTO raw_assets
           (asset_id, ticker, source_type, reference_date, fetched_at,
            data_version, content_raw, metadata_json)
           VALUES (?, 'AAPL', 'news', '2026-01-05', '2026-01-05T10:00:00Z',
                   'v1', ?, '{}')""",
        (asset_id, b"raw"),
    )


def _audited_conn() -> sqlite3.Connection:
    """Backfilled + dedup-persisted canonical DB (2 searchable news, 1 filing)."""
    from catalyst_data.canonical.backfill import backfill_from_subtypes
    from catalyst_data.canonical.dedup_independence import (
        compute_dedup_clusters,
        compute_independence_groups,
        load_canonical_assets,
        persist_dedup_independence,
    )
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    for raw_id in ("raw:a1", "raw:a2", "raw:sec", "raw:ohlcv", "raw:macro", "raw:fund"):
        _seed_raw(conn, raw_id)

    from catalyst_data.articles import upsert_article, upsert_article_ticker

    upsert_article(conn, article={
        "article_id": "finnhub:full-1",
        "raw_asset_id": "raw:a1",
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
        "raw_asset_id": "raw:a2",
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
    upsert_article_ticker(conn, article_id="finnhub:full-1", ticker="AAPL",
                          raw_asset_id="raw:a1", reference_date="2026-01-05")
    upsert_article_ticker(conn, article_id="finnhub:meta-1", ticker="AAPL",
                          raw_asset_id="raw:a2", reference_date="2026-01-05")

    # Batch A: FULL_TEXT news requires an authentic-body repair; the long
    # description alone is METADATA_ONLY and cannot mint FULL_TEXT.
    from catalyst_data.articles.body_recovery import (
        persist_article_content_repair,
        recover_body,
    )

    repair = recover_body(
        {
            "article_id": "finnhub:full-1",
            "title": "Apple announces new AI features",
            "description": FULL_BODY,
            "article_url": "https://example.com/apple-ai",
        },
        raw_payload={"body": FULL_BODY},
    )
    persist_article_content_repair(
        conn,
        article_id="finnhub:full-1",
        normalized_url=None,
        result=repair,
    )

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
        raw_asset_id="raw:sec",
    )
    conn.execute(
        """INSERT INTO filing_documents (
               filing_id, document_url, document_type, text, char_len,
               content_type, byte_size, extraction_status, extracted_at, document_id
           ) VALUES (?, ?, 'primary_doc', ?, ?, 'text/html', ?, 'success',
                     '2026-01-05T20:00:00Z', ?)""",
        (
            "filing-8k-0000320193-26-000001",
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/a.htm",
            "Item 1.01 Entry into a Material Definitive Agreement.\nOn January 5, "
            "2026, the registrant entered into a material definitive agreement. "
            + ("Disclosure text follows. " * 12),
            200,
            200,
            "b" * 64,
        ),
    )
    conn.execute(
        """INSERT INTO ohlcv (symbol, date, open, high, low, close, volume, source)
           VALUES ('AAPL', '2026-01-05', 200.0, 205.0, 198.0, 204.5, 50000000, 'polygon')"""
    )
    from catalyst_data.storage.sqlite import upsert_macro_observation

    upsert_macro_observation(
        conn, series_id="FEDFUNDS", observation_date="2026-01-05", value=4.5,
        released_at="2026-01-05T19:00:00Z", raw_asset_id="raw:macro",
    )
    conn.execute(
        """INSERT INTO fundamental_statements (
               statement_id, raw_asset_id, provider, ticker, statement_type,
               fiscal_date, fiscal_period, reported_currency, available_at,
               payload_json, created_at
           ) VALUES ('stmt-1', 'raw:fund', 'fmp', 'AAPL', 'income_statement',
                     '2025-12-31', 'FY', 'USD', '2026-01-05T20:00:00Z', '{}',
                     '2026-01-05T20:00:00Z')"""
    )
    conn.commit()

    backfill_from_subtypes(conn)
    assets = load_canonical_assets(conn)
    persist_dedup_independence(
        conn,
        compute_dedup_clusters(assets),
        compute_independence_groups(assets, source_class="reported_news"),
    )
    return conn


def test_audit_passes_on_healthy_fixture():
    conn = _audited_conn()
    audit = audit_canonical(conn)
    assert audit.passed is True, audit.failures
    assert audit.searchable_news == 2
    assert audit.searchable_serving_evidence == 2
    assert audit.source_class_present == 2
    assert audit.publisher_present == 2
    assert audit.dedup_identity_covered == 2
    assert audit.material_capable_evidence == 1  # FULL_TEXT only
    assert audit.empty_failed_excluded == 0
    assert audit.no_orphan_count == 0
    assert audit.structured_fact_certified == 3
    assert audit.structured_fact_excluded == 0
    assert audit.structured_reconciliation_gate is True
    assert audit.source_class_gate is True
    assert audit.publisher_gate is True
    assert audit.dedup_identity_gate is True
    assert audit.no_orphan_gate is True
    conn.close()


def test_audit_reports_total_assets_by_content_state():
    conn = _audited_conn()
    audit = audit_canonical(conn)
    assert audit.total_assets == 3
    assert audit.assets_by_content_state["FULL_TEXT"] == 2  # article + filing
    assert audit.assets_by_content_state["METADATA_ONLY"] == 1
    conn.close()


def test_audit_eligible_assets_under_cutoff():
    conn = _audited_conn()
    audit = audit_canonical(conn, cutoff_at="2026-01-05T22:00:00Z")
    assert audit.eligible_assets == 2  # both news eligible; filing fail-closed
    late = audit_canonical(conn, cutoff_at="2026-01-05T15:00:00Z")
    assert late.eligible_assets == 0
    conn.close()


def test_audit_empty_failed_never_serving_eligible():
    conn = _audited_conn()
    conn.execute(
        "UPDATE canonical_assets SET content_state='EMPTY', serving_status='excluded' "
        "WHERE asset_type='NEWS' AND content_state='FULL_TEXT'"
    )
    conn.commit()
    audit = audit_canonical(conn)
    assert audit.empty_failed_excluded == 0  # excluded rows are not in S
    assert audit.searchable_serving_evidence == 1
    assert audit.material_capable_evidence == 0
    conn.close()


def test_audit_publisher_gate_failure_lists_dimension():
    conn = _audited_conn()
    conn.execute("UPDATE canonical_assets SET publisher=NULL")
    conn.commit()
    audit = audit_canonical(conn)
    assert audit.passed is False
    assert "publisher_gate" in audit.failures
    assert "source_class_gate" not in audit.failures
    conn.close()


def test_audit_source_class_gate_failure_lists_dimension():
    conn = _audited_conn()
    # Simulate an absent source_class ('' violates the closed-set CHECK; the
    # audit still treats only NULL/empty as absent per §H).
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute("UPDATE canonical_assets SET source_class='' WHERE asset_type='NEWS'")
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    conn.commit()
    audit = audit_canonical(conn)
    assert audit.passed is False
    assert "source_class_gate" in audit.failures
    conn.close()


def test_audit_dedup_gate_failure_lists_dimension():
    conn = _audited_conn()
    conn.execute("UPDATE canonical_assets SET dedup_cluster_id=NULL")
    conn.commit()
    audit = audit_canonical(conn)
    assert audit.passed is False
    assert "dedup_identity_gate" in audit.failures
    conn.close()


def test_audit_no_orphan_failure_lists_dimension():
    conn = _audited_conn()
    # Orphan ticker row: FK enforcement is disabled only for this deliberate
    # orphan insert (production writers never create such rows).
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """INSERT INTO canonical_asset_tickers (asset_id, ticker, issuer_id)
           VALUES ('v1:asset:ghost', 'AAPL', 'issuer:AAPL')"""
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    audit = audit_canonical(conn)
    assert audit.passed is False
    assert audit.no_orphan_count >= 1
    assert "no_orphan_gate" in audit.failures
    conn.close()


def test_audit_denominator_zero_fails_closed():
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    audit = audit_canonical(conn)
    assert audit.passed is False
    assert "source_class_denominator_zero" in audit.failures
    assert "publisher_denominator_zero" in audit.failures
    assert "dedup_denominator_zero" in audit.failures
    conn.close()


def test_audit_structured_fact_reconciliation_gate():
    conn = _audited_conn()
    # A ref whose source row does not exist -> ref-per-source-row shortfall.
    conn.execute(
        """INSERT INTO canonical_structured_fact_refs (
               fact_id, fact_type, source_table, source_pk_json, eligible_at,
               temporal_precision, source_row_sha256, certification_status, created_at
           ) VALUES ('v1:fact:ghost', 'macro_observation', 'macro_observations',
                     '{"series_id":"NOPE","observation_date":"2026-01-05"}',
                     NULL, 'unknown', ?, 'certified', '2026-01-05T20:00:00Z')""",
        ("0" * 64,),
    )
    conn.commit()
    audit = audit_canonical(conn)
    assert audit.passed is False
    assert audit.no_orphan_count >= 1
    assert "structured_reconciliation_gate" in audit.failures or "no_orphan_gate" in audit.failures
    conn.close()


def test_audit_provenance_coverage_gate():
    conn = _audited_conn()
    # Drop provenance coverage for one serving-eligible asset.
    asset_id = conn.execute(
        "SELECT asset_id FROM canonical_assets WHERE serving_status='body_candidate' LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        "DELETE FROM normalized_provenance WHERE canonical_asset_id=?", (asset_id,)
    )
    conn.commit()
    audit = audit_canonical(conn)
    assert audit.passed is False
    assert audit.missing_provenance_coverage >= 1
    assert "no_orphan_gate" in audit.failures
    conn.close()


def test_audit_all_excluded_populated_table_is_not_reconciliation_failure():
    """Complete all-excluded coverage is not a reconciliation failure (0B).

    When certification ran (one ref per source row, certified+excluded ==
    source-row total) but every row was excluded, the table is all-excluded:
    the gate stays True and the table is recorded for M4 exclusion.
    """
    conn = _audited_conn()
    conn.execute(
        "UPDATE canonical_structured_fact_refs SET certification_status='excluded_invalid' "
        "WHERE source_table='macro_observations'"
    )
    conn.commit()
    audit = audit_canonical(conn)
    assert audit.structured_fact_ref_per_source_row_shortfall == 0
    assert audit.structured_fact_certified_plus_excluded_shortfall == 0
    assert audit.structured_fact_populated_tables_without_certified == (
        "macro_observations",
    )
    assert audit.structured_reconciliation_gate is True
    conn.close()


def test_audit_populated_table_with_zero_refs_fails_closed():
    """A populated table with zero refs means the certifier never ran (0B)."""
    conn = _audited_conn()
    conn.execute(
        "DELETE FROM canonical_structured_fact_refs WHERE source_table='macro_observations'"
    )
    conn.commit()
    audit = audit_canonical(conn)
    assert audit.structured_fact_ref_per_source_row_shortfall == 1
    assert audit.structured_fact_certified_plus_excluded_shortfall == 1
    assert audit.structured_reconciliation_gate is False
    assert "structured_reconciliation_gate" in audit.failures
    conn.close()
