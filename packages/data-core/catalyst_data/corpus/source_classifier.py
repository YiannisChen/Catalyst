"""Deterministic seven-class source classifier from the B3 contract."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

CLASSIFIER_VERSION = "1.1.0"

CLASSES = (
    "structured_market_data",
    "official_government",
    "issuer_disclosure",
    "corporate_press_release",
    "reported_news",
    "analysis_opinion",
    "aggregated_unknown",
)

_SOURCE_KIND = {
    "ohlcv": "structured_market_data",
    "market_data": "structured_market_data",
    "fmp_raw": "structured_market_data",
    "sec": "official_government",
    "sec_filing": "official_government",
    "fred": "official_government",
    "issuer_ir": "issuer_disclosure",
    "issuer_release": "issuer_disclosure",
    # Legacy storage value retained for callers predating the B3 taxonomy.
    "filing": "issuer_disclosure",
}

_HOST_CLASS = {
    "globenewswire.com": "corporate_press_release",
    "prnewswire.com": "corporate_press_release",
    "businesswire.com": "corporate_press_release",
    "cnbc.com": "reported_news",
    "marketwatch.com": "reported_news",
    "reuters.com": "reported_news",
    "apnews.com": "reported_news",
    "bloomberg.com": "reported_news",
    "wsj.com": "reported_news",
    "investors.com": "reported_news",
    "barrons.com": "reported_news",
    "seekingalpha.com": "analysis_opinion",
    "fool.com": "analysis_opinion",
    "zacks.com": "analysis_opinion",
    "chartmill.com": "analysis_opinion",
    "fintel.io": "analysis_opinion",
    "finance.yahoo.com": "aggregated_unknown",
    "yahoo.com": "aggregated_unknown",
    "finnhub.io": "aggregated_unknown",
}

_PUBLISHER_CLASS = {
    "globenewswire": "corporate_press_release",
    "globenewswireinc": "corporate_press_release",
    "prnewswire": "corporate_press_release",
    "businesswire": "corporate_press_release",
    "cnbc": "reported_news",
    "cnbcnews": "reported_news",
    "marketwatch": "reported_news",
    "marketwatchcom": "reported_news",
    "reuters": "reported_news",
    "ap": "reported_news",
    "associated press": "reported_news",
    "associatedpress": "reported_news",
    "apnews": "reported_news",
    "bloomberg": "reported_news",
    "wsj": "reported_news",
    "wallstreetjournal": "reported_news",
    "dowjones": "reported_news",
    "dowjonesnews": "reported_news",
    "dowjonesnewswires": "reported_news",
    "benzinga": "reported_news",
    "investing": "reported_news",
    "investingcom": "reported_news",
    "barrons": "reported_news",
    "investorsbusinessdaily": "reported_news",
    "seekingalpha": "analysis_opinion",
    "themotleyfool": "analysis_opinion",
    "motleyfool": "analysis_opinion",
    "zacks": "analysis_opinion",
    "zacksinvestmentresearch": "analysis_opinion",
    "chartmill": "analysis_opinion",
    "chartmillcom": "analysis_opinion",
    "fintel": "analysis_opinion",
    "fintelio": "analysis_opinion",
    "yahoo": "aggregated_unknown",
    "yahoofinance": "aggregated_unknown",
    "finnhub": "aggregated_unknown",
    "finnhubnews": "aggregated_unknown",
}

_PROXY_HOSTS = frozenset({"finnhub.io"})


def _normalized_host(article_url: str | None, explicit_host: str | None) -> str:
    host = explicit_host or (urlsplit(article_url).hostname if article_url else "") or ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _normalized_publisher(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def classify(
    source_kind: str | None = None,
    article_url: str | None = None,
    publisher: str | None = None,
    *,
    source_type: str | None = None,
    provider: str | None = None,
    publisher_name: str | None = None,
    article_category: str | None = None,
    host: str | None = None,
) -> str:
    """Classify with source-kind precedence and proxy-scoped publisher taxonomy.

    Legacy keyword arguments remain accepted while B2 callers migrate to the
    binding ``source_kind/article_url/publisher`` interface.
    """
    kind = (source_kind or source_type or "").strip().lower()
    if kind in _SOURCE_KIND:
        return _SOURCE_KIND[kind]

    normalized_host = _normalized_host(article_url, host)
    normalized_publisher = _normalized_publisher(publisher or publisher_name)
    if normalized_host in _PROXY_HOSTS:
        return _PUBLISHER_CLASS.get(normalized_publisher, "aggregated_unknown")

    if normalized_host in _HOST_CLASS:
        return _HOST_CLASS[normalized_host]

    if normalized_publisher in _PUBLISHER_CLASS:
        return _PUBLISHER_CLASS[normalized_publisher]

    if "press release" in (article_category or "").lower():
        return "corporate_press_release"
    if (provider or "").lower() in {"fred", "bls", "bea"}:
        return "official_government"
    return "aggregated_unknown"
