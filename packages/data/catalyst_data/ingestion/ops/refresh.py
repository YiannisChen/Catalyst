"""On-demand M9 refresh orchestration and CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from catalyst_data import trading_calendar
from catalyst_data.ingestion import ledger
from catalyst_data.ingestion.collectors import (
    benzinga,
    fetch,
    headlines,
    kitco,
    market,
    sec,
)
from catalyst_data.ingestion.ops import status
from catalyst_data.ingestion.storage import page_versions, store
from catalyst_data.ingestion.time import timefields, times
from catalyst_data.retrieval import web_bodies


class _NothingToLoad(Exception):
    """No bars or SIC are missing: skip the market-store lock."""


# Worker threads for a session's network work (discovery plus news subjects).
SUBJECT_WORKERS = int(os.environ.get("CATALYST_SUBJECT_WORKERS", "5"))

_SITEMAP_UNSET = object()
_SITEMAP_CACHE: str | None | object = _SITEMAP_UNSET


def _kitco_sitemap(fetcher) -> str | None:
    """Fetch Kitco's XML sitemaps (Reuters wire + Kitco News) once per process."""
    global _SITEMAP_CACHE
    if _SITEMAP_CACHE is not _SITEMAP_UNSET:
        return _SITEMAP_CACHE  # type: ignore[return-value]
    parts = []
    for url in kitco.SITEMAPS:
        try:
            if fetcher.allowed(url):
                response = fetcher.get(url)
                if response.status_code == 200:
                    parts.append(response.text)
        except Exception:
            pass
    # Both are required: a partial sitemap would record a session as exhausted.
    _SITEMAP_CACHE = "\n".join(parts) if len(parts) == len(kitco.SITEMAPS) else None
    return _SITEMAP_CACHE


@dataclass
class Deps:
    fetcher_factory: Callable = fetch.Fetcher
    select_with_status: Callable = headlines.select_with_status
    load_bars: Callable = market.load_bars
    load_sic: Callable = market.load_sic
    sec_intake: Callable = sec.intake
    kitco_sitemap: Callable = _kitco_sitemap
    benzinga_discover: Callable = benzinga.discover
    now: datetime | None = None
    # Whether select_with_status may run on a worker thread. A selector that reads a SQLite connection opened on
    # the calling thread (the daily job's archived selector) must say False: selection then runs here, in order,
    # and only the page fetching and Kitco run on workers.
    select_in_workers: bool = True


@dataclass
class RefreshReport:
    ticker: str
    session_date: str
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _safe_error(exc: Exception) -> str:
    value = str(exc)
    value = re.sub(
        r"(?i)\b(token|api[_-]?key|secret|password)\b\s*[:=]\s*([^&\s,;]+)",
        r"\1=[REDACTED_SECRET]", value,
    )
    return re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED_SECRET]", value)


def _error(report: RefreshReport, step: str, exc: Exception) -> str:
    message = f"{step}: {type(exc).__name__}: {_safe_error(exc)}"
    report.errors.append(message)
    return message


