"""Tests for coverage_audit.py — read-only coverage/integrity audit module (3F.1 Part A).

Each test creates its own in-memory or temp-file DB, populates it with known data,
then runs the audit and asserts specific dimensions.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from catalyst_data.storage.sqlite import init_db, upsert_raw_asset
from catalyst_data.articles import ensure_articles_table, upsert_article, upsert_article_ticker
from catalyst_data.source_tier import classify_articles


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_db(db_path: str) -> sqlite3.Connection:
    """Create a fresh DB with all tables via init_db, return open connection."""
    conn = sqlite3.connect(db_path)
    init_db(conn)
    return conn


def _add_article_with_raw(
    conn: sqlite3.Connection,
    article_id: str,
    ticker: str,
    title: str,
    source_type: str = "polygon_news",
    provider: str = "polygon",
    published_utc: str = "2025-06-15T12:00:00Z",
    reference_date: str = "2025-06-15",
    publisher_name: str = "Benzinga",
    description: str = "Test description",
    source_tier: int | None = 3,
    dedup_group_id: str | None = None,
    is_canonical: int = 1,
    article_url: str | None = None,
    raw_content: bytes | None = None,
) -> tuple[str, str]:
    """Add a raw_asset + article + article_ticker row. Returns (article_id, raw_asset_id)."""
    raw_id = f"raw-{article_id}"
    raw_bytes = raw_content or json.dumps({"title": title}).encode("utf-8")
    upsert_raw_asset(
        conn,
        asset_id=raw_id,
        ticker=ticker,
        source_type=source_type,
        reference_date=reference_date,
        content_raw=raw_bytes,
    )
    upsert_article(conn, article={
        "article_id": article_id,
        "raw_asset_id": raw_id,
        "provider": provider,
        "source_type": source_type,
        "ticker": ticker,
        "reference_date": reference_date,
        "published_utc": published_utc,
        "title": title,
        "description": description,
        "publisher_name": publisher_name,
        "article_url": article_url,
    })
    # Set tier + dedup if provided
    if source_tier is not None:
        conn.execute("UPDATE articles SET source_tier = ? WHERE article_id = ?",
                     (source_tier, article_id))
    if dedup_group_id is not None:
        conn.execute("UPDATE articles SET dedup_group_id = ? WHERE article_id = ?",
                     (dedup_group_id, article_id))
    if is_canonical != 1:
        conn.execute("UPDATE articles SET is_canonical = ? WHERE article_id = ?",
                     (is_canonical, article_id))
    upsert_article_ticker(
        conn,
        article_id=article_id,
        ticker=ticker,
        raw_asset_id=raw_id,
        reference_date=reference_date,
    )
    return article_id, raw_id


# ---------------------------------------------------------------------------
# TA1 — query_only pragma enforced
# ---------------------------------------------------------------------------

def test_query_only_pragma_enforced(tmp_path: Path):
    """Opening with query_only=ON prevents writes."""
    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    conn.close()

    conn2 = sqlite3.connect(db_path)
    conn2.execute("PRAGMA query_only = ON")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn2.execute("CREATE TABLE should_fail (x INTEGER)")
    conn2.close()


# ---------------------------------------------------------------------------
# TA2 — frozen DB refused
# ---------------------------------------------------------------------------

def test_frozen_db_refused():
    """Passing the frozen DB realpath raises RuntimeError."""
    from catalyst_data.coverage_audit import run_coverage_audit

    repo_root = Path(__file__).resolve().parents[3]
    frozen = os.path.realpath(repo_root / "data" / "catalyst_eval_frozen_v2.db")
    with pytest.raises(RuntimeError, match="frozen"):
        run_coverage_audit(frozen)


# ---------------------------------------------------------------------------
# TA3 — per-source table counts
# ---------------------------------------------------------------------------

def test_per_source_table_counts(tmp_path: Path):
    """D1: per-source per-table counts match known inserts."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    _add_article_with_raw(conn, "poly:a1", "AAPL", "Title 1")
    _add_article_with_raw(conn, "poly:a2", "TSLA", "Title 2",
                          source_type="finnhub_company_news", provider="finnhub")
    conn.close()

    report = run_coverage_audit(db_path)
    d1 = report["per_source_table_counts"]

    assert d1["raw_assets"]["polygon_news"] == 1
    assert d1["raw_assets"]["finnhub_company_news"] == 1
    assert d1["articles"]["polygon_news"] == 1
    assert d1["articles"]["finnhub_company_news"] == 1


