"""Record / replay of a run's model calls (live | record | replay), keyed by sha256 of (model, bound options, call
options, messages); the same key and file format as catalyst_app.llm_record (migration step 0), so a recording made on
the frozen code replays on the new one.

CATALYST_LLM_RECORD=<file.jsonl>: real calls, request and response appended. CATALYST_LLM_REPLAY=<file.jsonl>[,...]:
no provider call; each request is answered from the files by key and call order; a request without a recorded answer
left raises ReplayMiss, unless CATALYST_LLM_RECORD_MISSES=<file.jsonl> is set: then that request is made for real and
appended there (a later replay can list both files).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import threading
from collections import defaultdict
from typing import Any

from catalyst_agents.llm.client import ChatResult

_LOCK = threading.Lock()
_ACCOUNT_ERROR_COUNTS: dict[int, int] = defaultdict(int)
_SENSITIVE_KEY_NAMES = frozenset({
    "apikey", "authorization", "accesstoken", "token", "secret",
    "clientsecret", "privatekey", "secretkey", "password",
})
_SECRET_TEXT = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+)\b", re.IGNORECASE
)
_ACCOUNT_STATUS_TEXT = re.compile(r"\b(?:error\s+code\s*:\s*)?(401|402|403)\b", re.IGNORECASE)


def _redact_record_value(value: Any, secrets: tuple[str, ...]) -> Any:
    """Redact credential fields and known key values before record persistence."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            redacted[key] = (
                "[REDACTED_SECRET]"
                if normalized_key in _SENSITIVE_KEY_NAMES
                else _redact_record_value(item, secrets)
            )
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_record_value(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED_SECRET]")
        return _SECRET_TEXT.sub("[REDACTED_SECRET]", value)
    return value


class ReplayMiss(RuntimeError):
    pass


def account_error_status(value: Any, *, row: dict | None = None) -> int | None:
    """Classify only error data and explicit status fields, never response payloads."""
    status = getattr(value, "status_code", None)
    if status is None:
        status = getattr(getattr(value, "response", None), "status_code", None)
    if status in {401, 402, 403}:
        return int(status)
    if isinstance(value, dict):
        status = value.get("status_code") or value.get("status")
        if status in {401, 402, 403, "401", "402", "403"}:
            return int(status)
        for key in ("message", "error"):
            found = account_error_status(value.get(key)) if value.get(key) is not None else None
            if found is not None:
                return found
    elif isinstance(value, str):
        match = _ACCOUNT_STATUS_TEXT.search(value)
        if match:
            return int(match.group(1))
        lowered = value.lower()
        if "insufficient_balance_error" in lowered or "insufficient balance" in lowered:
            return 402
    if row is not None:
        for name in ("status_code", "status"):
            status = row.get(name)
            if status in {401, 402, 403, "401", "402", "403"}:
                return int(status)
    return None


_account_status = account_error_status


def account_error_counts_snapshot() -> dict[int, int]:
    with _LOCK:
        return dict(_ACCOUNT_ERROR_COUNTS)


def _record_account_error(status: int) -> None:
    with _LOCK:
        _ACCOUNT_ERROR_COUNTS[status] += 1


def _message(m: Any) -> dict:
    if isinstance(m, dict):
        return {"role": m.get("role"), "content": m.get("content")}
    return {"role": getattr(m, "type", type(m).__name__), "content": getattr(m, "content", str(m))}


def normalize(messages: Any) -> list[dict]:
    return [{"role": "user", "content": messages}] if isinstance(messages, str) else [_message(m) for m in messages]


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def request_key(model: str, bound: dict, options: dict, messages: Any) -> str:
    payload = [model, _jsonable(bound), _jsonable(options), normalize(messages)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, path: str, mode: str):
        self.path, self.mode = path, mode
        self.rows: dict[str, list[dict]] = defaultdict(list)
        self.next: dict[str, int] = defaultdict(int)
        self.counts: dict[str, int] = defaultdict(int)
        if mode == "replay":
            for part in path.split(","):
                with open(part) as fh:
                    for line in fh:
                        row = json.loads(line)
                        self.rows[row["key"]].append(row)
            for rows in self.rows.values():
                rows.sort(key=lambda r: r["n"])

    def append(self, row: dict) -> None:
        with _LOCK:
            row["n"] = self.counts[row["key"]]
            self.counts[row["key"]] += 1
            with open(self.path, "a") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def take(self, key: str) -> dict:
        with _LOCK:
            i = self.next[key]
            if i >= len(self.rows.get(key, ())):
                raise ReplayMiss(f"no recorded response left for request {key[:12]} (call {i + 1})")
            self.next[key] = i + 1
            return self.rows[key][i]

    def unused(self) -> int:
        return sum(len(rows) - self.next[k] for k, rows in self.rows.items())


