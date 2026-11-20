"""Public web evidence reads and source classification for runtime consumers.

The returned cache rows retain their collector-defined dictionaries for now;
this module provides a stable import boundary while that storage contract is
being formalized.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

_ANALYSIS_OPINION_HOSTS = frozenset({
    "www.fool.com", "seekingalpha.com", "www.seekingalpha.com", "investopedia.com",
    "www.investopedia.com", "marketbeat.com", "www.marketbeat.com",
    "stocktwits.com", "www.stocktwits.com", "tipranks.com", "www.tipranks.com",
})


def cached_discovery_v2_rows(ticker: str, session_date: str) -> list[dict[str, Any]]:
    """Read cached v2 discovery rows associated with a ticker and session."""
    from catalyst_data.ingestion.collectors import web_discovery_v2

    return web_discovery_v2.cached_rows_for_ticker_session(ticker, session_date)


def cached_roundup_segments(
    ticker: str,
    session_date: str,
    *,
    start_at: str,
    end_at: str,
) -> list[dict[str, Any]]:
    """Read cached roundup segments within the supplied publication window."""
    from catalyst_data.ingestion.collectors import cnbc_roundups

    return cnbc_roundups.cached_segments(
        ticker, session_date, start_at=start_at, end_at=end_at
    )


def web_source_class(canonical_url: str, source_class: str | None = None) -> str:
    """Resolve a web source class using the canonical host policy."""
    if source_class:
        return source_class

    from catalyst_data.ingestion.collectors import fetch

    host = (urlsplit(canonical_url).hostname or "").lower()
    return fetch.SOURCE_CLASS_BY_HOST.get(
        host,
        "analysis_opinion" if host in _ANALYSIS_OPINION_HOSTS else fetch.DEFAULT_SOURCE_CLASS,
    )


__all__ = [
    "cached_discovery_v2_rows",
    "cached_roundup_segments",
    "web_source_class",
]