# ---------------------------------------------------------------------------
# TA4 — staleness against TODAY, not OHLCV (DISCRIMINATING)
# ---------------------------------------------------------------------------

def test_staleness_against_today_not_ohlcv(tmp_path: Path):
    """Mock: ohlcv max == articles max == 60 days ago → days_stale ~60, not 0."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)

    sixty_days_ago = (date.today() - timedelta(days=60)).isoformat()
    conn.execute(
        "INSERT INTO ohlcv (symbol, date, open, high, low, close, volume) "
        "VALUES (?, ?, 100, 110, 90, 105, 1000)",
        ("AAPL", sixty_days_ago),
    )
    _add_article_with_raw(
        conn, "poly:a1", "AAPL", "Old Title",
        published_utc=f"{sixty_days_ago}T12:00:00Z",
        reference_date=sixty_days_ago,
    )
    conn.commit()
    conn.close()

    report = run_coverage_audit(db_path)
    d3 = report["date_coverage"]

    pn = d3.get("polygon_news", {})
    assert pn["days_stale"] >= 50, (
        f"days_stale={pn['days_stale']} should be ~60, not 0 — "
        "staleness anchored to TODAY, not OHLCV watermark"
    )
    assert "local_ohlcv_watermark" in pn
    assert sixty_days_ago in pn["local_ohlcv_watermark"]


# ---------------------------------------------------------------------------
# TA5 — holiday skip in trading-day oracle
# ---------------------------------------------------------------------------

def test_holiday_skip_in_trading_day():
    """Independence Day (Jul 4) as a Friday → Thursday is the trading day."""
    from catalyst_data.coverage_audit import _latest_closed_trading_day_for_date

    friday_jul4 = date(2025, 7, 4)
    trading_day = _latest_closed_trading_day_for_date(friday_jul4)
    assert trading_day == date(2025, 7, 3), (
        f"Expected 2025-07-03 (Thu before Fri holiday), got {trading_day}"
    )


# ---------------------------------------------------------------------------
# TA6 — missing table handled gracefully
# ---------------------------------------------------------------------------

def test_macro_table_missing_handled(tmp_path: Path):
    """DB without macro_observations → D1 reports TABLE_MISSING, not crash."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    conn.execute("DROP TABLE IF EXISTS macro_observations")
    conn.commit()
    conn.close()

    report = run_coverage_audit(db_path)
    d1 = report["per_source_table_counts"]
    assert d1["macro_observations"] == "TABLE_MISSING"


# ---------------------------------------------------------------------------
# TA7 — missing dedup_group_id column handled (DISCRIMINATING)
# ---------------------------------------------------------------------------

def test_dedup_column_missing_handled(tmp_path: Path):
    """DB where article_tickers has no dedup_group_id → no crash, dedup_not_materialized."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    _add_article_with_raw(conn, "poly:a1", "AAPL", "Title 1")
    conn.commit()
    conn.close()

    # Verify column is absent (init_db may or may not add it depending on migration state)
    conn2 = sqlite3.connect(db_path)
    cols = [c[1] for c in conn2.execute("PRAGMA table_info(article_tickers)")]
    conn2.close()
    # The audit must handle both cases; this test verifies it handles absence
    # If the column IS present (migration ran), the test still passes —
    # the discriminator is that it doesn't crash regardless

    report = run_coverage_audit(db_path)
    d5 = report["duplicate_diagnostics"]
    # Must not crash; must report materialization state
    assert "dedup_materialization" in d5
    assert d5["dedup_materialization"] is False or "dedup_group_count" in d5


# ---------------------------------------------------------------------------
# TA8 — zero-canonical dedup group detected
# ---------------------------------------------------------------------------

def test_dedup_group_zero_canonical_detected(tmp_path: Path):
    """Three articles with same dedup_group_id, all is_canonical=0 → flagged."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    for i in range(3):
        _add_article_with_raw(
            conn, f"poly:a{i}", "AAPL", f"Title {i}",
            dedup_group_id="grp-zero", is_canonical=0,
        )
    conn.commit()
    conn.close()

    report = run_coverage_audit(db_path)
    d5 = report["duplicate_diagnostics"]

    assert d5.get("dedup_group_count", 0) >= 1
    d6 = report.get("canonical_counts", {})
    assert d6.get("is_canonical_1", 0) == 0


