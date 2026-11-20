"""Credential rotation and usage state for the server-side Tavily key pool."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests


def keys_from_env() -> list[str]:
    """Return configured keys, preferring the plural variable."""
    raw = os.environ.get("TAVILY_API_KEYS")
    if raw is None:
        raw = os.environ.get("TAVILY_API_KEY", "")
    keys: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        key = part.strip().strip("\"'").strip().strip("\"'").strip()
        if key and key not in seen:
            keys.append(key)
            seen.add(key)
    return keys


def fingerprint(key: str) -> str:
    return f"{key[-4:]}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class TavilyKeyPool:
    """Try configured server keys in order and persist per-month health/usage."""

    def __init__(self, keys: Iterable[str], db_path: str | Path):
        self.keys = list(dict.fromkeys(key.strip() for key in keys if key and key.strip()))
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_state()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_state(self) -> None:
        month = _current_month()
        conn = self._connect()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS tavily_key_state (
                fingerprint TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('ok','exhausted','invalid')),
                status_at TEXT NOT NULL,
                month TEXT NOT NULL,
                uses INTEGER NOT NULL DEFAULT 0,
                credits INTEGER NOT NULL DEFAULT 0
            )""")
            conn.execute("""UPDATE tavily_key_state
                SET status='ok',status_at=?,month=?,uses=0,credits=0 WHERE month!=?""",
                         (_now(), month, month))
            for key in self.keys:
                fp = fingerprint(key)
                conn.execute("""INSERT OR IGNORE INTO tavily_key_state
                    (fingerprint,status,status_at,month,uses,credits)
                    VALUES (?, 'ok', ?, ?, 0, 0)""", (fp, _now(), month))
                conn.execute("""UPDATE tavily_key_state
                    SET status='ok', status_at=?, month=?, uses=0, credits=0
                    WHERE fingerprint=? AND month!=?""", (_now(), month, fp, month))
            conn.commit()
        finally:
            conn.close()

    def _mark(self, key: str, status: str) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE tavily_key_state SET status=?,status_at=? WHERE fingerprint=?",
                         (status, _now(), fingerprint(key)))
            conn.commit()
        finally:
            conn.close()

    def _record_success(self, key: str, result: Any) -> None:
        credits = 0
        if isinstance(result, dict):
            value = result.get("credits_used", result.get("credits"))
            try:
                credits = max(0, int(value)) if value is not None else 0
            except (TypeError, ValueError):
                credits = 0
        conn = self._connect()
        try:
            conn.execute("""UPDATE tavily_key_state SET status='ok',status_at=?,
                uses=uses+1,credits=credits+? WHERE fingerprint=?""",
                         (_now(), credits, fingerprint(key)))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _failure_status(exc: BaseException) -> str | None:
        code = str(getattr(exc, "code", ""))
        if code in {"tavily_key_exhausted", "tavily_plan_limit", "tavily_usage_limit"}:
            return "exhausted"
        if code in {"tavily_key_invalid", "tavily_unauthorized"}:
            return "invalid"
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in (401, 403):
            return "invalid"
        if status_code == 432:
            return "exhausted"
        body = ""
        if response is not None:
            try:
                body = str(response.text or "")
            except Exception:
                body = ""
        if status_code == 429 and re.search(r"usage|credit|plan|limit", body, re.I):
            return "exhausted"
        return None

    def call(self, fn: Callable[[str], Any]) -> Any:
        from catalyst_data.ingestion.collectors.web_search import WebSearchError

        for key in self.keys:
            row = self._state_for(key)
            if row is None or row[0] != "ok":
                continue
            try:
                result = fn(key)
            except Exception as exc:
                status = self._failure_status(exc)
                if status == "exhausted" or status == "invalid":
                    self._mark(key, status)
                    continue
                if getattr(exc, "code", None) == "tavily_network_error":
                    raise WebSearchError("tavily_network_error") from None
                if isinstance(exc, requests.RequestException):
                    raise WebSearchError("tavily_network_error") from None
                if isinstance(exc, WebSearchError):
                    code = str(exc.code)
                    if code in {"web_search_budget_exhausted", "web_search_public_disabled"}:
                        raise
                    if code in {"tavily_network_error", "tavily_invalid_response"}:
                        raise WebSearchError(code) from None
                    raise WebSearchError("tavily_network_error") from None
                raise WebSearchError("tavily_network_error") from None
            self._record_success(key, result)
            return result
        raise WebSearchError("tavily_keys_exhausted")

    def _state_for(self, key: str) -> tuple[str] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT status FROM tavily_key_state WHERE fingerprint=?",
                               (fingerprint(key),)).fetchone()
            return row
        finally:
            conn.close()

    def status(self) -> list[dict[str, Any]]:
        self._ensure_state()
        conn = self._connect()
        try:
            rows = conn.execute("""SELECT fingerprint,status,uses,credits
                FROM tavily_key_state WHERE fingerprint IN ({})""".format(
                    ",".join("?" for _ in self.keys) or "NULL"),
                tuple(fingerprint(key) for key in self.keys)).fetchall()
            by_fingerprint = {row[0]: row for row in rows}
            return [{"fingerprint": fp, "status": by_fingerprint[fp][1],
                     "uses": by_fingerprint[fp][2], "credits": by_fingerprint[fp][3]}
                    for fp in (fingerprint(key) for key in self.keys) if fp in by_fingerprint]
        finally:
            conn.close()

    def has_usable_key(self) -> bool:
        return any(row["status"] == "ok" for row in self.status())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
