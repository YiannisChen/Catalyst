from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_agents.trace.schema import init_trace_db


def prepare_runtime_db(
    db_path: Path,
    *,
    ticker: str = "AAPL",
    trade_date: str = "2025-09-08",
) -> None:
    conn = sqlite3.connect(db_path)
    init_trace_db(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ohlcv (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            source TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO ohlcv VALUES (?, ?, 100, 101, 99, 100.5, 1000, 'fixture')",
        (ticker, trade_date),
    )
    conn.commit()
    conn.close()