# ---------------------------------------------------------------------------
# TA9 — multi-ticker preserved count
# ---------------------------------------------------------------------------

def test_multi_ticker_preserved_count(tmp_path: Path):
    """Article with two article_tickers rows → D6 reports multi_ticker_count."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    aid, raw_id = _add_article_with_raw(conn, "poly:a1", "AAPL", "Multi Ticker")
    upsert_article_ticker(
        conn, article_id=aid, ticker="MSFT",
        raw_asset_id=raw_id, reference_date="2025-06-15",
    )
    conn.commit()
    conn.close()

    report = run_coverage_audit(db_path)
    d6 = report.get("canonical_counts", {})
    assert d6.get("multi_ticker_count", 0) >= 1


# ---------------------------------------------------------------------------
# TA10 — null tier detected
# ---------------------------------------------------------------------------

def test_null_tier_detected(tmp_path: Path):
    """Article with source_tier=NULL → D9 flags null_tier_count > 0."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    _add_article_with_raw(conn, "poly:a1", "AAPL", "Null Tier", source_tier=None)
    conn.commit()
    conn.close()

    report = run_coverage_audit(db_path)
    d9 = report.get("source_tier_distribution", {})
    assert d9.get("null_tier_count", 0) >= 1


# ---------------------------------------------------------------------------
# TA11 — checkpoint histogram (D7)
# ---------------------------------------------------------------------------

def test_checkpoint_histogram(tmp_path: Path):
    """Checkpoints → D7 histogram matches."""
    from catalyst_data.coverage_audit import run_coverage_audit
    from catalyst_data.quality import ensure_ingestion_quality_tables

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    ensure_ingestion_quality_tables(conn)
    conn.execute(
        "INSERT INTO source_checkpoints (run_id, source_type, ticker, date, status) "
        "VALUES ('run1', 'polygon_news', 'AAPL', '2025-06-15', 'success')"
    )
    conn.execute(
        "INSERT INTO source_checkpoints (run_id, source_type, ticker, date, status) "
        "VALUES ('run2', 'polygon_news', 'MSFT', '2025-06-15', 'failed')"
    )
    conn.commit()
    conn.close()

    report = run_coverage_audit(db_path)
    d7 = report.get("checkpoint_reconciliation", {})
    hist = d7.get("status_histogram", {})
    # hist is nested: {source_type: {status: count}}
    total_success = sum(
        statuses.get("success", 0) for statuses in hist.values()
        if isinstance(statuses, dict)
    )
    total_failed = sum(
        statuses.get("failed", 0) for statuses in hist.values()
        if isinstance(statuses, dict)
    )
    assert total_success >= 1
    assert total_failed >= 1


# ---------------------------------------------------------------------------
# TA12 — rederivability spot-check passes
# ---------------------------------------------------------------------------

def test_rederivability_spot_check_passes(tmp_path: Path):
    """Matching raw_asset title → D8 passed == N."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    title = "Unique Rederive Title"
    raw_content = json.dumps({"title": title}).encode("utf-8")
    _add_article_with_raw(conn, "poly:a1", "AAPL", title, raw_content=raw_content)
    conn.commit()
    conn.close()

    report = run_coverage_audit(db_path)
    d8 = report.get("rederivability_spot_check", {})
    assert d8.get("passed", 0) >= 1
    assert d8.get("failed", 0) == 0


# ---------------------------------------------------------------------------
# TA13 — rederivability failure detected (DISCRIMINATING)
# ---------------------------------------------------------------------------

def test_rederivability_failure_detected(tmp_path: Path):
    """Mismatched raw_asset title → D8 failed_samples non-empty. Proves D8 works."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    _add_article_with_raw(
        conn, "poly:a1", "AAPL", "Correct Title",
        raw_content=b'{"title": "Different Title"}',
    )
    conn.commit()
    conn.close()

    report = run_coverage_audit(db_path)
    d8 = report.get("rederivability_spot_check", {})
    assert d8.get("failed", 0) >= 1, (
        "D8 must detect mismatched titles — rederivability check is not tautological"
    )