def _company_rows(fetcher, items: list[dict], now: datetime | None = None,
                  deferred: list[dict] | None = None, cutoff: str | None = None) -> list[dict]:
    """Bodies for the items. Items whose host did not answer go to ``deferred``
    (when given) instead of being skipped."""
    rows = []
    fetcher = fetch.PrefetchedFetcher(fetcher, [item["url"] for item in items])
    for item in items:
        try:
            result = fetcher.fetch_page(item["url"])
        except fetch.HostUnavailable as exc:
            if deferred is None:
                raise
            deferred.append({**item, "host": exc.host, "reason": str(exc)})
            continue
        if result is None:
            continue
        final_url, page_html = result
        page_versions.record("company_body", item["url"], final_url=final_url, page_html=page_html,
                             subject=item.get("ticker"), cutoff=cutoff)
        text = fetch.extract(page_html, item["title"])
        if text is None:
            continue
        publisher = web_bodies.publisher_for_url(final_url)
        if publisher is None:
            continue
        # The API's time can be the New York clock labelled UTC (Finnhub for CNBC): keep both times, use the later.
        page_published = page_versions.declared_times(page_html)[0]
        eligible_at, time_flag = times.reconcile(item["published"], page_published, provider=item.get("provider"),
                                                 publisher=publisher)
        page_versions.record_times(final_url, provider=item.get("provider"), publisher=publisher,
                                   api_published=item["published"], page_published=page_published,
                                   eligible_at=eligible_at, flag=time_flag)
        if eligible_at is None:
            continue  # imprecise: a known-skewed source and no page time to check it against
        host = urlparse(final_url).netloc
        page_image = fetch.page_image_with_source(page_html, final_url)
        rows.append({
            "asset_id": "web:" + hashlib.sha256(final_url.encode()).hexdigest()[:32],
            "text": text,
            "url": final_url,
            "title": item["title"],
            "publisher": publisher,
            "source_class": fetch.SOURCE_CLASS_BY_HOST.get(host, fetch.DEFAULT_SOURCE_CLASS),
            "eligible_at": eligible_at,
            "fetched_at": _attempt_time(now),
            "body_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "tickers": [item["ticker"]],
            **({"image_url": page_image[0], "image_source": page_image[1]}
               if page_image else {}),
        })
    return rows


def _missing_symbols(missing: list[str], ticker: str) -> list[str]:
    symbols = []
    if "bars:target" in missing or "bars:target_history" in missing:
        symbols.append(ticker)
    symbols.extend(
        name.removeprefix("bars:") for name in missing
        if name.startswith("bars:") and not name.startswith("bars:peer:")
        and name not in ("bars:target", "bars:target_history")
    )
    symbols.extend(name.removeprefix("bars:peer:") for name in missing if name.startswith("bars:peer:"))
    if "bars:SPY" in missing:
        symbols.append("SPY")
    return list(dict.fromkeys(symbols))


def _action(report, *, step, subject, result, n_headlines=0, n_bodies=0, detail=""):
    report.actions.append({
        "step": step, "subject": subject, "status": result,
        "headlines": n_headlines, "bodies": n_bodies, "detail": detail,
    })


def _attempt_time(now: datetime | None) -> str:
    return timefields.stamp(timefields.now_utc(now))


def _news_items(missing: list[str]):
    # Preserve the status contract's order, with peer/proxy names already ordered there.
    ordered = [name for name in missing if name == "news:target"]
    ordered += [name for name in missing if name.startswith("news:peer:")]
    ordered += [name for name in missing if name.startswith("news:proxy:")]
    ordered += [name for name in missing if name == "news:market"]
    return ordered


def _discover_subjects(ticker, scope_info) -> dict[str, str]:
    subjects = {ticker: "target"}
    subjects.update({peer: "peer" for peer in scope_info.get("news_peers") or ()})
    proxy = scope_info.get("proxy")
    if proxy:
        subjects[proxy] = "proxy"
    sector = scope_info.get("sector")
    if sector:
        subjects[sector] = "sector"
    subjects["SPY"] = "market"
    return subjects


def _discover_rows(conn, fetcher, deps, subjects, start, cutoff, fetched_at):
    """The network part of discovery: (rows, candidates). ``conn`` is only read."""
    rows, n_candidates = deps.benzinga_discover(fetcher, conn, subjects, start, cutoff, fetched_at)
    xml = deps.kitco_sitemap(fetcher)
    if xml is None:
        raise RuntimeError("kitco sitemaps unavailable")
    entries = kitco.parse_sitemap(xml)
    for subject in [s for s, kind in subjects.items() if kind in ("proxy", "sector", "market")]:
        wanted = kitco.candidates(entries, subject, start, cutoff, broad_limit=kitco.MAX_BROAD_PER_SESSION)
        n_candidates += len(wanted)
        rows += [{**row, "fetched_at": fetched_at}
                 for row in kitco.fetch_bodies(fetcher, wanted, subject, start, cutoff, conn)]
    return rows, n_candidates


