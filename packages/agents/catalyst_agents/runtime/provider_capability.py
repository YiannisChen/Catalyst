"""V1.1 provider capability protocol and bounded technical retry seam (M5-0).

Final Migration TSD §15; Frozen §9.2; M5 plan §4. Providers must declare
structured Analyst output, true Writer streaming, token accounting, and
timeout normalization before admission. Incapable providers are rejected with
a typed ``ProviderCapabilityError`` at admission — never ``ABSTAIN``, never a
retry reason.

The V1.1 technical retry policy allows exactly one retry per logical role
(``MAX_TECHNICAL_RETRIES_PER_LOGICAL_ROLE = 1``, max 2 provider attempts) for
transport, timeout, or invalid structured schema with identical semantic
input. Valid ABSTAIN/PARTIAL/semantic inconsistency is never retried. Logical
model-call counts and provider attempts are recorded separately: a retry of
the same logical call does not increment the logical count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

MAX_TECHNICAL_RETRIES_PER_LOGICAL_ROLE = 1

CAPABILITY_REVISION = "v1.1-capability-1"


@dataclass(frozen=True)
class ProviderCapability:
    """Declared V1.1 provider capability surface (Final TSD §15)."""

    supports_structured_output: bool
    supports_true_streaming: bool
    declares_token_accounting: bool
    normalizes_timeout_errors: bool
    capability_revision: str


class ProviderCapabilityError(RuntimeError):
    """Admission failure: provider lacks a required V1.1 capability.

    This is a typed capability failure and is never an attribution status.
    """

    def __init__(self, *, provider: str, missing: list[str], needed: ProviderCapability):
        self.provider = provider
        self.missing = missing
        self.needed = needed
        super().__init__(
            f"provider {provider!r} lacks required V1.1 capabilities: "
            f"{sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# Retryable technical model errors (M5-0 / Frozen §6.6)
# ---------------------------------------------------------------------------

class ModelRoleCallError(Exception):
    """Base class for retryable technical model-role failures."""


class ModelTransportFailure(ModelRoleCallError):
    """Transport-level provider failure (retryable once)."""


class ModelTimeoutFailure(ModelRoleCallError):
    """Provider timeout (retryable once)."""


class ModelSchemaFailure(ModelRoleCallError):
    """Invalid structured schema/reference output (retryable once; then
    MODEL_SCHEMA_FAILURE per Frozen §6.6)."""


class TechnicalRetryExhausted(ModelRoleCallError):
    """The bounded technical retry budget for one logical role is exhausted.

    This is a system failure for the run, never an epistemic ABSTAIN.
    """

    def __init__(self, *, role: str, attempts: int, last_error: Exception):
        self.role = role
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"role {role!r} failed after {attempts} provider attempts: {last_error!r}"
        )


@dataclass(frozen=True)
class AttemptRecord:
    """One provider attempt inside one logical model call."""

    role: str
    attempt: int  # 1-based provider attempt
    semantic_input_hash: str
    outcome: str  # "ok" or the error class name
    error_code: str | None = None


@dataclass(frozen=True)
class BoundedCallCounts:
    """Logical model calls vs provider attempts, recorded separately."""

    logical_calls: int
    provider_attempts: int


@dataclass(frozen=True)
class BoundedRetryResult:
    result: Any
    counts: BoundedCallCounts
    attempts: tuple[AttemptRecord, ...] = field(default_factory=tuple)


_RETRYABLE = (ModelTransportFailure, ModelTimeoutFailure, ModelSchemaFailure)


def invoke_with_bounded_retry(
    fn: Callable[[], Any],
    *,
    role: str,
    semantic_input_hash: str,
    attempts_log: list[AttemptRecord] | None = None,
) -> BoundedRetryResult:
    """Run one logical model call with at most one technical retry.

    The semantic input is identical across attempts (same hash). A valid
    semantic result (including ABSTAIN/PARTIAL) is never retried. After
    exhaustion the caller sees ``TechnicalRetryExhausted``.
    """
    local_attempts: list[AttemptRecord] = []
    max_attempts = MAX_TECHNICAL_RETRIES_PER_LOGICAL_ROLE + 1
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
        except _RETRYABLE as exc:
            local_attempts.append(
                AttemptRecord(
                    role=role,
                    attempt=attempt,
                    semantic_input_hash=semantic_input_hash,
                    outcome=type(exc).__name__,
                    error_code=str(exc),
                )
            )
            if attempt >= max_attempts:
                if attempts_log is not None:
                    attempts_log.extend(local_attempts)
                raise TechnicalRetryExhausted(
                    role=role, attempts=attempt, last_error=exc
                ) from exc
            continue
        local_attempts.append(
            AttemptRecord(
                role=role,
                attempt=attempt,
                semantic_input_hash=semantic_input_hash,
                outcome="ok",
            )
        )
        if attempts_log is not None:
            attempts_log.extend(local_attempts)
        return BoundedRetryResult(
            result=result,
            counts=BoundedCallCounts(logical_calls=1, provider_attempts=attempt),
            attempts=tuple(local_attempts),
        )
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Capability adapter
# ---------------------------------------------------------------------------

def provider_capability_for(llm: Any) -> ProviderCapability:
    """Return the declared capability surface for an LLM client.

    Explicit ``capability_metadata`` (dict or ProviderCapability) wins;
    otherwise capabilities are inferred from the client method surface.
    """
    metadata = getattr(llm, "capability_metadata", None)
    if isinstance(metadata, ProviderCapability):
        return metadata
    if isinstance(metadata, dict):
        return ProviderCapability(**metadata)
    return ProviderCapability(
        supports_structured_output=callable(
            getattr(llm, "with_structured_output", None)
        ),
        supports_true_streaming=callable(getattr(llm, "stream", None)) or callable(
            getattr(llm, "astream", None)
        ),
        declares_token_accounting=(
            callable(getattr(llm, "get_num_tokens", None))
            or getattr(llm, "stream_usage", None) is not None
        ),
        normalizes_timeout_errors=hasattr(llm, "request_timeout"),
        capability_revision=CAPABILITY_REVISION,
    )


def require_capabilities(
    provider: Any,
    needed: ProviderCapability,
) -> ProviderCapability:
    """Admission gate: reject a provider missing any required capability.

    Raises ``ProviderCapabilityError`` (never ABSTAIN, never a retry reason)
    when the provider cannot satisfy the required surface.
    """
    actual = provider_capability_for(provider)
    missing: list[str] = []
    for field_name in (
        "supports_structured_output",
        "supports_true_streaming",
        "declares_token_accounting",
        "normalizes_timeout_errors",
    ):
        if getattr(needed, field_name) and not getattr(actual, field_name):
            missing.append(field_name)
    if missing:
        provider_name = getattr(provider, "__class__", type(provider)).__name__
        raise ProviderCapabilityError(
            provider=provider_name, missing=missing, needed=needed
        )
    return actual


__all__ = [
    "AttemptRecord",
    "BoundedCallCounts",
    "BoundedRetryResult",
    "CAPABILITY_REVISION",
    "MAX_TECHNICAL_RETRIES_PER_LOGICAL_ROLE",
    "ModelRoleCallError",
    "ModelSchemaFailure",
    "ModelTimeoutFailure",
    "ModelTransportFailure",
    "ProviderCapability",
    "ProviderCapabilityError",
    "TechnicalRetryExhausted",
    "invoke_with_bounded_retry",
    "provider_capability_for",
    "require_capabilities",
]
