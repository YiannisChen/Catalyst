"""EvidenceAnalyst node (M5-1) — strict structured-output boundary.

Frozen §6.3; Phase 4 TSD §§6–12; Final TSD §8. The node loads the persisted
EvidenceAnalystContextPack/render pair (M4), renders the exact persisted model
messages, runs capability preflight through the M5-0 protocol, calls the
injected LLM once (with at most one bounded technical retry for
transport/timeout/invalid structured schema), and parses a strict
``AnalystDecision`` (``extra="forbid"``). A second schema failure is
MODEL_SCHEMA_FAILURE — a system failure, never ABSTAIN. Valid ABSTAIN/PARTIAL
is never retried.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from catalyst_agents.attribution.analyst import (
    MODEL_FACING_DERIVED_RELATION_FIELDS,
    AnalystDecision,
    AttributionStatus,
    AttributionType,
    CandidateHypothesis,
    EvidenceDecision,
    ProposedCorrectiveIntent,
    ProposedMissingEvidence,
    ResearchDecision,
)
from catalyst_agents.attribution.context_pack_builder import (
    RenderMessage,
    canonical_context_pack_json,
)
from catalyst_agents.runtime.pack_persistence import (
    PackNotPersistedError,
    PackPersistence,
    require_committed_pair,
)
from catalyst_agents.runtime.provider_capability import (
    CAPABILITY_REVISION,
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

ANALYST_ROLE = "evidence_analyst"

# The Analyst admission surface requires structured output; streaming is the
# Writer's capability and is not required here.
ANALYST_REQUIRED_CAPABILITIES = ProviderCapability(
    supports_structured_output=True,
    supports_true_streaming=False,
    declares_token_accounting=True,
    normalizes_timeout_errors=True,
    capability_revision=CAPABILITY_REVISION,
)


def _prompt_template(prompt_path: Path | None, prompt_text: str | None) -> str:
    if prompt_text is not None:
        return prompt_text
    if prompt_path is not None:
        return prompt_path.read_text(encoding="utf-8")
    return ""


_ANALYST_DECISION_EXAMPLE_SCHEMA_VERSION = "1.0"

# Exact-field empty-inventory AnalystDecision. ``json_mode`` sends
# ``response_format={"type": "json_object"}``, which carries no schema, so the
# field contract must travel in the prompt text; this mapping is generated from
# the Pydantic contract so field names and enum values cannot drift.
_EMPTY_INVENTORY_EXAMPLE: dict[str, Any] = {
    "schema_version": _ANALYST_DECISION_EXAMPLE_SCHEMA_VERSION,
    "evidence_decisions": [],
    "candidate_hypotheses": [],
    "conflicts": [],
    "proposed_missing_evidence": [],
    "research_decision": ResearchDecision.ABSTAIN.value,
    "recommended_status": AttributionStatus.ABSTAIN.value,
    "proposed_attribution_type": AttributionType.NO_MATERIAL_PUBLIC_CATALYST.value,
    "proposed_corrective_intents": [],
}

# Nested contracts whose field names the model also has to reproduce exactly.
_NESTED_DECISION_MODELS: tuple[tuple[str, Any], ...] = (
    ("evidence_decisions[]", EvidenceDecision),
    ("candidate_hypotheses[]", CandidateHypothesis),
    ("proposed_missing_evidence[]", ProposedMissingEvidence),
    ("proposed_corrective_intents[]", ProposedCorrectiveIntent),
)


def _empty_citable_inventory_result(
    *,
    schema: type[AnalystDecision],
    pair: Any,
    hypothesis_policy_version: str | None,
) -> dict[str, Any]:
    """Deterministic evidence-free ABSTAIN when the citable inventory is empty.

    Round 1 and the corrective round share this path: ``included_evidence_ids``
    empty means there is nothing the Analyst may cite, so the node does not
    dispatch a provider. The decision is the contract-legal empty ABSTAIN
    (no evidence, candidates, or gaps); logical calls, provider attempts, and
    usage are all zero. Writer LIMITATION still runs downstream.
    """
    example = _empty_inventory_example(schema)
    if not example:
        raise ModelSchemaFailure(
            "empty citable inventory fast path has no legal AnalystDecision example"
        )
    decision = schema.model_validate(example)
    schema_version = getattr(schema, "schema_version", "1.0")
    if hypothesis_policy_version is not None:
        schema_version = f"{schema_version}+hypothesis:{hypothesis_policy_version}"
    semantic_input_hash = hashlib.sha256(
        canonical_context_pack_json(
            {
                "fast_path": "empty_citable_inventory_v1",
                "pack_sha256": pair.refs.pack_sha256,
                "rendered_messages_sha256": pair.refs.rendered_messages_sha256,
                "schema_version": schema_version,
            }
        )
    ).hexdigest()
    return {
        "analyst_decision": decision,
        "decision_hash": _decision_hash(decision),
        "context_pack_sha256": pair.refs.pack_sha256,
        "rendered_messages_sha256": pair.refs.rendered_messages_sha256,
        "semantic_input_hash": semantic_input_hash,
        "analyst_logical_calls": 0,
        "analyst_provider_attempts": 0,
        "analyst_attempts": (),
        "analyst_attempt_usages": (),
    }


def _empty_inventory_example(schema: type[AnalystDecision]) -> dict[str, Any]:
    """Return the exact-field empty-inventory example for ``schema``.

    Generated against the live Pydantic contract: the example is filtered to
    the model's declared fields and is validated before use. If the contract
    ever drifts (a field added or renamed) this returns ``{}`` and no example
    is injected — the strict ``extra="forbid"`` parse stays authoritative.
    """
    fields = set(schema.model_fields)
    example = {
        name: value
        for name, value in _EMPTY_INVENTORY_EXAMPLE.items()
        if name in fields
    }
    if set(example) != fields:
        return {}
    try:
        schema.model_validate(example)
    except ValidationError:
        return {}
    return example


def _json_mode_field_contract(schema: type[AnalystDecision]) -> str:
    """System-prompt text carrying the AnalystDecision *field shape* into json_mode.

    ``json_object`` mode embeds no schema, so the model receives the exact
    field names and the empty-inventory example here instead. The text is
    appended to the prompt BEFORE the semantic input hash, so the hash covers
    exactly what the provider receives. Under ``function_calling`` the schema
    travels in ``tools[]`` and this text is documentation only: it is not
    injected, because its own preamble ("no schema is transmitted") would be
    false on that request.
    """
    example = _empty_inventory_example(schema)
    if not example:
        return ""
    # C1b (Frozen-A3 §8): the decision-side relation mirror is derived in code
    # from the hypothesis ledger, so it must not appear in the model-facing
    # field contract. The hypothesis-side evidence-id lists stay required.
    nested = "\n".join(
        "  - `{label}` items use exactly these fields: {fields}".format(
            label=label,
            fields=", ".join(
                name
                for name in model.model_fields
                if name not in MODEL_FACING_DERIVED_RELATION_FIELDS
            ),
        )
        for label, model in _NESTED_DECISION_MODELS
    )
    return (
        "\n\n## JSON output contract for this call\n"
        "This call uses JSON object mode, so no schema is transmitted with the "
        "request. Emit exactly these field names (no additions, no renames, no "
        "extra wrapper object):\n"
        "```json\n"
        + json.dumps(example, indent=2)
        + "\n```\n"
        "Nested records must use exactly these fields:\n"
        + nested
    )


def _inventory_constraint_text() -> str:
    """System-prompt text carrying the pack-inventory rules into every call.

    No JSON schema can express "only evidence IDs present in the pack
    inventory are citable", so these rules are prompt text on every
    structured-output method — the transmitted tool schema constrains field
    names and enum values, not pack membership. The live 2026-09-15 c01 run is
    the evidence: with the rules dropped from the prompt the model cited a
    METADATA_ONLY coverage row as evidence and the run failed
    reference-integrity validation.
    """
    return (
        "\n\n## Evidence inventory rules for this call\n"
        "- Only evidence IDs listed in the evidence inventory may appear in "
        "`evidence_decisions[].evidence_id`, "
        "`candidate_hypotheses[].supporting_evidence_ids`, or "
        "`candidate_hypotheses[].contradicting_evidence_ids`.\n"
        "- If the evidence inventory is empty, `evidence_decisions` MUST be "
        "`[]` and `research_decision` MUST be `\"ABSTAIN\"`; never cite an "
        "ID that is not in the inventory.\n"
        "- METADATA_ONLY coverage rows are NOT inventory: they are never "
        "citable evidence and never a reason to emit a decision.\n"
    )


# Public-diagnostic excerpt carried by a typed schema failure. The parser
# message is the only evidence a live schema failure leaves behind (the raw
# completion is never persisted), so a bounded, sanitized excerpt travels in
# the exception. The pattern mirrors catalyst_app.public_text; the agents
# package must not depend on the app package, so the redaction is duplicated
# here and the app-level sanitizer stays the final gate.
_SCHEMA_FAILURE_EXCERPT_CHARACTERS = 200
_REFERENCE_CORRECTION_MESSAGE_CHARACTERS = 800
_REFERENCE_CORRECTION_TEMPLATE = (
    "Correct the reference-consistency violation below. Every evidence ID "
    "cited in a hypothesis's supporting or contradicting evidence IDs must "
    "also appear in evidence_decisions, with that hypothesis_ref in "
    "supports_hypothesis_refs or contradicts_hypothesis_refs. Evidence IDs "
    "must be copied exactly, character for character, from the evidence "
    "inventory shown above. Return the complete corrected JSON object.\n"
    "Violation: "
)
_REFERENCE_FAILURE_MARKERS = (
    "must have an EvidenceDecision",
    "is inconsistent with the EvidenceDecision",
    "references unknown hypothesis refs",
    "outside the pack inventory",
)

_UNSAFE_EXCERPT_PATTERN = re.compile(
    r"(?:api[_ -]?key|provider[_ -]?key|authorization|bearer\s+\S+|"
    r"raw[_ -]?(?:provider[_ -]?)?response|provider\s+response)\s*(?:=|:|\b)|"
    r"\bsk-[A-Za-z0-9_-]{16,}\b",
    re.IGNORECASE,
)


def _schema_failure_excerpt(detail: BaseException | str) -> str:
    """Bounded single-line sanitized parser/validation detail.

    Whitespace is collapsed, credential/raw-provider shaped text is redacted,
    and the result is truncated so the durable ``failure_message`` stays a
    bounded public diagnostic.
    """
    text = detail if isinstance(detail, str) else str(detail)
    collapsed = " ".join(text.split())
    redacted = _UNSAFE_EXCERPT_PATTERN.sub("[redacted]", collapsed)
    bounded = redacted[:_SCHEMA_FAILURE_EXCERPT_CHARACTERS].strip()
    return bounded or "no parser detail"


def _schema_failure(
    message: str, detail: BaseException | str | None = None
) -> ModelSchemaFailure:
    """Typed schema failure carrying a bounded sanitized excerpt when known."""
    if detail is None:
        return ModelSchemaFailure(message)
    return ModelSchemaFailure(f"{message}: {_schema_failure_excerpt(detail)}")


def _reference_violation_text(exc: ModelSchemaFailure) -> str:
    """The failure plus its underlying cause: the failure's own excerpt is cut at 200
    characters, which can drop the marker ("...must have an EvidenceD")."""
    cause = exc.__cause__
    return f"{exc} {cause}" if cause is not None else str(exc)


def _reference_violation_kind(exc: ModelSchemaFailure) -> str | None:
    """Classify the reference failures that get a corrective prompt on retry."""
    message = _reference_violation_text(exc)
    if "outside the pack inventory" in message:
        return "out_of_pack"
    if any(marker in message for marker in _REFERENCE_FAILURE_MARKERS[:3]):
        return "missing_decision"
    return None


def _reference_correction_message(exc: ModelSchemaFailure) -> str:
    """Build the one-shot reference correction with a bounded violation detail."""
    detail = _reference_violation_text(exc)
    # Lead with the specific violation sentence, preferring the untruncated cause.
    cause = str(exc.__cause__) if exc.__cause__ is not None else ""
    pattern = re.compile(r"(candidate (?:supporting|contradicting) evidence[^\n]*|[^\n]*is inconsistent with the EvidenceDecision[^\n]*|"
                      r"[^\n]*references unknown hypothesis refs[^\n]*|[^\n]*outside the pack inventory[^\n]*)")
    match = pattern.search(cause) or pattern.search(detail)
    return (
        _REFERENCE_CORRECTION_TEMPLATE + (match.group(1).strip() if match else detail)
    )[:_REFERENCE_CORRECTION_MESSAGE_CHARACTERS]


def _is_structured_output_parse_failure(
    exc: BaseException, *, schema: type[AnalystDecision] | None = None
) -> bool:
    """True for a structured-output parse/validation failure.

    ``with_structured_output(schema, method="json_mode")`` parses with
    ``PydanticOutputParser``, which raises ``OutputParserException`` when the
    model returns valid JSON with the wrong fields. ``method="function_calling"``
    parses the tool call with ``PydanticToolsParser``, which validates the
    arguments against the schema and lets pydantic's ``ValidationError`` escape
    unchanged. Both are structured-schema failures, not transport failures;
    the langchain class is matched by class/module name so the agents package
    keeps no hard import dependency on langchain parse internals. A
    ``ValidationError`` is only the decision schema's own failure — any other
    pydantic validation error propagates unchanged.
    """
    if isinstance(exc, ModelSchemaFailure):
        return True
    if isinstance(exc, ValidationError):
        if schema is None:
            return True
        return (
            str(getattr(exc, "title", "") or "") == schema.__name__
            or f"for {schema.__name__}" in str(exc)
        )
    cls = type(exc)
    if cls.__name__ != "OutputParserException":
        return False
    module = getattr(cls, "__module__", "") or ""
    return module == "langchain_core.exceptions" or module.startswith(
        "langchain_core."
    )


def _semantic_input_hash(
    prompt: str,
    rendered_messages: tuple[RenderMessage, ...],
    schema_version: str,
) -> str:
    payload = {
        "prompt": prompt,
        "rendered_messages": [
            {"role": message.role, "content": message.content}
            for message in rendered_messages
        ],
        "schema_version": schema_version,
    }
    return hashlib.sha256(
        canonical_context_pack_json(payload)
    ).hexdigest()


def _decision_hash(decision: AnalystDecision) -> str:
    return hashlib.sha256(
        canonical_context_pack_json(decision.model_dump(mode="json"))
    ).hexdigest()


# Provider SDK errors that mean "the request did not get a model answer":
# matched by class name so this package does not import any provider SDK.
_TRANSPORT_ERROR_NAMES = frozenset({
    "APIConnectionError", "InternalServerError", "RateLimitError",
    "ServiceUnavailableError", "ConnectError", "RemoteProtocolError",
    "ReadError", "WriteError",
})
_TIMEOUT_ERROR_NAMES = frozenset({
    "APITimeoutError", "Timeout", "TimeoutException", "ReadTimeout",
    "ConnectTimeout", "PoolTimeout",
})
TRANSPORT_RETRY_DELAY_S = 2.0


def _transport_error(exc: Exception, llm: Any = None) -> Exception | None:
    """A retryable ModelTransportFailure/ModelTimeoutFailure for provider
    transport errors (DeepSeek 'Connection error.' killed 3 of 180 runs),
    else None."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _TIMEOUT_ERROR_NAMES:
        return ModelTimeoutFailure(f"provider timeout: {type(exc).__name__}")
    if names & _TRANSPORT_ERROR_NAMES:
        from catalyst_agents.runtime.provider_capability import redact_provider_error

        return ModelTransportFailure(
            f"provider transport error: {type(exc).__name__}: "
            f"{redact_provider_error(exc, getattr(llm, '_api_key', None))}"
        )
    return None


