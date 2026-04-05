from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime
from typing import Any

from catalyst_data.pipeline.stages import StageResult


def _compute_dedup_fingerprint(title: str, published_utc: str) -> str:
    """Fingerprint an article by normalised title and 2-hour time window."""
    title_norm = re.sub(r"[^\w\s]", "", title).lower().strip()
    dt = datetime.fromisoformat(published_utc.replace("Z", "+00:00"))
    window = dt.replace(
        hour=(dt.hour // 2) * 2, minute=0, second=0, microsecond=0
    )
    raw = f"{title_norm}|{window.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _clean_news(raw_data: dict) -> list[dict]:
    """Deduplicate news articles by title + 2-hour window."""
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

    seen: set[str] = set()
    deduped: list[dict] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = article.get("title", "")
        published = article.get("published_utc", "")
        if not title or not published:
            deduped.append(article)
            continue
        fp = _compute_dedup_fingerprint(title, published)
        if fp not in seen:
            seen.add(fp)
            deduped.append(article)

    return deduped


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
