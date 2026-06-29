"""Per-article Markdown transform — NO MERGE.

Replaces _transform_news for article-level processing.
Each article becomes exactly one clean_asset row.

GUARD: This module MUST NOT merge multiple articles into a single
       Markdown digest. The bundle-digest form is permanently retired.
       Each call to _transform_article handles exactly one article dict.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from catalyst_data.pipeline.stages import StageResult


def _compute_title_hash(title: str) -> str:
    """Stable SHA-256 of normalized title for dedup."""
    title_norm = re.sub(r"[^\w\s]", "", title).lower().strip()
    return hashlib.sha256(title_norm.encode()).hexdigest()[:16]


def _transform_article(article: dict[str, Any], ticker: str = "") -> dict[str, Any]:
    """Convert a single article dict to a clean_asset-compatible row.

    Args:
        article: Dict with keys: id, title, publisher, published_utc, article_url,
                 description.
        ticker: Ticker symbol for the header.

    Returns:
        Dict with keys: asset_id, content_md, title_hash.

    Raises:
        TypeError: If article is a list (merge guard).
    """
    if isinstance(article, list):
        raise TypeError(
            "_transform_article received a list — this is a merge guard. "
            "transform_v2 processes one article at a time. "
            "Callers must iterate over article lists."
        )

    title = article.get("title", "Untitled")
    publisher = article.get("publisher") or {}
    publisher_name = publisher.get("name") if isinstance(publisher, dict) else ""
    source = (
        article.get("source")
        or publisher_name
        or (publisher if isinstance(publisher, str) else "")
        or "Unknown"
    )
    published = article.get("published_utc", "")
    url = article.get("article_url", "")
    content = article.get("description", "")
    native_id = article.get("id") or article.get("article_id", "")

    asset_id = f"poly:{native_id}:{ticker}" if (native_id and ticker) else (f"poly:{native_id}" if native_id else "")

    header = f"## {ticker}: {title}" if ticker else f"## {title}"
    meta = f"*Source: {source} | {published} | Category: news*"
    body = content if content else "(No content available)"

    content_md = f"{header}\n{meta}\n\n{body}"
    if url:
        content_md += f"\n\n## References\n[1] {url}: {title}"

    return {
        "asset_id": asset_id,
        "content_md": content_md,
        "title_hash": _compute_title_hash(title),
    }


def run_transform_v2(
    articles: list[dict[str, Any]],
    source_type: str = "polygon_news",
    ticker: str = "",
) -> list[StageResult]:
    """Transform a list of articles to individual clean_asset-ready rows.

    Each article produces one StageResult. No merging occurs.

    Args:
        articles: List of article dicts from the clean stage.
        source_type: Source type label.
        ticker: Ticker symbol.

    Returns:
        List of StageResult, one per article.
    """
    results: list[StageResult] = []
    for article in articles:
        t0 = time.perf_counter()
        try:
            transformed = _transform_article(article, ticker)
            elapsed = (time.perf_counter() - t0) * 1000
            results.append(
                StageResult(
                    stage="transform",
                    ok=True,
                    data=transformed,
                    latency_ms=elapsed,
                )
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            results.append(
                StageResult(
                    stage="transform",
                    ok=False,
                    error=str(exc),
                    latency_ms=elapsed,
                )
            )
    return results
