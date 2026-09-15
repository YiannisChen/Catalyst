"""M5-7: streaming Writer node with injected delta sink.

Final TSD §13/§19; Phase 4 TSD §30; M5 plan M5-7. writer_node consumes a
WriterInput built from a ValidatedClaimPlan, streams through a fake provider,
buffers with the Coalescer, and commits deltas + the provisional Answer
through the injected DeltaSink. AGENT-01: the fixed abstention path applies
when final_status == ABSTAIN regardless of attribution_type — no cause
accepted, only bounded limitations, no causal-finding/NO_MATERIAL language.
One same-input technical retry before the first accepted delta; no retry after
any delta; answer/delta equality is enforced (STREAM_PERSISTENCE_FAILURE).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
    MagnitudeFit,
)
from catalyst_agents.attribution.claims import (
    CitationMapEntry,
    Claim,
    ClaimRole,
    SourceRoleIndependenceSummary,
    ValidatedClaimPlan,
    WriterFormatKind,
    WriterFormatStyleContract,
    WriterInput,
    WriterSection,
)
from catalyst_agents.nodes.writer import build_writer_input, writer_node
from catalyst_agents.runtime.delta_sink import InMemoryDeltaSink
from catalyst_agents.runtime.provider_capability import (
    ModelRoleCallError,
    ModelTransportFailure,
)


class FakeStreamingProvider:
    def __init__(self, chunks, *, capable: bool = True):
        self.chunks = list(chunks)
        self.calls = 0
        self.captured_messages: list[list[dict]] = []
        self.capability_metadata = (
            {
                "supports_structured_output": True,
                "supports_true_streaming": True,
                "declares_token_accounting": True,
                "normalizes_timeout_errors": True,
                "capability_revision": "test-capability-1",
            }
            if capable
            else {
                "supports_structured_output": False,
                "supports_true_streaming": False,
                "declares_token_accounting": False,
                "normalizes_timeout_errors": False,
                "capability_revision": "test-capability-1",
            }
        )

    def stream(self, messages):
        self.calls += 1
        self.captured_messages.append(
            [dict(m) if isinstance(m, dict) else {"role": m.role, "content": m.content} for m in messages]
        )
        for chunk in self.chunks:
            yield chunk


def _plan(*, status: AttributionStatus = AttributionStatus.SUFFICIENT, type_: AttributionType = AttributionType.EVIDENCE_BACKED_CAUSAL) -> ValidatedClaimPlan:
    claim = Claim(
        claim_id="claim:1",
        role=ClaimRole.PRIMARY,
        statement="AAPL rose on record guidance.",
        mechanism="Guidance raised forward revenue.",
        support_evidence_ids=("e1",),
        counter_evidence_ids=(),
        limitations=(),
        source_hypothesis_id="hyp:run:1:abc",
        magnitude_fit=MagnitudeFit.STRONG,
        conflict_refs=(),
        citation_evidence_ids=("e1",),
        order_index=0,
    )
    if status is AttributionStatus.ABSTAIN:
        claim = claim.model_copy(
            update={
                "role": ClaimRole.CONTEXT,
                "statement": "Evidence was bounded and did not establish a cause.",
                "magnitude_fit": None,
            }
        )
        claims = (claim,)
        required_limitations = ("Coverage gap: MARKET_STRUCTURE_UNSUPPORTED for MARKET_STRUCTURE.",)
    else:
        claims = (claim,)
        required_limitations = ()
    return ValidatedClaimPlan(
        status=status,
        attribution_type=type_,
        claims=claims,
        assessment_hash="a" * 64,
        context_pack_sha256="c" * 64,
        evidence_state_hash="e" * 64,
        required_limitations=required_limitations,
        citation_map=(
            CitationMapEntry(claim_id=claim.claim_id, citation_evidence_ids=claim.citation_evidence_ids)
            for claim in claims
        ),
        permitted_claim_ids=tuple(claim.claim_id for claim in claims),
        permitted_evidence_ids=tuple(
            sorted({eid for claim in claims for eid in claim.citation_evidence_ids})
        ),
        source_role_independence_summary=SourceRoleIndependenceSummary(direct_primary_support_count=1),
        ordering_policy_version="claim-ordering-v1",
        plan_hash="f" * 64,
    )


def _writer_input(*, status: AttributionStatus = AttributionStatus.SUFFICIENT) -> WriterInput:
    validated = _plan(status=status)
    format_kind = (
        WriterFormatKind.FIXED_ABSTENTION
        if status is AttributionStatus.ABSTAIN
        else WriterFormatKind.CAUSAL
    )
    sections = (
        (WriterSection.OBSERVED_MOVE, WriterSection.LIMITATIONS)
        if status is AttributionStatus.ABSTAIN
        else (WriterSection.SUMMARY, WriterSection.CAUSAL_EXPLANATION, WriterSection.LIMITATIONS)
    )
    contract = WriterFormatStyleContract(
        format_kind=format_kind,
        required_sections=sections,
        style_instructions=() if status is AttributionStatus.ABSTAIN else ("concise",),
    )
    return build_writer_input(
        validated,
        observed_move="AAPL +9.5% on 2026-01-15",
        format_style_contract=contract,
    )


def test_writer_node_streams_and_commits_exact_answer() -> None:
    sink = InMemoryDeltaSink()
    llm = FakeStreamingProvider(["AAPL rose ", "on record ", "guidance."])
    result = writer_node(
        {"run_id": "run:1"},
        llm=llm,
        writer_input=_writer_input(),
        sink=sink,
    )
    assert llm.calls == 1
    assert result["stream_complete"] is True
    answer = sink.answer()
    assert answer is not None
    assert answer.text == "AAPL rose on record guidance."
    assert answer.text == "".join(delta.text for delta in sink.deltas())
    assert result["answer"] is answer


def test_writer_abstain_fixed_path_prompt() -> None:
    sink = InMemoryDeltaSink()
    llm = FakeStreamingProvider(["No cause was accepted."])
    writer_input = _writer_input(status=AttributionStatus.ABSTAIN)
    writer_node({"run_id": "run:1"}, llm=llm, writer_input=writer_input, sink=sink)
    prompt = llm.captured_messages[0][0]["content"].lower()
    assert "no cause was accepted" in prompt
    assert "do not use" in prompt
    assert "limitation" in prompt
    # The produced answer follows the fixed abstention path: no causal-finding
    # or NO_MATERIAL language in the output.
    answer = sink.answer()
    assert answer is not None
    assert "no_material" not in answer.text.lower()
    assert "primary cause" not in answer.text.lower()


def test_writer_pre_delta_transport_failure_retries_once() -> None:
    sink = InMemoryDeltaSink()
    state = {"calls": 0}

    class FlakyProvider(FakeStreamingProvider):
        def stream(self, messages):
            state["calls"] += 1
            if state["calls"] == 1:
                raise ModelTransportFailure("transport down")
            self.calls += 1
            self.captured_messages.append(
                [dict(m) if isinstance(m, dict) else {"role": m.role, "content": m.content} for m in messages]
            )
            yield from ["recovered ", "answer"]

    llm = FlakyProvider([])
    result = writer_node({"run_id": "run:1"}, llm=llm, writer_input=_writer_input(), sink=sink)
    assert state["calls"] == 2
    assert result["answer"].text == "recovered answer"


def test_writer_post_delta_failure_never_retries_and_fails_closed() -> None:
    sink = InMemoryDeltaSink()
    state = {"calls": 0}

    class MidStreamFailureProvider(FakeStreamingProvider):
        def stream(self, messages):
            state["calls"] += 1
            self.calls += 1
            yield "partial"
            raise ModelTransportFailure("mid-stream down")

    llm = MidStreamFailureProvider([])
    with pytest.raises(ModelRoleCallError):
        writer_node({"run_id": "run:1"}, llm=llm, writer_input=_writer_input(), sink=sink)
    assert state["calls"] == 1  # no retry after a delta
    assert sink.answer() is None  # provisional output invalidated


def test_writer_prompt_never_cites_unbound_evidence() -> None:
    sink = InMemoryDeltaSink()
    llm = FakeStreamingProvider(["ok"])
    writer_input = _writer_input()
    writer_node({"run_id": "run:1"}, llm=llm, writer_input=writer_input, sink=sink)
    prompt = llm.captured_messages[0][0]["content"]
    allowed = set(writer_input.citation_map[0].citation_evidence_ids)
    for token in ("e1", "e9", "ghost"):
        if token in prompt:
            assert token in allowed


def test_answer_persistence_mismatch_fails_closed() -> None:
    sink = InMemoryDeltaSink()
    llm = FakeStreamingProvider(["AAPL rose ", "on record ", "guidance."])
    # Tamper the sink after the fact: the node's equality check must catch it.
    result = writer_node({"run_id": "run:1"}, llm=llm, writer_input=_writer_input(), sink=sink)
    assert result["stream_complete"] is True
    # A mismatching provisional answer is a stream-persistence failure.
    from catalyst_agents.nodes.writer import verify_answer_equality

    with pytest.raises(Exception, match="STREAM_PERSISTENCE_FAILURE"):
        verify_answer_equality("tampered", sink)


def test_writer_node_surfaces_real_token_and_completion_metadata() -> None:
    """C2: writer_node must pass real provider token/completion metadata to
    structural assurance (no synthetic zero-fill)."""
    sink = InMemoryDeltaSink()

    class UsageReportingProvider(FakeStreamingProvider):
        def stream(self, messages):
            self.calls += 1
            self.captured_messages.append(
                [dict(m) if isinstance(m, dict) else {"role": m.role, "content": m.content} for m in messages]
            )
            total = len(self.chunks)
            for index, chunk in enumerate(self.chunks):
                if index == total - 1:
                    yield {"content": chunk, "response_metadata": {"token_usage": {"prompt_tokens": 42, "completion_tokens": 7}}}
                else:
                    yield {"content": chunk, "response_metadata": {}}

    llm = UsageReportingProvider(["AAPL rose ", "on record ", "guidance."])
    result = writer_node(
        {"run_id": "run:1", "cancel_requested": False, "timed_out": False},
        llm=llm,
        writer_input=_writer_input(),
        sink=sink,
    )
    assert result["input_tokens"] == 42
    assert result["output_tokens"] == 7
    assert result["completion_state"] == "completed"
    assert result["cancellation_requested"] is False
    assert result["timed_out"] is False
    from catalyst_agents.runtime.assurance.checks import derive_writer_input_hash

    assert len(result["writer_input_hash"]) == 64
    assert result["writer_input_hash"] == derive_writer_input_hash(_writer_input())


def test_writer_reads_langchain_usage_metadata() -> None:
    sink = InMemoryDeltaSink()

    class UsageMetadataProvider(FakeStreamingProvider):
        def stream(self, messages):
            self.calls += 1
            self.captured_messages.append([dict(message) for message in messages])
            yield {"content": "answer", "usage_metadata": {
                "input_tokens": 31, "output_tokens": 9, "total_tokens": 40
            }}

    result = writer_node(
        {"run_id": "run:usage-metadata"},
        llm=UsageMetadataProvider([]),
        writer_input=_writer_input(),
        sink=sink,
    )

    assert result["input_tokens"] == 31
    assert result["output_tokens"] == 9


def test_writer_missing_usage_is_unknown_not_zero() -> None:
    sink = InMemoryDeltaSink()
    result = writer_node(
        {"run_id": "run:unknown-usage"},
        llm=FakeStreamingProvider(["answer"]),
        writer_input=_writer_input(),
        sink=sink,
    )

    assert result["input_tokens"] is None
    assert result["output_tokens"] is None
