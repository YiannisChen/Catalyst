"""Read-only coverage and integrity audit for the Catalyst dev DB.

Opens the dev DB with PRAGMA query_only=ON, runs nine audit dimensions,
writes a JSON report to data/provider_discovery/, and prints a summary.

Usage:
    from catalyst_data.coverage_audit import run_coverage_audit
    report = run_coverage_audit("data/catalyst_dev_ws4b.db")
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# US market holidays (observed dates) — dependency-free
# ---------------------------------------------------------------------------

_US_HOLIDAYS: frozenset[str] = frozenset({
    "2025-01-01",  # New Year's Day
    "2025-01-20",  # MLK Day
    "2025-02-17",  # Presidents' Day
    "2025-05-26",  # Memorial Day
    "2025-06-19",  # Juneteenth
    "2025-07-04",  # Independence Day
    "2025-09-01",  # Labor Day
    "2025-11-27",  # Thanksgiving
    "2025-12-25",  # Christmas
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents' Day
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed Fri Jul 3 for Sat Jul 4)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
})

_FROZEN_DB_RELPATH = str(
    (Path(__file__).resolve().parent.parent.parent.parent
     / "data" / "catalyst_eval_frozen_v2.db")
)

# ---------------------------------------------------------------------------
# Trading day oracle
# ---------------------------------------------------------------------------

def _latest_closed_trading_day_for_date(ref_date: date | None = None) -> date:
    """Return the most recent US market trading day on or before *ref_date*.

    Steps back over weekends and known US market holidays.
    """
    if ref_date is None:
        ref_date = date.today()

    candidate = ref_date
    # Step back while weekend or holiday
    while candidate.weekday() >= 5 or candidate.isoformat() in _US_HOLIDAYS:
        candidate = candidate - timedelta(days=1)
    return candidate


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def run_coverage_audit(
    db_path: str,
    output_dir: str | None = None,
) -> dict:
    """Run the full read-only coverage/integrity audit.

    Args:
        db_path: Path to the dev DB.
        output_dir: If provided, write a timestamped JSON report there.

    Returns:
        dict with all nine audit dimensions.

    Raises:
        RuntimeError: If *db_path* resolves to the frozen DB realpath.
    """
    # Frozen DB guard (realpath comparison — F7)
    resolved = os.path.realpath(db_path)
    if resolved == _FROZEN_DB_RELPATH:
        raise RuntimeError(
            "Refusing to open frozen eval DB for audit. "
            f"Use the dev DB instead: data/catalyst_dev_ws4b.db"
        )

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA query_only = ON")

    report: dict = {
        "audit_generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": resolved,
        "latest_closed_trading_day": _latest_closed_trading_day_for_date().isoformat(),
    }

    # Run each dimension independently — one failure doesn't block others
    for dim_name, dim_fn in [
        ("per_source_table_counts", _d1_per_source_table_counts),
        ("per_ticker_per_source", _d2_per_ticker_per_source),
        ("date_coverage", _d3_date_coverage),
        ("missing_ranges", _d4_missing_ranges),
        ("duplicate_diagnostics", _d5_duplicate_diagnostics),
        ("canonical_counts", _d6_canonical_counts),
        ("checkpoint_reconciliation", _d7_checkpoint_reconciliation),
        ("rederivability_spot_check", _d8_rederivability_spot_check),
        ("source_tier_distribution", _d9_source_tier_distribution),
    ]:
        try:
            report[dim_name] = dim_fn(conn, report)
        except Exception as exc:
            report[dim_name] = {"error": str(exc), "error_type": type(exc).__name__}
            logger.warning("Dimension %s failed: %s", dim_name, exc)

    conn.close()

    # Write JSON report if output_dir provided
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"step3f_coverage_{ts}.json"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info("Audit report written to %s", filepath)

    # Print summary
    _print_summary(report)

    return report


# ---------------------------------------------------------------------------
# D1 — Per-source per-table counts
# ---------------------------------------------------------------------------

_TABLES_TO_AUDIT = [
    "raw_assets", "clean_assets", "articles", "article_tickers",
    "filings", "filing_documents", "macro_observations",
    "index_state", "index_manifests", "source_checkpoints", "ohlcv",
]

_TABLE_SOURCE_COL = {
    "raw_assets": "source_type",
    "clean_assets": "source_type",
    "articles": "source_type",
    "article_tickers": None,  # no source_type — count total only
    "filings": "source_type",
    "filing_documents": None,
    "macro_observations": None,
    "index_state": "source_kind",
    "index_manifests": None,
    "source_checkpoints": "source_type",
    "ohlcv": None,
}


def _d1_per_source_table_counts(conn: sqlite3.Connection, report: dict) -> dict:
    result: dict = {}
    for table in _TABLES_TO_AUDIT:
        try:
            col = _TABLE_SOURCE_COL.get(table)
            if col:
                rows = conn.execute(
                    f"SELECT {col}, COUNT(*) FROM {table} GROUP BY {col} ORDER BY COUNT(*) DESC"
                ).fetchall()
                result[table] = {r[0] or "(null)": r[1] for r in rows}
            else:
                cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                result[table] = {"_total": cnt}
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                result[table] = "TABLE_MISSING"
            else:
                result[table] = f"ERROR: {e}"
    return result


# ---------------------------------------------------------------------------
# D2 — Per-ticker per-source article counts
# ---------------------------------------------------------------------------

def _d2_per_ticker_per_source(conn: sqlite3.Connection, report: dict) -> dict:
    try:
        rows = conn.execute("""
            SELECT a.ticker, a.source_type, COUNT(*),
                   MIN(a.published_utc), MAX(a.published_utc)
            FROM articles a
            GROUP BY a.ticker, a.source_type
            ORDER BY a.ticker, a.source_type
        """).fetchall()
    except sqlite3.OperationalError:
        return {"error": "articles table not queryable"}

    result: list[dict] = []
    for ticker, st, cnt, min_pub, max_pub in rows:
        result.append({
            "ticker": ticker,
            "source_type": st,
            "count": cnt,
            "min_published_utc": min_pub,
            "max_published_utc": max_pub,
        })
    return {"groups": result}


# ---------------------------------------------------------------------------
# D3 — Date coverage (today-anchored staleness oracle — F1)
# ---------------------------------------------------------------------------

def _d3_date_coverage(conn: sqlite3.Connection, report: dict) -> dict:
    trading_day = _latest_closed_trading_day_for_date()

    result: dict = {}

    # Per source_type from article_tickers joined with articles (for source_type filter)
    sources = [
        ("polygon_news", "article_tickers", "reference_date"),
        ("finnhub_company_news", "article_tickers", "reference_date"),
    ]

    for source_type, table, date_col in sources:
        try:
            row = conn.execute(
                f"""SELECT MIN(at.{date_col}), MAX(at.{date_col})
                    FROM {table} at
                    JOIN articles a ON a.article_id = at.article_id
                    WHERE a.source_type = ? AND at.{date_col} IS NOT NULL""",
                (source_type,)
            ).fetchone()
            if row and (row[0] or row[1]):
                min_d = row[0][:10] if row[0] else None
                max_d = row[1][:10] if row[1] else None
                max_date = date.fromisoformat(max_d) if max_d else None
                days_stale = (
                    (trading_day - max_date).days
                    if max_date else None
                )
            else:
                min_d = max_d = None
                days_stale = None
        except sqlite3.OperationalError:
            result[source_type] = {"error": f"{table} not queryable"}
            continue

        result[source_type] = {
            "min_date": min_d,
            "max_date": max_d,
            "latest_closed_trading_day": trading_day.isoformat(),
            "days_stale": days_stale,
        }

    # SEC filings — separate table
    try:
        frow = conn.execute(
            "SELECT MIN(filed_at), MAX(filed_at) FROM filings WHERE filed_at IS NOT NULL"
        ).fetchone()
        if frow and frow[0]:
            min_f = frow[0][:10] if frow[0] else None
            max_f = frow[1][:10] if frow[1] else None
            max_fdate = date.fromisoformat(max_f) if max_f else None
            days_stale_f = (trading_day - max_fdate).days if max_fdate else None
        else:
            min_f = max_f = None
            days_stale_f = None
    except sqlite3.OperationalError:
        result["sec_filings"] = {"error": "filings table not queryable"}
        min_f = max_f = None
        days_stale_f = None

    if min_f is not None or max_f is not None:
        result["sec_filings"] = {
            "min_date": min_f,
            "max_date": max_f,
            "latest_closed_trading_day": trading_day.isoformat(),
            "days_stale": days_stale_f,
        }

    # Add local OHLCV watermark as a separate field (informational, not staleness ref)
    try:
        ohlcv_max = conn.execute("SELECT MAX(date) FROM ohlcv").fetchone()[0]
    except sqlite3.OperationalError:
        ohlcv_max = None

    for key in result:
        result[key]["local_ohlcv_watermark"] = ohlcv_max

    return result


# ---------------------------------------------------------------------------
# D4 — Missing ranges (trading-day gaps)
# ---------------------------------------------------------------------------

def _d4_missing_ranges(conn: sqlite3.Connection, report: dict) -> dict:
    trading_day = _latest_closed_trading_day_for_date()
    gaps: list[dict] = []

    try:
        rows = conn.execute("""
            SELECT ticker, source_type, reference_date
            FROM article_tickers
            WHERE reference_date IS NOT NULL
            ORDER BY ticker, source_type, reference_date
        """).fetchall()
    except sqlite3.OperationalError:
        return {"gaps": [], "error": "article_tickers not queryable"}

    if not rows:
        return {"gaps": [], "note": "no article_tickers rows"}

    # Group by (ticker, source_type) and detect gaps
    from itertools import groupby
    for (ticker, source_type), group in groupby(rows, key=lambda r: (r[0], r[1])):
        dates = sorted(set(
            date.fromisoformat(r[2][:10])
            for r in group if r[2]
        ))
        if len(dates) < 2:
            # Trailing gap only
            trailing = (trading_day - dates[0]).days if dates else None
            if trailing and trailing > 3:
                gaps.append({
                    "ticker": ticker,
                    "source_type": source_type,
                    "gap_start": dates[0].isoformat(),
                    "gap_end": trading_day.isoformat(),
                    "gap_type": "trailing",
                })
            continue

        for i in range(1, len(dates)):
            prev = dates[i - 1]
            curr = dates[i]
            gap_days = (curr - prev).days
            # More than 3 calendar days between consecutive dates = internal gap
            if gap_days > 4:
                gaps.append({
                    "ticker": ticker,
                    "source_type": source_type,
                    "gap_start": prev.isoformat(),
                    "gap_end": curr.isoformat(),
                    "gap_type": "internal",
                })

        # Trailing gap
        last_date = dates[-1]
        trailing = (trading_day - last_date).days
        if trailing > 3:
            gaps.append({
                "ticker": ticker,
                "source_type": source_type,
                "gap_start": last_date.isoformat(),
                "gap_end": trading_day.isoformat(),
                "gap_type": "trailing",
            })

    return {"gaps": gaps, "total_gaps": len(gaps)}


# ---------------------------------------------------------------------------
# D5 — Duplicate diagnostics (AMENDED F2, F6)
# ---------------------------------------------------------------------------

def _d5_duplicate_diagnostics(conn: sqlite3.Connection, report: dict) -> dict:
    result: dict = {}

    # Check if article_tickers has dedup_group_id column (F2)
    ticker_cols = [c[1] for c in conn.execute("PRAGMA table_info(article_tickers)").fetchall()]
    article_cols = [c[1] for c in conn.execute("PRAGMA table_info(articles)").fetchall()]
    has_at_dedup = "dedup_group_id" in ticker_cols
    has_art_dedup = "dedup_group_id" in article_cols

    result["dedup_materialization"] = has_at_dedup
    result["dedup_source"] = "article_tickers" if has_at_dedup else (
        "articles" if has_art_dedup else "none"
    )

    if has_at_dedup:
        # Count groups with >=2 members
        groups = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT dedup_group_id FROM article_tickers
                WHERE dedup_group_id IS NOT NULL
                GROUP BY dedup_group_id HAVING COUNT(*) >= 2
            )
        """).fetchone()[0]
        result["dedup_group_count"] = groups
    elif has_art_dedup:
        groups = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT dedup_group_id FROM articles
                WHERE dedup_group_id IS NOT NULL
                GROUP BY dedup_group_id HAVING COUNT(*) >= 2
            )
        """).fetchone()[0]
        result["dedup_group_count"] = groups
    else:
        result["dedup_group_count"] = "dedup_not_materialized"

    # Cross-check: if article_tickers has column but found 0 groups,
    # fall back to articles.dedup_group_id (may have data when AT doesn't)
    if has_at_dedup and has_art_dedup and isinstance(result.get("dedup_group_count"), int) and result["dedup_group_count"] == 0:
        art_groups = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT dedup_group_id FROM articles
                WHERE dedup_group_id IS NOT NULL
                GROUP BY dedup_group_id HAVING COUNT(*) >= 2
            )
        """).fetchone()[0]
        if art_groups:
            result["dedup_group_count"] = art_groups
            result["dedup_source"] = "articles (fallback; article_tickers had 0)"

    # count of articles with is_canonical=0
    try:
        nc = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE is_canonical = 0"
        ).fetchone()[0]
        result["non_canonical_count"] = nc
    except sqlite3.OperationalError:
        result["non_canonical_count"] = "N/A"

    # Provider-native-ID collisions: group by (provider, native_id from article_id)
    # Extract native_id by splitting on first ':'
    try:
        collisions = conn.execute("""
            SELECT provider,
                   SUBSTR(article_id, INSTR(article_id, ':') + 1) AS native_id,
                   COUNT(*) AS cnt
            FROM articles
            WHERE article_id LIKE '%:%'
            GROUP BY provider, native_id
            HAVING cnt > 1
            LIMIT 20
        """).fetchall()
        result["native_id_collisions"] = len(collisions)
        if collisions:
            result["native_id_collision_examples"] = [
                {"provider": r[0], "native_id": r[1], "count": r[2]}
                for r in collisions[:5]
            ]
    except sqlite3.OperationalError:
        result["native_id_collisions"] = "N/A"

    # URL collisions: same article_url, different article_id
    try:
        url_cols = conn.execute("""
            SELECT article_url, COUNT(*) AS cnt
            FROM articles
            WHERE article_url IS NOT NULL AND article_url != ''
            GROUP BY article_url HAVING cnt > 1
            LIMIT 20
        """).fetchall()
        result["url_collision_groups"] = len(url_cols)
        if url_cols:
            result["url_collision_examples"] = [
                {"url": r[0][:120], "count": r[1]} for r in url_cols[:5]
            ]
    except sqlite3.OperationalError:
        result["url_collision_groups"] = "N/A"

    return result


