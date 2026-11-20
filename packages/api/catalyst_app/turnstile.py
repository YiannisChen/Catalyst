"""Fail-closed Cloudflare Turnstile verification for public live-run admission."""

from __future__ import annotations

import json
import logging
import os
from http.client import HTTPException as HttpProtocolError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import HTTPException

TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TIMEOUT_SECONDS = 5
TURNSTILE_SECRET_ENV = "CATALYST_TURNSTILE_SECRET_KEY"
TURNSTILE_ALLOWED_HOSTS_ENV = "CATALYST_ALLOWED_HOSTS"
TURNSTILE_EXPECTED_ACTION_ENV = "CATALYST_TURNSTILE_EXPECTED_ACTION"
TURNSTILE_MAX_TOKEN_LENGTH = 2048
TURNSTILE_MAX_RESPONSE_BYTES = 64 * 1024

_TOKEN_ERROR_CODES = {
    "invalid-input-response",
    "missing-input-response",
    "timeout-or-duplicate",
}
_KNOWN_ERROR_CODES = _TOKEN_ERROR_CODES | {
    "invalid-input-secret",
    "missing-input-secret",
    "bad-request",
    "internal-error",
}
_logger = logging.getLogger(__name__)


def _runtime_unavailable(reason: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"runtime_unavailable: {reason}",
    )


def require_valid_turnstile(token: str | None) -> None:
    """Require a valid siteverify response or raise a public API error.

    Missing/invalid visitor tokens map to the contract's ``turnstile_failed``.
    Missing server configuration and siteverify transport/protocol failures
    map to ``runtime_unavailable``; none of these paths may admit a run.
    """
    secret = os.environ.get(TURNSTILE_SECRET_ENV, "").strip()
    if not secret:
        raise _runtime_unavailable("turnstile_not_configured")

    configured_hosts = {
        host.strip().rstrip(".").casefold()
        for host in os.environ.get(TURNSTILE_ALLOWED_HOSTS_ENV, "").split(",")
        if host.strip()
    }
    if not configured_hosts:
        raise _runtime_unavailable("turnstile_not_configured")

    if token is None or not token.strip() or len(token) > TURNSTILE_MAX_TOKEN_LENGTH:
        raise HTTPException(status_code=401, detail="turnstile_failed")

    request = Request(
        TURNSTILE_SITEVERIFY_URL,
        data=urlencode({"secret": secret, "response": token}).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=TURNSTILE_TIMEOUT_SECONDS) as response:
            body = response.read(TURNSTILE_MAX_RESPONSE_BYTES + 1)
        if len(body) > TURNSTILE_MAX_RESPONSE_BYTES:
            raise ValueError("siteverify response exceeds limit")
        payload = json.loads(body)
    except (
        OSError,
        TimeoutError,
        URLError,
        HttpProtocolError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        _logger.warning(
            "Turnstile Siteverify transport or response failure: %s", type(exc).__name__
        )
        raise _runtime_unavailable("turnstile_verification_unavailable") from None

    if not isinstance(payload, dict) or type(payload.get("success")) is not bool:
        raise _runtime_unavailable("turnstile_verification_unavailable")
    if payload["success"] is not True:
        codes = payload.get("error-codes")
        if (
            not isinstance(codes, list)
            or not codes
            or any(not isinstance(code, str) for code in codes)
        ):
            raise _runtime_unavailable("turnstile_verification_unavailable")
        known_codes = sorted(set(codes) & _KNOWN_ERROR_CODES)
        if known_codes:
            _logger.warning(
                "Turnstile Siteverify rejected request: %s", ",".join(known_codes)
            )
        if set(codes) and set(codes) <= _TOKEN_ERROR_CODES:
            raise HTTPException(status_code=401, detail="turnstile_failed")
        raise _runtime_unavailable("turnstile_verification_unavailable")

    hostname = payload.get("hostname")
    if (
        not isinstance(hostname, str)
        or hostname.strip().rstrip(".").casefold() not in configured_hosts
    ):
        raise HTTPException(status_code=401, detail="turnstile_failed")

    expected_action = os.environ.get(TURNSTILE_EXPECTED_ACTION_ENV, "").strip()
    if expected_action and payload.get("action") != expected_action:
        raise HTTPException(status_code=401, detail="turnstile_failed")


__all__ = [
    "TURNSTILE_SECRET_ENV",
    "TURNSTILE_ALLOWED_HOSTS_ENV",
    "TURNSTILE_EXPECTED_ACTION_ENV",
    "TURNSTILE_MAX_RESPONSE_BYTES",
    "TURNSTILE_MAX_TOKEN_LENGTH",
    "TURNSTILE_SITEVERIFY_URL",
    "TURNSTILE_TIMEOUT_SECONDS",
    "require_valid_turnstile",
]
