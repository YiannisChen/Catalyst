"""Public summary projection regression tests (AMEND-7).

The persisted/public ``judge_summary`` (and ``judge_causes``) must be derived
from the post-filter Validator state, never from the raw Judge output, because
the Judge node runs before prerequisite gates and its raw causal text can
assert hypotheses that the Validator later discards.
"""
from __future__ import annotations

from catalyst_agents.trace.projection import project_node_artifacts

_CANONICAL_ABSTAIN = (
    "Catalyst abstained for AAPL on 2026-01-15 because no drafted "
    "hypothesis satisfied the required evidence gates. This may indicate the "
    "price move was driven by factors outside our data coverage (private "
    "information, market microstructure, or sources we do not ingest)."
)


def _post_filter_validator_state() -> dict:
    """State exactly as the graph merges after the Validator node runs."""
    return {
        "causes": [],
        "summary_md": _CANONICAL_ABSTAIN,
        "grounding_rate": 0.0,
        "hypotheses": [],
        "output_status": "ABSTAIN",
    }


def test_judge_node_no_longer_persists_raw_judge_causes_or_summary():
    """The Judge node must not persist judge_causes/judge_summary artifacts:
    at that point gate filtering has not happened, so the raw causal text
    would be persisted before the Validator can sanitize it."""
    merged_state = {
        "causes": [
            {
                "text": "AAPL dropped because of [c1] demand weakness",
                "category": "product_demand",
                "evidence_ids": ["c1"],
                "direction": "negative",
            }
        ],
        "summary_md": "AAPL dropped after [c1] demand weakness.",
        "grounding_rate": 0.5,
    }
    artifacts = project_node_artifacts("judge", merged_state, {})
    artifact_types = {artifact["artifact_type"] for artifact in artifacts}
    assert "judge_causes" not in artifact_types
    assert "judge_summary" not in artifact_types


def test_validator_projects_judge_summary_from_post_filter_state():
    """The Validator node persists judge_summary/judge_causes from the
    post-filter state, so leaked causal text cannot reach the persisted
    artifact."""
    merged_state = _post_filter_validator_state()
    artifacts = project_node_artifacts("validator", merged_state, {})
    by_type = {artifact["artifact_type"]: artifact["payload_json"] for artifact in artifacts}

    assert "judge_summary" in by_type
    assert by_type["judge_summary"]["summary_md"] == _CANONICAL_ABSTAIN
    assert "demand weakness" not in by_type["judge_summary"]["summary_md"]

    assert "judge_causes" in by_type
    assert by_type["judge_causes"]["causes"] == []


def test_validator_projects_partial_judge_causes_from_published_only():
    """With a partial filter, judge_causes contains only surviving causes."""
    merged_state = {
        "causes": [
            {
                "text": "Guidance weakness reduced expectations",
                "category": "earnings_guidance",
                "evidence_ids": ["c1"],
                "direction": "negative",
            }
        ],
        "summary_md": (
            "Catalyst attributes AAPL's move on 2026-01-15 primarily to:\n"
            "- earnings_guidance: Guidance weakness reduced expectations (evidence: c1)"
        ),
        "grounding_rate": 1.0,
        "hypotheses": [],
        "output_status": "SUFFICIENT",
    }
    artifacts = project_node_artifacts("validator", merged_state, {})
    by_type = {artifact["artifact_type"]: artifact["payload_json"] for artifact in artifacts}

    causes = by_type["judge_causes"]["causes"]
    assert [cause["category"] for cause in causes] == ["earnings_guidance"]
    assert "demand" not in by_type["judge_summary"]["summary_md"]
