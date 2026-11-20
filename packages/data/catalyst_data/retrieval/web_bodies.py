"""Attached news-body store (M9-1, ADR-011 C1 "attached database").

Public article bodies fetched by rule for headline-only assets live in a
separate SQLite file, never in the served snapshot. At retrieval time a
headline hit in the FTS window whose asset has a stored body is served as that
body's chunks: the headline is the lead, the body is the evidence.

Enabled by the ``CATALYST_NEWS_BODIES_DB`` environment variable (dev).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

from catalyst_data.storage.connect import connect_readonly
from catalyst_data.storage.errors import StoreReadError

ENV_VAR = "CATALYST_NEWS_BODIES_DB"
WEB_BODY_POLICY = "web_bodies_v1"
CHUNK_BYTES = 1_100
MAX_CHUNKS_PER_BODY = 2
# Chunks scanned per body for the paragraph that names the subject.
MAX_SCAN_ORDINAL = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_body_chunks (
    asset_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    chunk_id TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    source_class TEXT NOT NULL,
    eligible_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    capture_mode TEXT NOT NULL,
    PRIMARY KEY (asset_id, ordinal)
);
CREATE TABLE IF NOT EXISTS web_headlines (
    asset_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    eligible_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_web_headlines_ticker_time
    ON web_headlines (ticker, eligible_at);
"""


def chunk_text(text: str, limit: int = CHUNK_BYTES) -> list[str]:
    """Paragraph-bounded chunks of at most ``limit`` UTF-8 bytes (a longer
    paragraph becomes its own chunk and is truncated later by the pack cap)."""
    chunks: list[str] = []
    current = ""
    for paragraph in [p.strip() for p in text.split("\n\n") if p.strip()]:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if current and len(candidate.encode("utf-8")) > limit:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# Publisher is the site that serves the fetched body, never the headline
# feed's source field (Finnhub labels Stocktwits, 24/7 Wall St and others
# "Yahoo"). Unknown hosts fall back to the bare host name.
PUBLISHER_BY_HOST = {
    "finance.yahoo.com": "Yahoo Finance",
    "www.kitco.com": "Kitco (Reuters wire)",
    "www.fool.com": "The Motley Fool",
    "www.benzinga.com": "Benzinga",
    "stocktwits.com": "Stocktwits",
    "247wallst.com": "24/7 Wall St.",
    "www.globenewswire.com": "GlobeNewswire",
    "www.cnbc.com": "CNBC",
    "seekingalpha.com": "Seeking Alpha",
    "www.investopedia.com": "Investopedia",
    "www.proactiveinvestors.com": "Proactive Investors",
    "investorshub.advfn.com": "InvestorsHub (ADVFN)",
    "www.foxbusiness.com": "Fox Business",
    "www.nbcnews.com": "NBC News",
    "www.kiplinger.com": "Kiplinger",
    "www.euronews.com": "Euronews",
    "time.com": "TIME",
    "decrypt.co": "Decrypt",
    "beincrypto.com": "BeInCrypto",
    "www.trefis.com": "Trefis",
    "www.just-auto.com": "Just Auto",
    "www.cryptoprowl.com": "CryptoProwl",
    "www.sec.gov": "SEC filing",
}
# Hosts whose page is an aggregator shell, not the publisher's article.
EXCLUDED_HOSTS = frozenset({"finnhub.io"})


def publisher_for_url(url: str) -> str | None:
    """The serving site's name, or None for an excluded aggregator host."""
    host = urlparse(url).netloc.lower()
    if host in EXCLUDED_HOSTS:
        return None
    if host == "www.kitco.com" and "/news/off-the-wire/" not in urlparse(url).path:
        return "Kitco News"  # Kitco's own reporting, not the Reuters wire
    return PUBLISHER_BY_HOST.get(host, host.removeprefix("www."))


