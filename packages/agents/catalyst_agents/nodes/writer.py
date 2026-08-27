"""Streaming Writer node (M5-7).

Frozen §6.5; Phase 4 TSD §§28–30; Final TSD §13/§19; M5 plan M5-7. The Writer
is an expression layer over the ValidatedClaimPlan: it consumes the narrow
WriterInput, streams through a true provider stream interface, buffers via the
Coalescer, and commits deltas + the provisional Answer through the injected
DeltaSink. AGENT-01: when final_status == ABSTAIN the fixed abstention path
applies regardless of attribution_type — no cause accepted, only bounded
limitations, no causal-finding or NO_MATERIAL language. One same-input
technical retry is allowed before the first accepted delta; after any delta,
failure has no retry and invalidates the provisional output. The provisional
Answer must equal the exact ordered concatenation of persisted deltas.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterator

from catalyst_agents.attribution.analyst import AttributionStatus
from catalyst_agents.attribution.claims import (
    WriterFormatKind,
    WriterInput,
    ValidatedClaimPlan,
)
from catalyst_agents.runtime.coalescer import Coalescer
from catalyst_agents.runtime.delta_sink import DeltaSink
from catalyst_agents.runtime.provider_capability import (
    CAPABILITY_REVISION,
    ProviderCapability,
    ModelRoleCallError,
    ModelTransportFailure,
    invoke_with_bounded_retry,
    require_capabilities,
)

WRITER_ROLE = "streaming_writer"

WRITER_REQUIRED_CAPABILITIES = ProviderCapability(
    supports_structured_output=False,
    supports_true_streaming=True,
    declares_token_accounting=True,
    normalizes_timeout_errors=True,
    capability_revision=CAPABILITY_REVISION,
)


class StreamPersistenceFailure(Exception):
    """Provisional Answer does not equal the persisted delta concatenation."""


def build_writer_input(
    validated_plan: ValidatedClaimPlan,
    observed_move: str,
    format_style_contract: Any,
    snippets: tuple[Any, ...] = (),
) -> WriterInput:
    """Build the narrow WriterInput over a ValidatedClaimPlan (Phase 4 §28)."""
    return WriterInput(
        final_status=validated_plan.status,
        attribution_type=validated_plan.attribution_type,
        observed_move=observed_move,
        validated_claim_plan=validated_plan,
        narrowly_bound_supporting_snippets=snippets,
        citation_map=validated_plan.citation_map,
        required_limitations=validated_plan.required_limitations,
        format_style_contract=format_style_contract,
    )


def _writer_messages(writer_input: WriterInput) -> list[dict]:
    """Deterministic prompt over the narrow WriterInput surface.

    The fixed abstention path (AGENT-01) states no cause was accepted and
    presents only bounded evidence/coverage/conflict limitations.
    """
    system_lines: list[str] = []
    if writer_input.final_status is AttributionStatus.ABSTAIN:
        system_lines.append(
            "You are writing the fixed abstention report. No cause was accepted: "
            "state that no cause was accepted and present only the bounded "
            "evidence/coverage/conflict limitations. Do not search for a cause, "
            "do not speculate about a mechanism, and do not use NO_MATERIAL "
            "language."
        )
    else:
        system_lines.append(
            "You are writing the attribution report. Paraphrase, organize, and "
            "connect only the validated claims below. Do not invent facts, "
            "mechanisms, or causes; do not raise certainty; do not omit required "
            "limitations; and never cite evidence outside the citation map."
        )
    system_lines.append(f"Observed move: {writer_input.observed_move}")
    for claim in writer_input.validated_claim_plan.claims:
        line = f"- [{claim.role.value}] (claim_id: {claim.claim_id}) {claim.statement}"
        if claim.mechanism:
            line += f" (mechanism: {claim.mechanism})"
        if claim.citation_evidence_ids:
            line += f" [citations: {','.join(claim.citation_evidence_ids)}]"
        system_lines.append(line)
    if writer_input.required_limitations:
        system_lines.append("Required limitations:")
        for limitation in writer_input.required_limitations:
            system_lines.append(f"- {limitation}")
    sections = ",".join(section.value for section in writer_input.format_style_contract.required_sections)
    system_lines.append(f"Required sections: {sections}")
    return [{"role": "system", "content": "\n".join(system_lines)}]


def _stream_texts(llm: Any, messages: list[dict]) -> Iterator[str]:
    """Iterate accepted provider stream text (str or content-bearing chunks)."""
    stream = getattr(llm, "stream", None)
    if not callable(stream):
        raise ModelTransportFailure(
            f"provider {type(llm).__name__!r} exposes no true streaming interface"
        )
    for chunk in stream(messages):
        if isinstance(chunk, str):
            text = chunk
        else:
            text = getattr(chunk, "content", None)
            if text is None and isinstance(chunk, dict):
                text = chunk.get("content")
        if isinstance(text, str) and text:
            yield text


def verify_answer_equality(answer_text: str, sink: DeltaSink) -> None:
    """STREAM_PERSISTENCE_FAILURE when the provisional Answer diverges."""
    persisted = "".join(delta.text for delta in sink.deltas())
    if answer_text != persisted:
        raise StreamPersistenceFailure(
            "STREAM_PERSISTENCE_FAILURE: provisional Answer does not equal the "
            "exact ordered concatenation of accepted persisted deltas"
        )


def writer_node(
    state: dict,
    *,
    llm: Any,
    writer_input: WriterInput,
    sink: DeltaSink,
    flush_interval_ms: int = 50,
    flush_chars: int = 2048,
) -> dict:
    """Run one streaming Writer logical call and commit deltas + Answer."""
    run_id = state.get("run_id")
    if not run_id:
        raise ValueError("writer_node requires run_id in state")

    require_capabilities(llm, WRITER_REQUIRED_CAPABILITIES)
    messages = _writer_messages(writer_input)
    semantic_input_hash = hashlib.sha256(
        repr([writer_input.model_dump(mode="json"), messages]).encode("utf-8")
    ).hexdigest()

    def attempt():
        sink.reset()
        coalescer = Coalescer(
            flush_interval_ms=flush_interval_ms,
            flush_chars=flush_chars,
            sink=sink,
            stream_id=f"stream:{run_id}",
        )
        accepted_any = False
        try:
            for text in _stream_texts(llm, messages):
                if text:
                    accepted_any = True
                    coalescer.accept(text)
        except Exception as exc:
            if accepted_any:
                # After any accepted delta: no retry; provisional output is
                # invalidated and the run fails/cancels.
                sink.reset()
                raise ModelRoleCallError(
                    f"WRITER_FAILURE after accepted delta: {exc!r}"
                ) from exc
            raise ModelTransportFailure(f"WRITER_FAILURE before first delta: {exc!r}") from exc
        coalescer.complete()
        answer = sink.answer()
        if answer is None:
            raise ModelRoleCallError("WRITER_FAILURE: sink committed no Answer artifact")
        verify_answer_equality(answer.text, sink)
        return answer

    try:
        bounded = invoke_with_bounded_retry(
            attempt,
            role=WRITER_ROLE,
            semantic_input_hash=semantic_input_hash,
        )
    except Exception:
        sink.fail("WRITER_FAILURE")
        raise
    answer = bounded.result
    return {
        "writer_deltas": tuple(sink.deltas()),
        "answer": answer,
        "stream_complete": True,
        "writer_logical_calls": bounded.counts.logical_calls,
        "writer_provider_attempts": bounded.counts.provider_attempts,
        "run_id": run_id,
    }


__all__ = [
    "WRITER_REQUIRED_CAPABILITIES",
    "WRITER_ROLE",
    "StreamPersistenceFailure",
    "build_writer_input",
    "verify_answer_equality",
    "writer_node",
]
