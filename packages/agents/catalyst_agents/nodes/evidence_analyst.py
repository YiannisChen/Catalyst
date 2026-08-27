"""EvidenceAnalyst node (M5-1) — strict structured-output boundary.

Frozen §6.3; Phase 4 TSD §§6–12; Final TSD §8. The node loads the persisted
EvidenceAnalystContextPack/render pair (M4), renders the exact persisted model
messages, runs capability preflight through the M5-0 protocol, calls the
injected LLM once (with at most one identical-semantic-input technical retry
for transport/timeout/invalid structured schema), and parses a strict
``AnalystDecision`` (``extra="forbid"``). A second schema failure is
MODEL_SCHEMA_FAILURE — a system failure, never ABSTAIN. Valid ABSTAIN/PARTIAL
is never retried.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from catalyst_agents.attribution.analyst import AnalystDecision
from catalyst_agents.attribution.context_pack_builder import (
    RenderMessage,
    canonical_context_pack_json,
)
from catalyst_agents.runtime.pack_persistence import (
    PackPersistence,
    require_committed_pair,
)
from catalyst_agents.runtime.provider_capability import (
    CAPABILITY_REVISION,
    ProviderCapability,
    ModelRoleCallError,
    ModelSchemaFailure,
    ModelTimeoutFailure,
    ModelTransportFailure,
    TechnicalRetryExhausted,
    invoke_with_bounded_retry,
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


def _invoke_llm(llm: Any, messages: list[RenderMessage]) -> Any:
    """Call the injected provider with LangChain-style message objects.

    The provider may be a LangChain client (``invoke(messages)``) or a plain
    callable. Messages are converted to role/content dicts so fakes and real
    clients share the same contract.
    """
    dict_messages = [
        {"role": message.role, "content": message.content}
        for message in messages
    ]
    if callable(llm):
        return llm(dict_messages)
    invoke = getattr(llm, "invoke", None)
    if callable(invoke):
        return invoke(dict_messages)
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
            raise ModelSchemaFailure("unparseable structured output JSON") from exc
    else:
        content = getattr(raw, "content", None)
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ModelSchemaFailure("unparseable structured output JSON") from exc
        else:
            raise ModelSchemaFailure("provider did not return structured output")
    if not isinstance(payload, dict):
        raise ModelSchemaFailure("structured output must be a JSON object")
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise ModelSchemaFailure(
            f"invalid AnalystDecision schema: {exc.error_count()} error(s)"
        ) from exc


def evidence_analyst(
    state: dict,
    *,
    llm: Any,
    persistence: PackPersistence,
    prompt_template: str | None = None,
    prompt_path: Path | None = None,
    schema: type[AnalystDecision] = AnalystDecision,
    pack_inventory_ids: tuple[str, ...] | None = None,
) -> dict:
    """Run one EvidenceAnalyst logical call and emit decision/ref artifacts."""
    run_id = state.get("run_id")
    if not run_id:
        raise ValueError("evidence_analyst requires run_id in state")

    # Provider dispatch gate: never call a provider before the persisted
    # pack/render pair is committed (Final TSD §7).
    pair = require_committed_pair(persistence, run_id=run_id)
    rendered_messages = pair.rendered_messages

    prompt = _prompt_template(prompt_path, prompt_template)
    schema_version = getattr(schema, "schema_version", "1.0")
    semantic_input_hash = _semantic_input_hash(
        prompt, rendered_messages, schema_version
    )

    # Capability admission: structured Analyst output is mandatory.
    require_capabilities(llm, ANALYST_REQUIRED_CAPABILITIES)

    messages: list[RenderMessage] = []
    if prompt:
        messages.append(RenderMessage(role="system", content=prompt))
    messages.extend(rendered_messages)

    def attempt() -> AnalystDecision:
        raw = _invoke_llm(llm, messages)
        decision = _parse_decision(raw, schema)
        # Reference-integrity against the pack inventory is structured-output
        # validation INSIDE the bounded technical retry boundary (C6): an
        # unknown/pack-external evidence ref is a ModelSchemaFailure that gets
        # at most one identical-semantic-input retry, then MODEL_SCHEMA_FAILURE.
        if pack_inventory_ids is not None:
            inventory = set(pack_inventory_ids)
            referenced = set(decision.evidence_decisions and [
                item.evidence_id for item in decision.evidence_decisions
            ])
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

    try:
        bounded = invoke_with_bounded_retry(
            attempt,
            role=ANALYST_ROLE,
            semantic_input_hash=semantic_input_hash,
        )
    except TechnicalRetryExhausted as exc:
        if isinstance(exc.last_error, ModelSchemaFailure):
            raise ModelSchemaFailure(
                f"MODEL_SCHEMA_FAILURE: {exc}"
            ) from exc
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
    }


__all__ = [
    "ANALYST_ROLE",
    "ANALYST_REQUIRED_CAPABILITIES",
    "evidence_analyst",
]