def _invoke_llm(llm: Any, messages: list[RenderMessage] | list[dict]) -> Any:
    """Call the injected provider with standard role/content dict messages.

    The provider may be a LangChain client (``invoke(messages)``) or a plain
    callable. Accepts either RenderMessage objects or already-converted dicts
    so the identical dictionary payload is used on every attempt.
    """
    dict_messages = [
        dict(message)
        if isinstance(message, dict)
        else {"role": message.role, "content": message.content}
        for message in messages
    ]
    try:
        if callable(llm):
            return llm(dict_messages)
        invoke = getattr(llm, "invoke", None)
        if callable(invoke):
            return invoke(dict_messages)
    except Exception as exc:  # map provider transport errors to the retry seam
        mapped = _transport_error(exc, llm)
        if mapped is None:
            raise
        time.sleep(TRANSPORT_RETRY_DELAY_S)
        raise mapped from exc
    raise ModelTransportFailure(
        f"provider {type(llm).__name__!r} exposes no callable invoke surface"
    )


def _parse_decision(raw: Any, schema: type[AnalystDecision]) -> AnalystDecision:
    """Strictly parse provider output; no regex salvage or coercion."""
    if isinstance(raw, AnalystDecision):
        return raw
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _schema_failure("unparseable structured output JSON", exc) from exc
    else:
        content = getattr(raw, "content", None)
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError as exc:
                raise _schema_failure(
                    "unparseable structured output JSON", exc
                ) from exc
        else:
            raise ModelSchemaFailure("provider did not return structured output")
    if not isinstance(payload, dict):
        raise ModelSchemaFailure("structured output must be a JSON object")
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise _schema_failure(
            f"invalid AnalystDecision schema: {exc.error_count()} error(s)", exc
        ) from exc




