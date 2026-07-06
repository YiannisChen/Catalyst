"""WS4B Phase 0 remediation helpers.

These functions are intentionally small orchestration wrappers around existing
data-core materializers. They are offline unless a caller explicitly invokes a
live FRED helper in a later phase.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from catalyst_data.dedup.cross_source import compute_cross_source_dedup
from catalyst_data.coverage_audit import (
    classify_source_checkpoints,
    summarize_checkpoint_classifications,
)
from catalyst_data.pipeline.finnhub_normalize import rederive_finnhub_news
from catalyst_data.pipeline.fred_normalize import rederive_fred_macro
from catalyst_data.regenerate_clean import regenerate_polygon_clean_assets
from catalyst_data.rederive import rederive_polygon_news
from catalyst_data.storage.sqlite import (
    compute_asset_id,
    ensure_clean_provenance,
    ensure_macro_tables,
    upsert_raw_asset,
)

PROSE_SOURCES = ("polygon_news", "finnhub_company_news")
PROSE_PROVIDERS = ("polygon", "finnhub")
FROZEN_DB_NAME = "catalyst_eval_frozen_v2.db"


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] if row else 0)


def prose_counts(db_path: str | Path) -> dict[str, int]:
    conn = _connect(db_path)
    try:
        return {
            "articles": _scalar(
                conn,
                "SELECT COUNT(*) FROM articles WHERE source_type IN (?, ?)",
                PROSE_SOURCES,
            ),
            "article_tickers": _scalar(
                conn,
                """SELECT COUNT(*) FROM article_tickers at
                   JOIN articles a ON a.article_id = at.article_id
                   WHERE a.source_type IN (?, ?)""",
                PROSE_SOURCES,
            ),
            "blank_article_refs": _scalar(
                conn,
                """SELECT COUNT(*) FROM articles
                   WHERE source_type IN (?, ?)
                     AND COALESCE(reference_date, '') = ''""",
                PROSE_SOURCES,
            ),
            "blank_article_ticker_refs": _scalar(
                conn,
                """SELECT COUNT(*) FROM article_tickers at
                   JOIN articles a ON a.article_id = at.article_id
                   WHERE a.source_type IN (?, ?)
                     AND COALESCE(at.reference_date, '') = ''""",
                PROSE_SOURCES,
            ),
            "dedup_null_articles": _scalar(
                conn,
                """SELECT COUNT(*) FROM articles
                   WHERE source_type IN (?, ?)
                     AND dedup_group_id IS NULL""",
                PROSE_SOURCES,
            ),
            "dedup_null_article_tickers": _scalar(
                conn,
                """SELECT COUNT(*) FROM article_tickers at
                   JOIN articles a ON a.article_id = at.article_id
                   WHERE a.source_type IN (?, ?)
                     AND at.dedup_group_id IS NULL""",
                PROSE_SOURCES,
            ),
            "polygon_clean_assets": _scalar(
                conn,
                "SELECT COUNT(*) FROM clean_assets WHERE source_type = 'polygon_news'",
            ),
        }
    finally:
        conn.close()


def reset_prose_dedup_fields(db_path: str | Path) -> dict[str, int]:
    """Unconditionally reset prose dedup state before full-corpus materialization."""
    conn = _connect(db_path)
    try:
        article_ids = [
            row[0]
            for row in conn.execute(
                "SELECT article_id FROM articles WHERE source_type IN (?, ?)",
                PROSE_SOURCES,
            ).fetchall()
        ]
        if not article_ids:
            return {"articles_reset": 0, "article_tickers_reset": 0}

        placeholders = ",".join("?" for _ in article_ids)
        conn.execute(
            f"""UPDATE articles
                SET dedup_group_id = NULL, is_canonical = 1
                WHERE article_id IN ({placeholders})""",
            tuple(article_ids),
        )
        conn.execute(
            f"""UPDATE article_tickers
                SET dedup_group_id = NULL
                WHERE article_id IN ({placeholders})""",
            tuple(article_ids),
        )
        conn.commit()
        return {
            "articles_reset": len(article_ids),
            "article_tickers_reset": _scalar(
                conn,
                f"SELECT COUNT(*) FROM article_tickers WHERE article_id IN ({placeholders})",
                tuple(article_ids),
            ),
        }
    finally:
        conn.close()


def rederive_prose_from_bronze(db_path: str | Path) -> dict[str, Any]:
    """Re-materialize Polygon and Finnhub prose from existing Bronze only."""
    db_path = Path(db_path)
    before = prose_counts(db_path)

    polygon = rederive_polygon_news(db_path)
    finnhub = rederive_finnhub_news(db_path)

    conn = _connect(db_path)
    try:
        ensure_clean_provenance(conn)
    finally:
        conn.close()
    clean = regenerate_polygon_clean_assets(db_path)
    dedup_reset = reset_prose_dedup_fields(db_path)
    after = prose_counts(db_path)

    return {
        "before_counts": before,
        "polygon": polygon,
        "finnhub": finnhub,
        "clean_assets": clean,
        "dedup_reset": dedup_reset,
        "after_counts": after,
    }


def materialize_cross_source_dedup(db_path: str | Path) -> dict[str, Any]:
    """Materialize full-corpus Polygon/Finnhub dedup after prose rederive."""
    before = prose_counts(db_path)
    conn = _connect(db_path)
    try:
        groups_resolved = compute_cross_source_dedup(conn)
    finally:
        conn.close()
    after = prose_counts(db_path)
    return {
        "before": before,
        "groups_resolved": groups_resolved,
        "after": after,
    }


def reconcile_news_checkpoints(db_path: str | Path) -> dict[str, Any]:
    """Repair phantom news success checkpoints without network access."""
    conn = _connect(db_path)
    try:
        before_items = [
            item for item in classify_source_checkpoints(conn)
            if item["source_type"] in PROSE_SOURCES
        ]
        before = summarize_checkpoint_classifications(before_items)
        phantom = [
            item for item in before_items
            if item["classification"] == "phantom_success"
        ]
        for item in phantom:
            conn.execute(
                """UPDATE source_checkpoints
                   SET status = 'failed',
                       error_class = 'PhantomSuccess'
                   WHERE run_id = ?
                     AND source_type = ?
                     AND ticker = ?
                     AND date = ?""",
                (
                    item["run_id"],
                    item["source_type"],
                    item["ticker"],
                    item["date"],
                ),
            )
        conn.commit()
        after_items = [
            item for item in classify_source_checkpoints(conn)
            if item["source_type"] in PROSE_SOURCES
        ]
        after = summarize_checkpoint_classifications(after_items)
        return {
            "before": before,
            "after": after,
            "repaired_phantom_success": len(phantom),
            "actionable_failed": after.get("actionable_failed", 0),
            "actionable_skipped": after.get("actionable_skipped", 0),
        }
    finally:
        conn.close()


async def fred_fit_guard(
    fetcher: Any,
    *,
    series_id: str = "DFF",
    from_date: str,
    to_date: str,
) -> dict[str, Any]:
    """Validate that scoped FRED output_type=4 response shape fits the window."""
    result = await fetcher(
        "",
        series_id,
        to_date,
        start_date=from_date,
        output_type=4,
        realtime_start=from_date,
        realtime_end=to_date,
    )
    if getattr(result, "status", None) != 200:
        return {
            "fit_proven": False,
            "series_id": series_id,
            "status": getattr(result, "status", None),
            "error": "[REDACTED]" if getattr(result, "error", None) else None,
            "observation_count": 0,
            "reason": "non_200",
        }

    data = result.data if isinstance(getattr(result, "data", None), dict) else {}
    observations = data.get("observations", [])
    if not isinstance(observations, list) or not observations:
        return {
            "fit_proven": False,
            "series_id": series_id,
            "status": 200,
            "observation_count": 0,
            "reason": "empty_or_invalid_observations",
        }

    has_release_dates = all(
        isinstance(obs, dict) and bool(obs.get("realtime_start"))
        for obs in observations
    )
    return {
        "fit_proven": has_release_dates,
        "series_id": series_id,
        "status": 200,
        "observation_count": len(observations),
        "first_observation": observations[0].get("date") if isinstance(observations[0], dict) else None,
        "last_observation": observations[-1].get("date") if isinstance(observations[-1], dict) else None,
        "reason": "ok" if has_release_dates else "missing_per_observation_realtime_start",
    }


def _refuse_frozen_db(db_path: Path) -> None:
    if db_path.resolve().name == FROZEN_DB_NAME:
        raise RuntimeError("Refusing to mutate frozen evaluation DB")


async def replace_fred_macro_from_fetcher(
    db_path: str | Path,
    fetcher: Any,
    *,
    from_date: str,
    to_date: str,
    series_ids: list[str],
    fit_guard: dict[str, Any],
    confirm: bool,
) -> dict[str, Any]:
    """Clear and replace dev FRED macro rows after a proven fit-guard."""
    db_path = Path(db_path)
    _refuse_frozen_db(db_path)
    if not confirm:
        return {"status": "refused_unconfirmed"}
    if not fit_guard.get("fit_proven"):
        return {"status": "refused_fit_not_proven", "fit_guard": fit_guard}

    conn = _connect(db_path)
    try:
        ensure_macro_tables(conn)
        cleared_macro = _scalar(conn, "SELECT COUNT(*) FROM macro_observations")
        cleared_raw = _scalar(
            conn, "SELECT COUNT(*) FROM raw_assets WHERE source_type = 'fred_macro'"
        )
        cleared_clean = _scalar(
            conn, "SELECT COUNT(*) FROM clean_assets WHERE source_type = 'fred_macro'"
        )
        try:
            cleared_checkpoints = _scalar(
                conn,
                "SELECT COUNT(*) FROM source_checkpoints WHERE source_type = 'fred_macro'",
            )
        except sqlite3.OperationalError:
            cleared_checkpoints = 0

        conn.execute("DELETE FROM macro_observations")
        try:
            conn.execute(
                """DELETE FROM asset_quality_flags
                   WHERE asset_id IN (
                       SELECT asset_id FROM clean_assets
                       WHERE source_type = 'fred_macro'
                   )"""
            )
        except sqlite3.OperationalError:
            pass
        conn.execute(
            """DELETE FROM news_alignment
               WHERE asset_id IN (
                   SELECT asset_id FROM clean_assets
                   WHERE source_type = 'fred_macro'
               )"""
        )
        conn.execute("DELETE FROM clean_assets WHERE source_type = 'fred_macro'")
        conn.execute("DELETE FROM raw_assets WHERE source_type = 'fred_macro'")
        try:
            conn.execute("DELETE FROM source_checkpoints WHERE source_type = 'fred_macro'")
        except sqlite3.OperationalError:
            pass
        conn.commit()

        fetched = 0
        failed = 0
        for series_id in series_ids:
            result = await fetcher(
                "",
                series_id,
                to_date,
                start_date=from_date,
                output_type=4,
                realtime_start=from_date,
                realtime_end=to_date,
            )
            if getattr(result, "status", None) != 200:
                failed += 1
                continue
            raw_data = result.data if isinstance(getattr(result, "data", None), dict) else {}
            raw_data["id"] = series_id
            observations = raw_data.get("observations", [])
            if not isinstance(observations, list):
                failed += 1
                continue
            asset_id = compute_asset_id(series_id, to_date, "fred_macro")
            upsert_raw_asset(
                conn,
                asset_id=asset_id,
                ticker=series_id,
                source_type="fred_macro",
                reference_date=to_date,
                content_raw=json.dumps(raw_data, ensure_ascii=True).encode("utf-8"),
                http_status=200,
                metadata={
                    "series_id": series_id,
                    "endpoint": "series/observations",
                    "output_type": 4,
                    "observation_start": from_date,
                    "observation_end": to_date,
                    "realtime_start": from_date,
                    "realtime_end": to_date,
                    "observation_count": len(observations),
                },
            )
            fetched += 1
    finally:
        conn.close()

    rederive_counts = rederive_fred_macro(db_path)
    return {
        "status": "replaced",
        "cleared_macro_rows": cleared_macro,
        "cleared_raw_assets": cleared_raw,
        "cleared_clean_assets": cleared_clean,
        "cleared_checkpoints": cleared_checkpoints,
        "fetched_series": fetched,
        "failed_series": failed,
        "rederive": rederive_counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("rederive-prose", "dedup", "reconcile-checkpoints"))
    parser.add_argument("--db", required=True, help="SQLite database path")
    args = parser.parse_args(argv)

    if args.command == "rederive-prose":
        result = rederive_prose_from_bronze(args.db)
    elif args.command == "dedup":
        result = materialize_cross_source_dedup(args.db)
    else:
        result = reconcile_news_checkpoints(args.db)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
