"""Session news API."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Literal

from catalyst_app.access_control import resolve_access_context
from catalyst_app.db_paths import runtime_db_path
from catalyst_app.public_projection import must_hide_source_text
from catalyst_data.ingestion import session_news as session_news_service
from catalyst_data.ingestion import tavily_ledger
from catalyst_data.ingestion.collectors import web_search
from catalyst_data.retrieval.web_bodies import tickers_json_list
from catalyst_data.storage.runtime_paths import runtime_store_path
from catalyst_data.trading_calendar import is_trading_day
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
_MEDIA_FILL_LOCK = threading.Lock()
_MEDIA_FILL_ACTIVE: set[tuple[str, str, str]] = set()
_MAX_ACTIVE_MEDIA_FILLS = 4
_MEDIA_FILL_LIMIT = 25


class SessionNewsWindow(BaseModel):
    information_window_start_at: str
    cutoff_at: str
    session_open_at: str


class SessionNewsItem(BaseModel):
    id: str
    title: str
    publisher: str
    published_at: str
    url: str
    summary: str | None
    image_url: str | None
    image_source: Literal["og", "twitter", "polygon", "finnhub"] | None
    category: Literal[
        "EARNINGS", "ANALYST", "DEALS", "PRODUCT", "REGULATORY_LEGAL",
        "MANAGEMENT", "CAPITAL", "MARKET", "OPINION", "OTHER",
    ] | None
    category_source: Literal["llm"] | None
    mentions_subject: bool
    has_body: bool
    used_as_evidence: bool
    phase: Literal["PRE_OPEN", "SESSION"]


class SessionNewsResponse(BaseModel):
    ticker: str
    session_date: str
    window: SessionNewsWindow
    items: list[SessionNewsItem]
    counts_by_category: dict[str, int]
    next_cursor: str | None
    images_pending: int


class WebLeadsRequest(BaseModel):
    hints: list[str] | None = Field(default=None, max_length=2)


class WebLead(BaseModel):
    lead_id: str
    title: str
    url: str
    publisher: str
    published_at: str
    time_source: str
    snippet: str | None
    image_url: str | None
    mentions_subject: bool
    mentions_in_title: bool
    also_published_by: list[str]
    status: Literal["PRE"]


class WebLeadsResponse(BaseModel):
    ticker: str
    session_date: str
    leads: list[WebLead]
    excluded: dict[str, int]
    queries_used: list[str]
    credits_used: int
    cached: bool
    provider: Literal["tavily"]
    label: str


def _paths() -> tuple[Path, Path]:
    bodies_value = os.getenv("CATALYST_NEWS_BODIES_DB", "").strip()
    if not bodies_value:
        raise HTTPException(status_code=503, detail="DATA_STORES_NOT_CONFIGURED")
    archive = runtime_store_path("archive_db")
    bodies = Path(bodies_value)
    if not bodies.is_file() or not archive.is_file():
        raise HTTPException(status_code=503, detail="DATA_STORES_NOT_CONFIGURED")
    return bodies, archive


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tavily_context(request: Request, tier: str) -> tavily_ledger.TavilyCallContext:
    db_path = getattr(request.app.state, "db_path", None)
    path = Path(db_path) if db_path is not None else runtime_db_path()
    return tavily_ledger.TavilyCallContext.from_environment(db_path=path, tier=tier)


def _ticker_known(ticker: str, bodies_path: Path, archive_path: Path) -> bool:
    bodies = _ro(bodies_path)
    archive = _ro(archive_path)
    try:
        body_tables = {row[0] for row in bodies.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        archive_tables = {row[0] for row in archive.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "web_headlines" in body_tables and bodies.execute(
            "SELECT 1 FROM web_headlines WHERE ticker=? LIMIT 1", (ticker,)
        ).fetchone():
            return True
        if "finnhub_news" in archive_tables and archive.execute(
            "SELECT 1 FROM finnhub_news WHERE symbol=? LIMIT 1", (ticker,)
        ).fetchone():
            return True
        if "polygon_feed" in archive_tables:
            for row in archive.execute("SELECT tickers_json FROM polygon_feed"):
                if ticker in tickers_json_list(row[0]):
                    return True
        return False
    finally:
        bodies.close()
        archive.close()


def _run_evidence_ids(request: Request, run_id: str | None) -> set[str]:
    if not run_id:
        return set()
    db_path = getattr(request.app.state, "db_path", None)
    if db_path is None or not Path(db_path).is_file():
        return set()
    conn = _ro(Path(db_path))
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_artifacts'"
        ).fetchone()
        if table is None:
            return set()
        rows = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id=? AND artifact_type='context_pack' "
            "ORDER BY event_seq DESC",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return set()
    try:
        payload = json.loads(rows[0]["payload_json"])
    except (TypeError, ValueError):
        return set()
    result = set(payload.get("included_evidence_ids") or ())
    for item in payload.get("evidence_inventory") or ():
        if isinstance(item, dict) and item.get("evidence_id"):
            result.add(str(item["evidence_id"]))
    return result


def _fill_session_images(key: tuple[str, str, str], bodies: Path, archive: Path) -> None:
    try:
        from catalyst_data.ingestion import media

        media.headlines(key[1], date=key[2], bodies_db=bodies, archive_db=archive,
                        limit=_MEDIA_FILL_LIMIT)
    except Exception:
        # Image enrichment is best effort and must not alter the news response or job status.
        pass
    finally:
        with _MEDIA_FILL_LOCK:
            _MEDIA_FILL_ACTIVE.discard(key)


@router.get("/{ticker}/{session_date}/news", response_model=SessionNewsResponse)
def get_session_news(
    ticker: str,
    session_date: str,
    request: Request,
    background_tasks: BackgroundTasks,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    run_id: str | None = Query(default=None),
) -> SessionNewsResponse:
    symbol = ticker.upper()
    if re.fullmatch(r"[A-Z.]{1,10}", symbol) is None:
        raise HTTPException(status_code=404, detail="UNKNOWN_TICKER")
    try:
        parsed = date.fromisoformat(session_date)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="INVALID_DATE") from exc
    if parsed.isoformat() != session_date:
        raise HTTPException(status_code=422, detail="INVALID_DATE")
    bodies_path, archive_path = _paths()
    if not _ticker_known(symbol, bodies_path, archive_path):
        raise HTTPException(status_code=404, detail="UNKNOWN_TICKER")
    if not is_trading_day(session_date):
        raise HTTPException(status_code=422, detail="NOT_A_TRADING_DAY")
    try:
        result = session_news_service.session_news(
            symbol,
            session_date,
            bodies_db=bodies_path,
            archive_db=archive_path,
            cursor=cursor,
            limit=limit,
            evidence_ids=_run_evidence_ids(request, run_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="DATA_STORES_NOT_CONFIGURED") from exc
    items = result["items"]
    if resolve_access_context(request).tier != "admin":
        items = [
            {**item, "summary": None}
            if must_hide_source_text(item.get("publisher"), item.get("url"))
            else item
            for item in items
        ]
    if result.get("images_pending", 0):
        media_db = str(session_news_service.media.cache_path())
        key = (media_db, symbol, session_date)
        with _MEDIA_FILL_LOCK:
            if key not in _MEDIA_FILL_ACTIVE and len(_MEDIA_FILL_ACTIVE) < _MAX_ACTIVE_MEDIA_FILLS:
                _MEDIA_FILL_ACTIVE.add(key)
                background_tasks.add_task(_fill_session_images, key, bodies_path, archive_path)
    return SessionNewsResponse(
        ticker=symbol,
        session_date=session_date,
        window=result["window"],
        items=items,
        counts_by_category=result["counts_by_category"],
        next_cursor=result["next_cursor"],
        images_pending=result.get("images_pending", 0),
    )


@router.post("/{ticker}/{session_date}/web-leads", response_model=WebLeadsResponse)
def get_web_leads(
    ticker: str,
    session_date: str,
    request: Request,
    body: WebLeadsRequest,
    x_tavily_key: str | None = Header(default=None, alias="X-Tavily-Key"),
) -> WebLeadsResponse:
    symbol = ticker.upper()
    access = resolve_access_context(request)
    if re.fullmatch(r"[A-Z.]{1,10}", symbol) is None:
        raise HTTPException(status_code=404, detail={"code": "UNKNOWN_TICKER"})
    try:
        parsed = date.fromisoformat(session_date)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_DATE"}) from exc
    if parsed.isoformat() != session_date:
        raise HTTPException(status_code=422, detail={"code": "INVALID_DATE"})
    try:
        result = web_search.search_leads(
            symbol, session_date, hints=body.hints, api_key=x_tavily_key,
            tavily_context=_tavily_context(request, access.tier),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc)}) from exc
    except web_search.WebSearchError as exc:
        if exc.code == "tavily_key_missing":
            raise HTTPException(status_code=400, detail={"code": exc.code}) from exc
        if exc.code == "daily_credit_limit":
            raise HTTPException(status_code=429, detail={"code": exc.code}) from exc
        raise HTTPException(status_code=502, detail={"code": exc.code}) from exc
    if access.tier != "admin":
        result = dict(result)
        result["leads"] = [
            {**lead, "snippet": None}
            if must_hide_source_text(lead.get("publisher"), lead.get("url"))
            else lead
            for lead in result.get("leads", ())
        ]
    return WebLeadsResponse.model_validate(result)