def _discover_write(report, conn, deps, ticker, session_date, subjects, outcome):
    """Store discovery's rows and its one ledger row; ``outcome`` is (rows, candidates) or the exception raised."""
    try:
        if isinstance(outcome, Exception):
            raise outcome
        rows, n_candidates = outcome
        stored = store.write_assets(conn, rows)
        result = "OK" if rows else "EMPTY"
        ledger.record_attempt(
            conn, subject=ticker, session_date=session_date, source=status.DISCOVERY_SOURCE,
            status=result, headlines=n_candidates, bodies=len(rows),
            attempted_at=_attempt_time(deps.now),
        )
        _action(report, step="discovery", subject=",".join(subjects), result=result,
                n_headlines=n_candidates, n_bodies=len(rows),
                detail=f"new={stored.stored_assets} tagged={stored.tagged}")
    except Exception as exc:
        detail = _error(report, "discovery", exc)
        ledger.record_attempt(
            conn, subject=ticker, session_date=session_date, source=status.DISCOVERY_SOURCE,
            status="ERROR", headlines=0, bodies=0, attempted_at=_attempt_time(deps.now),
        )
        _action(report, step="discovery", subject=ticker, result="ERROR", detail=detail)


def _discover_session(report, conn, fetcher, deps, ticker, session_date, scope_info, start, cutoff):
    """Benzinga archive + Kitco session wires for every subject of the session,
    whatever the ticker feeds already returned (one ledger row per session)."""
    subjects = _discover_subjects(ticker, scope_info)
    fetched_at = _attempt_time(deps.now)
    try:
        outcome = _discover_rows(conn, fetcher, deps, subjects, start, cutoff, fetched_at)
    except Exception as exc:  # recorded by _discover_write
        outcome = exc
    _discover_write(report, conn, deps, ticker, session_date, subjects, outcome)


