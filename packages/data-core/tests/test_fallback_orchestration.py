from __future__ import annotations

from datetime import datetime, timezone

from catalyst_data.orchestrator import (
    NormalizedAsset,
    PrimaryFetchResult,
    should_trigger_fallback,
)


def _article(
    *,
    title: str = "Apple earnings beat",
    body_md: str = "A" * 240,
    source_type: str = "polygon_news",
    published_utc: datetime | None = None,
    url: str = "https://example.com/article",
    ticker_primary: str = "AAPL",
) -> NormalizedAsset:
    return NormalizedAsset(
        title=title,
        body_md=body_md,
        source_type=source_type,
        published_utc=published_utc or datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc),
        url=url,
        ticker_primary=ticker_primary,
    )


def test_empty_primary_with_big_move_triggers_fallback():
    result = PrimaryFetchResult(
        articles=[],
        connectivity_failure_count=0,
        price_move_pct=0.031,
    )

    assert should_trigger_fallback("AAPL", "2026-04-03", result) == (
        True,
        "empty_primary_with_big_move",
    )


def test_empty_primary_with_small_move_does_not_trigger_fallback():
    result = PrimaryFetchResult(
        articles=[],
        connectivity_failure_count=0,
        price_move_pct=0.02,
    )

    assert should_trigger_fallback("AAPL", "2026-04-03", result) == (False, None)


def test_all_primary_articles_failing_quality_triggers_fallback():
    result = PrimaryFetchResult(
        articles=[_article(body_md="short"), _article(body_md="tiny", url="https://example.com/2")],
        connectivity_failure_count=0,
        price_move_pct=0.01,
    )

    assert should_trigger_fallback("AAPL", "2026-04-03", result) == (
        True,
        "primary_all_fails_quality",
    )


def test_at_least_one_quality_passing_article_does_not_trigger_quality_fallback():
    result = PrimaryFetchResult(
        articles=[_article(body_md="short"), _article(url="https://example.com/pass")],
        connectivity_failure_count=0,
        price_move_pct=0.01,
    )

    assert should_trigger_fallback("AAPL", "2026-04-03", result) == (False, None)


def test_connectivity_failures_at_threshold_trigger_fallback():
    result = PrimaryFetchResult(
        articles=[_article()],
        connectivity_failure_count=3,
        price_move_pct=0.0,
    )

    assert should_trigger_fallback("AAPL", "2026-04-03", result) == (
        True,
        "primary_connectivity_failures",
    )
