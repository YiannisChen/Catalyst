from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Awaitable, TYPE_CHECKING

if TYPE_CHECKING:
    from catalyst_data.connectors.base import FetchResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryRule:
    base_seconds: float
    max_seconds: float
    max_retries: int
    jitter: bool = False
    min_delay_seconds: float = 0.0


@dataclass(frozen=True)
class RetryPolicy:
    rate_limit: RetryRule
    server_error: RetryRule
    timeout: RetryRule


RETRY_POLICIES = {
    "polygon": RetryPolicy(
        rate_limit=RetryRule(
            base_seconds=60.0,
            max_seconds=300.0,
            max_retries=3,
            jitter=True,
        ),
        server_error=RetryRule(
            base_seconds=5.0,
            max_seconds=60.0,
            max_retries=5,
            jitter=False,
        ),
        timeout=RetryRule(
            base_seconds=5.0,
            max_seconds=20.0,
            max_retries=3,
            jitter=False,
        ),
    ),
    "fmp": RetryPolicy(
        rate_limit=RetryRule(
            base_seconds=60.0,
            max_seconds=300.0,
            max_retries=3,
            jitter=True,
        ),
        server_error=RetryRule(
            base_seconds=5.0,
            max_seconds=20.0,
            max_retries=3,
            jitter=False,
        ),
        timeout=RetryRule(
            base_seconds=5.0,
            max_seconds=20.0,
            max_retries=3,
            jitter=False,
        ),
    ),
    "fred": RetryPolicy(
        rate_limit=RetryRule(
            base_seconds=10.0,
            max_seconds=30.0,
            max_retries=3,
            jitter=False,
        ),
        server_error=RetryRule(
            base_seconds=10.0,
            max_seconds=40.0,
            max_retries=3,
            jitter=False,
        ),
        timeout=RetryRule(
            base_seconds=10.0,
            max_seconds=40.0,
            max_retries=3,
            jitter=False,
        ),
    ),
    "sec": RetryPolicy(
        rate_limit=RetryRule(
            base_seconds=5.0,
            max_seconds=30.0,
            max_retries=3,
            jitter=False,
        ),
        server_error=RetryRule(
            base_seconds=10.0,
            max_seconds=60.0,
            max_retries=3,
            jitter=False,
        ),
        timeout=RetryRule(
            base_seconds=10.0,
            max_seconds=40.0,
            max_retries=2,
            jitter=False,
        ),
    ),
    "finnhub": RetryPolicy(
        rate_limit=RetryRule(
            base_seconds=1.0,
            max_seconds=30.0,
            max_retries=3,
            jitter=False,
            min_delay_seconds=1.0,
        ),
        server_error=RetryRule(
            base_seconds=5.0,
            max_seconds=30.0,
            max_retries=3,
            jitter=False,
        ),
        timeout=RetryRule(
            base_seconds=5.0,
            max_seconds=20.0,
            max_retries=2,
            jitter=False,
        ),
    ),
    "yfinance": RetryPolicy(
        rate_limit=RetryRule(
            base_seconds=10.0,
            max_seconds=30.0,
            max_retries=3,
            jitter=False,
        ),
        server_error=RetryRule(
            base_seconds=5.0,
            max_seconds=20.0,
            max_retries=3,
            jitter=False,
        ),
        timeout=RetryRule(
            base_seconds=5.0,
            max_seconds=20.0,
            max_retries=3,
            jitter=False,
        ),
    ),
}

MAX_RETRIES = RETRY_POLICIES["polygon"].rate_limit.max_retries
BACKOFF_BASE_SECONDS = 2.0

from catalyst_data.error_taxonomy import ErrorClass, classify_fetch_error, is_retryable, is_terminal  # noqa: F401


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


def get_retry_policy(provider: str) -> RetryPolicy:
    try:
        return RETRY_POLICIES[provider]
    except KeyError as exc:
        raise ValueError(f"Unknown provider: {provider}") from exc


