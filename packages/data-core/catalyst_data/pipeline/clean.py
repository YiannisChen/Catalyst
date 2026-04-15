from __future__ import annotations

import time
from typing import Any

from catalyst_data.dedup.hard import deduplicate_articles
from catalyst_data.pipeline.stages import StageResult


def _clean_news(raw_data: dict) -> list[dict]:
    """Normalize and deduplicate news articles.

    Dedup uses the single authoritative implementation in ``dedup.hard``
    (title fingerprint + 2-hour time window).
    """
    if isinstance(raw_data, dict) and len(raw_data) == 1:
        only_value = next(iter(raw_data.values()))
        if isinstance(only_value, dict):
            raw_data = only_value

    articles: Any = (
        raw_data.get("articles")
        or raw_data.get("results")
        or (raw_data if isinstance(raw_data, list) else [])
    )
    if isinstance(articles, dict):
        articles = [articles]
    elif not isinstance(articles, list):
        articles = []

    # Filter non-dict entries before dedup
    articles = [a for a in articles if isinstance(a, dict)]

    return deduplicate_articles(articles)


def _clean_fundamentals(raw_data: dict) -> dict[str, Any]:
    """Merge multi-endpoint financial data, taking the first item from lists."""
    merged: dict[str, Any] = {}
    for key, value in raw_data.items():
        if isinstance(value, list) and value:
            merged[key] = value[0]
        else:
            merged[key] = value
    return merged


def run_clean(raw_data: dict, source_type: str) -> StageResult:
    """Normalise and deduplicate raw fetched data.

    Dispatches to a source-specific cleaner based on *source_type*.
    """
    t0 = time.perf_counter()

    try:
        if "news" in source_type:
            cleaned = _clean_news(raw_data)
        elif "fmp" in source_type or "fundamentals" in source_type:
            cleaned = _clean_fundamentals(raw_data)
        else:
            cleaned = raw_data

        elapsed = (time.perf_counter() - t0) * 1000
        return StageResult(stage="clean", ok=True, data=cleaned, latency_ms=elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - t0) * 1000
        return StageResult(
            stage="clean", ok=False, error=str(exc), latency_ms=elapsed
        )
