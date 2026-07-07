"""Error taxonomy for fetch operations — drives retry/terminal/fallback decisions.

Replaces the coarse-grained 3-class ErrorClass in retry.py with a 12-class
taxonomy. The decision table maps each class to retry/terminal/fallback-eligible
and bronze-forensics behavior.

H2 owns this module. H3 references ErrorClass by name. H4 registers v3 as
a comment-only migration.
"""

from __future__ import annotations

from enum import Enum


class ErrorClass(str, Enum):
    AUTH = "auth"                          # 401, 403
    PERMISSION_PAID = "permission_paid"    # 402, 451
    RATE_LIMIT = "rate_limit"              # 429
    TIMEOUT = "timeout"                    # connect/read timeout (status==0 + "timeout" in error)
    TRANSPORT = "transport"                # DNS, connection refused, TLS errors
    PROVIDER_5XX = "provider_5xx"          # 500-599 (except recognized sub-codes)
    MALFORMED_RESPONSE = "malformed_response"  # 200 but unparseable body
    PARSE_FAILURE = "parse_failure"        # JSON decode error, schema mismatch
    EMPTY_VALID = "empty_valid"            # 200 + valid JSON + zero results
    BUDGET_EXHAUSTED = "budget_exhausted"  # DailyBudgetExhausted caught
    PARTIAL_SUCCESS = "partial_success"    # Some items stored, some failed
    UNKNOWN = "unknown"                    # Fallback for unclassified errors


# Decision table: {ErrorClass: (retryable, terminal, fallback_eligible, bronze_forensics)}
_DECISION_TABLE: dict[ErrorClass, tuple[bool, bool, bool, bool]] = {
    ErrorClass.AUTH:                (False, True,  False, False),
    ErrorClass.PERMISSION_PAID:     (False, True,  False, False),
    ErrorClass.RATE_LIMIT:          (True,  False, False, False),
    ErrorClass.TIMEOUT:             (True,  False, True,  False),
    ErrorClass.TRANSPORT:           (True,  False, True,  False),
    ErrorClass.PROVIDER_5XX:        (True,  False, True,  False),
    ErrorClass.MALFORMED_RESPONSE:  (False, True,  True,  True),
    ErrorClass.PARSE_FAILURE:       (False, True,  False, True),
    ErrorClass.EMPTY_VALID:         (False, False, True,  True),
    ErrorClass.BUDGET_EXHAUSTED:    (False, True,  False, False),
    ErrorClass.PARTIAL_SUCCESS:     (False, False, False, False),
    ErrorClass.UNKNOWN:             (True,  False, True,  False),
}


def is_retryable(error_class: ErrorClass) -> bool:
    return _DECISION_TABLE.get(error_class, (True, False, True, False))[0]


def is_terminal(error_class: ErrorClass) -> bool:
    return _DECISION_TABLE.get(error_class, (False, True, True, False))[1]


def is_fallback_eligible(error_class: ErrorClass) -> bool:
    return _DECISION_TABLE.get(error_class, (True, False, True, False))[2]


def stores_bronze_forensics(error_class: ErrorClass) -> bool:
    """True if the error class should store raw_asset for post-mortem analysis."""
    return _DECISION_TABLE.get(error_class, (True, False, True, False))[3]


def classify_fetch_error(
    status_code: int | None,
    error_message: str | None = None,
    *,
    is_timeout: bool = False,
    exception_type: str | None = None,
) -> ErrorClass:
    """Classify a fetch error into an ErrorClass.

    Priority order mirrors the H2 decision table: auth/permission first,
    then rate limiting, then transport errors, then 5xx, then successful
    responses with content issues, then parse errors.

    Args:
        status_code: HTTP status code (0 or None if no response received)
        error_message: Error text from exception or response
        is_timeout: True if the error was a connect/read timeout
        exception_type: Full qualified exception class name

    Returns:
        Appropriate ErrorClass enum value.
    """
    # Auth / permission — check first (terminal, never retry)
    if status_code in (401, 403):
        return ErrorClass.AUTH
    if error_message and any(p in error_message.lower() for p in (" 401", "401", "unauthorized", "forbidden")):
        return ErrorClass.AUTH
    if status_code in (402, 451):
        return ErrorClass.PERMISSION_PAID

    # Rate limit
    if status_code == 429:
        return ErrorClass.RATE_LIMIT

    # Timeout — status_code is typically 0 or None
    if is_timeout or (status_code == 0 and error_message and "timeout" in error_message.lower()):
        return ErrorClass.TIMEOUT

    # Transport errors (DNS, connection refused, TLS) — no HTTP response
    if exception_type and any(
        t in exception_type.lower()
        for t in ("connectionerror", "oserror", "connectionrefused", "dnserror",
                  "tlserror", "sslerror", "socket")
    ):
        return ErrorClass.TRANSPORT
    if error_message and any(
        p in error_message.lower()
        for p in ("connection refused", "name or service not known",
                   "network is unreachable", "tls", "ssl")
    ):
        return ErrorClass.TRANSPORT

    # Provider 5xx
    if status_code and 500 <= status_code <= 599:
        return ErrorClass.PROVIDER_5XX

    # Successful responses with content issues
    if status_code == 200:
        if error_message and "json" in error_message.lower():
            return ErrorClass.MALFORMED_RESPONSE
        # If we have a 200 with no data or empty data, it's empty_valid
        # (Caller should pass this explicitly when data is None/empty)
        return ErrorClass.EMPTY_VALID

    # Parse failures
    if exception_type and "jsondecodeerror" in exception_type.lower():
        return ErrorClass.PARSE_FAILURE
    if error_message and "json" in (error_message.lower() or ""):
        return ErrorClass.PARSE_FAILURE

    # Budget exhausted
    if exception_type and "dailybudgetexhausted" in exception_type.lower():
        return ErrorClass.BUDGET_EXHAUSTED

    # Explicit partial success
    if error_message and "partial_success" in (error_message.lower() or ""):
        return ErrorClass.PARTIAL_SUCCESS

    # Non-retryable client errors (4xx not handled above — terminal, no fallback)
    if status_code and 400 <= status_code < 500:
        return ErrorClass.AUTH  # treated as terminal, no fallback, no retry

    return ErrorClass.UNKNOWN
