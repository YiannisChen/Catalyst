"""Read-only status of the M9 data required for one closed session."""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from catalyst_data.canonical.temporal import build_temporal_identity, utc_iso_z
from catalyst_data.ingestion import ledger, scope
from catalyst_data.ingestion.collectors import market
from catalyst_data.retrieval.web_bodies import names_subject, subject_matcher

# Ledger sources that must both be exhausted before a proxy's news (commodity
# or SPY) counts as unavailable; company subjects use one combined source.
COMPANY_SOURCES = ("finnhub+polygon",)
PROXY_SOURCES = ("finnhub", "kitco")
# v2 adds the sector ETF subject (session wraps about the sector).
DISCOVERY_SOURCE = "discovery_v2"


@dataclass(frozen=True)
class Item:
    name: str
    kind: str
    present: bool
    detail: str = ""


@dataclass
class SessionDataStatus:
    ticker: str
    session_date: str
    closed: bool
    scope: dict[str, Any] = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [item.name for item in self.items if not item.present]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "session_date": self.session_date,
            "closed": self.closed,
            "scope": self.scope,
            "items": [asdict(item) for item in self.items],
            "missing": self.missing,
        }


def _open_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    from catalyst_data.storage.connect import connect_readonly
    return connect_readonly(path, immutable=True if immutable else False, timeout_ms=10_000)


def _target_bars(conn: sqlite3.Connection, ticker: str, session_date: str) -> bool:
    rows = conn.execute(
        "SELECT date FROM ohlcv WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 22",
        (ticker, session_date),
    ).fetchall()
    return len(rows) == 22 and rows[0][0] == session_date


def _prior_bar_count(conn: sqlite3.Connection, ticker: str, session_date: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM ohlcv WHERE symbol=? AND date<?",
        (ticker, session_date),
    ).fetchone()
    return int(row[0])


def _has_body(
    conn: sqlite3.Connection,
    *,
    subject: str,
    start: str,
    cutoff: str,
) -> bool:
    matches = subject_matcher(subject)
    assets = conn.execute(
        "SELECT DISTINCT h.asset_id FROM web_headlines h "
        "WHERE h.ticker=? AND h.eligible_at>? AND h.eligible_at<=?",
        (subject, start, cutoff),
    ).fetchall()
    for (asset_id,) in assets:
        row = conn.execute(
            "SELECT title, text FROM web_body_chunks WHERE asset_id=? AND ordinal=1",
            (asset_id,),
        ).fetchone()
        if row is not None and names_subject(subject, str(row[0]), str(row[1]), matches):
            return True
    return False


def _has_sec_filing(
    conn: sqlite3.Connection | None,
    *,
    ticker: str,
    session_date: str,
    start: str,
    cutoff: str,
) -> bool:
    if conn is None:
        return False
    assets = conn.execute(
        "SELECT DISTINCT h.asset_id, b.canonical_url FROM web_headlines h "
        "JOIN web_body_chunks b ON b.asset_id=h.asset_id AND b.ordinal=1 "
        "WHERE h.ticker=? AND h.eligible_at>? AND h.eligible_at<=?",
        (ticker, start, cutoff),
    ).fetchall()
    if any(urlparse(str(url)).netloc.lower() == "www.sec.gov" for _, url in assets):
        return True
    return ledger.completed_attempt_after(conn, ticker, session_date, "sec", cutoff, latest_only=True)


def _news_present(
    conn: sqlite3.Connection | None,
    *,
    subject: str,
    session_date: str,
    start: str,
    cutoff: str,
    sources: tuple[str, ...],
    now: datetime | None,
) -> tuple[bool, str]:
    if conn is None:
        return False, "NO_BODY_STORE"
    if _has_body(conn, subject=subject, start=start, cutoff=cutoff):
        return True, "body"
    cached = all(
        ledger.negative_cache_hit(
            conn,
            subject=subject,
            session_date=session_date,
            source=source,
            now=now,
        )
        for source in sources
    )
    return (True, "negative_cache") if cached else (False, "missing")


