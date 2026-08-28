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
from catalyst_agents.runtime.assurance.checks import derive_writer_input_hash
from catalyst_agents.runtime.coalescer import Coalescer
from catalyst_agents.runtime.control import control_expired
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

# Code-owned static Writer system instruction (non-abstention path). The app
# hashes this exact string into the RunManifest writer_prompt_hash so prompt
# identity can never drift from the agents-owned writer contract.
WRITER_STANDARD_INSTRUCTION = (
    "You are writing the attribution report. Paraphrase, organize, and "
    "connect only the validated claims below. Do not invent facts, "
    "mechanisms, or causes; do not raise certainty; do not omit required "
    "limitations; and never cite evidence outside the citation map."
)

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
        system_lines.append(WRITER_STANDARD_INSTRUCTION)
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


def _stream_chunks(llm: Any, messages: list[dict]) -> Iterator[tuple[str | None, dict[str, int]]]:
    """Iterate provider stream chunks with text and integer usage metadata.

    Real provider chunks expose usage through ``response_metadata``
    (e.g. ``token_usage``); fakes yield plain strings with empty usage. The
    writer surfaces the ACTUAL token counts to structural assurance.
    """
    stream = getattr(llm, "stream", None)
    if not callable(stream):
        raise ModelTransportFailure(
            f"provider {type(llm).__name__!r} exposes no true streaming interface"
        )
    for chunk in stream(messages):
        if isinstance(chunk, str):
            text = chunk
            metadata: dict[str, Any] = {}
        elif isinstance(chunk, dict):
            text = chunk.get("content")
            metadata = chunk.get("response_metadata") or {}
        else:
            text = getattr(chunk, "content", None)
            metadata = getattr(chunk, "response_metadata", None) or {}
        usage: dict[str, int] = {}
        if isinstance(metadata, dict):
            token_usage = metadata.get("token_usage") or metadata.get("usage") or {}
            if isinstance(token_usage, dict):
                usage = {
                    key: int(value)
                    for key, value in token_usage.items()
                    if isinstance(value, (int, float))
                }
        yield (text if isinstance(text, str) else None), usage


def _stream_texts(llm: Any, messages: list[dict]) -> Iterator[str]:
    """Iterate accepted provider stream text (str or content-bearing chunks)."""
    for text, _usage in _stream_chunks(llm, messages):
        if text:
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
    control: Any | None = None,
) -> dict:
    """Run one streaming Writer logical call and commit deltas + Answer.

    ``control`` is the agents-owned cooperative boundary: between provider
    stream chunks the Writer checks it and stops accepting deltas once
    cancellation or the absolute deadline wins (no provisional Answer is
    committed in that case).
    """
    run_id = state.get("run_id")
    if not run_id:
        raise ValueError("writer_node requires run_id in state")

    require_capabilities(llm, WRITER_REQUIRED_CAPABILITIES)
    messages = _writer_messages(writer_input)
    semantic_input_hash = hashlib.sha256(
        repr([writer_input.model_dump(mode="json"), messages]).encode("utf-8")
    ).hexdigest()

    input_tokens_total = 0
    output_tokens_total = 0
    cancelled = False
    timed_out = False

    def attempt():
        nonlocal input_tokens_total, output_tokens_total, cancelled, timed_out
        sink.reset()
        coalescer = Coalescer(
            flush_interval_ms=flush_interval_ms,
            flush_chars=flush_chars,
            sink=sink,
            stream_id=f"stream:{run_id}",
        )
        accepted_any = False
        attempt_input = 0
        attempt_output = 0
        try:
            for text, usage in _stream_chunks(llm, messages):
                if control is not None and control_expired(control):
                    # Cancellation/deadline won between chunks: stop accepting
                    # deltas and never commit a provisional Answer.
                    cancelled = bool(control.should_cancel())
                    timed_out = not cancelled
                    break
                attempt_input += int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
                attempt_output += int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
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
        if cancelled or timed_out:
            # No provisional Answer is committed; partial accepted deltas are
            # invalidated by the terminal cancellation path.
            sink.fail("WRITER_CANCELLED" if cancelled else "WRITER_TIMEOUT")
            return None
        coalescer.complete()
        answer = sink.answer()
        if answer is None:
            raise ModelRoleCallError("WRITER_FAILURE: sink committed no Answer artifact")
        verify_answer_equality(answer.text, sink)
        input_tokens_total = attempt_input
        output_tokens_total = attempt_output
        return answer

    if cancelled or timed_out:
        # Cancellation/deadline won before the first provider attempt: report
        # the control outcome without dispatching the provider.
        return {
            "writer_deltas": (),
            "answer": None,
            "stream_complete": False,
            "writer_input_hash": derive_writer_input_hash(writer_input),
            "input_tokens": 0,
            "output_tokens": 0,
            "completion_state": "cancelled" if cancelled else "timed_out",
            "cancellation_requested": cancelled,
            "timed_out": timed_out,
            "writer_logical_calls": 0,
            "writer_provider_attempts": 0,
            "run_id": run_id,
        }

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
    if cancelled or timed_out:
        # Cancellation/deadline won mid-stream: no provisional Answer is
        # committed and the graph raises the typed control winner before
        # consuming the output.
        return {
            "writer_deltas": tuple(sink.deltas()),
            "answer": None,
            "stream_complete": False,
            "writer_input_hash": derive_writer_input_hash(writer_input),
            "input_tokens": input_tokens_total,
            "output_tokens": output_tokens_total,
            "completion_state": "cancelled" if cancelled else "timed_out",
            "cancellation_requested": cancelled,
            "timed_out": timed_out,
            "writer_logical_calls": bounded.counts.logical_calls,
            "writer_provider_attempts": bounded.counts.provider_attempts,
            "run_id": run_id,
        }
    return {
        "writer_deltas": tuple(sink.deltas()),
        "answer": answer,
        "stream_complete": True,
        "writer_input_hash": derive_writer_input_hash(writer_input),
        "input_tokens": input_tokens_total,
        "output_tokens": output_tokens_total,
        "completion_state": "completed",
        "cancellation_requested": bool(state.get("cancel_requested", False)),
        "timed_out": bool(state.get("timed_out", False)),
        "writer_logical_calls": bounded.counts.logical_calls,
        "writer_provider_attempts": bounded.counts.provider_attempts,
        "run_id": run_id,
    }


__all__ = [
    "WRITER_REQUIRED_CAPABILITIES",
    "WRITER_ROLE",
    "WRITER_STANDARD_INSTRUCTION",
    "StreamPersistenceFailure",
    "build_writer_input",
    "verify_answer_equality",
    "writer_node",
]
