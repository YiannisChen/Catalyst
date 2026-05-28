from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any


class WorkbenchStoreError(RuntimeError):
    pass


class WorkbenchStore:
    def __init__(self, *, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def list_tickers(self) -> list[str]:
        query = "SELECT DISTINCT symbol FROM ohlcv WHERE symbol IS NOT NULL AND symbol != '' ORDER BY symbol ASC"
        rows = self._fetchall(query, ())
        return [str(row[0]) for row in rows]

    def read_ohlcv(
        self,
        *,
        ticker: str,
        start_date: str | None,
        end_date: str | None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT symbol, date, open, high, low, close, volume, source
            FROM ohlcv
            WHERE symbol = ?
        """
        params: list[Any] = [ticker.upper()]
        if start_date is not None:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date is not None:
            sql += " AND date <= ?"
            params.append(end_date)
        sql += " ORDER BY date ASC"

        rows = self._fetchall(sql, tuple(params))
        return [
            {
                "symbol": row[0],
                "date": row[1],
                "open": row[2],
                "high": row[3],
                "low": row[4],
                "close": row[5],
                "volume": row[6],
                "source": row[7],
            }
            for row in rows
        ]

    def list_news(
        self,
        *,
        ticker: str,
        trade_date: str,
        window_days: int = 3,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Retrieve news articles near a trade date for a given ticker."""
        sql = """
            SELECT asset_id, ticker, reference_date, published_utc, content_md
            FROM clean_assets
            WHERE ticker = ?
              AND source_type = 'polygon_news'
              AND reference_date BETWEEN date(?, '-' || ? || ' days') AND ?
              AND is_rag_eligible = 1
            ORDER BY reference_date DESC, published_utc DESC
            LIMIT ?
        """
        rows = self._fetchall(sql, (ticker.upper(), trade_date, str(window_days), trade_date, limit))
        results = []
        for row in rows:
            content = row[4] or ""
            title = ""
            source = ""
            # Parse title from markdown header
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("## "):
                    title = stripped[3:].strip()
                elif stripped.startswith("*Source:") and not source:
                    source = stripped.strip("* ")
                if title and source:
                    break
            results.append({
                "asset_id": row[0],
                "ticker": row[1],
                "reference_date": row[2],
                "published_utc": row[3],
                "title": title,
                "source_line": source,
                "snippet": content[:500],
            })
        return results

    def get_fundamentals(
        self,
        *,
        ticker: str,
        trade_date: str,
    ) -> dict[str, Any] | None:
        """Get the most recent fundamentals snapshot for a ticker on or before trade_date."""
        sql = """
            SELECT asset_id, ticker, reference_date, content_md
            FROM clean_assets
            WHERE ticker = ?
              AND source_type = 'fmp_fundamentals'
              AND reference_date <= ?
            ORDER BY reference_date DESC
            LIMIT 1
        """
        rows = self._fetchall(sql, (ticker.upper(), trade_date))
        if not rows:
            return None
        row = rows[0]
        content = row[3] or ""
        # Parse key-value pairs from markdown table
        metrics: dict[str, str] = {}
        for line in content.split("\n"):
            if "|" in line and "---" not in line and "Field" not in line:
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) == 2:
                    metrics[parts[0]] = parts[1]
        return {
            "asset_id": row[0],
            "ticker": row[1],
            "reference_date": row[2],
            "metrics": metrics,
        }

    def local_range(self) -> dict[str, Any]:
        row = self._fetchone(
            """
            SELECT MIN(date) AS min_date,
                   MAX(date) AS max_date,
                   COUNT(DISTINCT symbol) AS ticker_count,
                   COUNT(*) AS row_count
            FROM ohlcv
            """,
            (),
        )
        return {
            "min_date": row[0],
            "max_date": row[1],
            "ticker_count": int(row[2] or 0),
            "row_count": int(row[3] or 0),
        }

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise WorkbenchStoreError(f"SQLite DB path does not exist: {self.db_path}")
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        return conn

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        conn = self._connect()
        try:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                raise WorkbenchStoreError("Missing required table: ohlcv") from exc
            raise
        finally:
            conn.close()

    def _fetchone(self, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...]:
        rows = self._fetchall(sql, params)
        if not rows:
            return (None, None, 0, 0)
        return rows[0]
