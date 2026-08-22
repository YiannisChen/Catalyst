"""M3-6: canonical dedup/independence computation and persistence tests.

Execution-lock §A.7/A.8/§G: union-find over exact normalized URL / content
hash / provider document ID / known syndication; ambiguous lineage is never
independent by default. M3-5B creates the assets with null IDs; M3-6 computes
and atomically persists them before M3-7 audit.
"""
from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.canonical.dedup_independence import (
    DEDUP_ALGORITHM_VERSION,
    INDEPENDENCE_ALGORITHM_VERSION,
    DedupResult,
    compute_dedup_clusters,
    compute_independence_groups,
    load_canonical_assets,
    persist_dedup_independence,
)

FULL_BODY = (
    "Apple Inc. announced new AI features during its product event. The "
    "company said the updates will roll out to customers starting next month. "
    "Analysts expect the changes to improve device performance and battery "
    "life across the lineup. This paragraph is deliberately long enough to "
    "clear the minimum material body threshold."
)


def _asset(**overrides):
    base = {
        "asset_id": "v1:asset:default",
        "asset_type": "NEWS",
        "source_class": "reported_news",
        "canonical_url": "https://example.com/news/a",
        "content_hash": "a" * 64,
        "document_id": "finnhub:a",
    }
    base.update(overrides)
    return base


def test_singleton_assets_get_cluster_ids():
    assets = [
        _asset(asset_id="v1:asset:1", canonical_url="https://example.com/x", content_hash="1" * 64, document_id="finnhub:x"),
        _asset(asset_id="v1:asset:2", canonical_url="https://example.com/y", content_hash="2" * 64, document_id="finnhub:y"),
    ]
    result = compute_dedup_clusters(assets)
    assert result.algorithm_version == DEDUP_ALGORITHM_VERSION == "catalyst_dedup_v1.0"
    assert len(result.cluster_id_by_asset) == 2
    assert result.cluster_id_by_asset["v1:asset:1"] != result.cluster_id_by_asset["v1:asset:2"]
    assert all(cid.startswith("v1:dedup:") for cid in result.cluster_id_by_asset.values())


def test_duplicate_across_providers_same_cluster_via_url():
    assets = [
        _asset(
            asset_id="v1:asset:fn",
            canonical_url="https://example.com/news/dup?utm_source=finnhub",
            content_hash="1" * 64,
            document_id="finnhub:dup",
        ),
        _asset(
            asset_id="v1:asset:pg",
            canonical_url="https://EXAMPLE.com/news/dup",
            content_hash="2" * 64,
            document_id="polygon:dup",
        ),
    ]
    result = compute_dedup_clusters(assets)
    assert result.cluster_id_by_asset["v1:asset:fn"] == result.cluster_id_by_asset["v1:asset:pg"]


def test_same_content_hash_same_cluster_even_different_urls():
    assets = [
        _asset(asset_id="v1:asset:1", canonical_url="https://example.com/a", content_hash="c" * 64, document_id="finnhub:a"),
        _asset(asset_id="v1:asset:2", canonical_url="https://synd.example.com/b", content_hash="c" * 64, document_id="polygon:b"),
    ]
    result = compute_dedup_clusters(assets)
    assert result.cluster_id_by_asset["v1:asset:1"] == result.cluster_id_by_asset["v1:asset:2"]


def test_same_publisher_different_urls_separate_unless_content_equal():
    assets = [
        _asset(asset_id="v1:asset:1", canonical_url="https://example.com/a", content_hash="1" * 64, document_id="finnhub:a"),
        _asset(asset_id="v1:asset:2", canonical_url="https://example.com/b", content_hash="2" * 64, document_id="finnhub:b"),
    ]
    result = compute_dedup_clusters(assets)
    assert result.cluster_id_by_asset["v1:asset:1"] != result.cluster_id_by_asset["v1:asset:2"]


