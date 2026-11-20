"""Request identity and administrator checks for the public API."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

AccessTier = Literal["anonymous", "invite", "admin"]
_INVITE_PREFIX = "inv_"
_INVITE_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_MIN_INVITE_SIGNING_KEY_BYTES = 32
_MAX_INVITE_LIFETIME = timedelta(days=30)


@dataclass(frozen=True)
class AccessContext:
    tier: AccessTier
    token_id: str | None = None


def sign_invite_token(
    token_id: str,
    expires_at: str,
    *,
    signing_key: str | None = None,
) -> str:
    """Create a contract-shaped invite token for offline operator issuance."""
    key_bytes = _invite_signing_key_bytes(signing_key)
    normalized_token_id = _normalize_token_id(token_id)
    expiry = _parse_expiry(expires_at)
    current = datetime.now(timezone.utc)
    if expiry <= current:
        raise ValueError("expires_at must be in the future")
    if expiry - current > _MAX_INVITE_LIFETIME:
        raise ValueError("expires_at exceeds the maximum invite lifetime of 30 days")
    canonical_expiry = expiry.isoformat().replace("+00:00", "Z")
    payload = json.dumps(
        {"expires_at": canonical_expiry, "token_id": normalized_token_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    payload_segment = _encode_segment(payload)
    signing_input = f"{_INVITE_PREFIX}{payload_segment}".encode("ascii")
    signature = hmac.new(key_bytes, signing_input, hashlib.sha256).digest()
    return f"{_INVITE_PREFIX}{payload_segment}.{_encode_segment(signature)}"


def _normalize_token_id(token_id: str) -> str:
    if not isinstance(token_id, str):
        raise ValueError("token_id must be a printable string of at most 128 characters")
    normalized = token_id.strip()
    if not normalized or len(normalized) > 128 or not normalized.isprintable():
        raise ValueError("token_id must be a printable string of 1 to 128 characters")
    return normalized


def _invite_signing_key_bytes(signing_key: str | None) -> bytes:
    key = (
        signing_key
        if signing_key is not None
        else os.environ.get("CATALYST_INVITE_SIGNING_KEY")
    )
    if not isinstance(key, str) or not key:
        raise ValueError("CATALYST_INVITE_SIGNING_KEY is not configured")
    key_bytes = key.encode("utf-8")
    if len(key_bytes) < _MIN_INVITE_SIGNING_KEY_BYTES:
        raise ValueError("CATALYST_INVITE_SIGNING_KEY must be at least 32 bytes")
    return key_bytes


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_segment(value: str) -> bytes:
    if not value or _INVITE_SEGMENT_RE.fullmatch(value) is None:
        raise ValueError("invalid base64url segment")
    padded = value + "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(padded)
    if _encode_segment(decoded) != value:
        raise ValueError("non-canonical base64url segment")
    return decoded


def _parse_expiry(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("expires_at must be a timezone-aware ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("expires_at must be a timezone-aware ISO-8601 string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expires_at must include a timezone")
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError("expires_at is outside the supported UTC range") from exc


def verify_invite_token(
    token: str,
    *,
    signing_key: str | None = None,
    now: datetime | None = None,
) -> AccessContext:
    """Verify a manually issued HMAC token without persisting its contents."""
    try:
        key_bytes = _invite_signing_key_bytes(signing_key)
    except (UnicodeError, ValueError):
        raise HTTPException(status_code=401, detail="invite_invalid") from None
    if not isinstance(token, str) or len(token) > 4096:
        raise HTTPException(status_code=401, detail="invite_invalid")
    try:
        prefix, signature_segment = token.split(".", maxsplit=1)
        if not prefix.startswith(_INVITE_PREFIX):
            raise ValueError("invalid token prefix")
        payload_segment = prefix[len(_INVITE_PREFIX):]
        payload_bytes = _decode_segment(payload_segment)
        signature = _decode_segment(signature_segment)
        if len(signature) != hashlib.sha256().digest_size:
            raise ValueError("invalid signature length")
        expected = hmac.new(
            key_bytes, prefix.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("signature mismatch")
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict) or set(payload) != {"token_id", "expires_at"}:
            raise ValueError("invalid payload fields")
        token_id = payload["token_id"]
        if _normalize_token_id(token_id) != token_id:
            raise ValueError("invalid token id")
        expires_at = _parse_expiry(payload["expires_at"])
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="invite_invalid") from None

    current = now or datetime.now(timezone.utc)
    try:
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current_utc = current.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise HTTPException(status_code=401, detail="invite_invalid") from None
    if expires_at <= current_utc:
        raise HTTPException(status_code=401, detail="invite_expired")
    if expires_at - current_utc > _MAX_INVITE_LIFETIME:
        raise HTTPException(status_code=401, detail="invite_invalid")
    return AccessContext(tier="invite", token_id=token_id)


def resolve_access_context(request: Request) -> AccessContext:
    """Resolve admin credentials and signed invite tokens without downgrade.

    Invite validation is stateless and uses the offline issuer's HMAC key. A
    supplied but invalid invite token must never silently become anonymous.
    """
    invite_token = request.headers.get("x-invite-token")
    invite_context = (
        verify_invite_token(invite_token)
        if invite_token is not None
        else None
    )

    authorization = request.headers.get("authorization")
    if authorization is None:
        if os.environ.get("CATALYST_ALLOW_NON_LOOPBACK") != "1":
            # Local-only mode treats the operator at the loopback boundary as
            # the administrator, matching the self-hosted deployment contract.
            # Validate any supplied invite above, but do not downgrade the
            # local operator merely because the client also carries an invite.
            return AccessContext(tier="admin")
        if invite_context is not None:
            return invite_context
        return AccessContext(tier="anonymous")

    scheme, separator, supplied = authorization.partition(" ")
    expected = os.environ.get("CATALYST_ADMIN_TOKEN", "")
    if (
        scheme.lower() != "bearer"
        or not separator
        or not supplied
        or not expected
        or not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))
    ):
        raise HTTPException(status_code=401, detail="admin_unauthenticated")
    return AccessContext(tier="admin")


def require_admin(request: Request) -> AccessContext:
    context = getattr(request.state, "access_context", None)
    if context is None:
        context = resolve_access_context(request)
    if context.tier != "admin":
        if context.tier != "anonymous" or request.headers.get("x-turnstile-token"):
            raise HTTPException(status_code=403, detail="admin_required")
        raise HTTPException(status_code=401, detail="admin_unauthenticated")
    return context


def install_access_control(app) -> None:
    """Reject malformed/invalid credentials before any API route can downgrade."""

    @app.middleware("http")
    async def _access_control(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            try:
                request.state.access_context = resolve_access_context(request)
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=exc.headers,
                )
        return await call_next(request)
