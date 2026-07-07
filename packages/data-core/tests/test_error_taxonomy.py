"""Tests for error_taxonomy.py — H2 12-class ErrorClass + decision table."""

from __future__ import annotations

from catalyst_data.error_taxonomy import (
    ErrorClass,
    classify_fetch_error,
    is_retryable,
    is_terminal,
    is_fallback_eligible,
    stores_bronze_forensics,
)


class TestClassifyFetchError:
    def test_auth_401(self):
        assert classify_fetch_error(401) == ErrorClass.AUTH

    def test_auth_403(self):
        assert classify_fetch_error(403) == ErrorClass.AUTH

    def test_permission_paid_402(self):
        assert classify_fetch_error(402) == ErrorClass.PERMISSION_PAID

    def test_permission_paid_451(self):
        assert classify_fetch_error(451) == ErrorClass.PERMISSION_PAID

    def test_rate_limit_429(self):
        assert classify_fetch_error(429) == ErrorClass.RATE_LIMIT

    def test_timeout_by_flag(self):
        assert classify_fetch_error(0, is_timeout=True) == ErrorClass.TIMEOUT

    def test_timeout_by_error_message(self):
        assert classify_fetch_error(0, error_message="read timeout") == ErrorClass.TIMEOUT

    def test_transport_by_connection_refused(self):
        assert classify_fetch_error(None, error_message="connection refused") == ErrorClass.TRANSPORT

    def test_transport_by_exception_type(self):
        assert classify_fetch_error(None, exception_type="ConnectionRefusedError") == ErrorClass.TRANSPORT

    def test_provider_500(self):
        assert classify_fetch_error(500) == ErrorClass.PROVIDER_5XX

    def test_provider_502(self):
        assert classify_fetch_error(502) == ErrorClass.PROVIDER_5XX

    def test_provider_503(self):
        assert classify_fetch_error(503) == ErrorClass.PROVIDER_5XX

    def test_malformed_200(self):
        assert classify_fetch_error(200, error_message="json decode error") == ErrorClass.MALFORMED_RESPONSE

    def test_parse_failure_by_exception(self):
        assert classify_fetch_error(None, exception_type="json.JSONDecodeError") == ErrorClass.PARSE_FAILURE

    def test_empty_valid_200(self):
        assert classify_fetch_error(200) == ErrorClass.EMPTY_VALID

    def test_budget_exhausted(self):
        assert classify_fetch_error(None, exception_type="DailyBudgetExhausted") == ErrorClass.BUDGET_EXHAUSTED

    def test_partial_success(self):
        assert classify_fetch_error(None, error_message="partial_success: 3 of 5 items") == ErrorClass.PARTIAL_SUCCESS

    def test_unknown(self):
        assert classify_fetch_error(None) == ErrorClass.UNKNOWN


class TestDecisionTable:
    def test_auth_no_retry_no_fallback(self):
        assert not is_retryable(ErrorClass.AUTH)
        assert is_terminal(ErrorClass.AUTH)
        assert not is_fallback_eligible(ErrorClass.AUTH)
        assert not stores_bronze_forensics(ErrorClass.AUTH)

    def test_rate_limit_retry_no_fallback(self):
        assert is_retryable(ErrorClass.RATE_LIMIT)
        assert not is_terminal(ErrorClass.RATE_LIMIT)
        assert not is_fallback_eligible(ErrorClass.RATE_LIMIT)

    def test_timeout_retry_fallback(self):
        assert is_retryable(ErrorClass.TIMEOUT)
        assert not is_terminal(ErrorClass.TIMEOUT)
        assert is_fallback_eligible(ErrorClass.TIMEOUT)
        assert not stores_bronze_forensics(ErrorClass.TIMEOUT)

    def test_transport_retry_fallback(self):
        assert is_retryable(ErrorClass.TRANSPORT)
        assert is_fallback_eligible(ErrorClass.TRANSPORT)
        assert not stores_bronze_forensics(ErrorClass.TRANSPORT)

    def test_malformed_terminal_stores_bronze(self):
        assert not is_retryable(ErrorClass.MALFORMED_RESPONSE)
        assert is_terminal(ErrorClass.MALFORMED_RESPONSE)
        assert is_fallback_eligible(ErrorClass.MALFORMED_RESPONSE)
        assert stores_bronze_forensics(ErrorClass.MALFORMED_RESPONSE)

    def test_parse_failure_terminal_stores_bronze(self):
        assert not is_retryable(ErrorClass.PARSE_FAILURE)
        assert is_terminal(ErrorClass.PARSE_FAILURE)
        assert stores_bronze_forensics(ErrorClass.PARSE_FAILURE)

    def test_empty_valid_stores_bronze(self):
        assert stores_bronze_forensics(ErrorClass.EMPTY_VALID)