def _discover_in_thread(bodies_db, fetcher, deps, subjects, start, cutoff, fetched_at):
    """Discovery's network part on a worker thread, with its own read connection (SQLite connections stay on the
    thread that opened them)."""
    try:
        rconn = sqlite3.connect(bodies_db, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return exc
    try:
        return _discover_rows(rconn, fetcher, deps, subjects, start, cutoff, fetched_at)
    except Exception as exc:  # noqa: BLE001  (recorded by _discover_write)
        return exc
    finally:
        rconn.close()


def _news_subject(ticker, missing_item):
    is_company = missing_item == "news:target" or missing_item.startswith("news:peer:")
    subject = (
        ticker if missing_item == "news:target"
        else "SPY" if missing_item == "news:market"
        else missing_item.split(":", 2)[-1]
    )
    return subject, is_company


def _select(deps, ticker, missing_item, start, cutoff):
    """(items, provider_status, exception or None) for one news subject."""
    subject, _ = _news_subject(ticker, missing_item)
    limit = headlines.MAX_HEADLINES if subject == ticker else headlines.MAX_PEER_HEADLINES
    try:
        items, provider_status = deps.select_with_status(subject, start, cutoff, limit)
        return items, provider_status, None
    except Exception as exc:  # noqa: BLE001
        return [], "ERROR", exc


def _news_network(fetcher, deps, ticker, missing_item, start, cutoff, selection=None) -> dict:
    """The network part of one news subject: headlines, bodies and (for a proxy or the market) Kitco wires.
    Nothing is written; exceptions are returned for _news_write to record, exactly as the sequential loop did."""
    subject, is_company = _news_subject(ticker, missing_item)
    out = {"subject": subject, "is_company": is_company, "rows_exc": None}
    out["items"], out["provider_status"], out["select_exc"] = (
        selection if selection is not None else _select(deps, ticker, missing_item, start, cutoff))
    try:
        out["deferred"] = []
        out["rows"] = _company_rows(fetcher, out["items"], deps.now, out["deferred"], cutoff=cutoff)
    except Exception as exc:  # noqa: BLE001
        out["rows_exc"] = exc
    if not is_company:
        out["kitco_exc"] = None
        try:
            xml = deps.kitco_sitemap(fetcher)
            out["kitco_xml_missing"] = xml is None
            if xml is None:
                out["kitco_wanted"], out["kitco_rows"] = [], []
            else:
                out["kitco_wanted"] = kitco.candidates(
                    kitco.parse_sitemap(xml), subject, start, cutoff, broad_limit=kitco.MAX_BROAD_PER_SESSION,
                )
                out["kitco_rows"] = kitco.fetch_bodies(fetcher, out["kitco_wanted"], subject, start, cutoff)
        except Exception as exc:  # noqa: BLE001
            out["kitco_exc"] = exc
    return out


def _news_write(report, conn, deps, session_date, res) -> None:
    """Store one subject's news and write its ledger rows and report actions (the sequential loop's writes)."""
    subject, is_company = res["subject"], res["is_company"]
    source = "finnhub+polygon" if is_company else "finnhub"
    items, provider_status = res["items"], res["provider_status"]
    if res["select_exc"] is not None:
        _error(report, f"finnhub:{subject}", res["select_exc"])
    try:
        if res["rows_exc"] is not None:
            raise res["rows_exc"]
        rows, deferred = res["rows"], res["deferred"]
        store.write_assets(conn, rows)
        if deferred:
            store.defer(conn, deferred, session_date, _attempt_time(deps.now))
            provider_status = f"{provider_status} deferred={len(deferred)}"
        source_status = "OK" if rows else (
            "EMPTY" if provider_status.split()[0] in ("OK", "EMPTY")
            else provider_status.split()[0]
        )
        ledger.record_attempt(
            conn, subject=subject, session_date=session_date, source=source,
            status=source_status, headlines=len(items), bodies=len(rows),
            attempted_at=_attempt_time(deps.now),
        )
        _action(report, step=source, subject=subject, result=source_status,
                n_headlines=len(items), n_bodies=len(rows), detail=provider_status)
    except Exception as exc:
        detail = _error(report, f"finnhub:{subject}", exc)
        ledger.record_attempt(
            conn, subject=subject, session_date=session_date, source=source,
            status="ERROR", headlines=0, bodies=0, attempted_at=_attempt_time(deps.now),
        )
        _action(report, step=source, subject=subject, result="ERROR", detail=detail)
    if is_company:
        return
    try:
        if res["kitco_exc"] is not None:
            raise res["kitco_exc"]
        wanted, rows = res["kitco_wanted"], res["kitco_rows"]
        if res["kitco_xml_missing"]:
            kitco_status = "ERROR"
        else:
            if deps.now is not None:
                fetched_at = _attempt_time(deps.now)
                rows = [{**row, "fetched_at": fetched_at} for row in rows]
            store.write_assets(conn, rows)
            kitco_status = "OK" if rows else "EMPTY"
        ledger.record_attempt(
            conn, subject=subject, session_date=session_date, source="kitco",
            status=kitco_status, headlines=len(wanted), bodies=len(rows),
            attempted_at=_attempt_time(deps.now),
        )
        _action(report, step="kitco", subject=subject, result=kitco_status,
                n_headlines=len(wanted), n_bodies=len(rows),
                detail="sitemap unavailable" if res["kitco_xml_missing"] else "")
    except Exception as exc:
        detail = _error(report, f"kitco:{subject}", exc)
        ledger.record_attempt(
            conn, subject=subject, session_date=session_date, source="kitco",
            status="ERROR", headlines=0, bodies=0, attempted_at=_attempt_time(deps.now),
        )
        _action(report, step="kitco", subject=subject, result="ERROR", detail=detail)


def refresh_session(
    ticker: str,
    session_date: str,
    *,
    snapshot_db,
    market_db,
    bodies_db,
    peer_map,
    deps: Deps | None = None,
    max_subjects: int = 8,
) -> RefreshReport:
    deps = deps or Deps()
    page_versions.use_store_dir(bodies_db)
    report = RefreshReport(ticker=ticker, session_date=session_date)
    current = status.session_status(
        ticker, session_date, snapshot_db=snapshot_db, market_db=market_db,
        bodies_db=bodies_db, peer_map=peer_map, now=deps.now,
    )
    report.before = list(current.missing)
    if not current.closed:
        report.errors.append("SESSION_NOT_CLOSED")
        report.after = list(report.before)
        return report

    start_bars = (date.fromisoformat(session_date) - timedelta(days=120)).isoformat()
    symbols = _missing_symbols(report.before, ticker)
    sic_missing = "sic:target" in report.before
    try:
        if not symbols and not sic_missing:
            raise _NothingToLoad
        with store.refresh_lock(market_db):
            conn = market.open_market_writer(market_db)
            try:
                try:
                    deps.load_bars(conn, symbols, start_bars, session_date, now=deps.now)
                    _action(report, step="bars", subject=",".join(symbols), result="OK")
                except Exception as exc:
                    detail = _error(report, "bars", exc)
                    _action(report, step="bars", subject=",".join(symbols), result="ERROR", detail=detail)
                if sic_missing:
                    try:
                        deps.load_sic(conn, [ticker])
                        _action(report, step="sic", subject=ticker, result="OK")
                    except ValueError as exc:
                        if str(exc) == "SEC_USER_AGENT not set":
                            _action(report, step="sic", subject=ticker, result="DISABLED", detail=str(exc))
                        else:
                            detail = _error(report, "sic", exc)
                            _action(report, step="sic", subject=ticker, result="ERROR", detail=detail)
                    except Exception as exc:
                        detail = _error(report, "sic", exc)
                        _action(report, step="sic", subject=ticker, result="ERROR", detail=detail)
            finally:
                conn.close()
    except _NothingToLoad:
        pass
    except store.RefreshBusy:
        report.errors.append("REFRESH_BUSY")
        report.after = list(report.before)
        return report

    current = status.session_status(
        ticker, session_date, snapshot_db=snapshot_db, market_db=market_db,
        bodies_db=bodies_db, peer_map=peer_map, now=deps.now,
    )
    todo = _news_items(current.missing)[:max_subjects]
    sec_missing = "filings:target" in current.missing
    discovery_missing = "news:discovery" in current.missing
    try:
        if todo or sec_missing or discovery_missing:
            with store.refresh_lock(bodies_db):
                conn = store.open_writer(bodies_db)
                try:
                    ledger.ensure_ledger(conn)
                    start, cutoff = headlines.session_window(session_date)
                    if sec_missing:
                        try:
                            rows, result = deps.sec_intake(ticker, session_date, now=deps.now)
                            if result not in ledger.STATUSES:
                                raise ValueError("invalid SEC intake status")
                            store.write_assets(conn, rows)
                            ledger.record_attempt(
                                conn, subject=ticker, session_date=session_date, source="sec",
                                status=result, headlines=len(rows), bodies=len(rows),
                                attempted_at=_attempt_time(deps.now),
                            )
                            _action(report, step="sec", subject=ticker, result=result,
                                    n_headlines=len(rows), n_bodies=len(rows))
                        except Exception as exc:
                            detail = _error(report, "sec", exc)
                            ledger.record_attempt(
                                conn, subject=ticker, session_date=session_date, source="sec",
                                status="ERROR", headlines=0, bodies=0,
                                attempted_at=_attempt_time(deps.now),
                            )
                            _action(report, step="sec", subject=ticker, result="ERROR", detail=detail)
                    fetcher = deps.fetcher_factory() if todo or discovery_missing else None
                    # Network work runs on worker threads, at the same time: discovery (Benzinga / Kitco) and each
                    # subject's headlines and bodies, the target first. Writes, ledger rows and report actions stay
                    # on this thread in the sequential order. Provider budgets: ratelimit; hosts: fetch.Fetcher.
                    with ThreadPoolExecutor(max_workers=max(1, SUBJECT_WORKERS)) as pool:
                        discovery = None
                        if discovery_missing:
                            subjects = _discover_subjects(ticker, current.scope)
                            discovery = pool.submit(_discover_in_thread, bodies_db, fetcher, deps, subjects,
                                                    start, cutoff, _attempt_time(deps.now))
                        selected = ({} if deps.select_in_workers else
                                    {item: _select(deps, ticker, item, start, cutoff) for item in todo})
                        news = [pool.submit(_news_network, fetcher, deps, ticker, item, start, cutoff,
                                            selected.get(item))
                                for item in todo]
                        if discovery is not None:
                            _discover_write(report, conn, deps, ticker, session_date, subjects, discovery.result())
                        for future in news:
                            _news_write(report, conn, deps, session_date, future.result())
                finally:
                    conn.close()
    except store.RefreshBusy:
        report.errors.append("REFRESH_BUSY")
        report.after = list(report.before)
        return report

    report.after = status.session_status(
        ticker, session_date, snapshot_db=snapshot_db, market_db=market_db,
        bodies_db=bodies_db, peer_map=peer_map, now=deps.now,
    ).missing
    return report


def refresh_latest(
    tickers,
    *,
    since: str | None = None,
    max_sessions: int = 20,
    **paths_and_deps,
) -> list[RefreshReport]:
    if max_sessions <= 0:
        return []
    deps = paths_and_deps.get("deps") or Deps()
    last = market.last_closed_session(deps.now)
    if since is None:
        back = trading_calendar.calendar_trading_days(
            (date.fromisoformat(last) - timedelta(days=20)).isoformat(), last,
        )
        since = back[max(0, len(back) - 6)]
    sessions = list(reversed(trading_calendar.calendar_trading_days(since, last)))
    reports = []
    for session_date in sessions:
        for ticker in tickers:
            current = status.session_status(
                ticker, session_date, snapshot_db=paths_and_deps.get("snapshot_db"),
                market_db=paths_and_deps.get("market_db"), bodies_db=paths_and_deps.get("bodies_db"),
                peer_map=paths_and_deps.get("peer_map"), now=deps.now,
            )
            if current.missing:
                reports.append(refresh_session(ticker, session_date, **paths_and_deps))
                if len(reports) >= max_sessions:
                    return reports
    return reports


def _cli_paths(parser):
    required = (
        "CATALYST_MARKET_CONTEXT_DB", "CATALYST_NEWS_BODIES_DB", "CATALYST_PEER_MAP",
    )
    unset = [name for name in required if not os.environ.get(name)]
    if unset:
        parser.error("required environment variable(s) unset: " + ", ".join(unset))
    return {
        "market_db": Path(os.environ[required[0]]),
        "bodies_db": Path(os.environ[required[1]]),
        "peer_map": json.loads(Path(os.environ[required[2]]).read_text()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m catalyst_data.ingestion.ops.refresh")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "session"):
        item = sub.add_parser(command)
        item.add_argument("--ticker", required=True)
        item.add_argument("--date", required=True)
        item.add_argument("--snapshot", type=Path, default=Path("data/snapshots/catalyst_m8a_derivative_7a004acc.db"))
    latest = sub.add_parser("latest")
    latest.add_argument("--tickers", required=True)
    latest.add_argument("--since")
    latest.add_argument("--snapshot", type=Path, default=Path("data/snapshots/catalyst_m8a_derivative_7a004acc.db"))
    args = parser.parse_args(argv)
    paths = _cli_paths(parser)
    paths["snapshot_db"] = args.snapshot
    if args.command == "status":
        result = status.session_status(
            args.ticker, args.date, snapshot_db=args.snapshot, peer_map=paths["peer_map"],
            market_db=paths["market_db"], bodies_db=paths["bodies_db"],
        ).to_dict()
    elif args.command == "session":
        result = refresh_session(args.ticker, args.date, **paths).to_dict()
    else:
        result = [report.to_dict() for report in refresh_latest(
            [ticker.strip() for ticker in args.tickers.split(",") if ticker.strip()],
            since=args.since, **paths,
        )]
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
