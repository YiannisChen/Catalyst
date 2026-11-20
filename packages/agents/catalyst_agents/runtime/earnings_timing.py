"""Run-scoped reads and deterministic qualification for issuer earnings timing."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from catalyst_data.storage.errors import StoreReadError

_ACCESSION_IN_SEC_URL = re.compile(r"/Archives/edgar/data/\d+/(\d{18})(?:/|$)", re.IGNORECASE)
_CURRENT: ContextVar["EarningsTimingCache | None"] = ContextVar("earnings_timing_cache", default=None)
logger = logging.getLogger(__name__)


def accession_from_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if (parts.hostname or "").casefold().rstrip(".") not in {"sec.gov", "www.sec.gov"}:
        return None
    match = _ACCESSION_IN_SEC_URL.search(parts.path)
    if match is None:
        return None
    digits = match.group(1)
    return f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"


def qualifies_as_issuer_earnings(timing: dict[str, Any] | None) -> bool:
    if not (
        timing
        and timing.get("issuer_cik_match") is True
        and timing.get("is_issuer_earnings") is True
        and timing.get("release_session") in {
            "pre_market", "intraday", "after_close", "non_trading_day",
        }
        and isinstance(timing.get("corrected_at"), str)
        and isinstance(timing.get("attributed_session_date"), str)
        and isinstance(timing.get("release_local_time"), str)
    ):
        return False
    try:
        accepted = datetime.fromisoformat(timing["corrected_at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    return accepted.tzinfo is not None and accepted.utcoffset() is not None


def public_release_fields(timing: dict[str, Any] | None) -> dict[str, str] | None:
    if not qualifies_as_issuer_earnings(timing):
        return None
    session = timing["release_session"]
    public_session = {
        "pre_market": "pre",
        "intraday": "intraday",
        "after_close": "post",
        "non_trading_day": "post",
    }[session]
    try:
        local = datetime.fromisoformat(timing["release_local_time"])
        if local.tzinfo is None or local.utcoffset() is None:
            return None
        release_time_et = local.isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return None
    return {"release_session": public_session, "release_time_et": release_time_et}


def apply_earnings_timing(extraction: Any, timing: dict[str, Any] | None, *, session_date: str):
    """Override model timing only for an explicitly matched issuer earnings 8-K."""
    from catalyst_agents.attribution.extraction import (
        AboutTicker,
        EventTiming,
        EventType,
        Session,
    )

    if (not qualifies_as_issuer_earnings(timing)
            or extraction.about_ticker is not AboutTicker.ISSUER
            or extraction.event_type is not EventType.EARNINGS):
        return extraction
    release = timing["release_session"]
    assigned_session = (
        Session.THIS_SESSION
        if timing["attributed_session_date"] == session_date
        else Session.OTHER_SESSION
    )
    if assigned_session is Session.OTHER_SESSION:
        return extraction.__class__(**{
            **extraction.__dict__,
            "session": assigned_session,
        })
    event_timing = EventTiming.INTRADAY if release == "intraday" else EventTiming.BEFORE_OPEN
    return extraction.__class__(**{
        **extraction.__dict__,
        "session": Session.THIS_SESSION,
        "event_timing": event_timing,
    })


class EarningsTimingCache:
    """Per-run accession cache; stores both no-match results and read failures."""

    def __init__(self) -> None:
        self._by_accession: dict[str, dict[str, Any] | None] = {}
        self._timings: dict[str, dict[str, float | int]] = {}

    def _record_timing(self, name: str, duration_ms: float) -> None:
        timing = self._timings.setdefault(name, {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
        timing["count"] = int(timing["count"]) + 1
        timing["total_ms"] = float(timing["total_ms"]) + duration_ms
        timing["max_ms"] = max(float(timing["max_ms"]), duration_ms)

    def for_item(self, item: Any, *, ticker: str) -> dict[str, Any] | None:
        started = time.perf_counter()
        try:
            return self._for_item(item, ticker=ticker)
        finally:
            self._record_timing("earnings_timing_cache_lookup", (time.perf_counter() - started) * 1000)

    def _for_item(self, item: Any, *, ticker: str) -> dict[str, Any] | None:
        url = item.get("canonical_url") if isinstance(item, dict) else getattr(item, "canonical_url", None)
        accession = accession_from_url(url)
        if accession is None:
            return None
        if accession not in self._by_accession:
            started = time.perf_counter()
            try:
                self._by_accession[accession] = self._read(accession, ticker)
            finally:
                self._record_timing("earnings_timing_cache_read", (time.perf_counter() - started) * 1000)
        return self._by_accession[accession]

    def _read(self, accession: str, ticker: str) -> dict[str, Any] | None:
        from catalyst_data import read_api
        from catalyst_data.ingestion.collectors import sec, sec_filings

        path = (
            os.environ.get("CATALYST_SEC_FILING_PROJECTION_DB", "").strip()
            or os.environ.get("CATALYST_NEWS_BODIES_DB", "").strip()
        )
        if not path:
            raise StoreReadError(
                "CATALYST_SEC_FILING_PROJECTION_DB", "read earnings_release_timing", "store path is unset"
            )
        correction_table_path = (
            Path(read_api.__file__).resolve().parents[3]
            / "data/corrections/sec_acceptance_v1.csv"
        )
        read_started = time.perf_counter()
        try:
            rows = read_api.earnings_release_timing(
                path,
                accessions=[accession],
                immutable=False,
                correction_table_path=correction_table_path,
                ticker_mapping_path=sec._tickers_path(),
                submissions_dir=sec_filings._dir(),
                timing_callback=self._record_timing,
            )
        except read_api.SecFilingDataError as exc:
            message = str(exc)
            if (message == "no SEC filing rows matched the requested local data"
                    or message.startswith("requested accessions are absent from the local SEC filing projection:")):
                return None
            raise StoreReadError(path, "read earnings_release_timing", message) from exc
        except (OSError, ValueError) as exc:
            raise StoreReadError(path, "read earnings_release_timing", str(exc)) from exc
        except Exception as exc:
            from catalyst_data.trading_calendar import CalendarCoverageError

            if not isinstance(exc, CalendarCoverageError):
                raise
            raise StoreReadError(path, "read earnings_release_timing", str(exc)) from exc
        finally:
            self._record_timing("read_api.earnings_release_timing", (time.perf_counter() - read_started) * 1000)
        return next((row for row in rows if str(row.get("ticker", "")).upper() == ticker.upper()), None)

    def for_context_pack(self, context_pack: Any) -> dict[str, dict[str, Any]]:
        observation = getattr(context_pack, "observation", None)
        ticker = str(getattr(observation, "ticker", "") or "")
        if not ticker:
            return {}
        return {
            item.evidence_id: timing
            for item in getattr(context_pack, "evidence_inventory", ())
            if (timing := self.for_item(item, ticker=ticker)) is not None
        }


@contextmanager
def run_earnings_timing_scope() -> Iterator[EarningsTimingCache]:
    from catalyst_data.ingestion.collectors import sec_filings

    cache = EarningsTimingCache()
    token = _CURRENT.set(cache)
    # A run reads SEC from the local cache only: an issuer can have hundreds of uncached index
    # pages, and fetching them one by one held runs for minutes past their deadline.
    sec_scope = sec_filings.cached_only()
    sec_scope.__enter__()
    try:
        yield cache
    finally:
        path = os.environ.get("CATALYST_AG_TIMINGS_PATH", "").strip()
        if path:
            target = Path(path)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "a", encoding="utf-8") as timings_file:
                    timings_file.write(json.dumps({"timings": cache._timings}, sort_keys=True) + "\n")
            except OSError as exc:
                logger.warning("Could not append A-g earnings timing to %s: %s", target, exc)
        logger.debug("A-g earnings timing: %s", json.dumps(cache._timings, sort_keys=True))
        sec_scope.__exit__(None, None, None)
        _CURRENT.reset(token)


def current_earnings_timing_cache() -> EarningsTimingCache | None:
    return _CURRENT.get()


def earnings_timings_for_context_pack(context_pack: Any) -> dict[str, dict[str, Any]]:
    """Resolve run-cached timing upstream for an explicit deterministic call."""
    cache = current_earnings_timing_cache()
    return cache.for_context_pack(context_pack) if cache is not None else {}


def with_earnings_timing_scope(function):
    """Give every graph invocation one cache, including local evaluation entry points."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with run_earnings_timing_scope():
            return function(*args, **kwargs)

    return wrapped
