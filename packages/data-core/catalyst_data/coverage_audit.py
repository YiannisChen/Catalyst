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
from datetime import date, datetime, timezone
from pathlib import Path

from catalyst_data.trading_calendar import latest_closed_trading_day_for_date

logger = logging.getLogger(__name__)

_FROZEN_DB_RELPATH = str(
    (Path(__file__).resolve().parent.parent.parent.parent
     / "data" / "catalyst_eval_frozen_v2.db")
)

# ---------------------------------------------------------------------------
# Trading day oracle
# ---------------------------------------------------------------------------

def _latest_closed_trading_day_for_date(ref_date: date | None = None) -> date:
    """Compatibility wrapper for existing audit tests/imports."""
    return latest_closed_trading_day_for_date(ref_date)


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



def _resolve_b2_lineage(
    db_path: str,
    terminal_run_id: str,
    *,
    plan_hash: str | None = None,
    expected_plan_hash: str | None = None,
) -> list[str]:
    """Resolve B2 run lineage from terminal_run_id back to root.

    Returns list of run_ids ordered [terminal, ..., root].
    Raises ValueError with specific messages for: missing run, missing parent,
    cycle, plan_hash drift, expected_plan_hash drift.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA query_only = ON")
    try:
        row = conn.execute(
            "SELECT plan_hash, expected_plan_hash, parent_run_id FROM ingestion_runs WHERE run_id = ?",
            (terminal_run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"terminal run not found: {terminal_run_id}")
        stored_plan, stored_expected, stored_parent = row[0], row[1], row[2]
        if plan_hash is not None and stored_plan != plan_hash:
            raise ValueError("terminal run plan_hash drift")
        if expected_plan_hash is not None and stored_expected != expected_plan_hash:
            raise ValueError("terminal run expected_plan_hash drift")

        lineage: list[str] = [terminal_run_id]
        current = stored_parent
        seen: set[str] = {terminal_run_id}
        while current:
            if current in seen:
                raise ValueError("cycle in terminal run lineage")
            seen.add(current)
            lineage.append(current)
            prow = conn.execute(
                "SELECT plan_hash, expected_plan_hash, parent_run_id FROM ingestion_runs WHERE run_id = ?",
                (current,),
            ).fetchone()
            if prow is None:
                raise ValueError(f"parent run not found: {current}")
            if plan_hash is not None and prow[0] != plan_hash:
                raise ValueError(
                    f"ancestor {current} plan_hash drift: expected {plan_hash}, got {prow[0]}"
                )
            if expected_plan_hash is not None and prow[1] != expected_plan_hash:
                raise ValueError(
                    f"ancestor {current} expected_plan_hash drift: expected {expected_plan_hash}, got {prow[1]}"
                )
            current = prow[2]
        return lineage
    finally:
        conn.close()


def run_b2o_readiness_audit(
    db_path: str,
    *,
    universe_manifest,
    plan=None,
    output_dir: str | None = None,
    terminal_run_id: str,
) -> dict:
    """Run existing coverage audit once, then add B2-O readiness fields."""
    if not terminal_run_id or not terminal_run_id.strip():
        raise ValueError("terminal_run_id must not be empty")
    report = run_coverage_audit(db_path, output_dir=None)
    manifest_dict = (
        universe_manifest.to_dict()
        if hasattr(universe_manifest, "to_dict")
        else dict(universe_manifest)
    )
    source_scopes = []
    if plan is not None:
        config = getattr(plan, "config", None) or {}
        raw_scopes = config.get("source_scopes") or []
        source_scopes = list(raw_scopes.values()) if isinstance(raw_scopes, dict) else list(raw_scopes)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA query_only = ON")
    try:
        # Resolve terminal run lineage (required for B2-O path)
        plan_hash_val = getattr(plan, "plan_hash", None) or None
        expected_val = getattr(plan, "expected_plan_hash", None) or plan_hash_val
        lineage_run_ids = _resolve_b2_lineage(
            db_path,
            terminal_run_id,
            plan_hash=plan_hash_val or None,
            expected_plan_hash=expected_val or None,
        )
        completed: list[dict] = []
        uncovered: list[dict] = []
        partial_or_error: list[dict] = []
        exact_counts: dict[str, int] = {}
        canonical_news_status: dict[str, dict[str, list[bool]]] = {}
        mandatory_missing_ids: list[str] = []
        optional_incomplete_ids: list[str] = []
        invalid_provenance_ids: list[str] = []
        for scope in source_scopes:
            source_type = scope["source_type"]
            exact_counts[source_type] = 0
            subjects = scope.get("subjects", [])
            cells = _b2o_scope_cells(scope)
            for subject in subjects:
                for cell in [c for c in cells if c["subject"] == subject]:
                    window_start, window_end = cell["window_start"], cell["window_end"]
                    exact_counts[source_type] += 1
                    placeholders = ",".join("?" for _ in lineage_run_ids)
                    row = conn.execute(
                        f"""SELECT status, COALESCE(is_complete, 0), raw_asset_id,
                                  COALESCE(items_count, 0), COALESCE(request_count, 0),
                                  cell_id, endpoint_name, window_start, window_end,
                                  logical_fetch_id
                           FROM source_checkpoints
                           WHERE cell_id = ? AND run_id IN ({placeholders})
                           ORDER BY rowid DESC LIMIT 1""",
                        [cell["cell_id"]] + lineage_run_ids,
                    ).fetchone()
                    if not row:
                        item = {**cell, "status": "missing"}
                        uncovered.append(item)
                        if source_type != "fmp_fundamentals":
                            mandatory_missing_ids.append(cell["cell_id"])
                        else:
                            optional_incomplete_ids.append(cell["cell_id"])
                        continue
                    status, is_complete, raw_asset_id, items_count, request_count, stored_cell_id, endpoint_name, stored_start, stored_end, cp_logical_fetch_id = row
                    identity_matches = (
                        stored_cell_id == cell["cell_id"]
                        and endpoint_name == cell["endpoint_name"]
                        and stored_start == window_start
                        and stored_end == window_end
                    )
                    item = {**cell, "status": status, "is_complete": bool(is_complete), "identity_matches": identity_matches}
                    checkpoint_terminal = (
                        status in {"success", "success_empty"}
                        and bool(is_complete)
                        and identity_matches
                    )
                    # Use the logical_fetch_id from the same checkpoint row (already fetched above)
                    prov_lfid = cp_logical_fetch_id
                    provenance_valid = (
                        checkpoint_terminal
                        and prov_lfid is not None
                        and _b2o_cell_provenance_is_valid(
                            conn,
                            logical_fetch_id=prov_lfid,
                            request_count=request_count,
                            items_count=items_count,
                        )
                    )
                    item["provenance_valid"] = provenance_valid
                    terminal = checkpoint_terminal and provenance_valid
                    if terminal:
                        completed.append(item)
                    else:
                        partial_or_error.append(item)
                        if checkpoint_terminal and not provenance_valid:
                            invalid_provenance_ids.append(cell["cell_id"])
                        if source_type != "fmp_fundamentals":
                            mandatory_missing_ids.append(cell["cell_id"])
                        else:
                            optional_incomplete_ids.append(cell["cell_id"])
                    if source_type in {"polygon_news", "finnhub_company_news"} and window_end >= "2025-08-01":
                        canonical_news_status.setdefault(subject, {}).setdefault(source_type, []).append(terminal)
        missing_subjects = [
            ticker
            for ticker in manifest_dict.get("tickers", [])
            if not (
                canonical_news_status.get(ticker, {}).get("polygon_news")
                and all(canonical_news_status.get(ticker, {}).get("polygon_news", []))
                and canonical_news_status.get(ticker, {}).get("finnhub_company_news")
                and all(canonical_news_status.get(ticker, {}).get("finnhub_company_news", []))
            )
        ]
        gate_status = "complete" if not missing_subjects and manifest_dict.get("tickers") else "incomplete"
        overall_status = "complete" if not mandatory_missing_ids and source_scopes else "incomplete"
        report["b2o_readiness"] = {
            "planned_windows": source_scopes,
            "completed_windows": completed,
            "uncovered_ranges": uncovered,
            "partial_or_error_status": partial_or_error,
            "required_provenance": {
                "status": "complete" if not invalid_provenance_ids else "incomplete",
                "missing_or_invalid_cell_ids": invalid_provenance_ids,
            },
            "overall_readiness": {
                "status": overall_status,
                "missing_or_incomplete_cell_ids": mandatory_missing_ids,
            },
            "canonical_news_comparable_gate": {
                "status": gate_status,
                "missing_tickers": missing_subjects,
                "missing_or_incomplete_cell_ids": mandatory_missing_ids,
            },
            "optional_source_status": {
                "fmp_fundamentals": {
                    "status": "complete" if not optional_incomplete_ids else "degraded",
                    "missing_or_incomplete_cell_ids": optional_incomplete_ids,
                }
            },
            "comparable_gate": {"status": gate_status, "missing_tickers": missing_subjects},
            "exact_cell_counts": exact_counts,
        }
    finally:
        conn.close()
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "b2o_readiness_audit.json")
        with open(path, "w") as fh:
            json.dump(report, fh, indent=2, default=str)
        report["b2o_readiness"]["report_path"] = path
    return report


def _b2o_cell_provenance_is_valid(
    conn: sqlite3.Connection,
    *,
    logical_fetch_id: str | None,
    request_count: int,
    items_count: int,
) -> bool:
    if not logical_fetch_id or request_count < 1:
        return False
    rows = conn.execute(
        """SELECT a.status, a.raw_asset_id, a.response_sha256,
                  r.asset_id, r.response_sha256
           FROM provider_request_attempts AS a
           LEFT JOIN raw_assets AS r ON r.asset_id = a.raw_asset_id
           WHERE a.logical_fetch_id = ?
           ORDER BY a.page_no, a.attempt_no""",
        (logical_fetch_id,),
    ).fetchall()
    if len(rows) != request_count:
        return False
    succeeded = [row for row in rows if row[0] == "SUCCEEDED"]
    if not succeeded:
        return False
    if any(
        not row[1]
        or not row[2]
        or row[3] != row[1]
        or row[4] != row[2]
        for row in succeeded
    ):
        return False
    if items_count <= 0:
        return True
    provenance_count = conn.execute(
        """SELECT COUNT(*)
           FROM provider_request_attempts AS a
           JOIN normalized_provenance AS p ON p.raw_asset_id = a.raw_asset_id
           WHERE a.logical_fetch_id = ? AND a.status = 'SUCCEEDED'""",
        (logical_fetch_id,),
    ).fetchone()[0]
    return provenance_count > 0


def _b2o_scope_windows(scope: dict) -> list[tuple[str, str]]:
    from datetime import date as _date, timedelta

    if scope["date_domain"] == "as_of":
        return [(scope["end_date"], scope["end_date"])]
    start = _date.fromisoformat(scope["start_date"])
    end = _date.fromisoformat(scope["end_date"])
    window_days = int(scope.get("request_window_days") or 1)
    windows = []
    cur = start
    while cur <= end:
        win_end = min(end, cur + timedelta(days=window_days - 1))
        windows.append((cur.isoformat(), win_end.isoformat()))
        cur = win_end + timedelta(days=1)
    return windows


def _b2o_scope_cells(scope: dict) -> list[dict]:
    from catalyst_data.manifests.universe import SourceCell

    endpoint_names = scope.get("endpoint_names") or []
    cells = []
    for subject in scope.get("subjects", []):
        endpoints = endpoint_names
        if scope["source_type"] == "fred_macro" and not endpoints:
            endpoints = [subject]
        if not endpoints:
            endpoints = [scope["source_type"]]
        for window_start, window_end in _b2o_scope_windows(scope):
            for endpoint_name in endpoints:
                cell = SourceCell.create(
                    scope["stage"],
                    scope["source_type"],
                    endpoint_name,
                    subject,
                    window_start,
                    window_end,
                    scope["date_domain"],
                    scope.get("provider_profile_version", "v1"),
                    page_cap=scope.get("page_cap"),
                    item_cap=scope.get("item_cap"),
                )
                cells.append(cell.to_identity())
    return cells


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

def _raw_payload_is_empty(payload: object) -> bool:
    if isinstance(payload, list):
        return len(payload) == 0
    if isinstance(payload, dict):
        if isinstance(payload.get("news"), dict):
            results = payload["news"].get("results")
            if isinstance(results, list):
                return len(results) == 0
        results = payload.get("results")
        if isinstance(results, list):
            return len(results) == 0
    return False


def _raw_success_empty_for_cell(
    conn: sqlite3.Connection, source_type: str, ticker: str, date_s: str
) -> bool:
    rows = conn.execute(
        """SELECT http_status, metadata_json, content_raw
           FROM raw_assets
           WHERE source_type = ? AND ticker = ? AND reference_date = ?""",
        (source_type, ticker, date_s),
    ).fetchall()
    for http_status, metadata_json, content_raw in rows:
        if http_status is not None and int(http_status) != 200:
            continue
        try:
            metadata = json.loads(metadata_json or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if metadata.get("article_count") == 0:
            return True
        try:
            payload = json.loads(zlib.decompress(content_raw))
        except (TypeError, ValueError, zlib.error, json.JSONDecodeError):
            continue
        if _raw_payload_is_empty(payload):
            return True
    return False


def classify_source_checkpoints(conn: sqlite3.Connection) -> list[dict]:
    """Classify source checkpoint rows against materialized/raw evidence."""
    try:
        rows = conn.execute(
            """SELECT run_id, source_type, ticker, date, status, error_class
               FROM source_checkpoints
               ORDER BY source_type, ticker, date, run_id"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    classifications: list[dict] = []
    for run_id, source_type, ticker, date_s, status, error_class in rows:
        materialized = False
        raw_success_empty = False

        if status == "success":
            materialized = conn.execute(
                """SELECT 1
                   FROM article_tickers at
                   JOIN articles a ON a.article_id = at.article_id
                   WHERE a.source_type = ?
                     AND at.ticker = ?
                     AND at.reference_date = ?
                   LIMIT 1""",
                (source_type, ticker, date_s),
            ).fetchone() is not None
            if not materialized:
                raw_success_empty = _raw_success_empty_for_cell(
                    conn, source_type, ticker, date_s
                )

        if status == "success" and materialized:
            classification = "materialized"
        elif status == "success" and raw_success_empty:
            classification = "success_empty"
        elif status == "success":
            classification = "phantom_success"
        elif status == "failed":
            classification = "actionable_failed"
        elif status == "skipped":
            classification = "actionable_skipped"
        else:
            classification = f"status_{status}"

        classifications.append({
            "run_id": run_id,
            "source_type": source_type,
            "ticker": ticker,
            "date": date_s,
            "status": status,
            "error_class": error_class,
            "classification": classification,
            "materialized": materialized,
            "raw_success_empty": raw_success_empty,
        })
    return classifications