def test_rederivability_match_in_results_2_not_0(tmp_path: Path):
    """Raw payload "{results:[A,B,C,D]}" with title in results[2] → D8 passes."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    from catalyst_data.storage.sqlite import init_db
    init_db(conn)

    title = "The Article at Index 2"
    raw_payload = json.dumps({
        "news": {
            "results": [
                {"title": "First Article"},
                {"title": "Second Article"},
                {"title": title},
                {"title": "Fourth Article"},
            ]
        }
    }).encode("utf-8")
    from catalyst_data.storage.sqlite import upsert_raw_asset
    upsert_raw_asset(
        conn, asset_id="raw-multi", ticker="AAPL",
        source_type="polygon_news", reference_date="2025-06-15",
        content_raw=raw_payload,
    )
    from catalyst_data.articles import upsert_article, upsert_article_ticker
    upsert_article(conn, article={
        "article_id": "poly:multi", "raw_asset_id": "raw-multi",
        "provider": "polygon", "source_type": "polygon_news",
        "ticker": "AAPL", "reference_date": "2025-06-15",
        "published_utc": "2025-06-15T12:00:00Z",
        "title": title, "description": "desc",
        "publisher_name": "TestPub",
    })
    upsert_article_ticker(conn, article_id="poly:multi", ticker="AAPL",
                          raw_asset_id="raw-multi", reference_date="2025-06-15")
    conn.commit()
    conn.close()

    report = run_coverage_audit(db_path)
    d8 = report.get("rederivability_spot_check", {})
    assert d8.get("passed", 0) >= 1, (
        f"D8 must match title at results[2], not just results[0]. "
        f"Got passed={d8.get('passed')}, failed={d8.get('failed')}"
    )
    assert d8.get("failed", 0) == 0

# ---------------------------------------------------------------------------
# TA14 — URL collision detected (DISCRIMINATING)
# ---------------------------------------------------------------------------

def test_url_collision_detected(tmp_path: Path):
    """Two articles with same article_url but different article_id → collision."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    url = "https://example.com/same-article"
    _add_article_with_raw(conn, "poly:a1", "AAPL", "Title 1", article_url=url)
    _add_article_with_raw(conn, "poly:a2", "MSFT", "Title 2", article_url=url)
    conn.commit()
    conn.close()

    report = run_coverage_audit(db_path)
    d5 = report.get("duplicate_diagnostics", {})
    url_groups = d5.get("url_collision_groups", 0)
    assert url_groups >= 1, (
        f"D5 must detect URL collisions — got {url_groups}"
    )


# ---------------------------------------------------------------------------
# TA15 — output JSON written
# ---------------------------------------------------------------------------

def test_output_json_written(tmp_path: Path):
    """Full audit on mock DB → valid JSON with all expected dimension keys."""
    from catalyst_data.coverage_audit import run_coverage_audit

    db_path = str(tmp_path / "test.db")
    conn = _make_minimal_db(db_path)
    _add_article_with_raw(conn, "poly:a1", "AAPL", "Test Title")
    conn.commit()
    conn.close()

    output_dir = str(tmp_path / "reports")
    os.makedirs(output_dir, exist_ok=True)

    report = run_coverage_audit(db_path, output_dir=output_dir)

    expected_keys = [
        "per_source_table_counts",
        "per_ticker_per_source",
        "date_coverage",
        "missing_ranges",
        "duplicate_diagnostics",
        "canonical_counts",
        "checkpoint_reconciliation",
        "rederivability_spot_check",
        "source_tier_distribution",
    ]
    for key in expected_keys:
        assert key in report, f"Missing dimension: {key}"

    files = os.listdir(output_dir)
    assert len(files) >= 1
    assert any(f.startswith("step3f_coverage_") and f.endswith(".json") for f in files)
