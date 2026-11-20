"""WP-C C3c: production-path graph runs pass structural assurance while the
Writer model writes adversarial prose (no markers, stray brackets, fake ids).

Covers the SUFFICIENT path, ABSTAIN, and the c04-shaped
ABSTAIN + NO_MATERIAL_PUBLIC_CATALYST path (Frozen-A4 / Frozen-A3 C11).
"""
from __future__ import annotations

from catalyst_agents.graphs.graph import extract_answer_markers, run_v1_graph
from test_v1_graph import (
    GraphAnalystProvider,
    GraphWriterProvider,
    _abstain_decision,
    _EmptyRetriever,
    _graph_kwargs,
    _NoContextObservationProvider,
    _ready_decision,
)

# C3 attempt 2 (prose only, no markers) and attempt 3 (markers without their
# brackets) Writer outputs, plus stray tokens a model may emit.
ATTEMPT_2_PROSE = (
    "## SUMMARY\nAAPL moved on record guidance (EPS). The primary claim is "
    "supported by the cited filing evidence.\n"
)
ATTEMPT_3_PROSE = (
    "PRIMARY claim:0000aaaa AAPL rose on guidance. [citations: e1] "
    "(claim_id: claim:ffff) [e9] (8-K)"
)


def _assert_assured(result, status: str) -> None:
    assert result.terminal_envelope["assured"] is True
    assert result.terminal_envelope["final_status"] == status


def test_sufficient_path_with_marker_free_prose_is_assured() -> None:
    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(ATTEMPT_2_PROSE)
    result = run_v1_graph(**_graph_kwargs(analyst=analyst, writer=writer))
    _assert_assured(result, "SUFFICIENT")
    assert "## CAUSAL_EXPLANATION\n[PRIMARY] (claim_id: " in result.answer.text
    assert "(EPS)" not in result.answer.text


def test_sufficient_path_with_broken_marker_prose_is_assured() -> None:
    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(ATTEMPT_3_PROSE)
    result = run_v1_graph(**_graph_kwargs(analyst=analyst, writer=writer))
    _assert_assured(result, "SUFFICIENT")
    citations, markers = extract_answer_markers(result.answer.text)
    assert "claim:ffff" not in markers and "claim:0000aaaa" not in markers
    assert "e9" not in citations and "8-K" not in citations


def test_abstain_path_with_stray_parenthetical_is_assured() -> None:
    analyst = GraphAnalystProvider(_abstain_decision)
    writer = GraphWriterProvider(
        "OBSERVED_MOVE\nAAPL +9.5% (EPS).\nLIMITATIONS\n"
        "No causal explanation was established from the available evidence."
    )
    result = run_v1_graph(**_graph_kwargs(analyst=analyst, writer=writer))
    _assert_assured(result, "ABSTAIN")
    assert "CAUSAL_EXPLANATION" not in result.answer.text


def test_c04_shaped_abstain_no_material_with_stray_tokens_is_assured() -> None:
    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(
        "OBSERVED_MOVE\nAAPL +9.5% [1] (8-K).\nLIMITATIONS\n"
        "No causal explanation was established from the available evidence."
    )
    kwargs = _graph_kwargs(analyst=analyst, writer=writer)
    kwargs["retriever"] = _EmptyRetriever()
    kwargs["observation_provider"] = _NoContextObservationProvider()
    result = run_v1_graph(**kwargs)
    _assert_assured(result, "ABSTAIN")
    assert result.terminal_envelope["attribution_type"] == "NO_MATERIAL_PUBLIC_CATALYST"
