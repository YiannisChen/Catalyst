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
        """Get the most recent fundamentals snapshot for a ticker on or before trade_date.

        Scans snapshots in reverse chronological order and skips any whose fiscal
        period end date (the "date" field in the content markdown) falls after
        trade_date.  This prevents look-ahead bias from quarterly filings that were
        ingested with the fiscal period-end date but not actually published until
        after the trade session.
        """
        sql = """
            SELECT asset_id, ticker, reference_date, content_md
            FROM clean_assets
            WHERE ticker = ?
              AND source_type = 'fmp_fundamentals'
              AND reference_date <= ?
            ORDER BY reference_date DESC
        """
        rows = self._fetchall(sql, (ticker.upper(), trade_date))
        for row in rows:
            content = row[3] or ""
            metrics: dict[str, str] = {}
            has_valid_date = False
            for line in content.split("\n"):
                if "|" in line and "---" not in line and "Field" not in line:
                    parts = [p.strip() for p in line.split("|") if p.strip()]
                    if len(parts) == 2:
                        metrics[parts[0]] = parts[1]
            # Check the fiscal period end date — must not be after trade_date
            fiscal_date = metrics.get("date")
            if fiscal_date and fiscal_date > trade_date:
                continue  # skip future-leaking snapshot
            return {
                "asset_id": row[0],
                "ticker": row[1],
                "reference_date": row[2],
                "metrics": metrics,
            }
        return None

    def get_session(
        self,
        *,
        ticker: str,
        trade_date: str,
    ) -> dict[str, Any] | None:
        """Return selected-session OHLCV data with properly computed previous_close.

        previous_close must be the immediately preceding available trading
        session, not one calendar day earlier.
        """
        # Fetch the selected candle
        candle_sql = """
            SELECT symbol, date, open, high, low, close, volume, source
            FROM ohlcv
            WHERE symbol = ? AND date = ?
        """
        candle_rows = self._fetchall(candle_sql, (ticker.upper(), trade_date))
        if not candle_rows:
            return None

        row = candle_rows[0]
        open_val = row[2]
        high_val = row[3]
        low_val = row[4]
        close_val = row[5]

        # Fetch the immediately preceding trading session
        prev_sql = """
            SELECT close FROM ohlcv
            WHERE symbol = ? AND date < ?
            ORDER BY date DESC
            LIMIT 1
        """
        prev_rows = self._fetchall(prev_sql, (ticker.upper(), trade_date))
        previous_close = prev_rows[0][0] if prev_rows else None

        # Compute close_move_pct using previous_close
        close_move_pct = None
        if previous_close is not None and previous_close != 0 and close_val is not None:
            close_move_pct = round(((close_val - previous_close) / previous_close) * 100, 2)

        # Compute intraday range
        intraday_range = None
        if high_val is not None and low_val is not None:
            intraday_range = round(high_val - low_val, 2)

        # Compute event window (trade_date ± a few days)
        from datetime import datetime, timedelta
        try:
            dt = datetime.strptime(trade_date, "%Y-%m-%d")
            event_window_start = (dt - timedelta(days=3)).strftime("%Y-%m-%d")
            event_window_end = dt.strftime("%Y-%m-%d")
        except ValueError:
            event_window_start = None
            event_window_end = None

        return {
            "ticker": ticker.upper(),
            "trade_date": trade_date,
            "open": open_val,
            "high": high_val,
            "low": low_val,
            "close": close_val,
            "volume": row[6],
            "previous_close": previous_close,
            "close_move_pct": close_move_pct,
            "intraday_range": intraday_range,
            "source": row[7],
            "event_window_start": event_window_start,
            "event_window_end": event_window_end,
            "is_trading_day": True,
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
