"""Deterministic seven-class source classifier from the B3 contract."""

from __future__ import annotations

from urllib.parse import urlsplit

CLASSIFIER_VERSION = "1.0.0"

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
    "pr newswire": "corporate_press_release",
    "business wire": "corporate_press_release",
    "cnbc": "reported_news",
    "marketwatch": "reported_news",
    "reuters": "reported_news",
    "associated press": "reported_news",
    "bloomberg": "reported_news",
    "wall street journal": "reported_news",
    "barron's": "reported_news",
    "investor's business daily": "reported_news",
    "seeking alpha": "analysis_opinion",
    "the motley fool": "analysis_opinion",
    "zacks": "analysis_opinion",
    "chartmill": "analysis_opinion",
    "fintel": "analysis_opinion",
    "yahoo": "aggregated_unknown",
    "finnhub": "aggregated_unknown",
}


def _normalized_host(article_url: str | None, explicit_host: str | None) -> str:
    host = explicit_host or (urlsplit(article_url).hostname if article_url else "") or ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


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
    """Classify using source-kind precedence, then host, then publisher.

    Legacy keyword arguments remain accepted while B2 callers migrate to the
    binding ``source_kind/article_url/publisher`` interface.
    """
    kind = (source_kind or source_type or "").strip().lower()
    if kind in _SOURCE_KIND:
        return _SOURCE_KIND[kind]

    normalized_host = _normalized_host(article_url, host)
    if normalized_host in _HOST_CLASS:
        return _HOST_CLASS[normalized_host]

    normalized_publisher = (publisher or publisher_name or "").strip().lower()
    if normalized_publisher in _PUBLISHER_CLASS:
        return _PUBLISHER_CLASS[normalized_publisher]

    if "press release" in (article_category or "").lower():
        return "corporate_press_release"
    if (provider or "").lower() in {"fred", "bls", "bea"}:
        return "official_government"
    return "aggregated_unknown"
