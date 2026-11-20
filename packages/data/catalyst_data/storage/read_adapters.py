"""Typed, read-only adapters for metadata archives and market bars.

These adapters return positive metadata leads or market facts. They do not
create coverage facts, interpret an empty result as EMPTY coverage, or promote
headlines and provider summaries to body evidence. Legacy market bars are
returned only as unverified published/session facts and never as observed facts.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from catalyst_data import trading_calendar
from catalyst_data.ingestion import input_manifest
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.snapshots import (
    VerifiedStores,
    readonly_snapshot,
    validate_verified_stores,
)
from catalyst_data.storage.time_corrections import CorrectionTable, load_corrections

_TABLES = {
    "headline_archive": {
        "finnhub_news": (
            "symbol", "news_id", "published_utc", "headline", "url", "source",
            "category", "related", "summary", "raw_json", "first_seen_at",
        ),
        "archive_windows": (
            "symbol", "window_start", "window_end", "status", "items", "attempts",
            "detail", "fetched_at",
        ),
    },
    "news_archive": {
        "news_items": (
            "provider", "item_key", "url", "title", "summary", "publisher",
            "published_at", "tickers_json", "extra_json", "first_seen_at",
        ),
        "news_item_tickers": ("provider", "item_key", "ticker"),
    },
    "press_rss": {
        "press_rss": (
            "provider", "guid", "url", "title", "summary", "published_at",
            "tickers_json", "feed", "first_seen_at",
        ),
        "press_rss_tickers": ("provider", "guid", "ticker"),
    },
    "daily_bars": {
        "ohlcv": ("symbol", "date", "open", "high", "low", "close", "volume", "source"),
    },
    "minute_bars": {
        "minute_bars": (
            "symbol", "ts", "session_date", "open", "high", "low", "close", "volume",
            "source", "fetched_at",
        ),
    },
}
_OPTIONAL_TABLES = {
    "daily_bars": {
        "ohlcv_metadata": ("symbol", "date", "first_seen_at", "is_backfill", "provenance"),
    },
    "press_rss": {
        "press_rss_observations": ("provider", "guid", "content_sha256", "observed_at"),
    },
}
_ADAPTERS = frozenset(_TABLES)

# A source namespace may differ from its storage name only through an explicit
# code-owned exception. Registry contents cannot authorize arbitrary rebinding.
_SOURCE_STORE_PREFIX_EXCEPTIONS = {"daily:price_bars": "market"}


def _instant(value: str | datetime, name: str) -> datetime:
    try:
        parsed = timefields.stored(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a timezone-aware timestamp") from None
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _require_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"headline archive has invalid {field}")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"headline archive has invalid {field}")
    return value


def _registered_source_id(adapter: str, provider: str, source_stores: dict[str, str]) -> str:
    source_id = f"{adapter}:{provider}"
    # This map verifies the registered upstream source namespace. The actual
    # snapshot location is the separately verified ``store_id`` in the lead
    # and audit, and need not equal the registry's upstream store namespace.
    if source_stores.get(source_id) != adapter:
        raise ValueError("headline row references an unknown registry source")
    return source_id


def _validate_market_source_binding(
    adapter: str, source_id: str | None, source_stores: dict[str, str],
    source_categories: dict[str, str | None],
) -> str:
    if not isinstance(source_id, str) or not source_id.strip() or source_id != source_id.strip():
        raise ValueError("market adapters require an explicit registered market_source_id")
    if source_id not in source_stores:
        raise ValueError("market_source_id is not registered")
    if adapter == "minute_bars":
        expected = {
            "market_archive:minute": ("market_archive", "market"),
            "market_archive:research": ("market_archive", "research"),
        }
        if source_id not in expected:
            raise ValueError("minute bars require the registered minute or research source")
        if (source_stores[source_id], source_categories.get(source_id)) != expected[source_id]:
            raise ValueError("minute bar source store or category does not match registration")
        return source_id
    if adapter == "daily_bars":
        # No daily bar source is currently registered. A future explicit
        # registration must be in the daily store and market category.
        expected_store = _SOURCE_STORE_PREFIX_EXCEPTIONS.get(source_id, "daily")
        if (not source_id.startswith("daily:") or source_stores[source_id] != expected_store
                or source_categories.get(source_id) != "market"):
            raise ValueError("daily bars have no matching registered market source")
        return source_id
    raise ValueError("market source binding is only valid for bar adapters")


def _schema(conn: sqlite3.Connection, adapter: str) -> None:
    expected = _TABLES[adapter]
    optional = _OPTIONAL_TABLES.get(adapter, {})
    checked = {**expected, **optional}
    rows = conn.execute(
        "SELECT name,type FROM sqlite_master WHERE name IN (" +
        ",".join("?" for _ in checked) + ")",
        tuple(checked),
    ).fetchall()
    objects = {row[0]: row[1] for row in rows}
    if not set(expected) <= set(objects) or any(kind != "table" for kind in objects.values()):
        raise ValueError(f"{adapter} adapter schema is missing or invalid")
    for name, columns in checked.items():
        if name in optional and name not in objects:
            continue
        actual = tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{name}")'))
        compatible_minute_extension = (
            adapter == "minute_bars" and name == "minute_bars"
            and actual == columns + ("is_backfill",)
        )
        if actual != columns and not compatible_minute_extension:
            raise ValueError(f"{adapter} adapter schema is incompatible")


def _ticker_membership(raw: Any) -> frozenset[str]:
    return frozenset(_ticker_values(raw))


def _ticker_values(raw: Any) -> list[str]:
    if not isinstance(raw, str):
        raise ValueError("headline ticker membership is not valid JSON")
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("headline ticker membership is not valid JSON") from None
    if (not isinstance(values, list)
            or any(not isinstance(value, str) or not value or value != value.strip()
                   for value in values)
            or len(values) != len(set(values))):
        raise ValueError("headline ticker membership is invalid")
    return values


def _lead(*, adapter: str, source_id: str, item_id: str, ticker: str,
          title: str, url: str, published_at: str, first_seen_at: str,
          summary: str | None = None, publisher: str | None = None,
          feed: str | None = None, mode: str, store_id: str,
          snapshot_id: str, observation_available_at: str | None = None) -> dict[str, Any]:
    item = {
        "adapter": adapter,
        "source_id": source_id,
        "item_id": item_id,
        "store_id": store_id,
        "snapshot_id": snapshot_id,
        "product": "headlines",
        "subject_kind": "ticker",
        "subject_id": ticker,
        "title": title,
        "url": url,
        "published_at": published_at,
        "first_seen_at": first_seen_at,
        "temporal_status": "observed" if mode == "observed" else "unverified_published",
        "evidence_role": "lead",
    }
    if summary is not None:
        item["summary"] = summary
    if publisher is not None:
        item["publisher"] = publisher
    if feed is not None:
        item["feed"] = feed
    if observation_available_at is not None:
        item["observation_available_at"] = observation_available_at
    return item


def _observation_available_at(raw: str) -> datetime:
    parsed = _instant(raw, "first_seen_at")
    precision = re.search(r"[T ]\d{2}:\d{2}:\d{2}\.(\d+)", raw)
    if precision is not None and len(precision.group(1)) > 6:
        raise ValueError("first_seen_at exceeds supported microsecond precision")
    # The legacy collectors write whole-second first_seen values. They are
    # request timestamps, so a response could finish anywhere in that second.
    if re.search(r"[T ]\d{2}:\d{2}:\d{2}\.\d+", raw) is None:
        return parsed.replace(microsecond=0) + timedelta(seconds=1)
    return parsed


def _pinned_registry(
    verified: VerifiedStores, registry_path: str | Path,
) -> tuple[dict[str, str], dict[str, str | None], str]:
    path = Path(registry_path).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("source registry must be an absolute pinned artifact")
    try:
        path = path.resolve(strict=True)
        receipt = input_manifest.verify(
            verified.manifest_path,
            invocation=list(verified.invocation),
            required_paths=(path,),
            required_env=verified.required_env,
            environ=verified.environ_override,
        )
    except (OSError, RuntimeError, input_manifest.PreflightError):
        raise input_manifest.PreflightError("source registry is not a manifest-pinned artifact") from None
    if receipt["manifest_sha256"] != verified.manifest_sha256:
        raise input_manifest.PreflightError("input manifest changed after store verification")
    pins = {Path(row["path"]): row["sha256"] for row in receipt["files"]}
    digest = pins.get(path)
    if digest is None or input_manifest.fingerprint(path) != digest:
        raise input_manifest.PreflightError("source registry bytes differ from their manifest pin")
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(registry, dict):
            raise ValueError
        rows = registry["sources"]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError
        ids = [row.get("id") for row in rows]
        if (any(not isinstance(source_id, str) or not source_id.strip()
                or source_id != source_id.strip() for source_id in ids)
                or len(ids) != len(set(ids))):
            raise ValueError
        if type(registry.get("version")) is int and registry["version"] == 1:
            # Source identity and storage identity are separate: a collector
            # namespace can write to a differently named logical store.
            stores = registry.get("stores")
            if (not isinstance(stores, dict) or not stores
                    or any(not isinstance(key, str) or not key.strip() or key != key.strip()
                           or not isinstance(value, str) or not value.strip()
                           for key, value in stores.items())):
                raise ValueError
            source_stores: dict[str, str] = {}
            source_categories: dict[str, str | None] = {}
            for row in rows:
                source_id, store = row.get("id"), row.get("store")
                if (not isinstance(store, str) or store not in stores
                        or not isinstance(source_id, str)
                        or (source_id in _SOURCE_STORE_PREFIX_EXCEPTIONS
                            and store != _SOURCE_STORE_PREFIX_EXCEPTIONS[source_id])
                        or (source_id not in _SOURCE_STORE_PREFIX_EXCEPTIONS
                            and not source_id.startswith(store + ":"))):
                    raise ValueError
                source_stores[source_id] = store
                category = row.get("category")
                if category is not None and (not isinstance(category, str) or not category.strip()):
                    raise ValueError
                source_categories[source_id] = category
        elif (registry.get("version") is None
              and type(registry.get("schema_version")) is int
              and registry["schema_version"] == 1):
            # Existing minimal synthetic manifests use this explicitly
            # versioned shape. Their source IDs still bind the store prefix;
            # if a row supplies store, it must agree with that prefix.
            source_stores = {}
            source_categories = {}
            for row in rows:
                source_id = row["id"]
                prefix, separator, _provider = source_id.partition(":")
                if not separator or not prefix:
                    raise ValueError
                expected_store = _SOURCE_STORE_PREFIX_EXCEPTIONS.get(source_id, prefix)
                store = row.get("store", expected_store)
                if not isinstance(store, str) or store != expected_store:
                    raise ValueError
                source_stores[source_id] = store
                category = row.get("category")
                if category is not None and (not isinstance(category, str) or not category.strip()):
                    raise ValueError
                source_categories[source_id] = category
        else:
            raise ValueError
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise ValueError("source registry is missing or invalid") from None
    return source_stores, source_categories, digest


def _pinned_corrections(
    verified: VerifiedStores,
    correction_table_id: str | None,
    correction_tables: tuple[CorrectionTable, ...],
    correction_store_ids: dict[str, str],
    correction_artifacts: dict[str, str | Path],
) -> tuple[CorrectionTable | None, str | None]:
    if any(not isinstance(table, CorrectionTable) for table in correction_tables):
        raise ValueError("correction_tables must contain validated tables")
    if len({table.table_id for table in correction_tables}) != len(correction_tables):
        raise ValueError("duplicate correction table identity")
    if correction_table_id is None:
        if correction_tables or correction_store_ids or correction_artifacts:
            raise ValueError("correction inputs require an explicit correction_table_id")
        return None, None
    if not isinstance(correction_table_id, str) or not correction_table_id.strip():
        raise ValueError("correction_table_id must be a nonempty identity")
    table_by_id = {table.table_id: table for table in correction_tables}
    table = table_by_id.get(correction_table_id)
    if (table is None or len(correction_tables) != 1
            or set(correction_artifacts) != {correction_table_id}):
        raise ValueError("the selected correction table requires its pinned artifact")
    if set(correction_store_ids) - set(verified.stores):
        raise ValueError("correction binding references an undeclared store")
    if any(not isinstance(scope, str) or not scope.strip() or scope != scope.strip()
           for scope in correction_store_ids.values()):
        raise ValueError("correction bindings must name exact CSV store scopes")
    raw_path = Path(correction_artifacts[correction_table_id]).expanduser()
    if not raw_path.is_absolute() or raw_path.is_symlink():
        raise ValueError("correction artifact must be an absolute pinned file")
    try:
        path = raw_path.resolve(strict=True)
        receipt = input_manifest.verify(
            verified.manifest_path,
            invocation=list(verified.invocation),
            required_paths=(path,),
            required_env=verified.required_env,
            environ=verified.environ_override,
        )
    except (OSError, RuntimeError, input_manifest.PreflightError):
        raise input_manifest.PreflightError("correction table is not a manifest-pinned artifact") from None
    if receipt["manifest_sha256"] != verified.manifest_sha256:
        raise input_manifest.PreflightError("input manifest changed after store verification")
    pins = {Path(row["path"]): row["sha256"] for row in receipt["files"]}
    if pins.get(path) != table.sha256 or input_manifest.fingerprint(path) != table.sha256:
        raise input_manifest.PreflightError("correction bytes differ from their manifest pin")
    loaded = load_corrections(path.read_bytes(), kind=table.kind, filename=path.name,
                              expected_sha256=table.sha256)
    if loaded != table:
        raise ValueError("correction table object differs from its pinned CSV bytes")
    return table, table.sha256


def _press_content_sha256(*, url: str, title: str, summary: str | None,
                          published_at: str, tickers: list[str]) -> str:
    content = {"url": url, "title": title, "summary": summary,
               "published_at": published_at, "tickers": tickers}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _headline_archive(conn: sqlite3.Connection, ticker: str, start: datetime,
                      end: datetime, cutoff: datetime, mode: str, *, store_id: str,
                      snapshot_id: str, known_source_ids: dict[str, str]) -> tuple[list[dict], int, int]:
    rows = conn.execute(
        "SELECT symbol,news_id,published_utc,headline,url,source,summary,first_seen_at "
        "FROM finnhub_news ORDER BY symbol,news_id"
    ).fetchall()
    leads, excluded_missing_published, excluded_unobserved = [], 0, 0
    for row in rows:
        symbol = _require_text(row[0], "symbol")
        published_raw = _require_text(row[2], "published_utc")
        published = _instant(published_raw, "published_utc")
        first_seen_raw = _require_text(row[7], "first_seen_at")
        observation_available = _observation_available_at(first_seen_raw)
        if not (start < published <= end):
            continue
        if symbol.upper() != ticker:
            continue
        if published > cutoff:
            continue
        if mode == "observed" and observation_available > cutoff:
            excluded_unobserved += 1
            continue
        news_id = row[1]
        if type(news_id) is not int or news_id < 0:
            raise ValueError("headline archive has invalid news_id")
        source_id = ("daily:finnhub" if known_source_ids.get("daily:finnhub") == "daily"
                     else _registered_source_id("news_archive", "finnhub_company", known_source_ids))
        leads.append(_lead(
            adapter="headline_archive", source_id=source_id,
            item_id=f"{symbol}:{news_id}", ticker=ticker,
            title=_require_text(row[3], "headline", allow_empty=True),
            url=_require_text(row[4], "url"), published_at=_stamp(published),
            first_seen_at=first_seen_raw, summary=_optional_text(row[6], "summary"),
            publisher=_optional_text(row[5], "source"), mode=mode,
            store_id=store_id, snapshot_id=snapshot_id,
            observation_available_at=(_stamp(observation_available) if mode == "observed" else None),
        ))
    return leads, excluded_missing_published, excluded_unobserved


def _news_archive(conn: sqlite3.Connection, ticker: str, start: datetime,
                  end: datetime, cutoff: datetime, mode: str, *, store_id: str,
                  snapshot_id: str, known_source_ids: dict[str, str],
                  correction_table: CorrectionTable | None = None,
                  correction_store_scope: str | None = None) -> tuple[list[dict], int, int]:
    rows = conn.execute(
        "SELECT provider,item_key,url,title,summary,publisher,published_at,tickers_json,first_seen_at "
        "FROM news_items ORDER BY provider,item_key"
    ).fetchall()
    leads, excluded_missing_published, excluded_unobserved = [], 0, 0
    for row in rows:
        provider = _require_text(row[0], "provider")
        source_id = _registered_source_id("news_archive", provider, known_source_ids)
        item_key = _require_text(row[1], "item_key")
        membership = _ticker_membership(row[7])
        published_raw = row[6]
        first_seen_raw = _require_text(row[8], "first_seen_at")
        observation_available = _observation_available_at(first_seen_raw)
        if ticker not in membership:
            continue
        if published_raw is None:
            excluded_missing_published += 1
            continue
        published_raw = _require_text(published_raw, "published_at")
        effective_raw = published_raw
        if correction_table is not None:
            decision = correction_table.apply(
                published_raw, store=correction_store_scope,
                url=_require_text(row[2], "url"), publisher=_optional_text(row[5], "publisher"),
            )
            effective_raw = decision.effective_at
        published = _instant(_require_text(effective_raw, "effective published_at"),
                             "effective published_at")
        if not (start < published <= end) or published > cutoff:
            continue
        if mode == "observed" and observation_available > cutoff:
            excluded_unobserved += 1
            continue
        leads.append(_lead(
            adapter="news_archive", source_id=source_id, item_id=item_key,
            ticker=ticker, title=_require_text(row[3], "title"),
            url=_require_text(row[2], "url"), published_at=_stamp(published),
            first_seen_at=first_seen_raw, summary=_optional_text(row[4], "summary"),
            publisher=_optional_text(row[5], "publisher"), mode=mode,
            store_id=store_id, snapshot_id=snapshot_id,
            observation_available_at=(_stamp(observation_available) if mode == "observed" else None),
        ))
    return leads, excluded_missing_published, excluded_unobserved


def _press_rss(conn: sqlite3.Connection, ticker: str, start: datetime,
               end: datetime, cutoff: datetime, mode: str, *, store_id: str,
               snapshot_id: str, known_source_ids: dict[str, str]) -> tuple[list[dict], int, int]:
    rows = conn.execute(
        "SELECT provider,guid,url,title,summary,published_at,tickers_json,feed,first_seen_at "
        "FROM press_rss ORDER BY provider,guid"
    ).fetchall()
    has_observations = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='press_rss_observations'"
    ).fetchone() is not None
    leads, excluded_missing_published, excluded_unobserved = [], 0, 0
    for row in rows:
        provider = _require_text(row[0], "provider")
        source_id = _registered_source_id("press_rss", provider, known_source_ids)
        guid = _require_text(row[1], "guid")
        ticker_values = _ticker_values(row[6])
        membership = frozenset(ticker_values)
        first_seen_raw = _require_text(row[8], "first_seen_at")
        published_raw = row[5]
        if ticker not in membership:
            continue
        if published_raw is None:
            excluded_missing_published += 1
            continue
        published = _instant(_require_text(published_raw, "published_at"), "published_at")
        if not (start < published <= end) or published > cutoff:
            continue
        observation_available = None
        if mode == "observed":
            if has_observations:
                content_sha = _press_content_sha256(
                    url=_require_text(row[2], "url"),
                    title=_require_text(row[3], "title"),
                    summary=_optional_text(row[4], "summary"),
                    published_at=_require_text(published_raw, "published_at"),
                    tickers=ticker_values,
                )
                candidates = conn.execute(
                    "SELECT content_sha256,observed_at FROM press_rss_observations "
                    "WHERE provider=? AND guid=?", (provider, guid),
                ).fetchall()
                matching = []
                for observed_sha, observed_raw in candidates:
                    observed_sha = _require_text(observed_sha, "observation content hash")
                    observed_at = _instant(_require_text(observed_raw, "observation timestamp"),
                                           "observation timestamp")
                    if observed_sha == content_sha and observed_at <= cutoff:
                        matching.append(observed_at)
                observation_available = min(matching) if matching else None
            if observation_available is None:
                excluded_unobserved += 1
                continue
        leads.append(_lead(
            adapter="press_rss", source_id=source_id, item_id=guid,
            ticker=ticker, title=_require_text(row[3], "title"),
            url=_require_text(row[2], "url"), published_at=_stamp(published),
            first_seen_at=first_seen_raw, summary=_optional_text(row[4], "summary"),
            feed=_require_text(row[7], "feed"),
            mode=mode, store_id=store_id, snapshot_id=snapshot_id,
            observation_available_at=(_stamp(observation_available)
                                      if observation_available is not None else None),
        ))
    return leads, excluded_missing_published, excluded_unobserved


def _bar_sessions(session_dates: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(session_dates, (tuple, list)) or not session_dates:
        raise ValueError("market-bar adapters require explicit session_dates")
    normalized = []
    for raw in session_dates:
        if not isinstance(raw, str):
            raise ValueError("session_dates must contain canonical dates")
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            raise ValueError("session_dates must contain canonical dates") from None
        if parsed.isoformat() != raw or not trading_calendar.is_trading_day(raw):
            raise ValueError("session_dates must identify supported trading sessions")
        normalized.append(raw)
    if len(normalized) != len(set(normalized)):
        raise ValueError("session_dates must not contain duplicates")
    return tuple(sorted(normalized))


def _bar_values(row: sqlite3.Row) -> dict[str, float]:
    values = {}
    for name in ("open", "high", "low", "close", "volume"):
        value = row[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"market bar has invalid {name}")
        values[name] = float(value)
    return values


def _bar_provider(value: Any, adapter: str) -> str:
    provider = _require_text(value, "provider")
    supported = {
        "daily_bars": {"yfinance", "polygon"},
        "minute_bars": {"alpaca", "yfinance", "polygon"},
    }[adapter]
    if provider not in supported:
        raise ValueError("market bar has an unsupported provider")
    return provider


def _bar_fact(*, adapter: str, source_id: str, ticker: str,
              store_id: str, snapshot_id: str, session_date: str,
              provider: str, values: dict[str, float], date_value: str | None = None,
              timestamp: str | None = None) -> dict[str, Any]:
    fact = {
        "adapter": adapter,
        "source_id": source_id,
        "provider": provider,
        "store_id": store_id,
        "snapshot_id": snapshot_id,
        "product": "bars",
        "subject_kind": "instrument",
        "subject_id": ticker,
        "session_date": session_date,
        **values,
        "temporal_status": "unverified_published",
        "evidence_role": "fact",
    }
    if date_value is not None:
        fact["date"] = date_value
    if timestamp is not None:
        fact["timestamp"] = timestamp
    return fact


def _daily_bars(conn: sqlite3.Connection, ticker: str, start: datetime,
                end: datetime, cutoff: datetime, mode: str, *, store_id: str,
                snapshot_id: str,
                market_source_id: str,
                session_dates: tuple[str, ...]) -> tuple[list[dict], int]:
    sessions = set(session_dates)
    metadata_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ohlcv_metadata'"
    ).fetchone() is not None
    metadata_columns = (",m.first_seen_at,m.provenance" if metadata_exists else
                        ",NULL AS first_seen_at,NULL AS provenance")
    join = (" LEFT JOIN ohlcv_metadata m ON m.symbol=o.symbol AND m.date=o.date"
            if metadata_exists else "")
    rows = conn.execute(
        "SELECT o.symbol,o.date,o.open,o.high,o.low,o.close,o.volume,o.source" + metadata_columns
        + " FROM ohlcv o" + join + " ORDER BY o.symbol,o.date,o.source"
    ).fetchall()
    facts, excluded_unobserved = [], 0
    for row in rows:
        symbol = _require_text(row["symbol"], "symbol")
        if symbol.upper() != ticker:
            continue
        session = _require_text(row["date"], "session date")
        try:
            parsed_session = date.fromisoformat(session)
        except ValueError:
            raise ValueError("daily bar has invalid session date") from None
        if parsed_session.isoformat() != session or session not in sessions:
            continue
        provider = _bar_provider(row["source"], "daily_bars")
        # The scheduled close only gates session maturity and window membership;
        # it is not surfaced as publication or observation time.
        try:
            close_at = _instant(trading_calendar.session_close_utc(session), "session close")
        except ValueError:
            raise ValueError("daily bar session is not a supported trading session") from None
        if not (start < close_at <= end and close_at <= cutoff):
            continue
        values = _bar_values(row)
        first_seen_at = row["first_seen_at"]
        unknown_observation = first_seen_at is None
        if mode == "observed":
            if unknown_observation:
                excluded_unobserved += 1
                continue
            observed_at = _instant(first_seen_at, "first_seen_at")
            if observed_at > cutoff:
                excluded_unobserved += 1
                continue
        fact = _bar_fact(
            adapter="daily_bars", source_id=market_source_id, ticker=ticker,
            store_id=store_id, snapshot_id=snapshot_id, session_date=session,
            provider=provider, date_value=session, values=values,
        )
        fact["observation_provenance"] = (
            "legacy_unknown" if unknown_observation else "known"
        )
        facts.append(fact)
    return facts, excluded_unobserved


def _bar_instant(value: str, name: str) -> datetime:
    precision = re.search(r"[T ]\d{2}:\d{2}:\d{2}\.(\d+)", value)
    if precision is not None and len(precision.group(1)) > 6:
        raise ValueError(f"market bar {name} exceeds supported microsecond precision")
    return _instant(value, name)


def _minute_bars(conn: sqlite3.Connection, ticker: str, start: datetime,
                 end: datetime, cutoff: datetime, mode: str, *, store_id: str,
                 snapshot_id: str,
                 market_source_id: str,
                 session_dates: tuple[str, ...]) -> tuple[list[dict], int]:
    sessions = set(session_dates)
    rows = conn.execute(
        "SELECT symbol,ts,session_date,open,high,low,close,volume,source,fetched_at "
        "FROM minute_bars ORDER BY symbol,ts,source"
    ).fetchall()
    facts, excluded_unobserved = [], 0
    for row in rows:
        symbol = _require_text(row["symbol"], "symbol")
        if symbol.upper() != ticker:
            continue
        timestamp_raw = _require_text(row["ts"], "timestamp")
        timestamp = _bar_instant(timestamp_raw, "timestamp")
        session = _require_text(row["session_date"], "session date")
        try:
            parsed_session = date.fromisoformat(session)
        except ValueError:
            raise ValueError("minute bar has invalid session date") from None
        if parsed_session.isoformat() != session:
            raise ValueError("minute bar has invalid session date")
        if timestamp.astimezone(timefields.NY).date().isoformat() != session:
            raise ValueError("minute bar session date does not match its timestamp")
        if session not in sessions or not (start < timestamp <= end) or timestamp > cutoff:
            continue
        provider = _bar_provider(row["source"], "minute_bars")
        values = _bar_values(row)
        if mode == "observed":
            # Legacy fetched_at is recorded before provider requests, so it
            # cannot prove when these returned rows became available.
            _require_text(row["fetched_at"], "fetched_at")
            excluded_unobserved += 1
            continue
        facts.append(_bar_fact(
            adapter="minute_bars", source_id=market_source_id, ticker=ticker,
            store_id=store_id, snapshot_id=snapshot_id, session_date=session,
            provider=provider, timestamp=_stamp(timestamp), values=values,
        ))
    return facts, excluded_unobserved


def read_store_facts(
    verified: VerifiedStores,
    store_id: str,
    adapter: str,
    ticker: str,
    window_start: str | datetime,
    window_end: str | datetime,
    at: str | datetime,
    *,
    mode: str,
    session_dates: tuple[str, ...] = (),
    registry_path: str | Path,
    market_source_id: str | None = None,
    correction_table_id: str | None = None,
    correction_tables: tuple[CorrectionTable, ...] = (),
    correction_store_ids: dict[str, str] | None = None,
    correction_artifacts: dict[str, str | Path] | None = None,
) -> dict[str, Any]:
    """Read typed positive facts from one verified store snapshot.

    ``observed`` headline reads require a conservative availability bound for
    legacy second-precision timestamps, or an exact content-bound observation
    for RSS. ``published`` reads remain unverified leads because publication
    time alone does not show when archived metadata became available. Legacy
    market bars are returned only as unverified event/session facts; session
    dates and fetched_at do not establish observation time. This function
    never proves coverage completeness or emits body evidence.
    """
    if not isinstance(adapter, str) or adapter not in _ADAPTERS:
        raise ValueError("unsupported read adapter")
    if not isinstance(mode, str) or mode not in {"observed", "published"}:
        raise ValueError("mode must be observed or published")
    market_adapter = adapter in {"daily_bars", "minute_bars"}
    if not market_adapter and market_source_id is not None:
        raise ValueError("market_source_id is only valid for bar adapters")
    if market_adapter and (correction_table_id is not None or correction_tables
                           or correction_store_ids or correction_artifacts):
        raise ValueError("market bars do not support publication-time corrections")
    if session_dates and not market_adapter:
        raise ValueError("session_dates are only supported by market-bar adapters")
    normalized_sessions = _bar_sessions(session_dates) if market_adapter else ()
    if not isinstance(ticker, str) or not ticker.strip() or ticker != ticker.strip():
        raise ValueError("ticker must be a nonempty trimmed identifier")
    if not isinstance(store_id, str) or not store_id.strip() or store_id != store_id.strip():
        raise ValueError("store_id must be a nonempty trimmed identifier")
    ticker = ticker.upper()
    start = _instant(window_start, "window_start")
    end = _instant(window_end, "window_end")
    cutoff = _instant(at, "at")
    if start >= end:
        raise ValueError("requested time window must be nonempty")
    if not isinstance(verified, VerifiedStores):
        raise ValueError("manifest-verified stores are required")
    validate_verified_stores(verified)
    known_source_ids, source_categories, registry_sha256 = _pinned_registry(verified, registry_path)
    normalized_market_source_id = (
        _validate_market_source_binding(
            adapter, market_source_id, known_source_ids, source_categories,
        ) if market_adapter else None
    )
    table, correction_sha256 = _pinned_corrections(
        verified, correction_table_id, tuple(correction_tables),
        {} if correction_store_ids is None else dict(correction_store_ids),
        {} if correction_artifacts is None else dict(correction_artifacts),
    )
    correction_bindings = {} if correction_store_ids is None else dict(correction_store_ids)
    if table is not None and store_id not in correction_bindings:
        raise ValueError("selected store requires an explicit correction scope binding")
    if table is not None and set(correction_bindings) != {store_id}:
        raise ValueError("correction scope bindings must name only the selected store")
    if table is not None and table.kind != "finnhub":
        raise ValueError("news archive supports only Finnhub correction tables")
    store = verified.stores.get(store_id)
    if store is None:
        raise ValueError("store_id is not present in the verified store set")

    try:
        with readonly_snapshot(verified, store) as conn:
            _schema(conn, adapter)
            readers = {
                "headline_archive": _headline_archive,
                "news_archive": _news_archive,
                "press_rss": _press_rss,
                "daily_bars": _daily_bars,
                "minute_bars": _minute_bars,
            }
            kwargs = {
                "store_id": store_id,
                "snapshot_id": store.sha256,
                "known_source_ids": known_source_ids,
            }
            if adapter == "news_archive":
                kwargs["correction_table"] = table
                kwargs["correction_store_scope"] = correction_bindings.get(store_id)
            if market_adapter:
                kwargs["session_dates"] = normalized_sessions
                kwargs["market_source_id"] = normalized_market_source_id
                kwargs.pop("known_source_ids")
                facts, unobserved = readers[adapter](
                    conn, ticker, start, end, cutoff, mode, **kwargs,
                )
                leads, missing_published = [], 0
            else:
                leads, missing_published, unobserved = readers[adapter](
                    conn, ticker, start, end, cutoff, mode, **kwargs,
                )
                facts = []
    except sqlite3.Error as exc:
        raise ValueError("store snapshot is unreadable or has an invalid adapter schema") from exc

    leads.sort(key=lambda item: (item["published_at"], item["source_id"], item["item_id"]))
    return {
        "metadata_leads": leads,
        "market_facts": facts,
        "audit": {
            "adapter": adapter,
            "mode": mode,
            "manifest_sha256": verified.manifest_sha256,
            "source_registry_sha256": registry_sha256,
            "data_snapshot_id": store.sha256,
            "data_snapshot_ids": {store_id: store.sha256},
            "read_only": True,
            "market_source_id": normalized_market_source_id,
            "metadata_lead_count": len(leads),
            "market_fact_count": len(facts),
            "session_dates": list(normalized_sessions),
            "excluded_missing_published_count": missing_published,
            "excluded_unobserved_count": unobserved,
            "correction_table_id": None if table is None else table.table_id,
            "correction_table_sha256": correction_sha256,
            "correction_store_ids": ({store_id: correction_bindings[store_id]}
                                      if table is not None else {}),
        },
    }


__all__ = ["read_store_facts"]
