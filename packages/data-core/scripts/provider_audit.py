#!/usr/bin/env python3
"""Provider data audit for ingestion planning.

Fetches real payloads across sources/tickers/dates and reports:
- source stability (endpoint/run success rates)
- news text quality (length distribution, metadata completeness)
- rough RAG eligibility under a configurable minimum text length

Usage:
    python -m scripts.provider_audit --tickers AAPL,NVDA --days 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PACKAGE_ROOT))

from catalyst_data.pipeline.clean import run_clean
from catalyst_data.pipeline.ingest import run_ingest
from catalyst_data.pipeline.transform import run_transform
from catalyst_data.source_mapping import map_logical_source
from scripts.smoke_test import (
    _build_fetch_fn,
    _load_dotenv,
    get_available_sources,
    get_recent_trading_days,
    resolve_sources,
)


def _provider_of(source: str) -> str:
    if source.startswith("polygon_"):
        return "polygon"
    if source.startswith("fmp_"):
        return "fmp"
    if source.startswith("fred_"):
        return "fred"
    if source.startswith("yfinance_"):
        return "yfinance"
    return "unknown"


def _pick_article_body(article: dict[str, Any]) -> str:
    text = article.get("content") or article.get("description") or ""
    if not isinstance(text, str):
        return ""
    return text.strip()


def _extract_news_articles(raw_data: Any) -> list[dict[str, Any]]:
    """Best-effort extraction of article-like dicts from provider payload."""
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
    return [a for a in articles if isinstance(a, dict)]


def _percentile(values: list[int], p: float) -> float:
    if not values:
        return 0.0
    if p <= 0:
        return float(min(values))
    if p >= 1:
        return float(max(values))
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * p))
    return float(ordered[idx])


def _safe_json_size(data: Any) -> int:
    try:
        return len(json.dumps(data, default=str))
    except Exception:  # noqa: BLE001
        return 0


def _as_float(num: float, den: float) -> float:
    return (num / den) if den else 0.0


@dataclass
class SourceStats:
    source: str
    provider: str
    runs_total: int = 0
    runs_ok: int = 0
    endpoint_calls: int = 0
    endpoint_ok: int = 0
    endpoint_latency_ms_sum: float = 0.0
    ingest_ok: int = 0
    clean_ok: int = 0
    transform_ok: int = 0
    payload_bytes_sum: int = 0
    markdown_chars_sum: int = 0

    # News-specific metrics
    articles_total: int = 0
    articles_with_body: int = 0
    article_chars: list[int] = field(default_factory=list)
    eligible_articles: int = 0
    missing_title: int = 0
    missing_published: int = 0
    missing_url: int = 0
    raw_articles_total: int = 0
    cleaned_articles_total: int = 0
    dedup_removed_total: int = 0

    def summarize(self, min_news_chars: int) -> dict[str, Any]:
        is_news = "news" in self.source
        median_chars = statistics.median(self.article_chars) if self.article_chars else 0.0
        mean_chars = statistics.mean(self.article_chars) if self.article_chars else 0.0
        p90_chars = _percentile(self.article_chars, 0.9)
        return {
            "source": self.source,
            "provider": self.provider,
            "runs_total": self.runs_total,
            "run_ok_rate": _as_float(self.runs_ok, self.runs_total),
            "endpoint_ok_rate": _as_float(self.endpoint_ok, self.endpoint_calls),
            "avg_endpoint_latency_ms": _as_float(self.endpoint_latency_ms_sum, self.endpoint_calls),
            "ingest_ok_rate": _as_float(self.ingest_ok, self.runs_total),
            "clean_ok_rate": _as_float(self.clean_ok, self.runs_total),
            "transform_ok_rate": _as_float(self.transform_ok, self.runs_total),
            "avg_payload_bytes": _as_float(self.payload_bytes_sum, self.runs_total),
            "avg_markdown_chars": _as_float(self.markdown_chars_sum, self.transform_ok),
            "is_news_source": is_news,
            "articles_total": self.articles_total if is_news else None,
            "articles_with_body_rate": _as_float(self.articles_with_body, self.articles_total) if is_news else None,
            "rag_eligible_rate": _as_float(self.eligible_articles, self.articles_total) if is_news else None,
            "median_article_chars": median_chars if is_news else None,
            "mean_article_chars": mean_chars if is_news else None,
            "p90_article_chars": p90_chars if is_news else None,
            "short_text_threshold": min_news_chars if is_news else None,
            "missing_title_rate": _as_float(self.missing_title, self.articles_total) if is_news else None,
            "missing_published_rate": _as_float(self.missing_published, self.articles_total) if is_news else None,
            "missing_url_rate": _as_float(self.missing_url, self.articles_total) if is_news else None,
            "raw_articles_total": self.raw_articles_total if is_news else None,
            "cleaned_articles_total": self.cleaned_articles_total if is_news else None,
            "dedup_removed_total": self.dedup_removed_total if is_news else None,
            "duplicate_rate": _as_float(self.dedup_removed_total, self.raw_articles_total) if is_news else None,
            "avg_cleaned_articles_per_run": _as_float(self.cleaned_articles_total, self.runs_total) if is_news else None,
            "recommendation": _recommend_source(self, min_news_chars),
        }


def _recommend_source(stats: SourceStats, min_news_chars: int) -> str:
    run_ok_rate = _as_float(stats.runs_ok, stats.runs_total)
    endpoint_ok_rate = _as_float(stats.endpoint_ok, stats.endpoint_calls)

    if run_ok_rate < 0.7 or endpoint_ok_rate < 0.7:
        return "fallback_only_unstable"

    if "news" in stats.source:
        eligible_rate = _as_float(stats.eligible_articles, stats.articles_total)
        median_chars = statistics.median(stats.article_chars) if stats.article_chars else 0.0
        if stats.articles_total == 0:
            return "fallback_no_coverage"
        if eligible_rate < 0.35 or median_chars < min_news_chars * 0.6:
            return "fallback_low_text_quality"
        return "primary_candidate"

    return "primary_candidate"


async def _audit_source_run(
    *,
    ticker: str,
    date: str,
    source: str,
    fetch_fn: Any,
    stats: SourceStats,
    min_news_chars: int,
    verbose: bool,
) -> None:
    endpoints = map_logical_source(source)
    endpoint_data: dict[str, Any] = {}
    endpoint_statuses: dict[str, int] = {}

    stats.runs_total += 1
    for endpoint in endpoints:
        stats.endpoint_calls += 1
        result = await fetch_fn(ticker, endpoint, date)
        endpoint_data[endpoint] = result.data
        endpoint_statuses[endpoint] = result.status
        if result.status == 200 and result.error is None:
            stats.endpoint_ok += 1
        elif verbose:
            print(
                f"[warn] fetch_fail ticker={ticker} date={date} source={source} "
                f"endpoint={endpoint} status={result.status} error={result.error}"
            )
        stats.endpoint_latency_ms_sum += float(result.latency_ms or 0.0)

    ingest = run_ingest(endpoint_data, endpoint_statuses)
    if not ingest.ok:
        if verbose:
            print(
                f"[warn] ingest_fail ticker={ticker} date={date} source={source} "
                f"error={ingest.error}"
            )
        return
    stats.ingest_ok += 1
    stats.payload_bytes_sum += _safe_json_size(ingest.data)

    raw_articles: list[dict[str, Any]] = []
    if "news" in source:
        raw_articles = _extract_news_articles(ingest.data)
        stats.raw_articles_total += len(raw_articles)

    cleaned = run_clean(ingest.data, source)
    if not cleaned.ok:
        if verbose:
            print(
                f"[warn] clean_fail ticker={ticker} date={date} source={source} "
                f"error={cleaned.error}"
            )
        return
    stats.clean_ok += 1

    transformed = run_transform(cleaned.data, source, ticker)
    if not transformed.ok:
        if verbose:
            print(
                f"[warn] transform_fail ticker={ticker} date={date} source={source} "
                f"error={transformed.error}"
            )
        return
    stats.transform_ok += 1
    if isinstance(transformed.data, str):
        stats.markdown_chars_sum += len(transformed.data)

    stats.runs_ok += 1
    if "news" not in source:
        return

    articles = cleaned.data if isinstance(cleaned.data, list) else []
    cleaned_count = len([a for a in articles if isinstance(a, dict)])
    stats.cleaned_articles_total += cleaned_count
    removed = max(0, len(raw_articles) - cleaned_count)
    stats.dedup_removed_total += removed

    for article in articles:
        if not isinstance(article, dict):
            continue
        stats.articles_total += 1

        body = _pick_article_body(article)
        body_len = len(body)
        stats.article_chars.append(body_len)
        if body_len > 0:
            stats.articles_with_body += 1

        title = article.get("title")
        published = article.get("published_utc")
        url = article.get("url") or article.get("article_url")
        if not title:
            stats.missing_title += 1
        if not published:
            stats.missing_published += 1
        if not url:
            stats.missing_url += 1

        if body_len >= min_news_chars and title and published:
            stats.eligible_articles += 1


def _render_markdown(
    *,
    tickers: list[str],
    dates: list[str],
    min_news_chars: int,
    summaries: list[dict[str, Any]],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# Provider Data Audit Report",
        "",
        f"- Generated at: {now}",
        f"- Tickers: {', '.join(tickers)}",
        f"- Date count: {len(dates)}",
        f"- News min chars for RAG eligibility: {min_news_chars}",
        "",
        "## Summary",
        "",
        "| Source | Provider | Run OK | Endpoint OK | Eligible News | Median News Chars | Duplicate Rate | Recommendation |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for s in summaries:
        run_ok = f"{s['run_ok_rate'] * 100:.1f}%"
        endpoint_ok = f"{s['endpoint_ok_rate'] * 100:.1f}%"
        eligible = "-"
        median = "-"
        dup = "-"
        if s["is_news_source"]:
            eligible = f"{(s['rag_eligible_rate'] or 0.0) * 100:.1f}%"
            median = f"{s['median_article_chars']:.0f}"
            dup = f"{(s['duplicate_rate'] or 0.0) * 100:.1f}%"
        lines.append(
            f"| {s['source']} | {s['provider']} | {run_ok} | {endpoint_ok} | {eligible} | {median} | {dup} | {s['recommendation']} |"
        )

    lines.extend(
        [
            "",
            "## Detailed Metrics (JSON companion)",
            "",
            "See the JSON file generated in the same output directory for full metric fields.",
        ]
    )
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> None:
    _load_dotenv(_PACKAGE_ROOT / ".env")

    available = get_available_sources()
    sources = resolve_sources(args.sources, available)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    dates = get_recent_trading_days(args.days)

    stats_map = {
        source: SourceStats(source=source, provider=_provider_of(source))
        for source in sources
    }

    print(f"Auditing sources: {sources}")
    print(f"Tickers: {tickers}")
    print(f"Dates: {dates[0]} -> {dates[-1]} ({len(dates)} days)")

    fetch_fn, client = await _build_fetch_fn()
    total_runs = len(tickers) * len(dates) * len(sources)
    done_runs = 0
    t0 = time.monotonic()
    try:
        for ticker in tickers:
            for date in dates:
                for source in sources:
                    await _audit_source_run(
                        ticker=ticker,
                        date=date,
                        source=source,
                        fetch_fn=fetch_fn,
                        stats=stats_map[source],
                        min_news_chars=args.min_news_chars,
                        verbose=args.verbose_errors,
                    )
                    done_runs += 1
                    if done_runs % max(1, args.progress_every) == 0 or done_runs == total_runs:
                        elapsed = time.monotonic() - t0
                        avg_sec = elapsed / done_runs
                        eta = avg_sec * (total_runs - done_runs)
                        print(
                            f"[progress] {done_runs}/{total_runs} "
                            f"({done_runs / total_runs * 100:.1f}%) "
                            f"elapsed={elapsed/60:.1f}m eta={eta/60:.1f}m "
                            f"last={ticker}:{date}:{source}"
                        )
    finally:
        await client.aclose()

    summaries = [
        stats_map[source].summarize(args.min_news_chars) for source in sources
    ]
    summaries.sort(key=lambda x: x["source"])

    for s in summaries:
        print(
            f"{s['source']:24s} run_ok={s['run_ok_rate']*100:5.1f}% "
            f"endpoint_ok={s['endpoint_ok_rate']*100:5.1f}% rec={s['recommendation']}"
        )

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"provider_audit_{stamp}.json"
    md_path = out_dir / f"provider_audit_{stamp}.md"

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tickers": tickers,
        "dates": dates,
        "sources": sources,
        "min_news_chars": args.min_news_chars,
        "summaries": summaries,
    }
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    md_path.write_text(
        _render_markdown(
            tickers=tickers,
            dates=dates,
            min_news_chars=args.min_news_chars,
            summaries=summaries,
        )
    )
    print(f"Report JSON: {json_path}")
    print(f"Report MD:   {md_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit provider/source data quality for ingestion planning."
    )
    parser.add_argument(
        "--tickers",
        type=str,
        default="AAPL,NVDA,TSLA,MSFT,AMZN",
        help="Comma-separated ticker list.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=10,
        help="Number of recent trading days to sample.",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default=None,
        help="Comma-separated logical sources; default uses available sources.",
    )
    parser.add_argument(
        "--min-news-chars",
        type=int,
        default=200,
        help="Minimum article body chars to count as RAG-eligible.",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=str(_PACKAGE_ROOT.parent.parent / "data" / "eval_reports"),
        help="Directory for markdown/json reports.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Print progress every N source-runs.",
    )
    parser.add_argument(
        "--verbose-errors",
        action="store_true",
        help="Print fetch/ingest/clean/transform failures as they happen.",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