def test_known_syndication_relation_merges_cluster():
    relations = [
        {
            "asset_id_a": "v1:asset:1",
            "asset_id_b": "v1:asset:2",
            "relation": "syndicated",
            "evidence": "provider canonical url",
        }
    ]
    assets = [
        _asset(asset_id="v1:asset:1", canonical_url="https://a.example.com/x", content_hash="1" * 64, document_id="finnhub:x"),
        _asset(asset_id="v1:asset:2", canonical_url="https://b.example.com/x", content_hash="2" * 64, document_id="polygon:x"),
    ]
    result = compute_dedup_clusters(assets, known_syndication=relations)
    assert result.cluster_id_by_asset["v1:asset:1"] == result.cluster_id_by_asset["v1:asset:2"]


def test_unknown_lineage_increments_count_not_group_count():
    assets = [
        _asset(asset_id="v1:asset:1", canonical_url="https://example.com/a", content_hash="1" * 64, document_id="finnhub:a"),
        _asset(asset_id="v1:asset:2", canonical_url="https://example.com/a", content_hash="2" * 64, document_id="polygon:a"),
        _asset(asset_id="v1:asset:3", canonical_url="https://example.com/unique", content_hash="3" * 64, document_id="finnhub:unique"),
    ]
    indep = compute_independence_groups(assets, source_class="reported_news")
    assert indep.algorithm_version == INDEPENDENCE_ALGORITHM_VERSION == "catalyst_independence_v1.0"
    # assets 1+2 share a cluster -> known lineage -> one group with both.
    assert indep.group_id_by_asset["v1:asset:1"] == indep.group_id_by_asset["v1:asset:2"]
    assert indep.group_id_by_asset["v1:asset:1"].startswith("v1:independence:")
    # singleton asset 3: unknown lineage -> None and counted, not grouped.
    assert indep.group_id_by_asset["v1:asset:3"] is None
    assert indep.unknown_independence_asset_count == 1
    assert indep.eligible_reported_news_group_count == 1


def test_non_reported_news_never_independent():
    assets = [
        _asset(asset_id="v1:asset:filing", asset_type="FILING", source_class="official_government",
               canonical_url=None, content_hash="1" * 64, document_id="filing-1"),
        _asset(asset_id="v1:asset:news", source_class="reported_news",
               canonical_url="https://example.com/a", content_hash="2" * 64, document_id="finnhub:a"),
    ]
    indep = compute_independence_groups(assets, source_class="reported_news")
    assert indep.group_id_by_asset["v1:asset:filing"] is None
    # The singleton reported-news article has unknown lineage and is counted.
    assert indep.unknown_independence_asset_count == 1
    assert indep.eligible_reported_news_group_count == 0


def test_determinism_same_input_same_ids():
    assets = [
        _asset(asset_id="v1:asset:1", canonical_url="https://example.com/a?b=2&a=1", content_hash="1" * 64, document_id="finnhub:a"),
        _asset(asset_id="v1:asset:2", canonical_url="https://example.com/a?b=2&a=1", content_hash="2" * 64, document_id="polygon:a"),
    ]
    first = compute_dedup_clusters(assets)
    second = compute_dedup_clusters(list(reversed(assets)))
    assert first.cluster_id_by_asset == second.cluster_id_by_asset
    assert compute_independence_groups(assets).group_id_by_asset == compute_independence_groups(assets).group_id_by_asset


# ---------------------------------------------------------------------------
# Integration: M3-5B -> M3-6 persistence
# ---------------------------------------------------------------------------