def render_messages_to_dicts(messages: tuple[RenderMessage, ...]) -> list[dict]:
    """Convert RenderMessages to the standard role/content dictionary payload.

    Matches LangChain's standard message dict conversion (role + content) and
    is the exact payload passed to provider surfaces on every attempt.
    """
    return [
        {"role": message.role, "content": message.content}
        for message in messages
    ]


def _admit_structured_output_surface(
    llm: Any, schema: type[AnalystDecision], *, method: str | None = None
) -> Any | None:
    """Admit the provider's native structured-output surface, fail closed.

    ``with_structured_output(schema[, method])`` is a factory, not a model
    call. If the provider does not expose it, None is returned and the strict
    invoke+parse fallback is used (fake-provider testability). When the
    admitted capability metadata declares ``structured_output_method`` the
    method is forwarded so an endpoint that rejects ``json_schema`` receives
    the supported shape (DeepSeek -> ``function_calling``: the schema travels
    in ``tools[]`` with a forced ``tool_choice``); factories that only accept
    ``schema`` are unaffected because no method is declared. If the provider
    ADMITS the surface but the factory raises or returns an unusable surface,
    this fails closed with a typed ProviderCapabilityError — never a silent
    raw-invoke or method-less fallback.
    """
    with_structured = getattr(llm, "with_structured_output", None)
    if not callable(with_structured):
        return None
    if method is None:
        method = provider_capability_for(llm).structured_output_method
    try:
        surface = (
            with_structured(schema, method=method)
            if method
            else with_structured(schema)
        )
    except Exception as exc:
        raise ProviderCapabilityError(
            provider=type(llm).__name__,
            missing=["with_structured_output_surface"],
            needed=ANALYST_REQUIRED_CAPABILITIES,
        ) from exc
    if surface is None or not callable(getattr(surface, "invoke", None)):
        raise ProviderCapabilityError(
            provider=type(llm).__name__,
            missing=["with_structured_output_surface"],
            needed=ANALYST_REQUIRED_CAPABILITIES,
        )
    return surface


