from __future__ import annotations

import time
from typing import Any

from catalyst_data.pipeline.stages import StageResult
from catalyst_data.transmuter import financial_json_to_markdown_table


def _transform_news(articles: list[dict], ticker: str) -> str:
    """Convert cleaned news articles to structured Markdown."""
    sections: list[str] = []
    references: list[str] = []

    for idx, article in enumerate(articles, start=1):
        title = article.get("title", "Untitled")
        publisher = article.get("publisher")
        publisher_name = publisher.get("name") if isinstance(publisher, dict) else ""
        source = (
            article.get("source")
            or publisher_name
            or (publisher if isinstance(publisher, str) else "")
            or "Unknown"
        )
        published = article.get("published_utc", "")
        url = article.get("url") or article.get("article_url", "")
        content = article.get("content") or article.get("description", "")

        header = f"## {ticker}: {title}" if ticker else f"## {title}"
        meta = f"*Source: {source} | {published} | Category: news*"
        body = content if content else "(No content available)"

        sections.append(f"{header}\n{meta}\n\n{body}")

        if url:
            references.append(f"[{idx}] {url}: {title}")

    result = "\n\n".join(sections)
    if references:
        result += "\n\n## References\n" + "\n".join(references)

    return result


def _transform_fundamentals(data: dict[str, Any], ticker: str) -> str:
    """Convert cleaned financial data to Markdown tables."""
    sections: list[str] = []

    for statement_type, values in data.items():
        header = f"## {ticker} Fundamentals — {statement_type}" if ticker else f"## {statement_type}"
        if isinstance(values, dict):
            table = financial_json_to_markdown_table(values)
        else:
            table = str(values)
        sections.append(f"{header}\n\n{table}")

    return "\n\n".join(sections)


def _transform_generic(data: Any, ticker: str) -> str:
    """Simple key-value formatting for macro/other sources."""
    if isinstance(data, dict):
        lines = [f"## {ticker} Data" if ticker else "## Data"]
        for key, value in data.items():
            lines.append(f"- **{key}**: {value}")
        return "\n".join(lines)
    return str(data)


def run_transform(
    cleaned_data: dict | list | Any,
    source_type: str,
    ticker: str = "",
) -> StageResult:
    """Convert cleaned data to structured Markdown for the Silver layer."""
    t0 = time.perf_counter()

    try:
        if "news" in source_type:
            if not isinstance(cleaned_data, list):
                cleaned_data = [cleaned_data]
            md = _transform_news(cleaned_data, ticker)
        elif "fmp" in source_type or "fundamentals" in source_type:
            md = _transform_fundamentals(cleaned_data, ticker)
        else:
            md = _transform_generic(cleaned_data, ticker)

        elapsed = (time.perf_counter() - t0) * 1000
        return StageResult(stage="transform", ok=True, data=md, latency_ms=elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - t0) * 1000
        return StageResult(
            stage="transform", ok=False, error=str(exc), latency_ms=elapsed
        )