def _integrated_conn() -> sqlite3.Connection:
    """M3-5B fixture with 2 duplicated reported-news articles + 1 unique + 1 filing."""
    from catalyst_data.canonical.backfill import backfill_from_subtypes
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    for raw_id in ("raw:r1", "raw:r2", "raw:r3", "raw:r4", "raw:r5", "raw:r6", "raw:sec"):
        conn.execute(
            """INSERT INTO raw_assets
               (asset_id, ticker, source_type, reference_date, fetched_at,
                data_version, content_raw, metadata_json)
               VALUES (?, 'AAPL', 'news', '2026-01-05', '2026-01-05T10:00:00Z',
                       'v1', ?, '{}')""",
            (raw_id, b"raw"),
        )
    from catalyst_data.articles import upsert_article

    articles = [
        {
            "article_id": "finnhub:dup-1",
            "raw_asset_id": "raw:r1",
            "provider": "finnhub",
            "source_type": "finnhub_company_news",
            "ticker": "AAPL",
            "reference_date": "2026-01-05",
            "published_utc": "2026-01-05T15:30:00Z",
            "title": "Apple dup",
            "description": FULL_BODY,
            "article_url": "https://www.example.com/news/dup?utm_source=finnhub",
            "publisher_name": "Example News",
        },
        {
            "article_id": "polygon:dup-1",
            "raw_asset_id": "raw:r2",
            "provider": "polygon",
            "source_type": "polygon_news",
            "ticker": "AAPL",
            "reference_date": "2026-01-05",
            "published_utc": "2026-01-05T15:40:00Z",
            "title": "Apple dup",
            "description": FULL_BODY,
            "article_url": "https://www.example.com/news/dup?utm_source=polygon",
            "publisher_name": "Example News",
        },
        {
            "article_id": "finnhub:unique-1",
            "raw_asset_id": "raw:r3",
            "provider": "finnhub",
            "source_type": "finnhub_company_news",
            "ticker": "AAPL",
            "reference_date": "2026-01-05",
            "published_utc": "2026-01-05T16:00:00Z",
            "title": "Apple unique story",
            "description": FULL_BODY + " Unique additional detail.",
            "article_url": "https://example.com/news/unique",
            "publisher_name": "Example News",
        },
    ]
    for article in articles:
        upsert_article(conn, article=article)
        conn.execute(
            "UPDATE articles SET source_class='reported_news' WHERE article_id=?",
            (article["article_id"],),
        )
        conn.execute(
            """INSERT INTO article_tickers (article_id, ticker, raw_asset_id, reference_date)
               VALUES (?, 'AAPL', ?, '2026-01-05')""",
            (article["article_id"], article["raw_asset_id"]),
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
    conn.commit()
    backfill_from_subtypes(conn)
    return conn


def test_integration_persist_after_m35b():
    conn = _integrated_conn()
    assets = load_canonical_assets(conn)
    # M3-5B leaves IDs null.
    assert all(a["dedup_cluster_id"] is None for a in assets)
    assert all(a["independence_group_id"] is None for a in assets)

    dedup = compute_dedup_clusters(assets)
    indep = compute_independence_groups(assets, source_class="reported_news")
    persist_dedup_independence(conn, dedup, indep)

    # Every canonical asset has a dedup_cluster_id.
    rows = conn.execute(
        "SELECT dedup_cluster_id, independence_group_id, source_class "
        "FROM canonical_assets"
    ).fetchall()
    assert all(r["dedup_cluster_id"] is not None for r in rows)
    # Only eligible known-lineage reported-news assets carry an independence id.
    eligible = [r for r in rows if r["source_class"] == "reported_news"]
    assert any(r["independence_group_id"] is not None for r in eligible)
    # dup-1 pair share both cluster and group; unique article unknown lineage.
    dup1 = conn.execute(
        "SELECT a.dedup_cluster_id, a.independence_group_id FROM canonical_assets a "
        "JOIN canonical_subtype_assoc s ON s.asset_id=a.asset_id "
        "WHERE s.subtype_pk_value='finnhub:dup-1'"
    ).fetchone()
    dup2 = conn.execute(
        "SELECT a.dedup_cluster_id, a.independence_group_id FROM canonical_assets a "
        "JOIN canonical_subtype_assoc s ON s.asset_id=a.asset_id "
        "WHERE s.subtype_pk_value='polygon:dup-1'"
    ).fetchone()
    assert dup1["dedup_cluster_id"] == dup2["dedup_cluster_id"]
    assert dup1["independence_group_id"] == dup2["independence_group_id"]
    unique = conn.execute(
        "SELECT a.independence_group_id FROM canonical_assets a "
        "JOIN canonical_subtype_assoc s ON s.asset_id=a.asset_id "
        "WHERE s.subtype_pk_value='finnhub:unique-1'"
    ).fetchone()
    assert unique["independence_group_id"] is None
    # IDs survive a fresh read for the M3-7 audit.
    fresh = load_canonical_assets(conn)
    fresh_by_id = {a["asset_id"]: a for a in fresh}
    dup1_asset_id = _dup1_asset_id(conn)
    assert fresh_by_id[dup1_asset_id]["dedup_cluster_id"] is not None
    assert fresh_by_id[dup1_asset_id]["independence_group_id"] is not None
    conn.close()


def _dup1_asset_id(conn) -> str:
    from catalyst_data.canonical.ids import asset_id as build_asset_id

    return build_asset_id(
        asset_type="NEWS", source_table="articles", source_pk="finnhub:dup-1"
    )


def test_persist_idempotent_equal_rerun():
    conn = _integrated_conn()
    assets = load_canonical_assets(conn)
    dedup = compute_dedup_clusters(assets)
    indep = compute_independence_groups(assets, source_class="reported_news")
    persist_dedup_independence(conn, dedup, indep)
    before = conn.execute(
        "SELECT asset_id, dedup_cluster_id, independence_group_id FROM canonical_assets ORDER BY asset_id"
    ).fetchall()
    persist_dedup_independence(conn, dedup, indep)
    after = conn.execute(
        "SELECT asset_id, dedup_cluster_id, independence_group_id FROM canonical_assets ORDER BY asset_id"
    ).fetchall()
    assert before == after
    conn.close()


def test_persist_fails_closed_on_unequal_overwrite():
    conn = _integrated_conn()
    assets = load_canonical_assets(conn)
    dedup = compute_dedup_clusters(assets)
    indep = compute_independence_groups(assets, source_class="reported_news")
    persist_dedup_independence(conn, dedup, indep)
    conn.execute(
        "UPDATE canonical_assets SET dedup_cluster_id='v1:dedup:manual' "
        "WHERE asset_id=?", (_dup1_asset_id(conn),)
    )
    conn.commit()
    with pytest.raises(ValueError, match="dedup"):
        persist_dedup_independence(conn, dedup, indep)
    conn.close()


def test_persist_fails_closed_on_missing_asset():
    conn = _integrated_conn()
    assets = load_canonical_assets(conn)
    dedup = compute_dedup_clusters(assets)
    # Add an unknown asset to the result (new immutable result).
    dedup = DedupResult(
        cluster_id_by_asset={
            **dedup.cluster_id_by_asset, "v1:asset:ghost": "v1:dedup:ghost"
        },
        algorithm_version=dedup.algorithm_version,
    )
    indep = compute_independence_groups(assets, source_class="reported_news")
    with pytest.raises(ValueError, match="asset"):
        persist_dedup_independence(conn, dedup, indep)
    conn.close()


def test_persist_fails_closed_when_dedup_omits_asset():
    conn = _integrated_conn()
    assets = load_canonical_assets(conn)
    dedup = compute_dedup_clusters(assets)
    dropped = dict(dedup.cluster_id_by_asset)
    dropped.pop(_dup1_asset_id(conn))
    dedup = DedupResult(
        cluster_id_by_asset=dropped, algorithm_version=dedup.algorithm_version
    )
    indep = compute_independence_groups(assets, source_class="reported_news")
    with pytest.raises(ValueError, match="dedup"):
        persist_dedup_independence(conn, dedup, indep)
    conn.close()