def _validate_hypothesis_policy(
    decision: "AnalystDecision", hypothesis_policy_version: str | None
) -> None:
    """A2 behavioral seam: enforce the hypothesis competition bound.

    ``bounded_competition_v1`` (the M6 production default) accepts the schema-
    bounded competition set; ``legacy_single_candidate_v1`` accepts exactly one
    candidate hypothesis. ``None`` applies no extra bound and preserves the M6
    production behavior byte-for-byte.
    """
    if hypothesis_policy_version is None or hypothesis_policy_version == "bounded_competition_v1":
        return
    if hypothesis_policy_version == "legacy_single_candidate_v1":
        if len(decision.candidate_hypotheses) != 1:
            raise ValueError(
                "legacy_single_candidate_v1 permits a single candidate "
                f"hypothesis, got {len(decision.candidate_hypotheses)}"
            )
        return
    raise ValueError(
        f"unknown hypothesis policy version {hypothesis_policy_version!r}"
    )


def evidence_analyst(
    state: dict,
    *,
    llm: Any,
    persistence: PackPersistence,
    prompt_template: str | None = None,
    prompt_path: Path | None = None,
    schema: type[AnalystDecision] = AnalystDecision,
    pack_inventory_ids: tuple[str, ...] | None = None,
    hypothesis_policy_version: str | None = None,
    provider_budget: Any | None = None,
    provider_identity: str | None = None,
    model_identity: str | None = None,
) -> dict:
    """Run one EvidenceAnalyst logical call and emit decision/ref artifacts.

    The provider dispatch gate requires the persisted pack body (Final TSD
    §7): the Analyst inventory is derived ONLY from
    ``pair.pack.included_evidence_ids``. A caller-supplied ``pack_inventory_ids``
    override is rejected unless it is byte-for-byte equal to the persisted
    inventory. Missing pack or an enlarged override fail closed before any
    provider call. An empty citable inventory (``included_evidence_ids`` empty,
    including a non-empty METADATA_ONLY ``evidence_inventory``) takes a
    deterministic evidence-free ABSTAIN path with zero provider dispatch;
    round 1 and the corrective round share that path. When the Analyst is
    dispatched, any evidence reference outside the persisted included inventory
    enters the bounded schema retry and exhausts as MODEL_SCHEMA_FAILURE.
    When the admitted provider exposes a native structured-output surface
    (``with_structured_output(schema)``) it is invoked with the original
    dictionary message payload on the first attempt. A recognized reference
    failure adds one corrective user message to the retry; other retries use
    the original payload. An admitted-but-broken surface fails closed with a
    typed capability error (no silent raw-invoke fallback).
    """
    run_id = state.get("run_id")
    if not run_id:
        raise ValueError("evidence_analyst requires run_id in state")

    # Provider dispatch gate: never call a provider before the persisted
    # pack/render pair is committed AND the persisted pack body exists.
    pair = require_committed_pair(persistence, run_id=run_id)
    if pair.pack is None:
        raise PackNotPersistedError(
            f"run {run_id!r} has no persisted pack artifact; the Analyst "
            "inventory cannot be derived and provider dispatch is prohibited"
        )
    persisted_inventory = tuple(pair.pack.included_evidence_ids)
    if pack_inventory_ids is not None and tuple(pack_inventory_ids) != persisted_inventory:
        raise PackNotPersistedError(
            "caller-supplied pack_inventory_ids does not byte-for-byte match "
            "the persisted pack inventory"
        )
    inventory = set(persisted_inventory)
    rendered_messages = pair.rendered_messages

    if not persisted_inventory:
        return _empty_citable_inventory_result(
            schema=schema,
            pair=pair,
            hypothesis_policy_version=hypothesis_policy_version,
        )

    prompt = _prompt_template(prompt_path, prompt_template)
    schema_version = getattr(schema, "schema_version", "1.0")
    if hypothesis_policy_version is not None:
        schema_version = f"{schema_version}+hypothesis:{hypothesis_policy_version}"
    # The declared structured-output method decides how the AnalystDecision
    # *shape* reaches the provider. ``json_mode`` sends
    # ``response_format: json_object``, which carries no schema, so the field
    # contract (documented by ``_json_mode_field_contract``) is appended to the
    # prompt and covered by the semantic input hash below. Under
    # ``function_calling`` the schema travels in ``tools[]`` with a forced
    # ``tool_choice``, so the field contract is not injected and the tool schema
    # is the shape mechanism.
    #
    # The pack-inventory rules are NOT a shape mechanism: no JSON schema can
    # constrain pack membership, so they are appended on every method.
    declared_method = provider_capability_for(llm).structured_output_method
    if declared_method == "json_mode":
        prompt = prompt + _json_mode_field_contract(schema)
    prompt = prompt + _inventory_constraint_text()
    semantic_input_hash = _semantic_input_hash(
        prompt, rendered_messages, schema_version
    )

    # Capability admission: structured Analyst output is mandatory.
    require_capabilities(llm, ANALYST_REQUIRED_CAPABILITIES)

    messages: list[RenderMessage] = []
    if prompt:
        messages.append(RenderMessage(role="system", content=prompt))
    messages.extend(rendered_messages)

    # Standard role/content dictionaries, built once and used on the first
    # attempt (both the native surface and the fallback path).
    dict_messages = render_messages_to_dicts(tuple(messages))

    # Admitted native structured-output surface (built once; not a model call).
    # An admitted-but-broken surface fails closed at admission.
    structured_surface = _admit_structured_output_surface(
        llm, schema, method=declared_method
    )

    def _invoke_surface(attempt_messages: list[dict]) -> Any:
        """Call the admitted surface, typing parse failures for the retry.

        A langchain ``OutputParserException`` is a structured-schema failure:
        without this mapping it is not a ``ModelRoleCallError``, so the bounded
        retry never fires and the executor terminalizes a generic SYSTEM_ERROR
        instead of MODEL_SCHEMA_FAILURE. Unmapped provider errors (for example
        an endpoint 400) still propagate unchanged and are never retried.
        """
        try:
            if structured_surface is not None:
                return structured_surface.invoke(attempt_messages)
            return _invoke_llm(llm, attempt_messages)
        except Exception as exc:
            if _is_structured_output_parse_failure(exc, schema=schema):
                raise _schema_failure(
                    "unparseable structured output JSON", exc
                ) from exc
            raise

    last_reference_failure: ModelSchemaFailure | None = None
    corrective_retry = False
    corrective_violation_kind: str | None = None

    def attempt() -> AnalystDecision:
        nonlocal last_reference_failure, corrective_retry
        nonlocal corrective_violation_kind

        attempt_messages = dict_messages
        if last_reference_failure is not None:
            corrective_retry = True
            corrective_violation_kind = _reference_violation_kind(
                last_reference_failure
            )
            attempt_messages = [
                *dict_messages,
                {
                    "role": "user",
                    "content": _reference_correction_message(
                        last_reference_failure
                    ),
                },
            ]
            # The correction applies only to the immediately following attempt.
            last_reference_failure = None

        try:
            raw = _invoke_surface(attempt_messages)
            decision = _parse_decision(raw, schema)
            _validate_hypothesis_policy(decision, hypothesis_policy_version)
            # Reference-integrity against the authoritative persisted inventory
            # stays strict and inside the bounded retry boundary.
            referenced = {
                item.evidence_id for item in decision.evidence_decisions
            }
            for hypothesis in decision.candidate_hypotheses:
                referenced.update(hypothesis.supporting_evidence_ids)
                referenced.update(hypothesis.contradicting_evidence_ids)
            unknown = sorted(referenced - inventory)
            if unknown:
                raise ModelSchemaFailure(
                    "AnalystDecision references evidence outside the pack "
                    f"inventory: {unknown}"
                )
            return decision
        except ModelSchemaFailure as exc:
            if _reference_violation_kind(exc) is not None:
                last_reference_failure = exc
            raise

    try:
        bounded = invoke_with_bounded_retry(
            attempt,
            role=ANALYST_ROLE,
            semantic_input_hash=semantic_input_hash,
            budget=provider_budget,
            provider=provider_identity,
            model_id=model_identity,
            usage_extractor=extract_provider_usage,
        )
    except TechnicalRetryExhausted as exc:
        if isinstance(exc.last_error, ModelSchemaFailure):
            failure = ModelSchemaFailure(
                f"MODEL_SCHEMA_FAILURE: {exc}"
            )
            failure.corrective_retry = corrective_retry
            failure.violation_kind = corrective_violation_kind
            raise failure from exc
        raise

    decision = bounded.result
    return {
        "analyst_decision": decision,
        "decision_hash": _decision_hash(decision),
        "context_pack_sha256": pair.refs.pack_sha256,
        "rendered_messages_sha256": pair.refs.rendered_messages_sha256,
        "semantic_input_hash": semantic_input_hash,
        "analyst_logical_calls": bounded.counts.logical_calls,
        "analyst_provider_attempts": bounded.counts.provider_attempts,
        "analyst_attempts": bounded.attempts,
        "analyst_attempt_usages": bounded.attempt_usages,
        "analyst_retry_diagnostics": {
            "corrective_retry": corrective_retry,
            "violation_kind": corrective_violation_kind,
        },
    }


__all__ = [
    "ANALYST_ROLE",
    "_validate_hypothesis_policy",
    "ANALYST_REQUIRED_CAPABILITIES",
    "evidence_analyst",
]
