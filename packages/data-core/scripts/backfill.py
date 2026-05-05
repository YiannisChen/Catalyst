#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict, deque
import json
import logging
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PACKAGE_ROOT))

import httpx

from catalyst_data.config import RATE_POLICIES, api_key_id, provider_api_key
from catalyst_data.connectors.base import FetchResult
from catalyst_data.connectors.fmp import create_fmp_fetcher
from catalyst_data.connectors.fred import FRED_BASE_URL
from catalyst_data.connectors.polygon import POLYGON_BASE_URL
from catalyst_data.orchestrator import process_request
from catalyst_data.quality import ensure_ingestion_quality_tables
from catalyst_data.rate_limiter import TokenBucketLimiter
from catalyst_data.retry import with_retry
from catalyst_data.storage.sqlite import init_db
from scripts.smoke_test import _load_dotenv, get_available_sources, resolve_sources

logger = logging.getLogger("backfill")

DEFAULT_TICKERS = (
    "AAPL",
    "AMD",
    "AMZN",
    "GOOGL",
    "JPM",
    "META",
    "MSFT",
    "NVDA",
    "TSLA",
    "UNH",
)
DEFAULT_DB_PATH = Path("data") / "catalyst_eval_frozen.db"
POLYGON_ENDPOINTS = {"ohlcv", "news"}
FMP_ENDPOINTS = {"income_statement", "balance_sheet", "cash_flow"}
FRED_ENDPOINTS = {"DFF", "DGS10", "VIXCLS", "UNRATE", "CPIAUCSL"}


@dataclass
class BackfillStats:
    completed: int = 0
    ok: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.completed

    @property
    def fail_rate(self) -> float:
        if self.completed == 0:
            return 0.0
        return self.failed / self.completed


