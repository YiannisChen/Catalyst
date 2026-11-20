"""WP-C C3c / Frozen-A5: code-emitted claim markers on the Writer.

The model writes only the summary on the prose channel (ASCII brackets and
parentheses deleted); code emits the SUMMARY heading with the first accepted
provider text and, after a normally completed stream and a final control
check, the CAUSAL_EXPLANATION claim lines (markers + citations) and the
verbatim required limitations, all rendered from the ValidatedClaimPlan and
persisted as ``origin = writer_assembly`` deltas. Assurance predicates are
unchanged; for this path they become assembly invariants (C3c §3.3).
"""
from __future__ import annotations

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
    WriterSection,
)
from catalyst_agents.graphs.graph import extract_answer_markers
from catalyst_agents.nodes.writer import (
    SUMMARY_PREFIX,
    WRITER_ASSEMBLY_VERSION,
    WRITER_PROSE_INSTRUCTION,
    WRITER_STANDARD_INSTRUCTION,
    WRITER_STANDARD_PROMPT_CONTRACT,
    WriterAssemblyError,
    _writer_messages,
    assemble_structural_suffix,
    build_writer_input,
    prose_channel,
    writer_node,
)
from catalyst_agents.runtime.assurance.checks import (
    derive_answer_output_hash,
    derive_writer_input_hash,
    run_structural_assurance,
)
from catalyst_agents.runtime.delta_sink import InMemoryDeltaSink
from catalyst_agents.runtime.provider_capability import (
    ModelRoleCallError,
    ModelTransportFailure,
)
from test_writer import FakeStreamingProvider
from test_writer import _writer_input as abstention_input

E1 = "d5cec67a0dc38d23354561c35d906e62c536ab4a070402e6e383277547cc3545:filing_v3:unknown_000:0003"
E2 = "9d3dee285c30b332185c021d5ba246aeb8a0d7fe205d403a3986b3df85f5d9f4:filing_v3:unknown_000:0176"
LIMITATION = "Coverage gap: MISSING_SECTOR_CONTEXT for SECTOR_NEWS."


def _claim(claim_id, role, citations, order, statement=None):
    return Claim(
        claim_id=claim_id,
        role=role,
        statement=statement or f"statement for {claim_id}",
        support_evidence_ids=citations,
        counter_evidence_ids=(),
        limitations=(),
        source_hypothesis_id="hyp:run:1:abc",
        magnitude_fit=(
            MagnitudeFit.STRONG
            if role in (ClaimRole.PRIMARY, ClaimRole.SECONDARY)
            else None
        ),
        conflict_refs=(),
        citation_evidence_ids=citations,
        order_index=order,
    )


def _plan(*, primary_statement=None, limitations=(LIMITATION,), extra_claims=()) -> ValidatedClaimPlan:
    claims = (
        _claim("claim:aa11", ClaimRole.PRIMARY, (E1, E2), 0, primary_statement),
        _claim("claim:bb22", ClaimRole.SECONDARY, (E2,), 1),
        *extra_claims,
    )
    return ValidatedClaimPlan(
        status=AttributionStatus.SUFFICIENT,
        attribution_type=AttributionType.EVIDENCE_BACKED_CAUSAL,
        claims=claims,
        assessment_hash="a" * 64,
        context_pack_sha256="c" * 64,
        evidence_state_hash="e" * 64,
        required_limitations=limitations,
        citation_map=tuple(
            CitationMapEntry(claim_id=c.claim_id, citation_evidence_ids=c.citation_evidence_ids)
            for c in claims
        ),
        permitted_claim_ids=tuple(c.claim_id for c in claims),
        permitted_evidence_ids=tuple(sorted((E1, E2))),
        source_role_independence_summary=SourceRoleIndependenceSummary(
            direct_primary_support_count=1
        ),
        ordering_policy_version="claim-ordering-v1",
        plan_hash="f" * 64,
    )


def _input(plan=None):
    return build_writer_input(
        plan or _plan(),
        observed_move="MSFT +7.63% on 2025-05-01",
        format_style_contract=WriterFormatStyleContract(
            format_kind=WriterFormatKind.CAUSAL,
            required_sections=(
                WriterSection.SUMMARY,
                WriterSection.CAUSAL_EXPLANATION,
                WriterSection.LIMITATIONS,
            ),
            style_instructions=("concise",),
        ),
    )


