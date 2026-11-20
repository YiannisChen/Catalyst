"""Structured fact parsers and fetch CLI for the investigator agent."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, time, timezone
from typing import Any

from catalyst_data.ingestion.structured_types import FactRow, FetchReceipt
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.connect import connect_readonly

# These FRED release ids must be verified against FRED /releases before use.
MACRO_RELEASES = {
    10: "CPI",
    50: "Employment Situation",
    53: "GDP",
    46: "PPI",
}

# FRED's "FOMC Press Release" release lists daily dates, so decision days come from the Federal
# Reserve's published meeting calendar (statement at 14:00 New York on the second meeting day).
FOMC_DECISION_DATES = (
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17",
    "2025-10-29", "2025-12-10", "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
)


def fomc_rows() -> tuple[FactRow, ...]:
    return tuple(FactRow(kind="macro_release", subject="US", event_at=_ny_date_at(day, time(14, 0)),
                         available_at=_ny_date_at(day, time(14, 0)), precision="SECOND", source="federalreserve",
                         payload={"release_id": None, "name": "FOMC decision", "date": day})
                 for day in FOMC_DECISION_DATES)

NY_TZ = timefields.NY
EARLY_MORNING_RELEASES = {10, 50}


def _json_rows(receipt: FetchReceipt) -> list[dict[str, Any]]:
    if receipt.status != "OK" or not receipt.response_json:
        return []
    data = json.loads(receipt.response_json)
    if isinstance(data, dict) and isinstance(data.get("release_dates"), list):
        data = data["release_dates"]
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _ny_date_at(day: str, clock: time) -> datetime:
    local = datetime.combine(date.fromisoformat(day), clock, tzinfo=NY_TZ)
    return local.astimezone(timezone.utc)


def _surprise_pct(actual: Any, estimate: Any) -> float | None:
    if actual is None or estimate in (None, 0):
        return None
    return (actual - estimate) / abs(estimate) * 100


def parse_finnhub_earnings(receipt: FetchReceipt) -> tuple[FactRow, ...]:
    rows = []
    for row in _json_rows(receipt):
        payload = dict(row)
        payload["fiscal_period"] = {"year": row.get("year"), "quarter": row.get("quarter")}
        rows.append(
            FactRow(
                kind="earnings",
                subject=row["symbol"],
                event_at=None,
                available_at=None,
                precision="UNKNOWN",
                source="finnhub",
                payload=payload,
            )
        )
    return tuple(rows)


def parse_fmp_earnings(receipt: FetchReceipt) -> tuple[FactRow, ...]:
    rows = []
    for row in _json_rows(receipt):
        actual = row.get("epsActual")
        if actual is None:
            continue
        at = _ny_date_at(row["date"], time(23, 59, 59))
        payload = dict(row)
        payload["eps_surprise_pct"] = _surprise_pct(actual, row.get("epsEstimated"))
        payload["revenue_surprise_pct"] = _surprise_pct(
            row.get("revenueActual"), row.get("revenueEstimated")
        )
        rows.append(
            FactRow(
                kind="earnings",
                subject=row["symbol"],
                event_at=at,
                available_at=at,
                precision="DATE",
                source="fmp",
                payload=payload,
            )
        )
    return tuple(rows)


def parse_fmp_grades(receipt: FetchReceipt) -> tuple[FactRow, ...]:
    rows = []
    for row in _json_rows(receipt):
        at = _ny_date_at(row["date"], time(23, 59, 59))
        rows.append(
            FactRow(
                kind="grade",
                subject=row["symbol"],
                event_at=at,
                available_at=at,
                precision="DATE",
                source="fmp",
                payload={
                    "date": row["date"],
                    "firm": row["gradingCompany"],
                    "previous_grade": row["previousGrade"],
                    "new_grade": row["newGrade"],
                    "action": row["action"].lower(),
                },
            )
        )
    return tuple(rows)


def parse_fred_release_dates(receipt: FetchReceipt, release_name: str) -> tuple[FactRow, ...]:
    rows = []
    for row in _json_rows(receipt):
        release_id = row["release_id"]
        clock = time(8, 30) if release_id in EARLY_MORNING_RELEASES else time(23, 59, 59)
        at = _ny_date_at(row["date"], clock)
        rows.append(
            FactRow(
                kind="macro_release",
                subject="US",
                event_at=at,
                available_at=at,
                precision="DATE",
                source="fred",
                payload={"release_id": release_id, "name": release_name, "date": row["date"]},
            )
        )
    return tuple(rows)


def fetch_ticker(
    client: Any,
    ticker: str,
    kinds: tuple[str, ...],
    secrets: dict[str, str],
) -> dict[str, str]:
    from catalyst_data.ingestion.structured_cache import StructuredCache

    specs = {
        "finnhub_earnings": (
            "finnhub",
            "/stock/earnings",
            {"symbol": ticker},
            "token",
            "FINNHUB_API_KEY",
            parse_finnhub_earnings,
        ),
        "fmp_earnings": (
            "fmp",
            "/earnings",
            {"symbol": ticker, "limit": 5},  # free plan: 402 above 5 rows
            "apikey",
            "FMP_API_KEY",
            parse_fmp_earnings,
        ),
        "fmp_grades": (
            "fmp",
            "/grades",
            {"symbol": ticker, "limit": 2000},
            "apikey",
            "FMP_API_KEY",
            parse_fmp_grades,
        ),
    }
    statuses: dict[str, str] = {}
    for kind in kinds:
        if kind not in specs:
            statuses[kind] = "ERROR"
            continue
        provider, endpoint, params, secret_param, secret_name, parser = specs[kind]
        receipt = client.get(provider, endpoint, params, secret_param=secret_param, secret=secrets[secret_name])
        cache = getattr(client, "cache", None)
        if isinstance(cache, StructuredCache):
            cache.put(receipt, parser(receipt))
        statuses[kind] = receipt.status
    return statuses


def fetch_macro(
    client: Any,
    start: date,
    end: date,
    secret: str,
) -> dict[str, str]:
    from catalyst_data.ingestion.structured_cache import StructuredCache

    statuses: dict[str, str] = {}
    cache = getattr(client, "cache", None)
    for release_id, name in MACRO_RELEASES.items():
        receipt = client.get(
            "fred",
            "/release/dates",
            {
                "release_id": release_id,
                "realtime_start": start.isoformat(),
                "realtime_end": end.isoformat(),
                "include_release_dates_with_no_data": "true",
                "file_type": "json",
            },
            secret_param="api_key",
            secret=secret,
        )
        if isinstance(cache, StructuredCache):
            cache.put(receipt, parse_fred_release_dates(receipt, name))
        statuses[name] = receipt.status
    return statuses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch structured facts")
    parser.add_argument("command", choices=["fetch"])
    parser.add_argument("--db", required=True)
    parser.add_argument("--tickers")
    parser.add_argument("--tickers-file")
    parser.add_argument("--kinds", default="finnhub_earnings,fmp_earnings,fmp_grades,macro")
    parser.add_argument("--macro-start", default="2025-01-01")
    parser.add_argument("--macro-end", default="2026-12-31")
    args = parser.parse_args(argv)

    from catalyst_data.ingestion.structured_cache import StructuredCache
    from catalyst_data.ingestion.structured_http import (
        StructuredClient,
        default_http_get,
    )

    kinds = tuple(filter(None, (part.strip() for part in args.kinds.split(","))))
    tickers = []
    if args.tickers:
        tickers.extend(filter(None, (part.strip() for part in args.tickers.split(","))))
    if args.tickers_file:
        with open(args.tickers_file, encoding="utf-8") as handle:
            tickers.extend(filter(None, (line.strip() for line in handle)))
    tickers = list(dict.fromkeys(tickers))

    secrets = {
        name: os.environ.get(name) or None
        for name in ("FINNHUB_API_KEY", "FMP_API_KEY", "FRED_API_KEY")
    }
    client = StructuredClient(StructuredCache(args.db, create=True), default_http_get)
    ticker_kinds = tuple(kind for kind in kinds if kind != "macro")
    for ticker in tickers:
        for kind, status in fetch_ticker(client, ticker, ticker_kinds, secrets).items():
            print(f"{ticker} {kind}: {status}")
    if "macro" in kinds:
        for name, status in fetch_macro(
            client,
            date.fromisoformat(args.macro_start),
            date.fromisoformat(args.macro_end),
            secrets["FRED_API_KEY"],
        ).items():
            print(f"US macro_{name}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


_RATING_HEADLINE = re.compile(
    r"^(?P<firm>[A-Z][\w&.,' -]{1,60}?) (?P<action>Upgrades|Downgrades) (?P<company>.+?)"
    r"(?: \([A-Z.]{1,6}\))?(?: to (?P<grade>[^,]+?))?(?:,|$)")


def headline_grade_rows(db_path: str | os.PathLike, ticker: str, start: str, end: str) -> tuple[FactRow, ...]:
    """Analyst rating actions from timestamped wire headlines ("Telsey Upgrades Target to Outperform").

    FMP's free grades cover a subset of symbols and only carry a date; these headlines cover the
    archive's universe with publication times, so a pre-open note on the session day can be timed.
    """

    if not os.path.exists(db_path):
        return ()
    with connect_readonly(db_path, immutable=False, timeout_ms=10_000) as conn:
        rows = conn.execute(
            "SELECT published_utc, headline, url, source FROM finnhub_news WHERE symbol=? AND "
            "published_utc BETWEEN ? AND ? AND (headline LIKE '%Upgrades %' OR headline LIKE '%Downgrades %') "
            "ORDER BY published_utc", (ticker.upper(), start, end)).fetchall()
    out, seen = [], set()
    for published, headline, url, source in rows:
        match = _RATING_HEADLINE.match(headline.strip())
        if not match:
            continue
        firm, action = match["firm"].strip(), match["action"].lower().rstrip("s")
        at = timefields.stored(published)
        day = at.astimezone(NY_TZ).date().isoformat()
        if (firm, action, day) in seen:
            continue
        seen.add((firm, action, day))
        out.append(FactRow(kind="grade", subject=ticker.upper(), event_at=at, available_at=at, precision="SECOND",
                           source="headline", payload={"date": day, "firm": firm, "action": action,
                                                       "new_grade": (match["grade"] or "").strip() or None,
                                                       "headline": headline, "url": url, "publisher": source}))
    return tuple(out)
