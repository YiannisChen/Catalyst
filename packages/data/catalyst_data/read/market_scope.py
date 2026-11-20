"""Pure market and issuer scope resolution helpers for M9."""
from __future__ import annotations

from pathlib import Path

from catalyst_data.ingestion.universe import GICS_SECTOR_ETF
from catalyst_data.storage.connect import connect_readonly

PEER_CORRELATION_SESSIONS = 60
PEER_SIC_TIERS = ((4, 0.3), (3, 0.4), (2, 0.5))
GICS_MIN_CORRELATION = 0.3
MAX_SIC_PEERS = 4
MAX_PEER_SEARCHES = 4
COMMODITY_MIN_MOVE_PCT = 1.0
MARKET_PROXY = "SPY"
MARKET_MIN_MOVE_PCT = 1.0


def session_return(conn, symbol: str, session_date: str) -> float | None:
    rows = conn.execute(
        "SELECT date, close FROM ohlcv WHERE symbol = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 2",
        (symbol, session_date),
    ).fetchall()
    if len(rows) < 2 or rows[0][0] != session_date or not rows[1][1] or rows[0][1] is None:
        return None
    from catalyst_data.ingestion import corp_actions

    # Ex-dividend sessions count the cash back (bars are split- but not dividend-adjusted).
    return corp_actions.total_return_pct(rows[0][1], rows[1][1], symbol, session_date)


def symbol_return(conn, market_db: Path | None, symbol: str, session_date: str) -> float | None:
    value = session_return(conn, symbol, session_date)
    if value is None and market_db is not None:
        with connect_readonly(market_db, immutable=False, timeout_ms=10_000) as market:
            value = session_return(market, symbol, session_date)
    return value


def daily_returns(conn, market_db: Path | None, symbol: str, session_date: str) -> dict[str, float]:
    sql = (
        "SELECT date, close FROM ohlcv WHERE symbol = ? AND date < ? "
        "ORDER BY date DESC LIMIT ?"
    )
    args = (symbol, session_date, PEER_CORRELATION_SESSIONS + 1)
    rows = conn.execute(sql, args).fetchall()
    if len(rows) < PEER_CORRELATION_SESSIONS // 2 and market_db is not None:
        with connect_readonly(market_db, immutable=False, timeout_ms=10_000) as market:
            rows = market.execute(sql, args).fetchall()
    rows = list(reversed(rows))
    return {
        rows[i][0]: rows[i][1] / rows[i - 1][1] - 1.0
        for i in range(1, len(rows))
        if rows[i - 1][1]
    }


def rank_by_correlation(conn, market_db: Path | None, ticker: str, pool: list[str], session_date: str, min_corr: float) -> list[str]:
    target = daily_returns(conn, market_db, ticker, session_date)
    scored = []
    for peer in pool:
        other = daily_returns(conn, market_db, peer, session_date)
        days = sorted(set(target) & set(other))
        if len(days) < PEER_CORRELATION_SESSIONS // 2:
            continue
        xs = [target[d] for d in days]
        ys = [other[d] for d in days]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = sum((x - mx) ** 2 for x in xs)
        vy = sum((y - my) ** 2 for y in ys)
        if vx and vy:
            corr = cov / (vx * vy) ** 0.5
            if corr >= min_corr:
                scored.append((-corr, peer))
    return [peer for _, peer in sorted(scored)]