def _assure(answer_text: str, writer_input) -> dict[str, str]:
    plan = writer_input.validated_claim_plan
    citations, markers = extract_answer_markers(answer_text)
    artifacts = {
        "stream_complete": True,
        "answer_text": answer_text,
        "answer_text_sha256": derive_answer_output_hash(answer_text),
        "emitted_citations": citations,
        "emitted_claim_markers": markers,
        "permitted_claim_ids": plan.permitted_claim_ids,
        "permitted_evidence_ids": plan.permitted_evidence_ids,
        "required_sections": tuple(
            s.value for s in writer_input.format_style_contract.required_sections
        ),
        "required_limitations": plan.required_limitations,
        "emitted_status": plan.status.value,
        "emitted_attribution_type": plan.attribution_type.value,
        "validated_status": plan.status.value,
        "validated_attribution_type": plan.attribution_type.value,
        "writer_input": writer_input,
        "validated_plan": plan,
        "input_hash": derive_writer_input_hash(writer_input),
        "plan_hash": plan.plan_hash,
        "evidence_state_hash": plan.evidence_state_hash,
        "runtime_identity": "runtime:1",
        "bound_runtime_identity": "runtime:1",
        "output_hash": derive_answer_output_hash(answer_text),
        "cancellation_requested": False,
        "timed_out": False,
        "input_tokens": 10,
        "output_tokens": 20,
        "completion_state": "completed",
    }
    return {c.check_name: c.status for c in run_structural_assurance("run:1", artifacts)}


def _run(chunks, writer_input=None, **kwargs):
    sink = InMemoryDeltaSink()
    llm = FakeStreamingProvider(chunks)
    result = writer_node({"run_id": "run:1"}, llm=llm, writer_input=writer_input or _input(), sink=sink, **kwargs)
    return result, sink, llm


# ---------------------------------------------------------------- prompt ----
def test_standard_prompt_asks_only_for_the_summary():
    content = _writer_messages(_input())[0]["content"]
    assert WRITER_STANDARD_INSTRUCTION in content
    assert WRITER_PROSE_INSTRUCTION in content
    assert extract_answer_markers(WRITER_PROSE_INSTRUCTION) == ((), ())


def test_abstention_prompt_is_unchanged():
    content = _writer_messages(abstention_input(status=AttributionStatus.ABSTAIN))[0]["content"]
    assert WRITER_PROSE_INSTRUCTION not in content
    assert "fixed abstention report" in content


def test_writer_prompt_hash_covers_the_assembly_version():
    import hashlib

    from catalyst_app.runtime.composition import (
        canonical_prompt_identity,
        production_prompt_identity,
    )

    assert f"assembly={WRITER_ASSEMBLY_VERSION}" in WRITER_STANDARD_PROMPT_CONTRACT
    assert production_prompt_identity()[4] == hashlib.sha256(
        canonical_prompt_identity(WRITER_STANDARD_PROMPT_CONTRACT)
    ).hexdigest()


# -------------------------------------------------------------- assembly ----
def test_standard_answer_has_the_frozen_a5_shape_and_passes_assurance():
    result, sink, _ = _run(["MSFT rose after ", "its fiscal Q3 results."])
    text = result["answer"].text
    assert text == (
        SUMMARY_PREFIX
        + "MSFT rose after its fiscal Q3 results."
        + assemble_structural_suffix(_input())
    )
    assert f"[PRIMARY] (claim_id: claim:aa11) statement for claim:aa11 [citations: {E1},{E2}]" in text
    assert f"- {LIMITATION}" in text
    assert text == "".join(d.text for d in sink.deltas())
    assert all(status == "pass" for status in _assure(text, _input()).values())


def test_no_delta_mixes_origins_and_every_delta_is_bounded():
    long_statement = "Revenue grew " + "strongly " * 200
    plan_input = _input(_plan(primary_statement=long_statement))
    result, sink, _ = _run(["Summary sentence."], writer_input=plan_input)
    deltas = sink.deltas()
    assert all(len(d.text) <= 2048 for d in deltas)
    origins = [d.origin for d in deltas]
    assert origins[0] == "writer_assembly" and deltas[0].text == SUMMARY_PREFIX
    assert "provider" in origins
    provider_text = "".join(d.text for d in deltas if d.origin == "provider")
    assert provider_text == "Summary sentence."
    assert "".join(d.text for d in deltas) == result["answer"].text


@pytest.mark.parametrize(
    "junk",
    ["(EPS)", "[1]", "(claim_id: claim:zz99)", f"[citations: e9,{E1}]", "(8-K)"],
)
def test_adversarial_model_prose_cannot_add_markers(junk):
    result, _, _ = _run(["Earnings rose ", junk, " sharply."])
    citations, markers = extract_answer_markers(result["answer"].text)
    assert set(markers) == {"claim:aa11", "claim:bb22"}
    assert set(citations) == {E1, E2}
    assert _assure(result["answer"].text, _input())["citation_resolution"] == "pass"


def test_plan_statement_parentheses_never_become_citations():
    plan_input = _input(_plan(primary_statement="Diluted EPS (GAAP) beat in the 8-K (Item 2.02)."))
    result, _, _ = _run(["Summary."], writer_input=plan_input)
    citations, _ = extract_answer_markers(result["answer"].text)
    assert set(citations) == {E1, E2}
    assert "Diluted EPS GAAP beat in the 8-K Item 2.02." in result["answer"].text


