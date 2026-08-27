"""M5-8/M5-11: thin Finalizer persists only an assured terminal envelope."""
from __future__ import annotations

import pytest

from catalyst_agents.nodes.finalizer import (
    PROVISIONAL_RENDERING,
    AssuranceFailed,
    thin_finalizer,
)
from catalyst_agents.runtime.assurance.record import AssuranceCheck
from catalyst_agents.runtime.delta_sink import InMemoryDeltaSink


class _ValidatedPlan:
    status = type("S", (), {"value": "SUFFICIENT"})()
    attribution_type = type("T", (), {"value": "EVIDENCE_BACKED_CAUSAL"})()


def _passing_checks() -> list[AssuranceCheck]:
    return [
        AssuranceCheck(check_name=name, status="pass", detail="ok", checked_at="2026-01-01T00:00:00Z")
        for name in (
            "stream_complete",
            "citation_resolution",
            "claim_markers_subset",
            "required_sections",
            "required_limitations",
            "status_type_alignment",
            "hash_coherence",
            "metadata_consistency",
        )
    ]


def test_thin_finalizer_persists_assured_envelope() -> None:
    sink = InMemoryDeltaSink()
    result = thin_finalizer(
        {"run_id": "run:1"},
        answer_text="assured answer",
        validated_plan=_ValidatedPlan(),
        assurance_checks=_passing_checks(),
        sink=sink,
    )
    assert result["assured"] is True
    envelope = sink.assured_envelope()
    assert envelope is not None
    assert envelope["final_status"] == "SUFFICIENT"
    assert envelope["assured"] is True


def test_thin_finalizer_refuses_non_assured_envelope() -> None:
    sink = InMemoryDeltaSink()
    failing = _passing_checks()
    failing[0] = AssuranceCheck(
        check_name="stream_complete", status="fail", detail="incomplete", checked_at="2026-01-01T00:00:00Z"
    )
    with pytest.raises(AssuranceFailed, match="ASSURANCE_FAILED"):
        thin_finalizer(
            {"run_id": "run:1"},
            answer_text="provisional",
            validated_plan=_ValidatedPlan(),
            assurance_checks=failing,
            sink=sink,
        )
    assert sink.assured_envelope() is None


def test_provisional_rendering_label() -> None:
    assert PROVISIONAL_RENDERING == "PROVISIONAL_RENDERING"
