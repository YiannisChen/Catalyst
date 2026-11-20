from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class FactRow:
    kind: str
    subject: str
    event_at: datetime | None
    available_at: datetime | None
    precision: str
    source: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class FetchReceipt:
    request_hash: str
    provider: str
    endpoint: str
    params: dict[str, Any]
    status: str
    fetched_at: datetime
    response_json: str | None
    error: str | None


def request_hash(provider: str, endpoint: str, params: dict) -> str:
    encoded = json.dumps([provider, endpoint, params], sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