def _rebuild_error(err: dict) -> Exception:
    try:
        cls = getattr(importlib.import_module(err["module"]), err["type"])
        exc = cls.__new__(cls)
        Exception.__init__(exc, err["message"])
        return exc
    except Exception:
        return RuntimeError(f"{err['type']}: {err['message']}")


class Recorded:
    """Wraps a client with ``bind`` / ``invoke``; every other attribute is delegated."""

    def __init__(self, client: Any, store: Store, bound: dict | None = None, misses: Store | None = None):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_bound", dict(bound or {}))
        object.__setattr__(self, "_misses", misses)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def bind(self, **kwargs: Any) -> "Recorded":
        return Recorded(self._client, self._store, {**self._bound, **kwargs}, self._misses)

    def invoke(self, messages: Any, **options: Any) -> Any:
        model = str(getattr(self._client, "model_name", None) or getattr(self._client, "model", ""))
        key = request_key(model, self._bound, options, messages)
        store = self._store
        if store.mode == "replay":
            try:
                row = store.take(key)
            except ReplayMiss:
                if self._misses is None:
                    raise
                store = self._misses  # not recorded: a real call, appended to the misses file
            else:
                if row.get("error"):
                    if account_error_status(row.get("error"), row=row) is not None:
                        raise ReplayMiss("account-error recording is not replayable")
                    raise _rebuild_error(row["error"])
                r = row["response"]
                return ChatResult(content=r["content"], usage_metadata=r.get("usage_metadata"),
                                  response_metadata=r.get("response_metadata") or {})
        target = self._client.bind(**self._bound) if self._bound else self._client
        request = {"model": model, "bound": _jsonable(self._bound), "options": _jsonable(options),
                   "messages": normalize(messages)}
        client_api_key = getattr(self._client, "_api_key", "")
        secrets = (client_api_key,) if isinstance(client_api_key, str) else ()
        try:
            result = target.invoke(messages, **options)
        except Exception as exc:
            status = account_error_status(exc)
            if status is not None:
                _record_account_error(status)
                raise
            store.append(
                _redact_record_value(
                    {
                        "key": key,
                        "request": request,
                        "error": {
                            "module": type(exc).__module__,
                            "type": type(exc).__name__,
                            "message": str(exc)[:500],
                            "status_code": _account_status(exc),
                        },
                    },
                    secrets,
                )
            )
            raise
        store.append(
            _redact_record_value(
                {
                    "key": key,
                    "request": request,
                    "response": {
                        "content": getattr(result, "content", str(result)),
                        "usage_metadata": _jsonable(
                            getattr(result, "usage_metadata", None)
                        ),
                        "response_metadata": _jsonable(
                            getattr(result, "response_metadata", None)
                        ),
                    },
                },
                secrets,
            )
        )
        return result


_STORES: dict[tuple[str, str], Store] = {}


def store_for(mode: str, path: str) -> Store:
    with _LOCK:
        if (mode, path) not in _STORES:
            _STORES[(mode, path)] = Store(path, mode)
        return _STORES[(mode, path)]


def wrap(client: Any) -> Any:
    """The client itself unless CATALYST_LLM_REPLAY or CATALYST_LLM_RECORD is set."""
    if os.environ.get("CATALYST_LLM_REPLAY"):
        misses = os.environ.get("CATALYST_LLM_RECORD_MISSES")
        return Recorded(client, store_for("replay", os.environ["CATALYST_LLM_REPLAY"]),
                        misses=store_for("record", misses) if misses else None)
    if os.environ.get("CATALYST_LLM_RECORD"):
        return Recorded(client, store_for("record", os.environ["CATALYST_LLM_RECORD"]))
    return client