# ---------------------------------------------------------------------------
# D6 — Canonical counts
# ---------------------------------------------------------------------------

def _d6_canonical_counts(conn: sqlite3.Connection, report: dict) -> dict:
    result: dict = {}

    # is_canonical=1 per source_type + provider
    try:
        rows = conn.execute("""
            SELECT source_type, provider, COUNT(*)
            FROM articles WHERE is_canonical = 1
            GROUP BY source_type, provider
        """).fetchall()
        result["is_canonical_1_by_source"] = [
            {"source_type": r[0], "provider": r[1], "count": r[2]} for r in rows
        ]
        result["is_canonical_1"] = sum(r[2] for r in rows)
    except sqlite3.OperationalError:
        result["is_canonical_1"] = "N/A"

    # Multi-ticker articles: article_id with >1 article_tickers rows
    try:
        multi = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT article_id FROM article_tickers
                GROUP BY article_id HAVING COUNT(*) > 1
            )
        """).fetchone()[0]
        result["multi_ticker_count"] = multi
    except sqlite3.OperationalError:
        result["multi_ticker_count"] = "N/A"

    # Total articles
    try:
        result["total_articles"] = conn.execute(
            "SELECT COUNT(*) FROM articles"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        result["total_articles"] = "N/A"

    return result


# ---------------------------------------------------------------------------
# D7 — Checkpoint reconciliation
# ---------------------------------------------------------------------------

def _d7_checkpoint_reconciliation(conn: sqlite3.Connection, report: dict) -> dict:
    result: dict = {}

    # Status histogram
    try:
        sc_cols = [c[1] for c in conn.execute("PRAGMA table_info(source_checkpoints)").fetchall()]
        # source_checkpoints schema: (run_id, source_type, ticker, date, status, error_class, retries)
        hist_rows = conn.execute("""
            SELECT source_type, status, COUNT(*)
            FROM source_checkpoints
            GROUP BY source_type, status
            ORDER BY source_type, status
        """).fetchall()
    except sqlite3.OperationalError:
        return {"error": "source_checkpoints not queryable"}

    hist: dict[str, dict[str, int]] = {}
    for source_type, status, cnt in hist_rows:
        hist.setdefault(source_type, {})[status] = cnt
    result["status_histogram"] = hist

    # Compare checkpoint max date vs actual max data date per (ticker, source_type)
    try:
        gaps_list = conn.execute("""
            WITH cp_max AS (
                SELECT ticker, source_type, MAX(date) AS max_cp_date
                FROM source_checkpoints WHERE status = 'success'
                GROUP BY ticker, source_type
            ),
            data_max AS (
                SELECT a.ticker, a.source_type,
                       MAX(at.reference_date) AS max_data_date
                FROM article_tickers at
                JOIN articles a ON a.article_id = at.article_id
                WHERE at.reference_date IS NOT NULL
                GROUP BY a.ticker, a.source_type
            )
            SELECT cp.ticker, cp.source_type,
                   cp.max_cp_date, dm.max_data_date
            FROM cp_max cp
            LEFT JOIN data_max dm
              ON dm.ticker = cp.ticker AND dm.source_type = cp.source_type
        """).fetchall()
        result["checkpoint_vs_data"] = [
            {"ticker": r[0], "source_type": r[1],
             "checkpoint_max_date": r[2], "data_max_date": r[3]}
            for r in gaps_list[:50]
        ]
    except sqlite3.OperationalError:
        result["checkpoint_vs_data"] = "N/A"

    return result


# ---------------------------------------------------------------------------
# D8 — Rederivability spot-check
# ---------------------------------------------------------------------------

def _d8_rederivability_spot_check(conn: sqlite3.Connection, report: dict,
                                   sample_size: int = 20) -> dict:
    try:
        rows = conn.execute("""
            SELECT article_id, raw_asset_id, title
            FROM articles
            ORDER BY RANDOM() LIMIT ?
        """, (sample_size,)).fetchall()
    except sqlite3.OperationalError:
        return {"error": "articles not queryable", "spot_checked": 0, "passed": 0}

    if not rows:
        return {"spot_checked": 0, "passed": 0, "failed": 0,
                "note": "no articles to spot-check"}

    passed = 0
    failed_samples: list[dict] = []

    for article_id, raw_asset_id, title in rows:
        try:
            raw_row = conn.execute(
                "SELECT content_raw FROM raw_assets WHERE asset_id = ?",
                (raw_asset_id,)
            ).fetchone()
            if not raw_row:
                failed_samples.append({
                    "article_id": article_id,
                    "raw_asset_id": raw_asset_id,
                    "reason": "raw_asset not found",
                })
                continue

            compressed = raw_row[0]
            decompressed = zlib.decompress(compressed)

            # Try JSON parse and title match — search ALL entries, not just [0]
            try:
                payload = json.loads(decompressed)
                matched = False

                # Collect candidate titles from all possible shapes
                candidates: list[str] = []

                # Shape 1: {"news": {"results": [{"title": ...}, ...]}}  (Polygon)
                if isinstance(payload.get("news"), dict):
                    results = payload["news"].get("results", [])
                    if isinstance(results, list):
                        for r in results:
                            if isinstance(r, dict):
                                t = r.get("title") or r.get("headline") or ""
                                if t:
                                    candidates.append(t)

                # Shape 2: {"results": [{"title": ...}, ...]}  (top-level results)
                if isinstance(payload.get("results"), list) and not candidates:
                    for r in payload["results"]:
                        if isinstance(r, dict):
                            t = r.get("title") or r.get("headline") or ""
                            if t:
                                candidates.append(t)

                # Shape 3: bare list [{...}, ...]  (Finnhub)
                if isinstance(payload, list) and not candidates:
                    for r in payload:
                        if isinstance(r, dict):
                            t = r.get("title") or r.get("headline") or ""
                            if t:
                                candidates.append(t)

                # Shape 4: single-object {"title": ...} or {"headline": ...}
                if not candidates:
                    t = payload.get("title") or payload.get("headline") or ""
                    if t:
                        candidates.append(t)

                # Exact title match across all candidates
                if title:
                    for c in candidates:
                        if title in c or c in title:
                            matched = True
                            break

                # Fallback: substring-search the decoded UTF-8 text (handles ~90%)
                if not matched:
                    try:
                        text = decompressed.decode("utf-8", errors="replace")
                        # Also try a simple unescape for &amp; &quot; &#39; etc.
                        import html as _html
                        text_unescaped = _html.unescape(text)
                        if title and (title in text or title in text_unescaped):
                            matched = True
                    except Exception:
                        pass

                if matched:
                    passed += 1
                else:
                    failed_samples.append({
                        "article_id": article_id,
                        "raw_asset_id": raw_asset_id,
                        "title_in_articles": title,
                        "candidate_count": len(candidates),
                        "reason": "title mismatch after searching all candidates + text fallback",
                    })
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Non-JSON raw — check as substring in decoded text
                try:
                    text = decompressed.decode("utf-8", errors="replace")
                    if title and title in text:
                        passed += 1
                    else:
                        failed_samples.append({
                            "article_id": article_id,
                            "raw_asset_id": raw_asset_id,
                            "reason": "title not found in raw text",
                        })
                except Exception:
                    failed_samples.append({
                        "article_id": article_id,
                        "raw_asset_id": raw_asset_id,
                        "reason": "raw payload undecodable",
                    })
        except Exception as exc:
            failed_samples.append({
                "article_id": article_id,
                "raw_asset_id": raw_asset_id,
                "reason": f"decompress/parse error: {exc}",
            })

    return {
        "spot_checked": len(rows),
        "passed": passed,
        "failed": len(failed_samples),
        "failed_samples": failed_samples[:10],
    }


# ---------------------------------------------------------------------------
# D9 — Source-tier distribution
# ---------------------------------------------------------------------------

def _d9_source_tier_distribution(conn: sqlite3.Connection, report: dict) -> dict:
    result: dict = {}

    # Articles tier distribution
    try:
        rows = conn.execute("""
            SELECT source_tier, COUNT(*)
            FROM articles
            GROUP BY source_tier
            ORDER BY source_tier
        """).fetchall()
        result["articles_tier_distribution"] = {
            (r[0] if r[0] is not None else "null"): r[1] for r in rows
        }
        null_count = sum(
            v for k, v in result["articles_tier_distribution"].items() if k == "null"
        )
        result["null_tier_count"] = null_count
    except sqlite3.OperationalError:
        result["articles_tier_distribution"] = "N/A"

    # Filings tier distribution
    try:
        f_rows = conn.execute("""
            SELECT source_tier, COUNT(*)
            FROM filings
            GROUP BY source_tier
        """).fetchall()
        result["filings_tier_distribution"] = {
            (r[0] if r[0] is not None else "null"): r[1] for r in f_rows
        }
    except sqlite3.OperationalError:
        result["filings_tier_distribution"] = "N/A"

    # Flag tiers outside {1..6}
    if isinstance(result.get("articles_tier_distribution"), dict):
        bad = []
        for k in result["articles_tier_distribution"]:
            if k == "null":
                continue
            try:
                if int(k) not in range(1, 7):
                    bad.append(k)
            except (ValueError, TypeError):
                bad.append(k)
        result["unexpected_tiers"] = bad

    return result


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _print_summary(report: dict) -> None:
    """Print a human-readable summary of the audit report to stdout."""
    print()
    print("=" * 60)
    print("  Catalyst WS4B — Coverage & Integrity Audit")
    print(f"  Generated: {report.get('audit_generated_at', '?')}")
    print(f"  DB path:   {report.get('db_path', '?')}")
    print(f"  Trading day: {report.get('latest_closed_trading_day', '?')}")
    print("=" * 60)

    # D1 — table counts
    d1 = report.get("per_source_table_counts", {})
    print("\n--- Per-Source Table Counts ---")
    for table, counts in sorted(d1.items()):
        if counts == "TABLE_MISSING":
            print(f"  {table}: TABLE_MISSING")
        elif isinstance(counts, dict):
            for src, cnt in sorted(counts.items()):
                print(f"  {table}.{src}: {cnt}")
        else:
            print(f"  {table}: {counts}")

    # D3 — date coverage
    d3 = report.get("date_coverage", {})
    print("\n--- Date Coverage (staleness vs today's trading day) ---")
    for source, info in sorted(d3.items()):
        if isinstance(info, dict):
            ds = info.get("days_stale", "?")
            flag = " *** STALE ***" if (isinstance(ds, int) and ds > 7) else ""
            print(f"  {source}: {info.get('min_date','?')} → {info.get('max_date','?')}  "
                  f"stale={ds}d{flag}")
            print(f"           local_ohlcv_watermark={info.get('local_ohlcv_watermark','?')}")
        else:
            print(f"  {source}: {info}")

    # D4 — missing ranges
    d4 = report.get("missing_ranges", {})
    gaps = d4.get("gaps", [])
    if gaps:
        print(f"\n--- Missing Ranges ({len(gaps)} gaps) ---")
        for g in gaps[:10]:
            print(f"  {g['ticker']} {g['source_type']}: {g['gap_start']} → {g['gap_end']} ({g['gap_type']})")
        if len(gaps) > 10:
            print(f"  ... and {len(gaps) - 10} more gaps")

    # D5 — duplicate diagnostics
    d5 = report.get("duplicate_diagnostics", {})
    print(f"\n--- Dedup Diagnostics ---")
    print(f"  dedup_materialization: {d5.get('dedup_materialization', '?')}")
    print(f"  dedup_source:          {d5.get('dedup_source', '?')}")
    print(f"  dedup_group_count:     {d5.get('dedup_group_count', '?')}")
    print(f"  non_canonical_count:   {d5.get('non_canonical_count', '?')}")
    print(f"  native_id_collisions:  {d5.get('native_id_collisions', '?')}")
    print(f"  url_collision_groups:  {d5.get('url_collision_groups', '?')}")

    # D6 — canonical counts
    d6 = report.get("canonical_counts", {})
    print(f"\n--- Canonical Counts ---")
    print(f"  is_canonical=1:        {d6.get('is_canonical_1', '?')}")
    print(f"  multi_ticker_count:    {d6.get('multi_ticker_count', '?')}")
    print(f"  total_articles:        {d6.get('total_articles', '?')}")

    # D7 — checkpoint histogram
    d7 = report.get("checkpoint_reconciliation", {})
    hist = d7.get("status_histogram", {})
    if hist:
        print(f"\n--- Checkpoint Status ---")
        for source, statuses in sorted(hist.items()):
            parts = ", ".join(f"{s}={c}" for s, c in sorted(statuses.items()))
            print(f"  {source}: {parts}")

    # D8 — rederivability
    d8 = report.get("rederivability_spot_check", {})
    print(f"\n--- Rederivability Spot-Check ---")
    print(f"  Checked: {d8.get('spot_checked', '?')}, "
          f"Passed: {d8.get('passed', '?')}, "
          f"Failed: {d8.get('failed', '?')}")

    # D9 — tier distribution
    d9 = report.get("source_tier_distribution", {})
    print(f"\n--- Source-Tier Distribution ---")
    art_dist = d9.get("articles_tier_distribution", {})
    if isinstance(art_dist, dict):
        for tier, cnt in sorted(art_dist.items(), key=lambda x: str(x[0])):
            print(f"  Tier {tier}: {cnt}")
    print(f"  null_tier_count: {d9.get('null_tier_count', '?')}")
    unexpected = d9.get("unexpected_tiers", [])
    if unexpected:
        print(f"  UNEXPECTED TIERS: {unexpected}")

    print()
    print("=" * 60)
