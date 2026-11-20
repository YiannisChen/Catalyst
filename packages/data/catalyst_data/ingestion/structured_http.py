from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from catalyst_data.ingestion.structured_types import FetchReceipt, request_hash

BASE_URLS = {
    "finnhub": "https://finnhub.io/api/v1",
    "fmp": "https://financialmodelingprep.com/stable",
    "fred": "https://api.stlouisfed.org/fred",
}
DEFAULT_INTERVALS = {"finnhub": 1.1, "fmp": 1.0, "fred": 1.0}


def default_http_get(url: str, params: dict) -> tuple[int, str]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}" if query else url,
        headers={"User-Agent": "catalyst-structured"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


class StructuredClient:
    def __init__(
        self,
        cache,
        http_get: Callable[[str, dict], tuple[int, str]],
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval: dict[str, float] | None = None,
    ):
        self.cache = cache
        self.http_get = http_get
        self.sleep = sleep
        self.clock = clock
        self.min_interval = {**DEFAULT_INTERVALS, **(min_interval or {})}
        self._next_at: dict[str, float] = defaultdict(float)

    def _receipt(
        self,
        provider: str,
        endpoint: str,
        params: dict,
        status: str,
        response_json: str | None = None,
        error: str | None = None,
    ) -> FetchReceipt:
        return FetchReceipt(
            request_hash(provider, endpoint, params),
            provider,
            endpoint,
            params,
            status,
            datetime.now(timezone.utc),
            response_json,
            error,
        )

    def get(
        self,
        provider: str,
        endpoint: str,
        params: dict,
        *,
        secret_param: str,
        secret: str | None,
    ) -> FetchReceipt:
        cached = self.cache.get(request_hash(provider, endpoint, params))
        if cached is not None and cached.status in {"OK", "EMPTY"}:
            return cached
        if self.cache.offline or secret is None:
            return self._receipt(provider, endpoint, params, "UNAVAILABLE", error="offline or missing secret")
        delay = self._next_at[provider] - self.clock()
        if delay > 0:
            self.sleep(delay)
        request_params = {**params, secret_param: secret}
        url = f"{BASE_URLS[provider]}/{endpoint.lstrip('/')}"
        response_json: str | None = None
        error: str | None = None
        status: str | None = None
        for attempt in range(3):
            self._next_at[provider] = self.clock() + self.min_interval[provider]
            try:
                code, body = self.http_get(url, request_params)
                if code == 200:
                    response_json = body
                    parsed = json.loads(body)
                    if body in {"[]", "{}"} or (isinstance(parsed, dict) and any(
                        isinstance(value, list) and not value for value in parsed.values()
                    )):
                        status = "EMPTY"
                    else:
                        status = "OK"
                    break
                if code in {401, 402, 403}:
                    error = f"auth_or_plan:{code}"
                    break
                if code in {429} or code >= 500:
                    if attempt < 2:
                        self.sleep(2 ** attempt)
                        continue
                    error = f"http_status:{code}"
                else:
                    error = f"http_status:{code}"
            except Exception as exc:
                if attempt < 2:
                    self.sleep(2 ** attempt)
                    continue
                error = type(exc).__name__
        if status is None:
            status = "ERROR"
        message = error.replace(secret, "***") if error and secret else error
        return self.cache.put(self._receipt(provider, endpoint, params, status, response_json, message))
