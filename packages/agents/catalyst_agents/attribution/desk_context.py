"""Desk-style group decomposition of a session move (M3, class-B context).

A desk first asks how much of a stock's move its market, sector or commodity explains. This module
answers that from prices only: for each reference (S&P 500, the sector ETF, the issuer's commodity
proxy) it estimates the stock's beta over the prior LOOKBACK sessions (closes strictly before the
session date), the move that beta implies for the session, and the residual. No model is involved and
nothing after the session close is read.

v5/v6 dev: every missed ANSWER on v6 was a market, sector or commodity move. The comovement band
(ALIGNED = similar size) labelled high-beta names as idiosyncratic: NEM +5.8% on GLD +2.7% and DHR
+6.0% on XLV +3.5% are group moves once beta is taken into account.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

DESK_CONTEXT_VERSION = "desk_decomposition_v1"
LOOKBACK = 120
MIN_OBSERVATIONS = 60
MIN_REFERENCE_MOVE_PCT = 0.5  # a reference that barely moved explains nothing
GROUP_DRIVEN_SHARE = 0.6
MIXED_SHARE = 0.3

REFERENCE_KINDS = ("MARKET", "SECTOR", "PEERS", "COMMODITY")
MAX_PEERS = 8


def peers_for(ticker: str, peer_map: dict) -> list[str]:
    """Direct peers from the peer map, else same GICS sub-industry constituents (sector ETFs can be a poor
    proxy: XLC is mostly Meta and Alphabet, so AT&T's telecom peers are VZ and TMUS)."""
    ticker = ticker.upper()
    target = (peer_map.get("targets") or {}).get(ticker) or {}
    direct = [p for p in target.get("direct_peers") or [] if p != ticker]
    if direct:
        return direct[:MAX_PEERS]
    gics = ((peer_map.get("gics") or {}).get("tickers") or {})
    mine = gics.get(ticker)
    if not mine or len(mine) < 2:
        return []
    return sorted(t for t, v in gics.items() if t != ticker and len(v) >= 2 and v[1] == mine[1])[:MAX_PEERS]


@dataclass(frozen=True)
class ReferenceFit:
    kind: str  # MARKET | SECTOR | COMMODITY
    symbol: str
    beta: float
    observations: int
    reference_return_pct: float
    expected_pct: float
    residual_pct: float
    explained_share: float  # expected / actual; negative when the implied move has the wrong sign


@dataclass(frozen=True)
class Decomposition:
    ticker: str
    session_date: str
    target_return_pct: float | None
    fits: tuple[ReferenceFit, ...]
    best: ReferenceFit | None
    verdict: str  # GROUP_DRIVEN | MIXED | IDIOSYNCRATIC | UNAVAILABLE
    version: str = DESK_CONTEXT_VERSION

    def sentence(self) -> str:
        """Deterministic one-line reading, labelled as Catalyst's own analysis."""
        if self.best is None or self.target_return_pct is None:
            return ""
        b = self.best
        label = {"MARKET": "the S&P 500", "SECTOR": f"its sector ({b.symbol})",
                 "PEERS": f"its peers ({b.symbol})", "COMMODITY": f"its commodity proxy ({b.symbol})"}[b.kind]
        return (f"[Catalyst analysis, inferred] {self.ticker} {self.target_return_pct:+.1f}% moved with {label} "
                f"{b.reference_return_pct:+.1f}%: {LOOKBACK}-day beta {b.beta:.2f} implies {b.expected_pct:+.1f}%, "
                f"residual {b.residual_pct:+.1f}%.")


def _closes(conn: sqlite3.Connection, symbol: str, session_date: str, limit: int) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT date, close FROM ohlcv WHERE symbol = ? AND date <= ? AND close IS NOT NULL "
        "ORDER BY date DESC LIMIT ?", (symbol, session_date, limit + 2)).fetchall()
    return [(str(d), float(c)) for d, c in reversed(rows)]


def _returns(closes: list[tuple[str, float]]) -> dict[str, float]:
    return {closes[i][0]: (closes[i][1] / closes[i - 1][1] - 1.0) * 100.0
            for i in range(1, len(closes)) if closes[i - 1][1]}


def _fit(stock: dict[str, float], ref: dict[str, float], session_date: str, kind: str, symbol: str,
         target: float) -> ReferenceFit | None:
    if session_date not in ref:
        return None
    history = sorted(d for d in stock.keys() & ref.keys() if d < session_date)[-LOOKBACK:]
    if len(history) < MIN_OBSERVATIONS:
        return None
    xs = [ref[d] for d in history]
    ys = [stock[d] for d in history]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return None
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
    ref_today = ref[session_date]
    expected = beta * ref_today
    share = expected / target if target else 0.0
    return ReferenceFit(kind, symbol, round(beta, 4), len(history), round(ref_today, 4), round(expected, 4),
                        round(target - expected, 4), round(share, 4))


def _basket(conn: sqlite3.Connection, symbols: list[str], session_date: str) -> dict[str, float]:
    """Equal-weight daily return of the symbols that have a close on each date."""
    series = [_returns(_closes(conn, s, session_date, LOOKBACK + 5)) for s in symbols]
    dates = set().union(*(r.keys() for r in series)) if series else set()
    out = {}
    for d in dates:
        values = [r[d] for r in series if d in r]
        if len(values) >= max(1, len(series) // 2):
            out[d] = sum(values) / len(values)
    return out


def decompose(market_db: str | Path, ticker: str, session_date: str, *, sector_ticker: str | None,
              commodity_ticker: str | None, peers: list[str] | None = None,
              market_ticker: str = "SPY") -> Decomposition:
    """Beta decomposition of the session move against market, sector, peer and commodity references."""
    refs = [("MARKET", market_ticker), ("SECTOR", sector_ticker), ("COMMODITY", commodity_ticker)]
    with sqlite3.connect(f"file:{market_db}?mode=ro", uri=True) as conn:
        stock = _returns(_closes(conn, ticker, session_date, LOOKBACK + 5))
        target = stock.get(session_date)
        if target is None:
            return Decomposition(ticker, session_date, None, (), None, "UNAVAILABLE")
        fits = []
        for kind, symbol in refs:
            if not symbol:
                continue
            fit = _fit(stock, _returns(_closes(conn, symbol, session_date, LOOKBACK + 5)), session_date,
                       kind, symbol, target)
            if fit is not None:
                fits.append(fit)
        if peers:
            fit = _fit(stock, _basket(conn, list(peers), session_date), session_date, "PEERS",
                       ",".join(peers[:3]) + ("…" if len(peers) > 3 else ""), target)
            if fit is not None:
                fits.append(fit)
    movers = [f for f in fits if abs(f.reference_return_pct) >= MIN_REFERENCE_MOVE_PCT and f.explained_share > 0]
    best = max(movers, key=lambda f: min(f.explained_share, 1.5), default=None)
    share = best.explained_share if best else 0.0
    verdict = ("GROUP_DRIVEN" if share >= GROUP_DRIVEN_SHARE else "MIXED" if share >= MIXED_SHARE
               else "IDIOSYNCRATIC") if fits else "UNAVAILABLE"
    return Decomposition(ticker, session_date, round(target, 4), tuple(fits), best, verdict)


__all__ = ["DESK_CONTEXT_VERSION", "Decomposition", "ReferenceFit", "decompose", "peers_for"]