def summarize_checkpoint_classifications(classifications: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in classifications:
        classification = item["classification"]
        counts[classification] = counts.get(classification, 0) + 1
    return counts


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

    classifications = classify_source_checkpoints(conn)
    result["classification_counts"] = summarize_checkpoint_classifications(
        classifications
    )
    result["phantom_success_examples"] = [
        {
            "run_id": item["run_id"],
            "source_type": item["source_type"],
            "ticker": item["ticker"],
            "date": item["date"],
        }
        for item in classifications
        if item["classification"] == "phantom_success"
    ][:20]

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

# ---------------------------------------------------------------------------
# Gate P0 — Pass/fail invariant aggregation
# ---------------------------------------------------------------------------

def audit_gate_p0(conn):
    """Aggregate pass/fail for all Gate P0 invariants.

    Returns dict with gate_passed and per-invariant check results.
    """
    checks = []

    def _add(name, passed, detail, count=0):
        checks.append({"name": name, "passed": passed, "detail": detail, "count": count})

    # G1: Zero blank reference_date in prose articles
    blank_arts = conn.execute("""
        SELECT COUNT(*) FROM articles a
        JOIN raw_assets r ON a.raw_asset_id = r.asset_id
        WHERE (a.reference_date IS NULL OR a.reference_date = '')
          AND r.source_type IN ('polygon_news', 'finnhub_company_news')
    """).fetchone()[0]
    _add("G1_blank_article_refs", blank_arts == 0,
         f"{blank_arts} blank article reference_dates", blank_arts)

    # G2: Zero blank reference_date in prose article_tickers
    blank_ats = conn.execute("""
        SELECT COUNT(*) FROM article_tickers at
        JOIN raw_assets r ON at.raw_asset_id = r.asset_id
        WHERE (at.reference_date IS NULL OR at.reference_date = '')
          AND r.source_type IN ('polygon_news', 'finnhub_company_news')
    """).fetchone()[0]
    _add("G2_blank_article_ticker_refs", blank_ats == 0,
         f"{blank_ats} blank article_ticker reference_dates", blank_ats)

    # G3: No fetch-date FRED release collapse
    distinct_rel = conn.execute(
        "SELECT COUNT(DISTINCT released_at) FROM macro_observations"
    ).fetchone()[0]
    rel_collapse = distinct_rel <= 2
    _add("G3_fred_release_collapse", not rel_collapse,
         f"distinct released_at={distinct_rel}" + (" (COLLAPSED)" if rel_collapse else ""),
         distinct_rel)

    # G4: released_at <= fetched_at for all macro rows
    rel_gt_fetched = conn.execute(
        "SELECT COUNT(*) FROM macro_observations WHERE released_at > fetched_at"
    ).fetchone()[0]
    _add("G4_fred_released_le_fetched", rel_gt_fetched == 0,
         f"{rel_gt_fetched} macro rows with released_at > fetched_at", rel_gt_fetched)

    # G5: No NULL dedup for eligible prose articles
    null_dedup_arts = conn.execute("""
        SELECT COUNT(*) FROM articles a
        JOIN raw_assets r ON a.raw_asset_id = r.asset_id
        WHERE a.dedup_group_id IS NULL
          AND r.source_type IN ('polygon_news', 'finnhub_company_news')
    """).fetchone()[0]
    _add("G5_null_dedup_articles", null_dedup_arts == 0,
         f"{null_dedup_arts} prose articles with NULL dedup_group_id", null_dedup_arts)

    # G6: No (dedup_group, ticker) with zero canonical associations
    zero_canon_dup = 0
    has_at_canon = False
    try:
        conn.execute("SELECT is_canonical FROM article_tickers LIMIT 0")
        has_at_canon = True
    except sqlite3.OperationalError:
        pass
    if has_at_canon:
        zero_canon_dup = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT at.dedup_group_id, at.ticker, COUNT(*) as cnt,
                       SUM(CASE WHEN at.is_canonical = 1 THEN 1 ELSE 0 END) as canon_cnt
                FROM article_tickers at
                WHERE at.dedup_group_id IS NOT NULL
                GROUP BY at.dedup_group_id, at.ticker
                HAVING cnt >= 1 AND canon_cnt != 1
            )
        """).fetchone()[0]
    _add("G6_zero_canonical_dup_group", zero_canon_dup == 0,
         f"{zero_canon_dup} (group,ticker) with invalid canonical count" + 
         ("" if has_at_canon else " (column missing, skipping)"),
         zero_canon_dup)

    # G7: Per-association canonicality: each (group_id, ticker) has exactly 1 canonical
    per_assoc_violations = 0
    if has_at_canon:
        per_assoc_violations = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT at.dedup_group_id, at.ticker, COUNT(*) as cnt,
                       SUM(CASE WHEN at.is_canonical = 1 THEN 1 ELSE 0 END) as canon_cnt
                FROM article_tickers at
                WHERE at.dedup_group_id IS NOT NULL
                GROUP BY at.dedup_group_id, at.ticker
                HAVING canon_cnt != 1
            )
        """).fetchone()[0]
    _add("G7_per_assoc_canonical", per_assoc_violations == 0,
         f"{per_assoc_violations} (group,ticker) with !=1 canonical" +
         ("" if has_at_canon else " (column missing, skipping)"),
         per_assoc_violations)

    # G8: No phantom success checkpoints for prose sources
    PROSE = ("polygon_news", "finnhub_company_news")
    phantom = 0
    try:
        classifications = classify_source_checkpoints(conn)
        phantom = sum(
            1 for c in classifications
            if c["classification"] == "phantom_success"
            and c["source_type"] in PROSE
        )
    except Exception:
        phantom = -1
    _add("G8_phantom_success_checkpoints", phantom == 0,
         f"{phantom} phantom prose success checkpoints", phantom)

    # G9: Macro Plane-2: zero index_state
    idx_macro = 0
    try:
        idx_macro = conn.execute(
            "SELECT COUNT(*) FROM index_state WHERE source_kind = 'fred_macro'"
        ).fetchone()[0]
    except Exception:
        pass
    _add("G9_fred_plane2_index_state", idx_macro == 0,
         f"{idx_macro} fred_macro rows in index_state", idx_macro)

    # G10: Macro Plane-2: zero clean_assets
    clean_macro = 0
    try:
        clean_macro = conn.execute(
            "SELECT COUNT(*) FROM clean_assets WHERE source_type = 'fred_macro'"
        ).fetchone()[0]
    except Exception:
        pass
    _add("G10_fred_plane2_clean_assets", clean_macro == 0,
         f"{clean_macro} fred_macro rows in clean_assets", clean_macro)

    all_passed = all(c["passed"] for c in checks)
    failing = [c["name"] for c in checks if not c["passed"]]

    return {
        "gate_passed": all_passed,
        "checks": checks,
        "failing": failing,
        "total_checks": len(checks),
        "passed_count": sum(1 for c in checks if c["passed"]),
        "failed_count": sum(1 for c in checks if not c["passed"]),
    }


