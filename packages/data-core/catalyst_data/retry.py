from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Callable, Awaitable, TYPE_CHECKING

if TYPE_CHECKING:
    from catalyst_data.connectors.base import FetchResult

logger = logging.getLogger(__name__)

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


def compute_backoff(attempt: int, retry_after: float | None = None) -> float:
    """Return backoff delay in seconds.

    If *retry_after* is provided (from a 429 ``Retry-After`` header), it
    takes priority over the exponential formula.  Otherwise: 2, 4, 8, ...
    """
    if retry_after is not None and retry_after > 0:
        return retry_after
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))


def should_retry(attempt: int) -> bool:
    """True if we haven't exhausted retries yet."""
    return attempt < MAX_RETRIES


# ---------------------------------------------------------------------------
# Public async wrapper — wires retry logic into any fetch function
# ---------------------------------------------------------------------------

def with_retry(
    fetch_fn: Callable[..., Awaitable["FetchResult"]],
) -> Callable[..., Awaitable["FetchResult"]]:
    """Wrap an async fetch function with automatic retry on retryable errors.

    The wrapper inspects ``FetchResult.status`` after each call.  If the status
    is retryable (429, 5xx, or timeout indicated by status 0 + error containing
    'timeout'), it sleeps for an exponential backoff and retries.

    For 429 responses the wrapper reads the structured
    ``FetchResult.retry_after_seconds`` field (populated by connectors from the
    real HTTP ``Retry-After`` header).  As a fallback for callers that populate
    the error string instead, a regex match on the error text is attempted.

    Returns a new async function with the same signature.
    """
    from catalyst_data.connectors.base import FetchResult

    async def wrapper(*args, **kwargs) -> FetchResult:
        last_result: FetchResult | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            result = await fetch_fn(*args, **kwargs)
            last_result = result

            # Success — return immediately
            if result.status == 200:
                return result

            is_timeout = result.status == 0 and result.error and "timeout" in result.error.lower()
            err_class = classify_error(result.status, is_timeout=is_timeout)

            if err_class != ErrorClass.RETRYABLE:
                return result  # non-retryable — give up

            if attempt >= MAX_RETRIES:
                break  # exhausted

            # Use structured retry_after_seconds from connector (real HTTP header).
            # Fall back to regex on error string for legacy callers.
            retry_after: float | None = None
            if result.status == 429:
                retry_after = getattr(result, "retry_after_seconds", None)
                if retry_after is None and result.error:
                    import re
                    m = re.search(r"Retry-After:\s*(\d+)", result.error)
                    if m:
                        retry_after = float(m.group(1))

            delay = compute_backoff(attempt, retry_after=retry_after)
            logger.info(
                "Retry %d/%d for %s (status=%s, backoff=%.1fs)",
                attempt, MAX_RETRIES, result.source_label, result.status, delay,
            )
            await asyncio.sleep(delay)

        # All retries exhausted
        assert last_result is not None
        last_result.error = (
            f"{last_result.error or ''} [exhausted {MAX_RETRIES} retries]".strip()
        )
        return last_result

    return wrapper
