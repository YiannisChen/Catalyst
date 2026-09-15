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
from collections.abc import Mapping
from typing import Any, Callable

MAX_TECHNICAL_RETRIES_PER_LOGICAL_ROLE = 1

CAPABILITY_REVISION = "v1.1-capability-1"


@dataclass(frozen=True)
class ProviderCapability:
    """Declared V1.1 provider capability surface (Final TSD §15).

    ``structured_output_method`` optionally names the LangChain
    ``with_structured_output`` method the endpoint accepts. OpenAI-compatible
    endpoints that reject ``response_format: json_schema`` (for example
    DeepSeek, which documents ``json_object``) declare ``"json_mode"`` so the
    Analyst admission can steer the request instead of sending an unsupported
    shape. ``None`` keeps the provider default (function-calling/json-schema).
    """

    supports_structured_output: bool
    supports_true_streaming: bool
    declares_token_accounting: bool
    normalizes_timeout_errors: bool
    capability_revision: str
    structured_output_method: str | None = None


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
    attempt_usages: tuple[tuple[int | None, int | None], ...] = field(
        default_factory=tuple
    )


_RETRYABLE = (ModelTransportFailure, ModelTimeoutFailure, ModelSchemaFailure)


def _usage_candidates(raw: Any) -> tuple[Mapping[str, Any], ...]:
    """Return provider usage mappings across common response envelopes."""
    candidates: list[Mapping[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, Mapping):
            candidates.append(value)

    add(raw)
    for name in ("usage_metadata", "response_metadata"):
        add(getattr(raw, name, None))
    if isinstance(raw, Mapping):
        for name in ("usage_metadata", "response_metadata"):
            add(raw.get(name))

    for parent in tuple(candidates):
        for name in ("token_usage", "usage"):
            add(parent.get(name))

    return tuple(candidates)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def extract_provider_usage(raw: Any) -> tuple[int | None, int | None]:
    """Extract verifiable input/output usage without zero-filling.

    The returned pair may be partial; callers must treat either ``None`` as
    unknown. This is shared by Analyst/Writer retry accounting so every
    dispatched attempt is evaluated under the same rule.
    """
    input_tokens: int | None = None
    output_tokens: int | None = None
    for mapping in _usage_candidates(raw):
        if input_tokens is None:
            input_tokens = _nonnegative_int(
                mapping.get("input_tokens", mapping.get("prompt_tokens"))
            )
        if output_tokens is None:
            output_tokens = _nonnegative_int(
                mapping.get("output_tokens", mapping.get("completion_tokens"))
            )
        if input_tokens is not None and output_tokens is not None:
            break
    return input_tokens, output_tokens


def invoke_with_bounded_retry(
    fn: Callable[[], Any],
    *,
    role: str,
    semantic_input_hash: str,
    attempts_log: list[AttemptRecord] | None = None,
    budget: Any | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    usage_extractor: Callable[[Any], tuple[int | None, int | None]] | None = None,
) -> BoundedRetryResult:
    """Run one logical model call with at most one technical retry.

    The semantic input is identical across attempts (same hash). A valid
    semantic result (including ABSTAIN/PARTIAL) is never retried. After
    exhaustion the caller sees ``TechnicalRetryExhausted``.

    When ``budget`` (a ``ProviderBudgetGuard``) is supplied, every provider
    attempt reserves the shared budget *before* dispatch and is settled with
    the genuinely reported token usage afterwards. A reservation failure
    propagates unchanged: the provider is never dispatched without budget.

    Once an attempt has actually been dispatched its budget is consumed no
    matter how it ends: a success settles with the reported usage, a retryable
    failure settles the conservative bound and the retry reserves again, and a
    non-retryable failure settles the conservative bound before propagating.
    Only a reservation whose dispatch never happened can be refunded (and the
    guard never refunds it; the caller simply never settles it).
    """
    if budget is not None and (provider is None or model_id is None):
        raise ValueError(
            "a budget guard requires the exact provider/model_id identity so "
            "the pre-call cost bound is identity-bound"
        )
    local_attempts: list[AttemptRecord] = []
    local_usages: list[tuple[int | None, int | None]] = []
    max_attempts = MAX_TECHNICAL_RETRIES_PER_LOGICAL_ROLE + 1
    for attempt in range(1, max_attempts + 1):
        reservation = None
        if budget is not None:
            reservation = budget.reserve(
                role=role, provider=provider, model_id=model_id
            )
            if attempt == 1:
                record_logical_call = getattr(budget, "record_logical_call", None)
                if callable(record_logical_call):
                    record_logical_call(role=role)
        try:
            result = fn()
        except _RETRYABLE as exc:
            local_usages.append((None, None))
            if reservation is not None:
                # The attempt was dispatched; its budget is consumed even
                # though it failed (a retry reserves again).
                budget.settle(reservation)
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
        except BaseException as exc:
            local_usages.append((None, None))
            # A non-retryable failure is still a dispatched, billed attempt: a
            # provider that was reached may have consumed tokens before
            # failing, so the conservative bound is settled before the error
            # propagates. The reservation is never left outstanding.
            if reservation is not None:
                budget.settle(reservation)
            local_attempts.append(
                AttemptRecord(
                    role=role,
                    attempt=attempt,
                    semantic_input_hash=semantic_input_hash,
                    outcome=type(exc).__name__,
                    error_code=str(exc),
                )
            )
            if attempts_log is not None:
                attempts_log.extend(local_attempts)
            raise
        input_tokens: int | None = None
        output_tokens: int | None = None
        if usage_extractor is not None:
            try:
                input_tokens, output_tokens = usage_extractor(result)
            except Exception:  # pragma: no cover - defensive
                input_tokens, output_tokens = None, None
        local_usages.append((input_tokens, output_tokens))
        if reservation is not None:
            budget.settle(
                reservation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
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
            attempt_usages=tuple(local_usages),
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
        structured_output_method=None,
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
    "extract_provider_usage",
    "invoke_with_bounded_retry",
    "provider_capability_for",
    "require_capabilities",
]
