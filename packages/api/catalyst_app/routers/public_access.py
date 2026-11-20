"""Prepared options, quota view, and caller-scoped run history."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from catalyst_app.access_control import resolve_access_context
from catalyst_app.db_paths import runtime_db_path
from catalyst_app.persistence.connect import open_readonly, open_rw
from catalyst_app.product_tier import group_context, product_tier
from catalyst_app.runtime.ready_range import ReadyRangeUnavailable, load_ready_range
from catalyst_data.read.tavily_usage import used_for_day as tavily_calls_used_for_day
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api", tags=["public access"])
ANONYMOUS_SESSION_COOKIE = "catalyst_session"
_LIVE_RUNS_FROM = (
    "FROM runs r {join} LEFT JOIN run_artifacts a ON a.run_id=r.run_id "
    "AND a.artifact_type='run_manifest' LEFT JOIN run_artifacts answer ON answer.run_id=r.run_id "
    "AND answer.artifact_type='answer_snapshot'"
)


class SelectableTicker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    name: str
    sector_etf: str | None


class OptionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tickers: list[SelectableTicker]
    sessions: dict[str, list[str]]
    generated_at: str | None
    last_trading_day: str | None
    tier: str
    status: str


class CallerQuota(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int
    used: int
    remaining: int
    resets_at: str


class GlobalQuota(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_cost_usd: dict[str, float]
    tavily_credits: dict[str, int]
    exhausted: bool


class QuotaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tier: str
    as_of: str
    per_caller: CallerQuota | None
    global_: GlobalQuota = Field(alias="global")
    token: dict[str, str | None] | None


class RunHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    ticker: str | None
    session_date: str | None
    lifecycle_status: str
    outcome: str | None
    created_at: str
    duration_ms: int
    move_pct: float | None = None
    headline: str | None = None
    model: str | None = None
    cost_usd: float | None = None
    # Add-only: the answer's key sentence (answered runs), and how cost_usd was measured
    # ("reported", or "upper_bound_charged" when no usage came back and the reserved bound was booked).
    cause: str | None = None
    cost_method: str | None = None
    # Add-only: why a FAILED or CANCELLED run stopped (e.g. "TIMEOUT"); null otherwise.
    failure_code: str | None = None
    # Add-only (2026-11-16): the product tier of a finished answer; see catalyst_app.product_tier.
    product_tier: str | None = None


class RunHistorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: int
    answered: int
    spent_usd: float
    matching: int | None = None


class RunHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RunHistoryItem]
    next_cursor: str | None
    summary: RunHistorySummary | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _tier(request: Request) -> str:
    return resolve_access_context(request).tier


def _empty_options(tier: str) -> dict[str, Any]:
    return {
        "tickers": [],
        "sessions": {},
        "generated_at": None,
        "last_trading_day": None,
        "tier": tier,
        "status": "unavailable",
    }


@lru_cache(maxsize=16)
def _sec_company_names(path: str, mtime_ns: int) -> dict[str, str]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        entry["ticker"].upper(): entry["title"]
        for entry in payload.values()
        if isinstance(entry, dict)
        and isinstance(entry.get("ticker"), str)
        and isinstance(entry.get("title"), str)
    }


@lru_cache(maxsize=16)
def _peer_sector_etfs(path: str, mtime_ns: int) -> dict[str, str | None]:
    try:
        from catalyst_agents.runtime.peer_map import load_peer_map_from_env
        from catalyst_data.ingestion.universe import GICS_SECTOR_ETF

        peer_map = load_peer_map_from_env()
        result: dict[str, str | None] = {}
        targets = peer_map.get("targets", {})
        gics = peer_map.get("gics", {}).get("tickers", {})
        for ticker in set(targets) | set(gics):
            target = targets.get(ticker)
            result[ticker.upper()] = (
                target["sector_etf"] if isinstance(target, dict) and "sector_etf" in target
                else GICS_SECTOR_ETF.get(gics[ticker][0]) if ticker in gics and gics[ticker]
                else None
            )
        return result
    except Exception:
        return {}


def _file_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


@router.get("/options", response_model=OptionsResponse)
def get_options(request: Request) -> dict[str, Any]:
    tier = _tier(request)
    try:
        ready = load_ready_range(now=utc_now())
    except ReadyRangeUnavailable:
        return _empty_options(tier)

    sessions: dict[str, list[str]] = {}
    for ticker, session_date in sorted(ready.pairs):
        sessions.setdefault(ticker, []).append(session_date)
    from catalyst_data.storage.runtime_paths import default_sec_tickers_path

    sec_path = default_sec_tickers_path()
    sec_names = _sec_company_names(str(sec_path), _file_mtime_ns(sec_path))
    try:
        from catalyst_agents.runtime.peer_map import DEFAULT_PEER_MAP_PATH

        peer_path = Path(os.environ.get("CATALYST_PEER_MAP", "").strip() or DEFAULT_PEER_MAP_PATH)
    except Exception:
        peer_path = Path(os.environ.get("CATALYST_PEER_MAP", ""))
    peer_etfs = _peer_sector_etfs(str(peer_path), _file_mtime_ns(peer_path))
    return {
        "tickers": [
            {
                "ticker": ticker,
                "name": sec_names.get(ticker.upper(), ticker),
                "sector_etf": peer_etfs.get(ticker.upper()),
            }
            for ticker in sessions
        ],
        "sessions": sessions,
        "generated_at": ready.generated_at,
        "last_trading_day": ready.last_trading_day,
        "tier": tier,
        "status": ready.status,
    }


def _caller_key(request: Request) -> str:
    salt = os.environ.get("CATALYST_QUOTA_HASH_KEY", "")
    if not salt:
        raise HTTPException(status_code=503, detail="runtime_unavailable: quota_config_missing")
    peer = request.client.host if request.client is not None else None
    source_ip = peer
    forwarded = request.headers.get("cf-connecting-ip")
    try:
        trusted = [
            ipaddress.ip_network(item.strip(), strict=False)
            for item in os.environ.get("CATALYST_TRUSTED_PROXY_CIDRS", "").split(",")
            if item.strip()
        ]
        peer_ip = ipaddress.ip_address(peer) if peer else None
        if forwarded and peer_ip and any(peer_ip in network for network in trusted):
            source_ip = ipaddress.ip_address(forwarded.strip()).compressed
    except ValueError:
        source_ip = peer
    if not source_ip:
        raise HTTPException(status_code=503, detail="runtime_unavailable: quota_identity_unavailable")
    return hmac.new(salt.encode("utf-8"), source_ip.encode("utf-8"), hashlib.sha256).hexdigest()


def _limits(request: Request) -> tuple[int, int, float, int]:
    controller = getattr(request.app.state, "admission_controller", None)
    ledger = getattr(controller, "_budget_ledger", None)
    if ledger is None:
        try:
            from catalyst_app.runtime.budget import BudgetLedger

            ledger = BudgetLedger.from_environment()
        except ImportError:
            ledger = None
    if ledger is not None:
        return (
            ledger.anonymous_daily_limit,
            ledger.invite_daily_limit,
            ledger.daily_model_budget_micros / 1_000_000,
            ledger.daily_tavily_call_limit,
        )
    return (
        int(os.environ.get("CATALYST_ANONYMOUS_DAILY_RUN_LIMIT", "5")),
        int(os.environ.get("CATALYST_INVITE_DAILY_RUN_LIMIT", "50")),
        float(os.environ.get("CATALYST_DAILY_MODEL_BUDGET_USD", "2.00")),
        int(os.environ.get("CATALYST_DAILY_TAVILY_CALL_LIMIT", "50")),
    )


def _invite_expiry(request: Request) -> str | None:
    token = request.headers.get("x-invite-token", "")
    try:
        payload_segment = token.split(".", 1)[0][4:]
        raw = base64.urlsafe_b64decode(payload_segment + "=" * (-len(payload_segment) % 4))
        payload = json.loads(raw)
        value = payload.get("expires_at") if isinstance(payload, dict) else None
        return value if isinstance(value, str) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


@router.get("/quota", response_model=QuotaResponse)
def get_quota(request: Request) -> dict[str, Any]:
    access = resolve_access_context(request)
    now = utc_now()
    utc_day = now.date().isoformat()
    resets_at = datetime.combine(now.date() + timedelta(days=1), time.min, timezone.utc)
    anonymous_limit, invite_limit, daily_budget_usd, tavily_limit = _limits(request)
    caller_key: str | None = None
    if access.tier == "anonymous":
        caller_key = _caller_key(request)
    elif access.tier == "invite":
        caller_key = access.token_id

    try:
        with open_readonly(_db_path(request)) as conn:
            caller_used = 0
            if access.tier != "admin":
                row = conn.execute(
                    "SELECT COUNT(*) FROM run_budget_reservations "
                    "WHERE utc_day=? AND tier=? AND caller_key=? AND quota_charged=1"
                    # per_caller is the free-demo quota; anonymous own-key runs are counted apart
                    + (" AND global_budget_charged=1" if access.tier == "anonymous" else ""),
                    (utc_day, access.tier, caller_key),
                ).fetchone()
                caller_used = int(row[0])
            global_micros = int(conn.execute(
                "SELECT COALESCE(SUM(COALESCE(settled_cost_micros,reserved_cost_micros)),0) "
                "FROM run_budget_reservations WHERE utc_day=? AND global_budget_charged=1",
                (utc_day,),
            ).fetchone()[0])
            tavily_used = tavily_calls_used_for_day(conn, utc_day)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="runtime_unavailable: quota_unavailable") from exc

    caller_limit = anonymous_limit if access.tier == "anonymous" else invite_limit
    return {
        "tier": access.tier,
        "as_of": now.isoformat().replace("+00:00", "Z"),
        "per_caller": None if access.tier == "admin" else {
            "limit": caller_limit,
            "used": caller_used,
            "remaining": max(0, caller_limit - caller_used),
            "resets_at": resets_at.isoformat().replace("+00:00", "Z"),
        },
        "global": {
            "model_cost_usd": {
                "limit": daily_budget_usd,
                "used": global_micros / 1_000_000,
            },
            "tavily_credits": {"limit": tavily_limit, "used": tavily_used},
            "exhausted": global_micros >= daily_budget_usd * 1_000_000,
        },
        "token": None if access.tier != "invite" else {
            "label": access.token_id,
            "expires_at": _invite_expiry(request),
        },
    }


def _db_path(request: Request) -> Path:
    path = getattr(request.app.state, "db_path", None)
    return path if path is not None else runtime_db_path()


def _session_identity(request: Request) -> tuple[str, str] | None:
    value = request.cookies.get(ANONYMOUS_SESSION_COOKIE, "")
    day, separator, token = value.partition(".")
    today = utc_now().date().isoformat()
    if not separator or day != today or len(token) < 32:
        return None
    digest = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    return digest, day


def set_anonymous_session_cookie(request: Request, response: Response, run_id: str) -> None:
    now = utc_now()
    today = now.date().isoformat()
    session = _session_identity(request)
    token = request.cookies.get(ANONYMOUS_SESSION_COOKIE, "").partition(".")[2] if session else ""
    if not session:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        session = (digest, today)
    _record_session_run(_db_path(request), session[0], today, run_id, now)
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, timezone.utc)
    response.set_cookie(
        ANONYMOUS_SESSION_COOKIE,
        f"{today}.{token}",
        max_age=max(0, int((midnight - now).total_seconds())),
        httponly=True,
        # Behind nginx uvicorn sees http; nginx's X-Forwarded-Proto says the visitor used https.
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
        samesite="lax",
        path="/",
    )


def _record_session_run(db_path, session_hash: str, utc_day: str, run_id: str, now: datetime) -> None:
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO anonymous_session_runs(session_hash,utc_day,run_id,created_at) "
            "VALUES(?,?,?,?)",
            (session_hash, utc_day, run_id, now.isoformat()),
        )
        conn.commit()


def _encode_cursor(created_at: str, run_id: str) -> str:
    return base64.urlsafe_b64encode(f"{created_at}\n{run_id}".encode()).decode().rstrip("=")


def _decode_cursor(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        created_at, run_id = decoded.split("\n", 1)
        if not created_at or not run_id:
            raise ValueError
        return created_at, run_id
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid_cursor") from None


@router.get("/live-runs", response_model=RunHistoryResponse)
def list_live_runs(
    request: Request,
    ticker: str | None = None,
    session_date: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    outcome: str | None = Query(
        default=None, pattern="^(ANSWERED|ABSTAINED|FAILED|CANCELLED|TIER_ONLY|REPORTED|NO_CAUSE)$"
    ),
) -> dict[str, Any]:
    access = resolve_access_context(request)
    path = _db_path(request)
    where = []
    params: list[Any] = []
    join = ""
    if access.tier == "anonymous":
        session = _session_identity(request)
        if session is None:
            return {"items": [], "next_cursor": None, "summary": {
                "runs": 0, "answered": 0, "spent_usd": 0.0, "matching": 0,
            }}
        join = "JOIN anonymous_session_runs s ON s.run_id=r.run_id"
        where.append("s.session_hash=? AND s.utc_day=?")
        params.extend(session)
    elif access.tier == "invite":
        join = "JOIN run_budget_reservations b ON b.run_id=r.run_id"
        where.extend(["b.tier='invite'", "b.caller_key=?"])
        params.append(access.token_id)
    if ticker:
        where.append("r.ticker=?")
        params.append(ticker.strip().upper())
    if session_date:
        where.append("json_extract(a.payload_json,'$.temporal_identity.session_date')=?")
        params.append(session_date)
    # Summary follows the same access scope and optional selectors, but not paging/outcome.
    summary_where = list(where)
    summary_params = list(params)
    cursor_pair = _decode_cursor(cursor)
    if cursor_pair:
        where.append("(r.created_at<? OR (r.created_at=? AND r.run_id<?))")
        params.extend([cursor_pair[0], cursor_pair[0], cursor_pair[1]])
    if outcome == "FAILED":
        where.append("r.lifecycle_status='FAILED'")
    elif outcome == "CANCELLED":
        where.append("r.lifecycle_status='CANCELLED'")
    elif outcome in {"ANSWERED", "ABSTAINED", "TIER_ONLY"}:
        where.append("r.lifecycle_status NOT IN ('FAILED','CANCELLED') AND "
                     "json_extract(answer.payload_json,'$.outcome')=?")
        params.append(outcome)
    elif outcome in {"REPORTED", "NO_CAUSE"}:
        # Abstained answers split by whether a source states a reason (product tier REPORTED).
        where.append("r.lifecycle_status NOT IN ('FAILED','CANCELLED') AND "
                     "json_extract(answer.payload_json,'$.outcome')='ABSTAINED' AND "
                     "COALESCE(json_type(answer.payload_json,'$.reported_reason'),'')"
                     + ("='object'" if outcome == "REPORTED" else "<>'object'"))
    clause = " WHERE " + " AND ".join(where) if where else ""
    try:
        with open_readonly(path) as conn:
            rows = conn.execute(
                "SELECT r.run_id,r.lifecycle_status,r.failure_code,r.created_at,r.updated_at,r.ticker,r.provider,"
                "a.payload_json,answer.payload_json AS answer_payload "
                ",(SELECT d.payload_json FROM run_artifacts d WHERE d.run_id=r.run_id "
                "AND d.artifact_type='run_diagnostics' ORDER BY d.event_seq DESC LIMIT 1) AS diagnostics_payload "
                + _LIVE_RUNS_FROM.format(join=join) + clause +
                " GROUP BY r.run_id ORDER BY r.created_at DESC,r.run_id DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
            summary_clause = " WHERE " + " AND ".join(summary_where) if summary_where else ""
            summary_rows = conn.execute(
                "SELECT r.lifecycle_status,answer.payload_json AS answer_payload,"
                "(SELECT d.payload_json FROM run_artifacts d WHERE d.run_id=r.run_id "
                "AND d.artifact_type='run_diagnostics' ORDER BY d.event_seq DESC LIMIT 1) AS diagnostics_payload "
                + _LIVE_RUNS_FROM.format(join=join) + summary_clause + " GROUP BY r.run_id",
                summary_params,
            ).fetchall()
    except Exception:
        raise HTTPException(status_code=503, detail="runtime_unavailable: run_history_unavailable") from None

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for row in rows:
        try:
            manifest = json.loads(row["payload_json"] or "{}")
            trading_day = (manifest.get("temporal_identity") or {}).get("session_date")
        except (TypeError, ValueError):
            trading_day = None
        try:
            answer = json.loads(row["answer_payload"] or "{}")
            derived_outcome = _history_outcome(row)
        except (TypeError, ValueError):
            answer = {}
            derived_outcome = _history_outcome(row)
        try:
            start = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
            duration_ms = max(0, int((end - start).total_seconds() * 1000))
        except (TypeError, ValueError):
            duration_ms = 0
        items.append({
            "run_id": row["run_id"],
            "ticker": row["ticker"],
            "session_date": trading_day,
            "lifecycle_status": row["lifecycle_status"],
            "outcome": derived_outcome,
            "created_at": row["created_at"],
            "duration_ms": duration_ms,
            "move_pct": _history_move(answer),
            "headline": answer.get("headline"),
            "model": _history_model(manifest, row["provider"]),
            "cost_usd": _history_cost(answer, row["diagnostics_payload"]),
            "cause": _history_cause(answer, derived_outcome),
            "cost_method": _history_cost_method(row["diagnostics_payload"]),
            "failure_code": row["failure_code"] if row["lifecycle_status"] in {"FAILED", "CANCELLED"} else None,
            "product_tier": (
                product_tier(derived_outcome, {**group_context(answer.get("facts")), "snapshot": answer})
                if derived_outcome in {"ANSWERED", "ABSTAINED", "TIER_ONLY"} else None
            ),
        })
    next_cursor = _encode_cursor(rows[-1]["created_at"], rows[-1]["run_id"]) if has_more and rows else None
    total_cost = 0.0
    answered = 0
    for summary_row in summary_rows:
        try:
            summary_answer = json.loads(summary_row["answer_payload"] or "{}")
        except (TypeError, ValueError):
            summary_answer = {}
        if _history_outcome(summary_row) == "ANSWERED":
            answered += 1
        try:
            total_cost += float(_history_cost(summary_answer, summary_row["diagnostics_payload"]) or 0.0)
        except (TypeError, ValueError):
            pass
    matching = sum(
        _history_filter_matches(summary_row, outcome) for summary_row in summary_rows
    ) if outcome else len(summary_rows)
    return {"items": items, "next_cursor": next_cursor,
            "summary": {"runs": len(summary_rows), "answered": answered,
                        "spent_usd": total_cost, "matching": matching}}


def _history_outcome(row: Any) -> str | None:
    status = row["lifecycle_status"]
    if status in {"FAILED", "CANCELLED"}:
        return status
    try:
        value = json.loads(row["answer_payload"] or "{}").get("outcome")
    except (TypeError, ValueError):
        return None
    return value if value in {"ANSWERED", "ABSTAINED", "TIER_ONLY"} else None


def _history_model(manifest: dict[str, Any], provider: str | None = None) -> str | None:
    config = manifest.get("runtime_configuration") or {}
    provider = provider or manifest.get("provider") or config.get("provider")
    model_id = manifest.get("analyst_model_id") or config.get("model_id")
    return f"{provider}/{model_id}" if provider and model_id else None


def _history_cost(answer: dict[str, Any], diagnostics_payload: str | None) -> float | None:
    try:
        value = answer.get("cost_usd")
        if value is None and diagnostics_payload:
            diagnostics = json.loads(diagnostics_payload)
            value = (diagnostics.get("provider") or {}).get("cost_usd")
        return float(value) if value is not None else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _history_filter_matches(row: Any, outcome: str) -> bool:
    derived = _history_outcome(row)
    if outcome not in {"REPORTED", "NO_CAUSE"}:
        return derived == outcome
    if derived != "ABSTAINED":
        return False
    try:
        answer = json.loads(row["answer_payload"] or "{}")
    except (TypeError, ValueError):
        answer = {}
    reported = isinstance(answer, dict) and isinstance(answer.get("reported_reason"), dict)
    return reported if outcome == "REPORTED" else not reported


def _history_cause(answer: dict[str, Any], outcome: str | None) -> str | None:
    reported = answer.get("reported_reason")
    if outcome == "ABSTAINED" and isinstance(reported, dict):
        cause = reported.get("cause")
        if isinstance(cause, str) and cause.strip():
            text = cause.strip()
            return text[:1].upper() + text[1:]
        return None
    if outcome != "ANSWERED":
        return None
    key = answer.get("key_sentence")
    text = key.get("text") if isinstance(key, dict) else None
    return text.strip() if isinstance(text, str) and text.strip() else None


def _history_cost_method(diagnostics_payload: str | None) -> str | None:
    try:
        diagnostics = json.loads(diagnostics_payload) if diagnostics_payload else {}
        method = (diagnostics.get("provider") or {}).get("cost_method")
        return method if isinstance(method, str) else None
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return None


def _history_move(answer: dict[str, Any]) -> float | None:
    facts = answer.get("facts")
    value = facts.get("target_return_pct") if isinstance(facts, dict) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "ANONYMOUS_SESSION_COOKIE",
    "get_options",
    "get_quota",
    "list_live_runs",
    "router",
    "set_anonymous_session_cookie",
    "utc_now",
]
