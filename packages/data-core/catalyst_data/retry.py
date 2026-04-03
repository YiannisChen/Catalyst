from __future__ import annotations

from enum import Enum

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0


class ErrorClass(Enum):
    IMMEDIATE_FALLBACK = "immediate_fallback"
    RETRYABLE = "retryable"
    NO_FALLBACK = "no_fallback"


def classify_error(
    status_code: int | None, *, is_timeout: bool = False
) -> ErrorClass:
    """Classify an HTTP error into a retry/fallback decision bucket."""
    if status_code in (401, 403):
        return ErrorClass.IMMEDIATE_FALLBACK
    if status_code in (429, 500, 502, 503, 504) or is_timeout:
        return ErrorClass.RETRYABLE
    return ErrorClass.NO_FALLBACK


def compute_backoff(attempt: int) -> float:
    """Return backoff delay in seconds: 2, 4, 8, ..."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))


def should_retry(attempt: int) -> bool:
    """True if we haven't exhausted retries yet."""
    return attempt < MAX_RETRIES
