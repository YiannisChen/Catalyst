"""Read-only local context dependencies for the production graph (M4-1).

The SQLite structured reader supplies point-in-time facts only: session
open/close/volume, prior closes/volumes, and configured major macro release
facts when the macro source is available. Benchmark/sector/peer mappings stay
explicit unknowns until certified mappings exist (Q-008); the reader never
fabricates a proxy.
"""
from __future__ import annotations

from datetime import date
import sqlite3
from pathlib import Path

from catalyst_agents.attribution.move_profile import ScheduledMacroFlag
from catalyst_agents.attribution.provider import ContextInputs


class SQLiteContextProvider:
    """Load market-session context from the promoted local snapshot only."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        major_series_ids: tuple[str, ...] = (),
    ) -> None:
        self.db_path = Path(db_path)
        self.major_series_ids = tuple(major_series_ids)

    def load_context_inputs(
        self,
        *,
        ticker: str,
        session_date: str,
        cutoff: str,
        information_window_start_at: str | None = None,
    ) -> ContextInputs:
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                """
                SELECT date, open, close, volume
                FROM ohlcv
                WHERE symbol = ? AND date <= ?
                ORDER BY date DESC
                LIMIT 22
                """,
                (ticker, session_date),
            ).fetchall()
        if not rows or rows[0][0] != session_date:
            raise ValueError(f"snapshot has no {ticker} close for {session_date}")
        target_date, target_open, target_close, target_volume = rows[0]
        prior = list(reversed(rows[1:]))
        expected = tuple(str(row[0]) for row in prior)
        prior_volumes = {str(row[0]): row[3] for row in prior}
        previous_close = prior[-1][2] if prior else None
        previous_2_close = prior[-2][2] if len(prior) >= 2 else None
        return ContextInputs(
            ticker=ticker,
            session_date=date.fromisoformat(str(target_date)),
            cutoff=cutoff,
            target_close=target_close,
            previous_target_close=previous_close,
            target_open=target_open,
            previous_2_target_close=previous_2_close,
            target_volume=target_volume,
            expected_prior_sessions=expected,
            prior_volumes_by_session=prior_volumes,
            benchmark_ticker=None,
            benchmark_return_pct=None,
            sector_ticker=None,
            sector_return_pct=None,
            peer_returns_by_ticker={},
            scheduled_macro_flags=self._load_macro_flags(
                cutoff, information_window_start_at=information_window_start_at
            ),
            macro_source_available=self._macro_source_available(),
        )

    def _macro_source_available(self) -> bool:
        if not self.major_series_ids:
            return False
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                conn.execute("SELECT 1 FROM macro_observations LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def _load_macro_flags(
        self,
        cutoff: str,
        *,
        information_window_start_at: str | None = None,
    ) -> tuple[ScheduledMacroFlag, ...]:
        """Configured MAJOR releases bounded by the information window:
        ``information_window_start_at <= released_at <= cutoff_at`` (FIX 2)."""
        if not self.major_series_ids:
            return ()
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                if information_window_start_at is not None:
                    rows = conn.execute(
                        """SELECT series_id, released_at
                           FROM macro_observations
                           WHERE series_id IN ({placeholders})
                             AND released_at >= ?
                             AND released_at <= ?
                           ORDER BY series_id, released_at""".format(
                            placeholders=", ".join("?" for _ in self.major_series_ids)
                        ),
                        (*self.major_series_ids, information_window_start_at, cutoff),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT series_id, released_at
                           FROM macro_observations
                           WHERE series_id IN ({placeholders}) AND released_at <= ?
                           ORDER BY series_id, released_at""".format(
                            placeholders=", ".join("?" for _ in self.major_series_ids)
                        ),
                        (*self.major_series_ids, cutoff),
                    ).fetchall()
        except sqlite3.Error:
            return ()
        flags = []
        seen: set[str] = set()
        for series_id, released_at in rows:
            key = str(series_id)
            if key in seen:
                continue
            seen.add(key)
            flags.append(
                ScheduledMacroFlag(
                    name=str(series_id),
                    scheduled_at=str(released_at) if released_at is not None else None,
                )
            )
        return tuple(sorted(flags, key=lambda flag: (flag.name, str(flag.scheduled_at))))


__all__ = ["SQLiteContextProvider"]