def test_limitation_claims_are_not_rendered_as_claim_lines():
    lim = _claim("claim:cc33", ClaimRole.LIMITATION, (), 2, "A bounded limitation.")
    suffix = assemble_structural_suffix(_input(_plan(extra_claims=(lim,))))
    assert "claim:cc33" not in suffix


def test_a_limitation_with_parentheses_fails_closed():
    plan = _plan(limitations=("Coverage gap (sector).",))
    with pytest.raises(WriterAssemblyError):
        assemble_structural_suffix(_input(plan))


def test_empty_prose_fails_closed():
    with pytest.raises(ModelRoleCallError, match="WRITER_EMPTY_PROSE"):
        _run(["(", "[", ")]", "  "])


# ---------------------------------------------------------- retry / control -
def test_pre_first_token_failure_is_still_retried_without_code_deltas():
    sink = InMemoryDeltaSink()
    state = {"calls": 0}

    class Flaky(FakeStreamingProvider):
        def stream(self, messages):
            state["calls"] += 1
            if state["calls"] == 1:
                raise ModelTransportFailure("transport down")
            yield from ["recovered summary."]

    result = writer_node({"run_id": "run:1"}, llm=Flaky([]), writer_input=_input(), sink=sink)
    assert state["calls"] == 2
    assert result["answer"].text.startswith(SUMMARY_PREFIX + "recovered summary.")


def test_filtered_empty_first_chunk_does_not_disable_retry():
    """A chunk that filters to nothing is not an accepted delta."""
    sink = InMemoryDeltaSink()
    state = {"calls": 0}

    class FailsAfterBracket(FakeStreamingProvider):
        def stream(self, messages):
            state["calls"] += 1
            if state["calls"] == 1:
                yield "["
                raise ModelTransportFailure("transport down")
            yield from ["second attempt."]

    result = writer_node({"run_id": "run:1"}, llm=FailsAfterBracket([]), writer_input=_input(), sink=sink)
    assert state["calls"] == 2
    assert "second attempt." in result["answer"].text


class _CancelAfter:
    """Cancels once ``should_cancel`` has been asked ``n`` times."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.asked = 0

    def should_cancel(self) -> bool:
        self.asked += 1
        return self.asked > self.n

    def deadline_epoch_ms(self):
        return None

    def cancellation_reason(self):
        return "operator"


def test_a_control_win_after_the_last_chunk_emits_no_suffix():
    control = _CancelAfter(n=2)  # both chunks pass; the final check cancels
    result, sink, _ = _run(["one ", "two"], control=control)
    assert result["answer"] is None
    assert result["completion_state"] == "cancelled"
    assert not any("CAUSAL_EXPLANATION" in d.text for d in sink.deltas())


def test_prose_channel_is_chunk_boundary_independent():
    text = "a(b)[c]d"
    assert prose_channel(text) == "".join(prose_channel(ch) for ch in text) == "abcd"


# -------------------------------------------------------------- ABSTAIN -----
def test_abstention_prose_with_a_stray_parenthetical_passes_citation_resolution():
    writer_input = abstention_input(status=AttributionStatus.ABSTAIN)
    result, sink, _ = _run(
        ["No cause was accepted (EPS). ", LIMITATION_ABSTAIN],
        writer_input=writer_input,
    )
    text = result["answer"].text
    assert "(EPS)" not in text
    assert "CAUSAL_EXPLANATION" not in text
    assert all(d.origin == "provider" for d in sink.deltas())
    citations, markers = extract_answer_markers(text)
    assert citations == () and markers == ()


LIMITATION_ABSTAIN = "Coverage gap: MARKET_STRUCTURE_UNSUPPORTED for MARKET_STRUCTURE."


def test_whitespace_only_first_chunk_does_not_disable_retry():
    sink = InMemoryDeltaSink()
    state = {"calls": 0}

    class FailsAfterWhitespace(FakeStreamingProvider):
        def stream(self, messages):
            state["calls"] += 1
            if state["calls"] == 1:
                yield "  \n"
                raise ModelTransportFailure("transport down")
            yield from ["second ", "attempt."]

    result = writer_node(
        {"run_id": "run:1"}, llm=FailsAfterWhitespace([]), writer_input=_input(), sink=sink
    )
    assert state["calls"] == 2
    assert result["answer"].text.startswith(SUMMARY_PREFIX + "second attempt.")


def test_slices_never_exceed_the_payload_bound_even_with_a_larger_flush_setting():
    from catalyst_agents.runtime.coalescer import Coalescer

    sink = InMemoryDeltaSink()
    coalescer = Coalescer(flush_chars=10_000, sink=sink, stream_id="s")
    coalescer.accept("x" * 5000, origin="writer_assembly")
    coalescer.complete()
    assert all(len(d.text) <= 2048 for d in sink.deltas())