class PolygonIncidentMonitor:
    def __init__(
        self,
        *,
        build_polygon_fetcher: Any,
        backup_api_key: str | None,
    ) -> None:
        self._build_polygon_fetcher = build_polygon_fetcher
        self._backup_api_key = backup_api_key
        self._recent_ok = deque(maxlen=20)
        self.triggered = False
        self.rotated_to_backup = False

    def record(self, result: FetchResult) -> FetchResult:
        self._recent_ok.append(1 if result.status == 200 and result.error is None else 0)
        rolling_rate = sum(self._recent_ok) / len(self._recent_ok) if self._recent_ok else 1.0
        retry_after = result.retry_after_seconds or 0.0
        if len(self._recent_ok) == 20 and rolling_rate < 0.5:
            self._trigger_incident()
        elif result.status == 429 and retry_after > 300:
            self._trigger_incident()
        return result

    def _trigger_incident(self) -> None:
        logger.warning("OD-8 incident trigger fired")
        self.triggered = True
        if self.rotated_to_backup:
            return
        if self._backup_api_key:
            self._build_polygon_fetcher(self._backup_api_key)
            self.rotated_to_backup = True
            logger.warning("Polygon key rotated to backup")
        else:
            logger.warning("No backup key available — continuing with primary")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the 1-year Catalyst backfill.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--sources", default=None)
    args = parser.parse_args()
    if (args.start_date is None) != (args.end_date is None):
        parser.error("--start-date and --end-date must be provided together")
    if args.start_date is not None and args.days != 365:
        parser.error("--days cannot be combined with --start-date/--end-date")
    return args


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def trailing_calendar_days(days: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    return [
        (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(days)
    ]


def explicit_calendar_days(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        raise ValueError("end_date must be >= start_date")
    span_days = (end - start).days + 1
    return [
        (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(span_days)
    ]


def _month_key(current_date: str) -> str:
    dt = datetime.strptime(current_date, "%Y-%m-%d")
    return dt.strftime("%Y-%m")


def _month_window(current_date: str) -> tuple[str, str]:
    dt = datetime.strptime(current_date, "%Y-%m-%d")
    start = dt.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _with_api_key(next_url: str, api_key: str) -> str:
    parsed = urlparse(next_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("apiKey", api_key)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _date_from_polygon_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _windowed_observations(
    observations: list[dict[str, Any]],
    request_date: str,
) -> list[dict[str, Any]]:
    window_start = (
        datetime.strptime(request_date, "%Y-%m-%d").date() - timedelta(days=30)
    ).strftime("%Y-%m-%d")
    return [
        obs for obs in observations
        if window_start <= obs.get("date", "") <= request_date
    ]


def sources_for_date(selected_sources: list[str], current_date: str) -> list[str]:
    weekday = datetime.strptime(current_date, "%Y-%m-%d").weekday()
    if weekday < 5:
        return list(selected_sources)
    return []


def _checkpoint_error_class(summary: dict[str, Any]) -> str | None:
    error = summary.get("error")
    if not error:
        return None
    lowered = str(error).lower()
    if "429" in lowered or "rate" in lowered:
        return "rate_limit"
    if "timeout" in lowered:
        return "timeout"
    if "5" in lowered and ("503" in lowered or "502" in lowered or "500" in lowered or "504" in lowered):
        return "server_error"
    if lowered.startswith("ingest:"):
        return "ingest"
    if lowered.startswith("clean:"):
        return "clean"
    if lowered.startswith("transform:"):
        return "transform"
    if lowered.startswith("storage:"):
        return "storage"
    return "fetch"


def build_run_notes(
    *,
    tickers: list[str],
    sources: list[str],
    start_date: str,
    end_date: str,
) -> str:
    rate_policies = {
        provider: {
            "min_interval_sec": policy.min_interval_sec,
            "max_concurrent": policy.max_concurrent,
            "daily_budget": policy.daily_budget,
        }
        for provider, policy in RATE_POLICIES.get("dev", {}).items()
        if provider in {"polygon", "fmp", "fred", "yfinance"}
    }
    return json.dumps(
        {
            "tickers": tickers,
            "sources": sources,
            "date_window": {
                "start": start_date,
                "end": end_date,
            },
            "api_key_ids": {
                "polygon": api_key_id("polygon"),
                "fmp": api_key_id("fmp"),
                "fred": api_key_id("fred"),
            },
            "rate_policies": rate_policies,
        },
        sort_keys=True,
    )


def _run_notes(*, tickers: list[str], sources: list[str], dates: list[str]) -> str:
    if not dates:
        raise ValueError("dates must not be empty")
    return build_run_notes(
        tickers=tickers,
        sources=sources,
        start_date=dates[0],
        end_date=dates[-1],
    )


def _db_abspath(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return _PACKAGE_ROOT.parent.parent / path


def _tickers_from_arg(raw: str) -> list[str]:
    return [ticker.strip().upper() for ticker in raw.split(",") if ticker.strip()]


def resolve_dates(args: argparse.Namespace) -> list[str]:
    if args.start_date and args.end_date:
        return explicit_calendar_days(args.start_date, args.end_date)
    return trailing_calendar_days(args.days)


async def _init_database(db_path: Path) -> None:
    def _work() -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            init_db(conn)
            ensure_ingestion_quality_tables(conn)
        finally:
            conn.close()

    await asyncio.to_thread(_work)


async def _insert_ingestion_run(
    db_path: Path,
    *,
    run_id: str,
    tickers: list[str],
    sources: list[str],
    dates: list[str],
) -> None:
    def _work() -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                INSERT INTO ingestion_runs
                    (run_id, started_at, ticker_list_json, source_list_json, status, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _utc_now_iso(),
                    json.dumps(tickers),
                    json.dumps(sources),
                    "running",
                    _run_notes(tickers=tickers, sources=sources, dates=dates),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    await asyncio.to_thread(_work)


async def _update_ingestion_run(
    db_path: Path,
    *,
    run_id: str,
    stats: BackfillStats,
) -> None:
    def _work() -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            status = "partial" if stats.fail_rate >= 0.05 else "success"
            conn.execute(
                """
                UPDATE ingestion_runs
                SET ended_at = ?, status = ?, success_count = ?, fail_count = ?
                WHERE run_id = ?
                """,
                (_utc_now_iso(), status, stats.ok, stats.failed, run_id),
            )
            conn.commit()
        finally:
            conn.close()

    await asyncio.to_thread(_work)


async def _upsert_checkpoint(
    db_path: Path,
    *,
    run_id: str,
    source_type: str,
    ticker: str,
    date: str,
    status: str,
    error_class: str | None,
    retries: int = 0,
) -> None:
    def _work() -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO source_checkpoints
                    (run_id, source_type, ticker, date, status, error_class, retries)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, source_type, ticker, date, status, error_class, retries),
            )
            conn.commit()
        finally:
            conn.close()

    await asyncio.to_thread(_work)


def _build_fetch_router(
    *,
    dates: list[str],
) -> tuple[Any, httpx.AsyncClient, PolygonIncidentMonitor]:
    policies = RATE_POLICIES.get("dev", {})
    limiters = {name: TokenBucketLimiter(policy) for name, policy in policies.items()}
    client = httpx.AsyncClient(timeout=30.0)
    start_date = dates[0]
    end_date = dates[-1]

    polygon_key = provider_api_key("polygon")
    if not polygon_key:
        raise RuntimeError("POLYGON_API_KEY is required for T-06 backfill")

    current_polygon_key = {"value": polygon_key}

    async def polygon_http_get(
        url: str,
        *,
        params: dict[str, str] | None = None,
        source_label: str,
    ) -> FetchResult:
        start = time.monotonic()
        try:
            async with limiters["polygon"].acquire():
                resp = await client.get(url, params=params)
            latency = (time.monotonic() - start) * 1000
            if resp.status_code == 200:
                return FetchResult(
                    status=200,
                    data=resp.json(),
                    latency_ms=latency,
                    source_label=source_label,
                )
            return FetchResult(
                status=resp.status_code,
                error=f"Polygon {resp.status_code}: {resp.text.strip()}",
                latency_ms=latency,
                source_label=source_label,
                retry_after_seconds=_parse_retry_after(resp),
            )
        except (httpx.TimeoutException, httpx.ReadTimeout) as exc:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=f"Timeout: {exc}",
                latency_ms=latency,
                source_label=source_label,
            )
        except httpx.HTTPError as exc:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=str(exc),
                latency_ms=latency,
                source_label=source_label,
            )

    polygon_http = with_retry(polygon_http_get, provider="polygon")
    polygon_news_cache: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    polygon_ohlcv_cache: dict[str, dict[str, dict[str, Any]]] = {}

    async def _ensure_polygon_news_month(
        ticker: str,
        request_date: str,
    ) -> FetchResult | None:
        cache_key = (ticker, _month_key(request_date))
        if cache_key in polygon_news_cache:
            return None

        start_window, end_window = _month_window(request_date)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        next_url: str | None = None
        params: dict[str, str] | None = {
            "ticker": ticker,
            "published_utc.gte": f"{start_window}T00:00:00Z",
            "published_utc.lt": f"{end_window}T00:00:00Z",
            "limit": "1000",
            "sort": "published_utc",
            "order": "asc",
            "apiKey": current_polygon_key["value"],
        }
        while True:
            result = await polygon_http(
                next_url or f"{POLYGON_BASE_URL}/v2/reference/news",
                params=params,
                source_label="polygon:news_batch",
            )
            if result.status != 200:
                return result
            body = result.data if isinstance(result.data, dict) else {}
            for article in body.get("results", []) or []:
                published = article.get("published_utc")
                if isinstance(published, str) and len(published) >= 10:
                    grouped[published[:10]].append(article)
            next_url = body.get("next_url")
            if not next_url:
                break
            next_url = _with_api_key(next_url, current_polygon_key["value"])
            params = None

        polygon_news_cache[cache_key] = dict(grouped)
        return None

    async def _ensure_polygon_ohlcv_ticker(ticker: str) -> FetchResult | None:
        if ticker in polygon_ohlcv_cache:
            return None

        result = await polygon_http(
            f"{POLYGON_BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}",
            params={"apiKey": current_polygon_key["value"]},
            source_label="polygon:ohlcv_batch",
        )
        if result.status != 200:
            return result

        body = result.data if isinstance(result.data, dict) else {}
        bars: dict[str, dict[str, Any]] = {}
        for bar in body.get("results", []) or []:
            if isinstance(bar, dict) and "t" in bar:
                bars[_date_from_polygon_timestamp(int(bar["t"]))] = bar
        polygon_ohlcv_cache[ticker] = bars
        return None

    def build_polygon_fetcher(api_key: str) -> None:
        current_polygon_key["value"] = api_key

    build_polygon_fetcher(polygon_key)

    monitor = PolygonIncidentMonitor(
        build_polygon_fetcher=build_polygon_fetcher,
        backup_api_key=provider_api_key("polygon_backup"),
    )

    fmp_fetch = None
    fmp_cache: dict[tuple[str, str], FetchResult] = {}
    fmp_key = provider_api_key("fmp")
    if fmp_key:
        fmp_fetch = create_fmp_fetcher(
            api_key=fmp_key,
            limiter=limiters.get("fmp"),
            client=client,
        )

    fred_key = provider_api_key("fred")
    fred_cache: dict[str, dict[str, Any]] = {}
    fred_http = None
    if fred_key:
        async def fred_http_get(
            url: str,
            *,
            params: dict[str, str] | None = None,
            source_label: str,
        ) -> FetchResult:
            start = time.monotonic()
            try:
                async with limiters["fred"].acquire():
                    resp = await client.get(url, params=params)
                latency = (time.monotonic() - start) * 1000
                if resp.status_code == 200:
                    return FetchResult(
                        status=200,
                        data=resp.json(),
                        latency_ms=latency,
                        source_label=source_label,
                    )
                return FetchResult(
                    status=resp.status_code,
                    error=f"FRED {resp.status_code}: {resp.text.strip()}",
                    latency_ms=latency,
                    source_label=source_label,
                )
            except (httpx.TimeoutException, httpx.ReadTimeout) as exc:
                latency = (time.monotonic() - start) * 1000
                return FetchResult(
                    status=0,
                    error=f"Timeout: {exc}",
                    latency_ms=latency,
                    source_label=source_label,
                )
            except httpx.HTTPError as exc:
                latency = (time.monotonic() - start) * 1000
                return FetchResult(
                    status=0,
                    error=str(exc),
                    latency_ms=latency,
                    source_label=source_label,
                )

        fred_http = with_retry(fred_http_get, provider="fred")

    async def fetch(ticker: str, endpoint: str, request_date: str) -> FetchResult:
        if endpoint == "news":
            failure = await _ensure_polygon_news_month(ticker, request_date)
            if failure is not None:
                return monitor.record(failure)
            return monitor.record(
                FetchResult(
                    status=200,
                    data={
                        "results": polygon_news_cache[(ticker, _month_key(request_date))].get(request_date, [])
                    },
                    source_label="polygon:news",
                )
            )
        if endpoint == "ohlcv":
            failure = await _ensure_polygon_ohlcv_ticker(ticker)
            if failure is not None:
                return monitor.record(failure)
            bar = polygon_ohlcv_cache[ticker].get(request_date)
            return monitor.record(
                FetchResult(
                    status=200,
                    data={"ticker": ticker, "results": [bar] if bar is not None else []},
                    source_label="polygon:ohlcv",
                )
            )
        if endpoint in FMP_ENDPOINTS and fmp_fetch is not None:
            cache_key = (ticker, endpoint)
            if cache_key not in fmp_cache:
                fmp_cache[cache_key] = await fmp_fetch(ticker, endpoint, request_date)
            return fmp_cache[cache_key]
        if endpoint in FRED_ENDPOINTS and fred_http is not None:
            if endpoint not in fred_cache:
                response = await fred_http(
                    FRED_BASE_URL,
                    params={
                        "series_id": endpoint,
                        "api_key": fred_key,
                        "file_type": "json",
                        "observation_start": (
                            datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=30)
                        ).strftime("%Y-%m-%d"),
                        "observation_end": end_date,
                    },
                    source_label=f"fred:{endpoint}",
                )
                if response.status != 200:
                    return response
                body = response.data if isinstance(response.data, dict) else {}
                body["observations"] = [
                    obs for obs in body.get("observations", [])
                    if obs.get("value") != "."
                ]
                fred_cache[endpoint] = body
            body = dict(fred_cache[endpoint])
            body["observations"] = _windowed_observations(body.get("observations", []), request_date)
            return FetchResult(
                status=200,
                data=body,
                source_label=f"fred:{endpoint}",
            )
        return FetchResult(
            status=0,
            error=f"No connector available for endpoint: {endpoint}",
            source_label=f"unknown:{endpoint}",
        )

    return fetch, client, monitor


def _status_for_summary(summary: dict[str, Any]) -> str:
    if summary.get("skipped"):
        return "skipped"
    return "success" if summary.get("ok") else "failed"


async def run_backfill(args: argparse.Namespace) -> BackfillStats:
    _load_dotenv(_PACKAGE_ROOT / ".env")

    db_path = _db_abspath(args.db_path)
    tickers = _tickers_from_arg(args.tickers)
    available_sources = get_available_sources()
    selected_sources = resolve_sources(args.sources, available_sources)
    dates = resolve_dates(args)
    total_expected = sum(len(tickers) * len(sources_for_date(selected_sources, d)) for d in dates)

    await _init_database(db_path)

    run_id = str(uuid.uuid4())
    await _insert_ingestion_run(
        db_path,
        run_id=run_id,
        tickers=tickers,
        sources=selected_sources,
        dates=dates,
    )

    fetch_fn, client, monitor = _build_fetch_router(dates=dates)
    stats = BackfillStats()
    started = time.monotonic()

    try:
        for current_date in dates:
            active_sources = sources_for_date(selected_sources, current_date)
            if not active_sources:
                continue

            for ticker in tickers:
                summaries = await process_request(
                    ticker=ticker,
                    date=current_date,
                    sources=active_sources,
                    db_path=db_path,
                    fetch_fn=fetch_fn,
                )
                for summary in summaries:
                    status = _status_for_summary(summary)
                    stats.completed += 1
                    if status == "success":
                        stats.ok += 1
                    elif status == "failed":
                        stats.failed += 1
                    await _upsert_checkpoint(
                        db_path,
                        run_id=run_id,
                        source_type=summary["source"],
                        ticker=ticker,
                        date=current_date,
                        status=status,
                        error_class=_checkpoint_error_class(summary),
                    )
                    if stats.completed % 50 == 0:
                        elapsed = time.monotonic() - started
                        print(
                            f"[progress] {stats.completed}/{total_expected} checkpoints, "
                            f"{stats.ok}/{stats.failed} ok/fail, elapsed={elapsed:.1f}s"
                        )
    finally:
        await _update_ingestion_run(db_path, run_id=run_id, stats=stats)
        await client.aclose()

    elapsed = time.monotonic() - started
    print(
        f"Backfill complete: checkpoints={stats.total} ok={stats.ok} fail={stats.failed} "
        f"fail_rate={stats.fail_rate:.3f} wall_clock={elapsed:.1f}s"
    )
    if monitor.triggered:
        print(f"OD-8 triggered: yes (rotated_to_backup={monitor.rotated_to_backup})")
    else:
        print("OD-8 triggered: no")
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    args = parse_args()
    asyncio.run(run_backfill(args))


if __name__ == "__main__":
    main()