# ---------------------------------------------------------------------------
# Step 4a — Pre-GPU embed-readiness gate
# ---------------------------------------------------------------------------

def audit_embed_readiness(conn):
    """Pass/fail gate that must be green before any GPU spend.

    Checks: S1 eligibility, S2 index_state consistency, S3 clean layer,
            ticker-losslessness, SEC scope surfaced.
    """
    checks = []

    def _add(name, passed, detail, count=0):
        checks.append({"name": name, "passed": passed, "detail": detail, "count": count})

    # E1: Per-association canonicality: zero groups with !=1 canonical per (group, ticker)
    has_at_canon = False
    try:
        conn.execute("SELECT is_canonical FROM article_tickers LIMIT 0")
        has_at_canon = True
    except sqlite3.OperationalError:
        pass

    if has_at_canon:
        canon_violations = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT at.dedup_group_id, at.ticker,
                       SUM(CASE WHEN at.is_canonical = 1 THEN 1 ELSE 0 END) as canon_cnt
                FROM article_tickers at
                WHERE at.dedup_group_id IS NOT NULL
                GROUP BY at.dedup_group_id, at.ticker
                HAVING canon_cnt != 1
            )
        """).fetchone()[0]
    else:
        canon_violations = -1
    _add("E1_per_assoc_canonical", canon_violations == 0,
         f"{canon_violations} (group,ticker) with !=1 canonical" +
         ("" if has_at_canon else " (column missing)"),
         canon_violations)

    # E2: index_state article L1 count == eligible article count
    try:
        from catalyst_data.eligibility import eligible_article_count
        eligible = eligible_article_count(conn)
        pending_l1 = conn.execute(
            "SELECT COUNT(*) FROM index_state WHERE source_kind='article' AND chunk_level='l1' AND status='pending'"
        ).fetchone()[0]
    except Exception:
        eligible = -1
        pending_l1 = -1
    _add("E2_l1_equals_eligible", pending_l1 == eligible and eligible >= 0,
         f"pending_l1={pending_l1}, eligible={eligible}",
         pending_l1)

    # E3: One pending L1 per eligible article
    dup_l1 = 0
    try:
        dup_l1 = conn.execute(
            """SELECT COUNT(*) FROM (
                SELECT corpus_item_id, COUNT(*) as cnt
                FROM index_state WHERE source_kind='article' AND chunk_level='l1' AND status='pending'
                GROUP BY corpus_item_id HAVING cnt > 1
            )"""
        ).fetchone()[0]
    except Exception:
        dup_l1 = -1
    _add("E3_one_l1_per_article", dup_l1 == 0,
         f"{dup_l1} articles with >1 pending L1",
         dup_l1)

    # E4: Clean layer: only prose source_types
    non_prose = 0
    try:
        non_prose = conn.execute(
            "SELECT COUNT(*) FROM clean_assets WHERE source_type NOT IN ('polygon_news', 'finnhub_company_news')"
        ).fetchone()[0]
    except Exception:
        non_prose = -1
    _add("E4_clean_prose_only", non_prose == 0,
         f"{non_prose} non-prose clean_assets rows",
         non_prose)

    # E5: Zero FRED macro in index_state
    fred_idx = 0
    try:
        fred_idx = conn.execute(
            "SELECT COUNT(*) FROM index_state WHERE source_kind = 'fred_macro'"
        ).fetchone()[0]
    except Exception:
        fred_idx = -1
    _add("E5_fred_not_in_index", fred_idx == 0,
         f"{fred_idx} fred_macro rows in index_state",
         fred_idx)

    # E6: SEC scope surfaced (informational — always passes, count is informative)
    sec_count = 0
    try:
        sec_count = conn.execute(
            "SELECT COUNT(*) FROM filings WHERE is_rag_eligible = 1"
        ).fetchone()[0]
    except Exception:
        sec_count = -1
    thin = sec_count >= 0 and sec_count < 10
    _add("E6_sec_scope_surfaced", True,
         f"SEC rag_eligible filings={sec_count}" + (" (THIN)" if thin else ""),
         sec_count)

    all_passed = all(c["passed"] for c in checks)
    failing = [c["name"] for c in checks if not c["passed"]]

    return {
        "gate_passed": all_passed,
        "checks": checks,
        "failing": failing,
        "total_checks": len(checks),
        "passed_count": sum(1 for c in checks if c["passed"]),
        "failed_count": sum(1 for c in checks if not c["passed"]),
    }