def build_store(db_path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Write body chunks; existing assets are never overwritten.

    Each row: asset_id, text, url, title, publisher, source_class,
    eligible_at, fetched_at, body_sha256, tickers.
    """
    from catalyst_data.ingestion.storage import store

    conn = store.open_writer(db_path)
    try:
        report = store.write_assets(conn, rows)
        return report.chunks
    finally:
        conn.close()


def store_path() -> Path | None:
    value = os.environ.get(ENV_VAR, "").strip()
    return Path(value) if value and Path(value).is_file() else None


def name_map_path() -> Path | None:
    """``sec_company_tickers.json`` beside the configured body store, whether or not the store file exists.

    A run that reads a migrated legacy cohort keeps only this file in its data root; looking it up through
    ``store_path()`` would silently lose the company-name map there."""
    value = os.environ.get(ENV_VAR, "").strip()
    return Path(value).parent / "sec_company_tickers.json" if value else None


def _missing_configured_store() -> StoreReadError:
    path = Path(os.environ[ENV_VAR]).expanduser()
    cause = FileNotFoundError(f"configured web body store does not exist: {path}")
    return StoreReadError(path, "open web body store", str(cause))


def bodies_for_assets(
    asset_ids: Sequence[str], max_ordinal: int = MAX_CHUNKS_PER_BODY
) -> dict[str, list[dict[str, Any]]]:
    """Stored chunks (ordinal at most ``max_ordinal``) keyed by asset id."""
    from catalyst_data.retrieval import legacy_read

    cohort = legacy_read.configured_reader()
    if cohort is not None:
        return cohort.chunks_for_assets(asset_ids, max_ordinal)
    path = store_path()
    unique = list(dict.fromkeys(asset_ids))
    if not unique:
        return {}
    if path is None:
        if os.environ.get(ENV_VAR, "").strip():
            raise _missing_configured_store()
        return {}
    conn = connect_readonly(path, timeout_ms=10_000, immutable=False)
    conn.row_factory = sqlite3.Row
    try:
        out: dict[str, list[dict[str, Any]]] = {}
        for start in range(0, len(unique), 400):
            batch = unique[start : start + 400]
            marks = ",".join("?" for _ in batch)
            for row in conn.execute(
                f"SELECT * FROM web_body_chunks WHERE asset_id IN ({marks}) "
                "AND ordinal <= ? ORDER BY asset_id, ordinal",
                (*batch, max_ordinal),
            ):
                out.setdefault(row["asset_id"], []).append(dict(row))
        return out
    finally:
        conn.close()


def session_bodies(
    tickers: Sequence[str], window_start_at: str, cutoff_at: str,
    max_ordinal: int = MAX_CHUNKS_PER_BODY,
    *, include_window_start: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Stored chunks for headlines tagged with any of ``tickers`` whose time is
    in ``(window_start_at, cutoff_at]``, newest first; keyed by asset id.

    Independent of the snapshot's FTS window, so headlines ingested after the
    snapshot (M9 v2 block 2) are served the same way.
    """
    from catalyst_data.retrieval import legacy_read

    cohort = legacy_read.configured_reader()
    if cohort is not None:
        if not tickers:
            return {}
        assets = cohort.session_assets(
            tickers, window_start_at, cutoff_at, include_window_start=include_window_start
        )
        found = cohort.chunks_for_assets(assets, max_ordinal)
        return {asset: found[asset] for asset in assets if asset in found}
    path = store_path()
    if not tickers:
        return {}
    if path is None:
        if os.environ.get(ENV_VAR, "").strip():
            raise _missing_configured_store()
        return {}
    conn = connect_readonly(path, timeout_ms=10_000, immutable=False)
    conn.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" for _ in tickers)
        assets = [
            row[0]
            for row in conn.execute(
                f"SELECT asset_id, MAX(eligible_at) AS t FROM web_headlines "
                f"WHERE ticker IN ({marks}) AND eligible_at {'>=' if include_window_start else '>'} ? AND eligible_at <= ? "
                "GROUP BY asset_id ORDER BY t DESC",
                (*tickers, window_start_at, cutoff_at),
            )
        ]
    finally:
        conn.close()
    found = bodies_for_assets(assets, max_ordinal)
    return {asset: found[asset] for asset in assets if asset in found}


_MOVE_VERB = (
    r"(?:rose|rises|rising|jumped|jumps|soared|soars|surged|surges|rallied|rallies|climbed|climbs|"
    r"gained|gains|popped|pops|advanced|fell|falls|falling|dropped|drops|slumped|slumps|sank|sinks|"
    r"tumbled|tumbles|plunged|plunges|slid|slides|declined|declines|tanked|tanks|lost|"
    r"moved (?:higher|lower)|traded (?:higher|lower))"
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_OTHER_DAY_MOVE = re.compile(
    _MOVE_VERB + r"\b(?:[^.;]|\.(?=\d)){0,80}?\b(?:on |last )?(?:(" + "|".join(_WEEKDAYS) + r"|yesterday)\b"
    r"|(" + "|".join(m[:3] for m in _MONTHS) + r")[a-z]*\.? (\d{1,2})\b)",
    re.IGNORECASE,
)


def other_session_move(text: str, session_weekday: str, session_date: str | None = None) -> bool:
    """Does ``text`` describe a price move on a day other than the session?

    "Shares of Newmont slumped on Wednesday" read on a Thursday session,
    "... fell yesterday", or "surged by 2.6% on January 5" read on January 8:
    the sentence reports another session's move.
    """
    for match in _OTHER_DAY_MOVE.finditer(text or ""):
        day, month, dom = match.group(1), match.group(2), match.group(3)
        if day:
            if day.lower() == "yesterday" or day.lower() != session_weekday.lower():
                return True
        elif session_date:
            month_no = [m[:3] for m in _MONTHS].index(month.lower()) + 1
            if (month_no, int(dom)) != (int(session_date[5:7]), int(session_date[8:10])):
                return True
    return False


_MONTH_ALT = "|".join(_MONTHS)
# A price (not a statistic) moving over a named other month: "oil prices rising
# in January", "the January oil rally". Data releases about another period
# ("the May CPI rose 0.5%", "payrolls rose ... since March") do not match.
_OTHER_PERIOD_MOVE = re.compile(
    r"\b(?:prices?|stocks?|shares|crude|oil|gold|silver|copper|index)\b[^.;]{0,60}?"
    r"\b(?:rose|rising|rallied|gained|jumped|surged|soared|climbed|fell|falling|dropped|"
    r"slid|sank|tumbled|plunged|declined|up|down)\b[^.;]{0,40}?\bin (" + _MONTH_ALT + r")\b"
    r"|\b(" + _MONTH_ALT + r")(?:'s)? (?:\w+ )?(?:rally|selloff|sell-off|surge|slump)\b",
    re.IGNORECASE,
)


def other_period_move(text: str, session_date: str | None) -> bool:
    """Does ``text`` report a price move over another month?

    "Brent crude prices up around 17% in January" or "the January oil rally"
    read on a February session: a recap of another period, not this
    session's cause. The session's own month does not count.
    """
    if not text or not session_date:
        return False
    for match in _OTHER_PERIOD_MOVE.finditer(text):
        month = (match.group(1) or match.group(2)).lower()
        if _MONTHS.index(month) + 1 != int(session_date[5:7]):
            return True
    return False


def prior_session_recap(
    chunks: Sequence[dict[str, Any]], session_open_at: str, session_weekday: str, session_date: str
) -> bool:
    """A body published before the session opened whose title or lead
    reports another session's move is a recap of the previous session, not
    evidence about this one."""
    if not chunks or str(chunks[0]["eligible_at"]) >= session_open_at:
        return False
    lead = next((c for c in chunks if c["ordinal"] == 1), chunks[0])
    head = str(lead["title"]) + ". " + " ".join(re.split(r"(?<=[.!?])\s+", str(lead["text"]))[:2])
    return other_session_move(head, session_weekday, session_date)


_GENERIC_FIRST_WORDS = frozenset(
    {"advanced", "american", "united", "first", "general", "international",
     "national", "global", "the"}
)


# Commodity proxy ETFs (M9 v2 commodity path): a headline is "about" the
# proxy when it names the commodity, not the fund.
COMMODITY_TERMS: dict[str, tuple[str, ...]] = {
    "GLD": ("gold", "bullion", "precious metals?"),
    "SLV": ("silver", "precious metals?"),
    "CPER": ("copper", "base metals?", "industrial metals?"),
    "USO": ("oil", "crude", "brent", "wti", "opec\\+?", "barrels?"),
    "UNG": ("natural gas", "lng", "henry hub"),
    # Market proxy: US market wraps, the Fed, rates and macro data.
    "SPY": ("wall st(?:reet)?", "s&p 500", "dow", "nasdaq", "u\\.?s\\.? stocks?",
            "stocks", "equities", "fed", "federal reserve", "fomc", "treasur(?:y|ies)",
            "yields?", "rate (?:cut|hike)s?", "inflation", "cpi", "payrolls", "jobs report",
            "tariffs?"),
}
# Sector ETFs: a headline or lead about the sector's stocks as a group (a
# session wrap's "utilities led", "chip stocks slid"). Group phrases only, so an
# article about one company in the sector does not count as sector news.
_GROUP = r"(?:stocks?|shares|sector|names|group|index|etfs?)"
SECTOR_TERMS: dict[str, tuple[str, ...]] = {
    "XLK": (rf"(?:tech(?:nology)?|software|hardware) {_GROUP}", r"tech sector", r"tech selloff", r"tech rally"),
    "SMH": (rf"(?:chip|chipmaker|semiconductor|semis?) {_GROUP}", r"chipmakers", r"semiconductors?"),
    "XLC": (rf"(?:communications?(?: services)?|media|internet|telecom) {_GROUP}",),
    "XLY": (rf"(?:consumer discretionary|retail|retailer|automaker|homebuilder|travel|restaurant) {_GROUP}",
            r"consumer discretionary", r"retailers", r"homebuilders"),
    "XLP": (rf"(?:consumer staples|staples|defensive|food|beverage|tobacco) {_GROUP}", r"consumer staples"),
    "XLE": (rf"(?:energy|oil|oil and gas|e&p) {_GROUP}", r"energy sector", r"oil majors"),
    "XLF": (rf"(?:financials?|bank|banking|lender|insurer|brokerage|payments?) {_GROUP}", r"financials",
            r"bank stocks", r"lenders"),
    "XLV": (rf"(?:health ?care|healthcare|pharma(?:ceutical)?|biotech(?:nology)?|medical|drug) {_GROUP}",
            r"drugmakers", r"health ?care sector"),
    "XLI": (rf"(?:industrials?|defen[cs]e|aerospace|airline|transport(?:ation)?|machinery) {_GROUP}",
            r"industrials", r"defen[cs]e contractors"),
    "XLB": (rf"(?:materials?|chemicals?|mining|miners?|steel) {_GROUP}", r"materials sector", r"miners"),
    "XLU": (rf"(?:utilit(?:y|ies)|power|electric(?:ity)?) {_GROUP}", r"utilities", r"power producers"),
    "XLRE": (rf"(?:real estate|reits?|property) {_GROUP}", r"reits"),
}
# Subjects named by terms rather than by a company name.
TERM_SUBJECTS = {**COMMODITY_TERMS, **SECTOR_TERMS}
# Market-proxy titles about another market are not US market news.
_NON_US_MARKET = re.compile(
    r"\b(?:tsx|canadian dollar|toronto|ecb|bank of canada|bank of england|ftse|"
    r"emerging market|india|china's|japan|europe(?:an)? shares|asian shares)\b",
    re.IGNORECASE,
)


def subject_matcher(ticker: str, *, store_db: str | Path | None = None):
    """A predicate: does a headline name this company?

    Matches the ticker as a word (case-sensitive) or the first word of the SEC
    company name (``sec_company_tickers.json`` next to the store). A roundup
    that merely lists the ticker among others is filtered elsewhere by the
    window; this keeps articles *about* another company (c02, c06) out.
    """
    if ticker in TERM_SUBJECTS:
        terms = re.compile(
            r"\b(?:" + "|".join(TERM_SUBJECTS[ticker]) + r")\b", re.IGNORECASE
        )
        if ticker == "SPY":
            return lambda title: bool(terms.search(title)) and not _NON_US_MARKET.search(title)
        return lambda title: bool(terms.search(title))
    names: list[str] = []
    mapping_file = (
        Path(store_db).parent / "sec_company_tickers.json" if store_db is not None else name_map_path()
    )
    if mapping_file is not None and mapping_file.is_file():
        data = json.loads(mapping_file.read_text())
        for entry in data.values():
            if entry.get("ticker") == ticker:
                first = re.sub(r"[^a-z]", "", str(entry["title"]).split()[0].lower())
                if len(first) >= 4 and first not in _GENERIC_FIRST_WORDS:
                    names.append(first)
                break
    ticker_re = re.compile(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])")
    name_res = [re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE) for name in names]

    def matches(title: str) -> bool:
        return bool(ticker_re.search(title)) or any(r.search(title) for r in name_res)

    return matches


# Proxy subjects (a commodity, the US market, a sector ETF) are named by terms, so a wire
# whose headline is about the session ("miners, industrials boost") but whose
# lead states the copper move counts. A company must be named in the title:
# an article about another company that mentions it in its lead ("Alphabet
# raised guidance as it competes with Microsoft") is not about it (c02, c06).
LEAD_CHARS = CHUNK_BYTES


def names_subject(subject: str, title: str, lead: str, matcher=None) -> bool:
    """The intake and retrieval subject rule: the title, or a proxy's lead."""
    about = matcher or subject_matcher(subject)
    if about(title):
        return True
    return subject in TERM_SUBJECTS and about(" ".join(lead[:LEAD_CHARS].split()))


def tickers_json_list(value: Any) -> tuple[str, ...]:
    try:
        return tuple(json.loads(value)) if isinstance(value, str) else tuple(value or ())
    except (TypeError, ValueError):
        return ()


__all__ = [
    "COMMODITY_TERMS",
    "ENV_VAR",
    "WEB_BODY_POLICY",
    "bodies_for_assets",
    "build_store",
    "chunk_text",
    "publisher_for_url",
    "session_bodies",
    "store_path",
    "subject_matcher",
]
