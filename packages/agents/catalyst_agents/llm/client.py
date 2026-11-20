"""Thin OpenAI-compatible chat client with the surface the runtime uses: ``bind(**options)`` and
``invoke(messages, **options)`` returning an object with ``content``, ``usage_metadata`` and ``response_metadata``.

Request semantics follow the client it replaces (langchain_openai.ChatOpenAI as built by build_v1_llm): constructor
defaults (temperature, max_tokens, extra_body) apply unless a bound or call option overrides them; ``extra_body`` is
merged into the request body; provider errors propagate as the openai SDK's exceptions; no internal retries.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

_SDK_CLIENTS: dict[tuple[str, str, float | None], tuple[Any, Any]] = {}
_SDK_CLIENTS_LOCK = threading.Lock()
_SDK_CLIENT_CACHE_SIZE = 16


@dataclass(frozen=True)
class ChatResult:
    content: str
    usage_metadata: dict | None = None
    response_metadata: dict = field(default_factory=dict)


def _messages(messages: Any) -> list[dict]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    out = []
    for m in messages:
        if isinstance(m, dict):
            out.append({"role": m["role"], "content": m["content"]})
        else:  # a message object with .type / .content (system, human, ai)
            role = {"human": "user", "ai": "assistant"}.get(getattr(m, "type", ""), getattr(m, "type", "user"))
            out.append({"role": role, "content": m.content})
    return out


# Only these hosts accept the DeepSeek-style ``thinking`` switch in the request body; OpenAI and others reply
# 400 "Unrecognized request argument supplied: thinking" (an OpenAI-key run on 2026-11-19 lost its reported
# reason this way). Elsewhere the switch is dropped and the provider's default applies.
THINKING_SWITCH_HOSTS = ("api.deepseek.com", "open.bigmodel.cn", "api.z.ai")


def _accepts_thinking_switch(base_url: str | None) -> bool:
    hostname = urlsplit(base_url or "").hostname
    return hostname is not None and any(
        hostname == host or hostname.endswith("." + host)
        for host in THINKING_SWITCH_HOSTS
    )


class ChatClient:
    def __init__(self, *, model: str, api_key: str, base_url: str, temperature: float = 0.0,
                 max_tokens: int | None = None, timeout: float | None = None, extra_body: dict | None = None,
                 capability_metadata: dict | None = None, options: dict | None = None, _sdk: Any = None):
        self.model = self.model_name = model
        self.temperature, self.max_tokens, self.timeout = temperature, max_tokens, timeout
        self.extra_body = dict(extra_body or {})
        self.capability_metadata = capability_metadata
        self._options = dict(options or {})
        self._api_key, self._base_url = api_key, base_url
        self._sdk = _sdk

    def _client(self):
        if self._sdk is None:
            key = (self._base_url, hashlib.sha256(self._api_key.encode("utf-8")).hexdigest(), self.timeout)
            with _SDK_CLIENTS_LOCK:
                cached = _SDK_CLIENTS.get(key)
                if cached is None:
                    from openai import DefaultHttpxClient, OpenAI
                    try:  # the SDK's own httpx flavour, so its error mapping is unchanged
                        import httpx2 as _httpx
                    except ImportError:  # pragma: no cover - older SDKs use httpx
                        import httpx as _httpx

                    http_client = DefaultHttpxClient(
                        limits=_httpx.Limits(max_connections=32, max_keepalive_connections=32,
                                             keepalive_expiry=120.0),
                        timeout=self.timeout,
                    )
                    self._sdk = OpenAI(api_key=self._api_key, base_url=self._base_url,
                                       max_retries=0, timeout=self.timeout, http_client=http_client)
                    if len(_SDK_CLIENTS) >= _SDK_CLIENT_CACHE_SIZE:
                        # Least recently used goes. It is not closed: a run may still hold it, and
                        # its connections are released when the last holder lets it go.
                        _SDK_CLIENTS.pop(next(iter(_SDK_CLIENTS)))
                    _SDK_CLIENTS[key] = (self._sdk, http_client)
                else:
                    _SDK_CLIENTS[key] = _SDK_CLIENTS.pop(key)  # most recently used last
                    self._sdk = cached[0]
        return self._sdk

    def bind(self, **options: Any) -> "ChatClient":
        return ChatClient(model=self.model, api_key=self._api_key, base_url=self._base_url,
                          temperature=self.temperature, max_tokens=self.max_tokens, timeout=self.timeout,
                          extra_body=self.extra_body, capability_metadata=self.capability_metadata,
                          options={**self._options, **options}, _sdk=self._sdk)

    def request(self, messages: Any, **options: Any) -> dict:
        """The request body this call sends (also used by tests and the record file)."""
        opts = {**self._options, **options}
        body: dict[str, Any] = {"model": self.model, "messages": _messages(messages), "temperature": self.temperature}
        max_tokens = opts.pop("max_tokens", self.max_tokens)
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        extra = {**self.extra_body, **(opts.pop("extra_body", None) or {})}
        if "thinking" in extra and not _accepts_thinking_switch(self._base_url):
            extra.pop("thinking")
        body.update(opts)
        return {"body": body, "extra_body": extra}

    def invoke(self, messages: Any, **options: Any) -> ChatResult:
        req = self.request(messages, **options)
        resp = self._client().chat.completions.create(**req["body"], extra_body=req["extra_body"] or None)
        choice = resp.choices[0]
        usage = resp.usage
        usage_metadata = ({"input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens,
                           "total_tokens": usage.total_tokens} if usage is not None else None)
        return ChatResult(
            content=choice.message.content or "", usage_metadata=usage_metadata,
            response_metadata={"token_usage": usage.model_dump() if usage is not None else None,
                               "model_name": resp.model, "finish_reason": choice.finish_reason})
