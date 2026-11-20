"""Read-only local context dependencies for the production graph (M4-1).

The SQLite structured reader supplies point-in-time facts only: session
open/close/volume, prior closes/volumes, and configured major macro release
facts when the macro source is available.

M9 v2 block 1 (2026-10-01): benchmark, sector-ETF and peer session returns
come from the versioned peer map (``CATALYST_PEER_MAP``, seeded from the
2026-07-16 research audit; unset configuration uses the repository default).
The map must be readable before the run starts. The attached market store
(``CATALYST_MARKET_CONTEXT_DB``: ETF bars and SEC SIC codes). Peer bars are
read from the snapshot. Anything unmapped or missing stays an explicit null;
the reader never fabricates a proxy.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date
from pathlib import Path

from catalyst_data.ingestion import corp_actions
from catalyst_data.read import market_scope as scope
from catalyst_data.storage.errors import StoreReadError
from catalyst_data.storage.runtime_paths import runtime_store_path

from catalyst_agents.attribution.move_profile import ScheduledMacroFlag
from catalyst_agents.attribution.provider import ContextInputs
from catalyst_agents.runtime.peer_map import load_peer_map_from_env


# SIC-fallback peers: same SIC code, ranked by trailing return correlation.
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
        market = os.environ.get("CATALYST_MARKET_CONTEXT_DB", "").strip()
        self.market_db = Path(market).expanduser() if market else None
        if self.market_db is None:
            # 2026-11-16: the earnings node, health check and manifest resolve the market store through the
            # runtime data root; this reader read only the variable, so with the root alone every run saw
            # no market or sector return and the gate treated every move as company-specific.
            default = runtime_store_path("market_db")
            self.market_db = default if default.is_file() else None
        if self.market_db is not None and not self.market_db.is_file():
            cause = FileNotFoundError(self.market_db)
            raise StoreReadError(
                self.market_db, "open market context store", str(cause)
            ) from cause
        self.peer_map = load_peer_map_from_env()

    # -- market / sector / peer context (M9 v2 block 1) ----------------------

    _TARGET_SQL = (
        "SELECT date, open, close, volume FROM ohlcv WHERE symbol = ? "
        "AND date <= ? ORDER BY date DESC LIMIT 22"
    )

    def _target_rows(self, conn, ticker: str, session_date: str) -> list:
        """Target bars from the snapshot, else from the attached market store
        (any symbol and date loaded by ``scripts/m9_market_context.py``)."""
        rows = conn.execute(self._TARGET_SQL, (ticker, session_date)).fetchall()
        if rows and rows[0][0] == session_date:
            return rows
        if self.market_db is not None:
            try:
                with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True) as market:
                    stored = market.execute(self._TARGET_SQL, (ticker, session_date)).fetchall()
            except sqlite3.Error as exc:
                raise StoreReadError(self.market_db, "read market context OHLCV", str(exc)) from exc
            if stored and stored[0][0] == session_date:
                return stored
        return rows

    def _symbol_return(self, conn, symbol: str, session_date: str) -> float | None:
        return scope.symbol_return(conn, self.market_db, symbol, session_date)

    @staticmethod
    def _session_return(conn, symbol: str, session_date: str) -> float | None:
        return scope.session_return(conn, symbol, session_date)

    def _resolve_context(
        self, conn, ticker: str, session_date: str | None = None
    ) -> tuple[str | None, str | None, tuple[str, ...]]:
        """(benchmark, sector ETF, direct peers) for ``ticker``, by rule.

        Mapped targets use the peer map. Others fall back to same-SIC issuers,
        ranked by how closely their daily returns tracked the target over the
        ``PEER_CORRELATION_SESSIONS`` sessions before ``session_date`` (no
        look-ahead); a SIC code alone is too coarse (MA's 7389 also holds
        ACN and DASH)."""
        return scope.resolve_context(conn, self.market_db, self.peer_map, ticker, session_date)

    def _daily_returns(self, conn, symbol: str, session_date: str) -> dict[str, float]:
        return scope.daily_returns(conn, self.market_db, symbol, session_date)

    def _rank_by_correlation(
        self, conn, ticker: str, pool: list[str], session_date: str, min_corr: float
    ) -> list[str]:
        return scope.rank_by_correlation(conn, self.market_db, ticker, pool, session_date, min_corr)

    def _commodity(self, ticker: str) -> str | None:
        """The issuer's commodity proxy ETF: a per-target entry in the peer
        map, else the map's SIC band (gold ores -> GLD, oil & gas -> USO)."""
        return scope.commodity_proxy(self.market_db, self.peer_map, ticker)

    def _commodity_return(self, ticker: str, session_date: str) -> tuple[str | None, float | None]:
        return scope.commodity_return(self.market_db, self.peer_map, ticker, session_date)

    def _market_returns(
        self, conn, ticker: str, session_date: str
    ) -> tuple[str | None, float | None, str | None, float | None, dict[str, float | None]]:
        try:
            benchmark, sector, peers = self._resolve_context(conn, ticker, session_date)
            benchmark_ret = sector_ret = None
            if self.market_db is not None and (benchmark or sector):
                with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True) as market:
                    if benchmark:
                        benchmark_ret = self._session_return(market, benchmark, session_date)
                    if sector:
                        sector_ret = self._session_return(market, sector, session_date)
            peer_returns = {
                peer: self._symbol_return(conn, peer, session_date) for peer in peers
            }
        except sqlite3.Error as exc:
            path = self.market_db or self.db_path
            raise StoreReadError(path, "read market session context", str(exc)) from exc
        return (
            benchmark if benchmark_ret is not None else None,
            benchmark_ret,
            sector if sector_ret is not None else None,
            sector_ret,
            peer_returns,
        )

    def load_context_inputs(
        self,
        *,
        ticker: str,
        session_date: str,
        cutoff: str,
        information_window_start_at: str | None = None,
    ) -> ContextInputs:
        try:
            with sqlite3.connect(
                f"file:{self.db_path}?mode=ro&immutable=1", uri=True
            ) as conn:
                rows = self._target_rows(conn, ticker, session_date)
                market_context = self._market_returns(conn, ticker, session_date)
        except sqlite3.Error as exc:
            raise StoreReadError(self.db_path, "read snapshot context", str(exc)) from exc
        if not rows or rows[0][0] != session_date:
            raise ValueError(f"snapshot has no {ticker} close for {session_date}")
        commodity_ticker, commodity_return = self._commodity_return(ticker, session_date)
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
            benchmark_ticker=market_context[0],
            benchmark_return_pct=market_context[1],
            sector_ticker=market_context[2],
            sector_return_pct=market_context[3],
            peer_returns_by_ticker=market_context[4],
            scheduled_macro_flags=self._load_macro_flags(
                cutoff, information_window_start_at=information_window_start_at
            ),
            macro_source_available=self._macro_source_available(),
            commodity_ticker=commodity_ticker,
            commodity_return_pct=commodity_return,
            target_ex_dividend_cash=corp_actions.dividend(ticker, session_date),
            target_split_ratio=corp_actions.split_ratio(ticker, session_date),
        )

    def issuer_filings_in_window(
        self, *, ticker: str, information_window_start_at: str, cutoff_at: str
    ) -> int:
        """Count issuer 8-K/6-K or earnings filings inside the frozen window."""
        try:
            with sqlite3.connect(
                f"file:{self.db_path}?mode=ro&immutable=1", uri=True
            ) as conn:
                row = conn.execute(
                    """SELECT COUNT(DISTINCT filing_id)
                   FROM filings
                   WHERE ticker = ? AND julianday(filed_at) > julianday(?)
                     AND julianday(filed_at) <= julianday(?)
                     AND (form_type IN ('8-K', '6-K')
                          OR lower(COALESCE(items_json, '')) LIKE '%earnings%')""",
                    (ticker, information_window_start_at, cutoff_at),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StoreReadError(self.db_path, "read issuer filings in context window", str(exc)) from exc
        return int(row[0]) if row else 0

    def _macro_source_available(self) -> bool:
        if not self.major_series_ids:
            return False
        try:
            with sqlite3.connect(
                f"file:{self.db_path}?mode=ro&immutable=1", uri=True
            ) as conn:
                conn.execute("SELECT 1 FROM macro_observations LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            raise StoreReadError(self.db_path, "check macro_observations availability", str(exc)) from exc
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
            with sqlite3.connect(
                f"file:{self.db_path}?mode=ro&immutable=1", uri=True
            ) as conn:
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
        except sqlite3.Error as exc:
            raise StoreReadError(self.db_path, "read macro_observations", str(exc)) from exc
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