def resolve_context(conn, market_db: Path | None, peer_map: dict | None, ticker: str, session_date: str | None = None) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Return (benchmark, sector ETF, direct peers) using mapped, GICS, or SIC scope."""
    if peer_map is None:
        return None, None, ()
    benchmark = peer_map.get("benchmark")
    entry = peer_map.get("targets", {}).get(ticker)
    if entry is not None:
        return benchmark, entry.get("sector_etf"), tuple(entry.get("direct_peers", ()))

    gics = (peer_map or {}).get("gics", {}).get("tickers", {})
    if ticker not in gics and market_db is None:
        return benchmark, None, ()
    gics_sector = GICS_SECTOR_ETF.get(gics[ticker][0]) if ticker in gics else None
    available = {str(r[0]) for r in conn.execute("SELECT DISTINCT symbol FROM ohlcv")}
    if market_db is not None:
        with connect_readonly(market_db, immutable=False, timeout_ms=10_000) as market:
            available |= {str(r[0]) for r in market.execute("SELECT DISTINCT symbol FROM ohlcv")}
    if ticker in gics:
        sub_industry = gics[ticker][1]
        pool = sorted(
            candidate
            for candidate, (_, sub) in gics.items()
            if candidate != ticker and sub == sub_industry and candidate in available
        )
        if session_date is not None and pool:
            ranked = rank_by_correlation(
                conn, market_db, ticker, pool, session_date, GICS_MIN_CORRELATION
            )
            if ranked:
                return benchmark, gics_sector, tuple(ranked[:MAX_SIC_PEERS])
        elif session_date is None and pool:
            return benchmark, gics_sector, tuple(pool[:MAX_SIC_PEERS])
    if market_db is None:
        return benchmark, gics_sector, ()
    with connect_readonly(market_db, immutable=False, timeout_ms=10_000) as market:
        row = market.execute("SELECT sic FROM issuer_sic WHERE ticker = ?", (ticker,)).fetchone()
        if not row or not row[0]:
            return benchmark, gics_sector, ()
        sic = int(row[0])
        sector = gics_sector or next((band["etf"] for band in peer_map.get("sic_sector_fallback", ()) if band["sic_from"] <= sic <= band["sic_to"]), None)
        all_sic = [(str(r[0]), str(r[1])) for r in market.execute(
            "SELECT ticker, sic FROM issuer_sic WHERE ticker != ? AND sic IS NOT NULL", (ticker,)
        )]
    for digits, min_corr in PEER_SIC_TIERS:
        pool = sorted(t for t, code in all_sic if code[:digits] == str(sic)[:digits] and t in available)
        if not pool:
            continue
        if session_date is None:
            return benchmark, sector, tuple(pool[:MAX_SIC_PEERS])
        ranked = rank_by_correlation(conn, market_db, ticker, pool, session_date, min_corr)
        if ranked:
            return benchmark, sector, tuple(ranked[:MAX_SIC_PEERS])
    return benchmark, sector, ()


def commodity_proxy(market_db: Path | None, peer_map: dict | None, ticker: str) -> str | None:
    if peer_map is None:
        return None
    entry = peer_map.get("targets", {}).get(ticker) or {}
    if entry.get("commodity_proxy"):
        return entry["commodity_proxy"]
    if market_db is None:
        return None
    with connect_readonly(market_db, immutable=False, timeout_ms=10_000) as market:
        row = market.execute("SELECT sic FROM issuer_sic WHERE ticker = ?", (ticker,)).fetchone()
    if not row or not row[0]:
        return None
    sic = int(row[0])
    return next((band["proxy"] for band in peer_map.get("commodity_by_sic", ()) if band["sic_from"] <= sic <= band["sic_to"]), None)


def commodity_return(market_db: Path | None, peer_map: dict | None, ticker: str, session_date: str) -> tuple[str | None, float | None]:
    proxy = commodity_proxy(market_db, peer_map, ticker)
    if proxy is None or market_db is None:
        return None, None
    with connect_readonly(market_db, immutable=False, timeout_ms=10_000) as market:
        value = session_return(market, proxy, session_date)
    return (proxy, value) if value is not None else (None, None)


def commodity_aligned(target_return: float | None, commodity_ticker: str | None, commodity_return: float | None) -> bool:
    return bool(commodity_ticker and target_return is not None and commodity_return is not None and abs(commodity_return) >= COMMODITY_MIN_MOVE_PCT and (commodity_return > 0) == (target_return > 0))


def market_aligned(target_return: float | None, market_return: float | None) -> bool:
    return bool(target_return is not None and market_return is not None and abs(market_return) >= MARKET_MIN_MOVE_PCT and (market_return > 0) == (target_return > 0))
