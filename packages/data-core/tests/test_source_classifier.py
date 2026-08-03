"""Tests for source classifier — 7-class static algorithm."""
from __future__ import annotations

import pytest


def test_source_classifier_has_seven_classes():
    """The classifier recognizes exactly 7 source classes."""
    from catalyst_data.corpus.source_classifier import CLASSES, CLASSIFIER_VERSION

    assert len(CLASSES) == 7
    assert CLASSIFIER_VERSION == "1.1.0"


def test_ohlcv_is_structured_market_data():
    """OHLCV data → structured_market_data."""
    from catalyst_data.corpus.source_classifier import classify

    result = classify(source_type="ohlcv", provider="polygon")
    assert result == "structured_market_data"


def test_fred_is_official_government():
    """FRED data → official_government."""
    from catalyst_data.corpus.source_classifier import classify

    result = classify(source_type="fred", provider="fred")
    assert result == "official_government"


def test_sec_filing_is_issuer_disclosure():
    """SEC filings → issuer_disclosure."""
    from catalyst_data.corpus.source_classifier import classify

    result = classify(source_type="filing", provider="sec")
    assert result == "issuer_disclosure"


def test_unresolved_news_is_aggregated_unknown():
    """Unresolved news has no invented editorial origin."""
    from catalyst_data.corpus.source_classifier import classify

    result = classify(source_type="news", provider="polygon")
    assert result == "aggregated_unknown"


def test_finnhub_press_release():
    """Finnhub press releases → corporate_press_release."""
    from catalyst_data.corpus.source_classifier import classify

    result = classify(
        source_type="news",
        provider="finnhub",
        publisher_name="PR Newswire",
        article_category="press release",
    )
    assert result == "corporate_press_release"


def test_unknown_falls_back_to_aggregated():
    """Unknown source types → aggregated_unknown."""
    from catalyst_data.corpus.source_classifier import classify

    result = classify(source_type="unknown_type", provider="unknown")
    assert result == "aggregated_unknown"


def test_fmp_raw_is_not_chunked():
    """FMP raw statements → classified but not chunked (should_chunk returns False)."""
    from catalyst_data.corpus.source_classifier import classify
    from catalyst_data.corpus.profile import should_chunk

    assert not should_chunk("fmp_raw")
    result = classify(source_type="fmp_raw", provider="fmp")
    assert result in {
        "structured_market_data", "issuer_disclosure", "aggregated_unknown"
    }


def test_all_non_searchable_envelopes_are_rejected_case_insensitively():
    from catalyst_data.corpus.profile import should_chunk

    for source_type in (
        "OHLCV", "market_data", "fred_observations", "fmp_statement",
        "sec_submissions_manifest", "provider_envelope", "raw_json",
    ):
        assert not should_chunk(source_type), source_type


def test_all_classes_are_valid():
    """Every class in CLASSES is a valid classification."""
    from catalyst_data.corpus.source_classifier import CLASSES, classify

    valid = set(CLASSES)
    assert "reported_news" in valid
    assert "issuer_disclosure" in valid
    assert "official_government" in valid
    assert "structured_market_data" in valid
    assert "corporate_press_release" in valid
    assert "analysis_opinion" in valid
    assert "aggregated_unknown" in valid
    assert len(valid) == 7


def test_classifier_contract_precedence_and_exact_mappings():
    """The binding §5 mapping wins by source kind, then host, then publisher."""
    from catalyst_data.corpus.source_classifier import classify

    assert classify("sec_filing") == "official_government"
    assert classify("issuer_release") == "issuer_disclosure"
    assert classify("news", article_url="https://www.marketwatch.com/a") == "reported_news"
    assert classify("news", article_url="https://finance.yahoo.com/a") == "aggregated_unknown"
    assert classify("news", article_url="https://www.fintel.io/a") == "analysis_opinion"
    assert classify("news", publisher="Business Wire") == "corporate_press_release"
    assert classify("news", publisher="Investor's Business Daily") == "reported_news"


@pytest.mark.parametrize(
    ("publisher", "expected"),
    [
        ("CNBC", "reported_news"),
        ("MarketWatch", "reported_news"),
        ("SeekingAlpha", "analysis_opinion"),
        ("Yahoo", "aggregated_unknown"),
        ("Finnhub", "aggregated_unknown"),
        ("Unmapped Publisher", "aggregated_unknown"),
    ],
)
def test_finnhub_proxy_uses_trusted_publisher_taxonomy(publisher, expected):
    from catalyst_data.corpus.source_classifier import classify

    assert classify(
        source_type="news",
        article_url="https://finnhub.io/api/news?id=proxy",
        publisher=publisher,
    ) == expected


@pytest.mark.parametrize(
    ("publisher", "expected"),
    [
        ("CNBC News", "reported_news"),
        ("marketwatch.com", "reported_news"),
        ("Reuters", "reported_news"),
        ("AP", "reported_news"),
        ("Bloomberg", "reported_news"),
        ("WSJ", "reported_news"),
        ("Dow Jones Newswires", "reported_news"),
        ("Benzinga", "reported_news"),
        ("Investing.com", "reported_news"),
        ("Seeking Alpha", "analysis_opinion"),
        ("The Motley Fool", "analysis_opinion"),
        ("Zacks Investment Research", "analysis_opinion"),
        ("ChartMill.com", "analysis_opinion"),
        ("Fintel.io", "analysis_opinion"),
        ("GlobeNewswire Inc.", "corporate_press_release"),
        ("PR Newswire", "corporate_press_release"),
        ("Business Wire", "corporate_press_release"),
        ("Yahoo Finance", "aggregated_unknown"),
        ("Finnhub News", "aggregated_unknown"),
    ],
)
def test_publisher_taxonomy_is_case_and_variant_stable(publisher, expected):
    from catalyst_data.corpus.source_classifier import classify

    assert classify(source_type="news", publisher=publisher) == expected
    assert classify(source_type="news", publisher=publisher.upper()) == expected


def test_proxy_precedence_does_not_change_direct_host_or_source_kind_precedence():
    from catalyst_data.corpus.source_classifier import classify

    assert classify("news", article_url="https://www.marketwatch.com/story") == "reported_news"
    assert classify("news", article_url="https://finance.yahoo.com/story") == "aggregated_unknown"
    assert classify("news", article_url="https://www.fintel.io/story") == "analysis_opinion"
    assert classify("sec_filing", article_url="https://finnhub.io/api/news", publisher="CNBC") == "official_government"
    assert classify("filing", article_url="https://finnhub.io/api/news", publisher="SeekingAlpha") == "issuer_disclosure"


def test_classifier_is_deterministic_for_equivalent_inputs():
    from catalyst_data.corpus.source_classifier import classify

    inputs = {
        "source_type": "news",
        "article_url": "https://FINNHUB.IO/api/news?id=proxy",
        "publisher_name": "  Seeking Alpha  ",
    }
    assert {classify(**inputs) for _ in range(100)} == {"analysis_opinion"}
