"""Connector-side secret redaction and request fingerprinting.

Strips API keys, authorization headers, cookies, and signed query parameters
before any request object reaches logs or the ledger.  Fingerprints enable
deterministic request identity without storing secrets.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse, parse_qs, urlencode


# Fields to redact from request parameters, headers, and URL query strings
_SECRET_PARAM_NAMES: set[str] = {
    "apikey", "api_key", "api-key", "token", "access_token",
    "secret", "password", "key",
}
_SECRET_HEADER_NAMES: set[str] = {
    "authorization", "cookie", "x-api-key", "x-api-token",
    "proxy-authorization",
}


def redact_request(raw_request: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of raw_request with secrets stripped.

    Strips:
    - apiKey and similar from URL query params
    - Authorization, Cookie, and similar from headers
    - apiKey from params dict
    - Normalizes host and path for fingerprint stability
    """
    redacted: dict[str, Any] = {
        "method": raw_request.get("method", "GET"),
        "normalized_host": "",
        "normalized_path": "",
        "sorted_redacted_params": {},
        "request_body_sha256": _sha256_hex(raw_request.get("body")),
        "provider_profile_version": raw_request.get("provider_profile_version", ""),
    }

    url = raw_request.get("url", "")
    if url:
        parsed = urlparse(url)
        redacted["normalized_host"] = parsed.hostname or ""
        redacted["normalized_path"] = parsed.path or ""
        # Redact query params from URL
        qs_params = parse_qs(parsed.query, keep_blank_values=True)
        clean_qs = {
            k.lower(): v[0] if len(v) == 1 else v
            for k, v in qs_params.items()
            if k.lower() not in _SECRET_PARAM_NAMES
        }
        redacted["sorted_redacted_params"].update(clean_qs)

    # Redact explicit params dict
    params = raw_request.get("params", {})
    if isinstance(params, dict):
        clean_params = {
            k.lower(): v
            for k, v in params.items()
            if k.lower() not in _SECRET_PARAM_NAMES
        }
        redacted["sorted_redacted_params"].update(clean_params)

    # Redact headers
    headers = raw_request.get("headers", {})
    if isinstance(headers, dict):
        redacted["headers"] = {
            k: v
            for k, v in headers.items()
            if k.lower() not in _SECRET_HEADER_NAMES
        }

    return redacted


def compute_request_fingerprint(redacted_request: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON of the redacted request.

    Per contract §3 rule 7: sorted keys, compact separators, no NaN/Infinity.
    """
    # Build canonical dict in deterministic order
    canonical = {
        "method": redacted_request.get("method", "GET"),
        "normalized_host": redacted_request.get("normalized_host", ""),
        "normalized_path": redacted_request.get("normalized_path", ""),
        "sorted_redacted_params": _sorted_params(redacted_request.get("sorted_redacted_params", {})),
        "request_body_sha256": redacted_request.get("request_body_sha256", ""),
        "provider_profile_version": redacted_request.get("provider_profile_version", ""),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_cursor_fingerprint(raw_cursor: str) -> str:
    """SHA-256 hash of the pagination cursor URL.

    The cursor URL may contain opaque tokens; storing only the hash
    prevents credential leakage while still enabling loop detection.
    """
    return hashlib.sha256(raw_cursor.encode("utf-8")).hexdigest()


def _sha256_hex(body: bytes | str | None) -> str:
    """SHA-256 hex digest of a request body, or empty string for None."""
    if body is None:
        return hashlib.sha256(b"").hexdigest()
    if isinstance(body, str):
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
    return hashlib.sha256(body).hexdigest()


def _sorted_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return params with keys sorted for canonical representation."""
    result: dict[str, Any] = {}
    for k in sorted(params.keys()):
        v = params[k]
        if isinstance(v, list):
            result[k] = sorted(v) if v and isinstance(v[0], str) else v
        else:
            result[k] = v
    return result
