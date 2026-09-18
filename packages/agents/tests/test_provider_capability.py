"""M5-0: provider capability protocol and bounded V1.1 retry seam.

Phase 4 TSD §15 / Final Migration TSD §15; M5 plan §4. The V1.1 capability
adapter declares structured output + true streaming + token accounting +
timeout normalization; incapable providers are rejected at admission with a
typed ProviderCapabilityError (never ABSTAIN, never a retry reason). The
bounded retry wrapper allows exactly one technical retry per logical role
(max 2 provider attempts), records logical calls separately from provider
attempts, and never retries a valid semantic result such as ABSTAIN/PARTIAL.
"""
from __future__ import annotations

import pytest

from catalyst_agents.attribution.analyst import AttributionStatus
from catalyst_agents.runtime.provider_capability import (
    MAX_TECHNICAL_RETRIES_PER_LOGICAL_ROLE,
    BoundedCallCounts,
    ModelSchemaFailure,
    ModelTimeoutFailure,
    ModelTransportFailure,
    ProviderCapability,
    ProviderCapabilityError,
    TechnicalRetryExhausted,
    extract_provider_usage,
    invoke_with_bounded_retry,
    provider_capability_for,
    require_capabilities,
)

CAPABLE = ProviderCapability(
    supports_structured_output=True,
    supports_true_streaming=True,
    declares_token_accounting=True,
    normalizes_timeout_errors=True,
    capability_revision="test-capability-1",
)


class CapableFake:
    """Fake provider exposing the V1.1 capability surface."""

    capability_metadata = {
        "supports_structured_output": True,
        "supports_true_streaming": True,
        "declares_token_accounting": True,
        "normalizes_timeout_errors": True,
        "capability_revision": "test-capability-1",
    }


class IncapableFake:
    """Fake provider without structured-output or streaming capability."""

    capability_metadata = {
        "supports_structured_output": False,
        "supports_true_streaming": False,
        "declares_token_accounting": False,
        "normalizes_timeout_errors": False,
        "capability_revision": "test-capability-1",
    }


class MethodCapableFake:
    """Capability inferred from method presence when no metadata exists."""

    def with_structured_output(self, *args, **kwargs):  # noqa: ANN002,ANN003
        return self

    def stream(self, *args, **kwargs):  # noqa: ANN002,ANN003
        return iter(())


def test_capable_provider_accepted_at_admission() -> None:
    capability = require_capabilities(CapableFake(), CAPABLE)
    assert capability.supports_structured_output is True
    assert capability.supports_true_streaming is True


def test_incapable_provider_rejected_with_typed_error_never_abstain() -> None:
    with pytest.raises(ProviderCapabilityError) as excinfo:
        require_capabilities(IncapableFake(), CAPABLE)
    assert excinfo.value.missing
    # An admission failure is a typed capability error, never an ABSTAIN status.
    assert not isinstance(excinfo.value, AttributionStatus)


def test_provider_capability_for_infers_from_methods_without_metadata() -> None:
    capability = provider_capability_for(MethodCapableFake())
    assert capability.supports_structured_output is True
    assert capability.supports_true_streaming is True
    assert capability.declares_token_accounting is False


def test_max_technical_retries_per_logical_role_is_one() -> None:
    assert MAX_TECHNICAL_RETRIES_PER_LOGICAL_ROLE == 1


def test_provider_failing_twice_is_never_called_a_third_time() -> None:
    attempts = []

    def failing() -> str:
        attempts.append("call")
        raise ModelTransportFailure("transport down")

    with pytest.raises(TechnicalRetryExhausted):
        invoke_with_bounded_retry(failing, role="evidence_analyst", semantic_input_hash="h" * 64)
    assert len(attempts) == 2


def test_second_attempt_success_records_attempts_and_counts() -> None:
    calls: list[str] = []
    logged: list[AttemptRecord] = []

    def flaky() -> str:
        calls.append("call")
        if len(calls) == 1:
            raise ModelTimeoutFailure("timed out")
        return "ok"

    result = invoke_with_bounded_retry(
        flaky,
        role="evidence_analyst",
        semantic_input_hash="h" * 64,
        attempts_log=logged,
    )
    assert result.result == "ok"
    assert result.counts.logical_calls == 1
    assert result.counts.provider_attempts == 2
    assert len(calls) == 2
    assert [record.outcome for record in logged] == ["ModelTimeoutFailure", "ok"]


def test_retry_with_unknown_attempt_usage_keeps_whole_attempt_usage_unknown():
    """A retry whose first dispatched attempt has no usage stays non-numeric."""
    calls = 0

    def call():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelTransportFailure("transport failed after dispatch")
        return {"usage_metadata": {"input_tokens": 12, "output_tokens": 3}}

    result = invoke_with_bounded_retry(
        call,
        role="evidence_analyst",
        semantic_input_hash="h" * 64,
        usage_extractor=extract_provider_usage,
    )

    assert result.attempt_usages == ((None, None), (12, 3))


def test_valid_abstain_and_partial_are_never_retried() -> None:
    for status in (AttributionStatus.ABSTAIN, AttributionStatus.PARTIAL):
        calls = []

        def returns_status() -> AttributionStatus:
            calls.append(status)
            return status

        result = invoke_with_bounded_retry(
            returns_status,
            role="evidence_analyst",
            semantic_input_hash="h" * 64,
        )
        assert result.result is status
        assert len(calls) == 1
        assert result.counts.logical_calls == 1
        assert result.counts.provider_attempts == 1


def test_schema_failure_is_retried_once_then_exhausted() -> None:
    calls = []

    def bad_schema() -> dict:
        calls.append("call")
        raise ModelSchemaFailure("invalid structured schema")

    with pytest.raises(TechnicalRetryExhausted):
        invoke_with_bounded_retry(bad_schema, role="evidence_analyst", semantic_input_hash="h" * 64)
    assert len(calls) == 2


def test_non_retryable_errors_propagate_immediately() -> None:
    calls = []

    def broken() -> None:
        calls.append("call")
        raise ValueError("not a retryable technical error")

    with pytest.raises(ValueError, match="not a retryable"):
        invoke_with_bounded_retry(broken, role="evidence_analyst", semantic_input_hash="h" * 64)
    assert len(calls) == 1


def test_retry_uses_identical_semantic_input_hash_on_every_attempt() -> None:
    observed: list[str] = []

    def flaky() -> str:
        observed.append("call")
        if len(observed) == 1:
            raise ModelTransportFailure("transport down")
        return "ok"

    result = invoke_with_bounded_retry(
        flaky,
        role="evidence_analyst",
        semantic_input_hash="a" * 64,
    )
    assert result.result == "ok"
    assert all(record.semantic_input_hash == "a" * 64 for record in result.attempts)
    assert result.counts == BoundedCallCounts(logical_calls=1, provider_attempts=2)