def _retry_rule_for_result(
    provider: str,
    result: "FetchResult",
) -> RetryRule | None:
    policy = get_retry_policy(provider)
    is_timeout = result.status == 0 and result.error and "timeout" in result.error.lower()
    if result.status == 429:
        return policy.rate_limit
    if result.status in (500, 502, 503, 504):
        return policy.server_error
    if is_timeout:
        return policy.timeout
    return None


def _compute_rule_delay(
    rule: RetryRule,
    attempt: int,
    *,
    retry_after: float | None = None,
) -> float:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")

    if retry_after is not None and retry_after > 0:
        delay = max(retry_after, rule.min_delay_seconds)
    elif rule.base_seconds == 0.0:
        delay = 0.0
    else:
        delay = rule.base_seconds * (2 ** (attempt - 1))
        if rule.jitter:
            delay += random.uniform(0.0, 1.0)
        delay = max(delay, rule.min_delay_seconds)
    return min(delay, rule.max_seconds)


# ---------------------------------------------------------------------------
# Public async wrapper — wires retry logic into any fetch function
# ---------------------------------------------------------------------------

def with_retry(
    fetch_fn: Callable[..., Awaitable["FetchResult"]],
    *,
    provider: str | None = None,
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
        if provider is None:
            for attempt in range(1, MAX_RETRIES + 1):
                result = await fetch_fn(*args, **kwargs)
                last_result = result

                if result.status == 200:
                    return result

                is_timeout = result.status == 0 and result.error and "timeout" in result.error.lower()
                err_class = classify_fetch_error(result.status, result.error, is_timeout=is_timeout)

                if not is_retryable(err_class):  # terminal or non-retryable — give up
                    return result

                if attempt >= MAX_RETRIES:
                    break

                retry_after: float | None = None
                if result.status == 429:
                    retry_after = getattr(result, "retry_after_seconds", None)
                    if retry_after is None and result.error:
                        m = re.search(r"Retry-After:\s*(\d+)", result.error)
                        if m:
                            retry_after = float(m.group(1))

                delay = compute_backoff(attempt, retry_after=retry_after)
                logger.info(
                    "Retry %d/%d for %s (status=%s, backoff=%.1fs)",
                    attempt, MAX_RETRIES, result.source_label, result.status, delay,
                )
                await asyncio.sleep(delay)

            assert last_result is not None
            last_result.error = (
                f"{last_result.error or ''} [exhausted {MAX_RETRIES} retries]".strip()
            )
            return last_result

        policy = get_retry_policy(provider)
        max_attempts = max(
            policy.rate_limit.max_retries,
            policy.server_error.max_retries,
            policy.timeout.max_retries,
        )

        for attempt in range(1, max_attempts + 1):
            result = await fetch_fn(*args, **kwargs)
            last_result = result

            # Success — return immediately
            if result.status == 200:
                return result

            is_timeout = result.status == 0 and result.error and "timeout" in result.error.lower()
            err_class = classify_fetch_error(result.status, result.error, is_timeout=is_timeout)

            if not is_retryable(err_class):  # terminal or non-retryable — give up
                return result

            rule = _retry_rule_for_result(provider, result)
            if rule is None or attempt >= rule.max_retries:
                break  # exhausted

            # Use structured retry_after_seconds from connector (real HTTP header).
            # Fall back to regex on error string for legacy callers.
            retry_after: float | None = None
            if result.status == 429:
                retry_after = getattr(result, "retry_after_seconds", None)
                if retry_after is None and result.error:
                    m = re.search(r"Retry-After:\s*(\d+)", result.error)
                    if m:
                        retry_after = float(m.group(1))

            delay = _compute_rule_delay(rule, attempt, retry_after=retry_after)
            logger.info(
                "Retry %d/%d for %s provider=%s (status=%s, backoff=%.1fs)",
                attempt, rule.max_retries, result.source_label, provider, result.status, delay,
            )
            await asyncio.sleep(delay)

        # All retries exhausted
        assert last_result is not None
        last_result.error = (
            f"{last_result.error or ''} [exhausted retries for {provider}]".strip()
        )
        return last_result

    return wrapper
