"""Read-only local context dependencies for the production graph."""

from __future__ import annotations

from datetime import date
import sqlite3
from pathlib import Path

from catalyst_agents.attribution.provider import ContextInputs


class SQLiteContextProvider:
    """Load market-session context from the promoted local snapshot only."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def load_context_inputs(self, *, ticker: str, session_date: str, cutoff: str) -> ContextInputs:
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                """
                SELECT date, close, volume
                FROM ohlcv
                WHERE symbol = ? AND date <= ?
                ORDER BY date DESC
                LIMIT 21
                """,
                (ticker, session_date),
            ).fetchall()
        if not rows or rows[0][0] != session_date:
            raise ValueError(f"snapshot has no {ticker} close for {session_date}")
        target_date, target_close, target_volume = rows[0]
        prior = list(reversed(rows[1:]))
        expected = tuple(str(row[0]) for row in prior)
        prior_volumes = {str(row[0]): row[2] for row in prior}
        previous_close = prior[-1][1] if prior else None
        return ContextInputs(
            ticker=ticker,
            session_date=date.fromisoformat(str(target_date)),
            cutoff=cutoff,
            target_close=target_close,
            previous_target_close=previous_close,
            target_volume=target_volume,
            expected_prior_sessions=expected,
            prior_volumes_by_session=prior_volumes,
            benchmark_ticker=None,
            benchmark_return_pct=None,
            sector_ticker=None,
            sector_return_pct=None,
            peer_returns_by_ticker={},
        )


__all__ = ["SQLiteContextProvider"]