def session_status(
    ticker: str,
    session_date: str,
    *,
    snapshot_db: Path | None,
    market_db: Path | None,
    bodies_db: Path | None,
    peer_map: dict | None,
    now: datetime | None = None,
) -> SessionDataStatus:
    """Report the read-only M9 data coverage for ``ticker`` and session."""
    status = SessionDataStatus(ticker=ticker, session_date=session_date, closed=False)
    if not market.session_closed(session_date, now):
        status.items.append(Item("session", "scope", False, "SESSION_NOT_CLOSED"))
        return status
    status.closed = True

    snapshot_conn = (
        _open_readonly(snapshot_db, immutable=True)
        if snapshot_db is not None
        else sqlite3.connect(":memory:")
    )
    if snapshot_db is None:
        snapshot_conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    market_conn = _open_readonly(market_db) if market_db is not None else None
    bodies_conn = _open_readonly(bodies_db) if bodies_db is not None else None
    try:
        target_store: sqlite3.Connection | None = None
        target_store_name = "missing"
        if _target_bars(snapshot_conn, ticker, session_date):
            target_store, target_store_name = snapshot_conn, "snapshot"
        elif market_conn is not None and _target_bars(market_conn, ticker, session_date):
            target_store, target_store_name = market_conn, "market"
        status.items.append(
            Item("bars:target", "bars", target_store is not None, target_store_name)
        )

        benchmark, sector, peers = scope.resolve_context(
            snapshot_conn, market_db, peer_map, ticker, session_date
        )
        proxy = scope.commodity_proxy(market_db, peer_map, ticker)
        status.scope = {
            "benchmark": benchmark,
            "sector": sector,
            "peers": list(peers),
            "proxy": proxy,
        }

        mapped = ticker in (peer_map or {}).get("targets", {})
        if not mapped:
            sic_present = False
            if market_conn is not None:
                row = market_conn.execute(
                    "SELECT sic FROM issuer_sic WHERE ticker=?", (ticker,)
                ).fetchone()
                sic_present = row is not None and row[0] is not None
            status.items.append(Item("sic:target", "sic", sic_present))
            history_present = _prior_bar_count(snapshot_conn, ticker, session_date) >= 61
            if not history_present and market_conn is not None:
                history_present = _prior_bar_count(market_conn, ticker, session_date) >= 61
            status.items.append(
                Item("bars:target_history", "bars", history_present)
            )

        status.items.append(
            Item(
                "scope:peers",
                "scope",
                bool(peers),
                ",".join(peers) if peers else "NO_PEERS_RESOLVED",
            )
        )

        market_return = (
            scope.session_return(market_conn, "SPY", session_date)
            if market_conn is not None
            else None
        )
        proxy_return = (
            scope.session_return(market_conn, proxy, session_date)
            if market_conn is not None and proxy is not None
            else None
        )
        target_return = (
            scope.session_return(target_store, ticker, session_date)
            if target_store is not None
            else None
        )
        status.scope.update(
            target_return=target_return,
            market_return=market_return,
            proxy_return=proxy_return,
        )

        for symbol in (benchmark, sector, proxy):
            if symbol is None or symbol in {
                item.name.removeprefix("bars:") for item in status.items
            }:
                continue
            present = (
                market_conn is not None
                and scope.session_return(market_conn, symbol, session_date) is not None
            )
            status.items.append(Item(f"bars:{symbol}", "bars", present))
        peer_returns: dict[str, float | None] = {}
        for peer in peers:
            peer_return = scope.symbol_return(snapshot_conn, market_db, peer, session_date)
            peer_returns[peer] = peer_return
            status.items.append(
                Item(f"bars:peer:{peer}", "bars", peer_return is not None)
            )

        identity = build_temporal_identity(session_date)
        start = utc_iso_z(identity.information_window_start_at)
        cutoff = utc_iso_z(identity.cutoff_at)
        no_store_detail = "NO_BODY_STORE" if bodies_conn is None else ""
        sec_present = _has_sec_filing(
            bodies_conn, ticker=ticker, session_date=session_date,
            start=start, cutoff=cutoff,
        )
        status.items.append(
            Item(
                "news:target",
                "news",
                *_news_present(
                    bodies_conn,
                    subject=ticker,
                    session_date=session_date,
                    start=start,
                    cutoff=cutoff,
                    sources=COMPANY_SOURCES,
                    now=now,
                ),
            )
        )
        status.items.append(
            Item(
                "filings:target", "news",
                sec_present,
                "SEC filing or completed intake" if sec_present else "missing",
            )
        )
        news_peers = sorted(
            peer for peer, value in peer_returns.items() if value is not None
        )[: scope.MAX_PEER_SEARCHES]
        for peer in news_peers:
            present, detail = _news_present(
                bodies_conn,
                subject=peer,
                session_date=session_date,
                start=start,
                cutoff=cutoff,
                sources=COMPANY_SOURCES,
                now=now,
            )
            status.items.append(Item(f"news:peer:{peer}", "news", present, detail))

        if scope.commodity_aligned(target_return, proxy, proxy_return):
            present, detail = _news_present(
                bodies_conn,
                subject=proxy,
                session_date=session_date,
                start=start,
                cutoff=cutoff,
                sources=PROXY_SOURCES,
                now=now,
            )
            status.items.append(Item(f"news:proxy:{proxy}", "news", present, detail))
        if scope.market_aligned(target_return, market_return):
            present, detail = _news_present(
                bodies_conn,
                subject="SPY",
                session_date=session_date,
                start=start,
                cutoff=cutoff,
                sources=PROXY_SOURCES,
                now=now,
            )
            status.items.append(Item("news:market", "news", present, detail))

        # Session discovery (Benzinga archive + Kitco session wires) runs once
        # per session after the cutoff, whatever the ticker feeds returned.
        status.scope["news_peers"] = list(news_peers)
        discovered = bodies_conn is not None and ledger.completed_attempt_after(
            bodies_conn, ticker, session_date, DISCOVERY_SOURCE, cutoff
        )
        status.items.append(Item("news:discovery", "news", discovered,
                                 "attempted" if discovered else "missing"))

        # Keep the NO_BODY_STORE detail explicit on all applicable news items.
        if no_store_detail:
            status.items = [
                Item(item.name, item.kind, item.present, no_store_detail)
                if item.kind == "news"
                else item
                for item in status.items
            ]
        return status
    finally:
        snapshot_conn.close()
        if market_conn is not None:
            market_conn.close()
        if bodies_conn is not None:
            bodies_conn.close()
