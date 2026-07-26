"""Finalizer node — assign the final output status for the current run."""
from __future__ import annotations

from catalyst_agents.attribution.hypothesis import Hypothesis
from catalyst_agents.attribution.output_status import determine_status
from catalyst_agents.attribution.ranking import rank_hypotheses
from catalyst_agents.state import AttributionState, OutputStatus, Phase


def finalizer(state: AttributionState) -> dict:
    """Normalize the final status after Judge/Validator or fallback handlers."""
    status = state.get("output_status")
    ranked_payload: list[dict] = []
    if state.get("hypotheses"):
        hypotheses = [Hypothesis.model_validate(item) for item in state.get("hypotheses", [])]
        ranked = rank_hypotheses(hypotheses)
        ranked_payload = [item.model_dump(mode="json") for item in ranked]
        if status not in {OutputStatus.SYSTEM_ERROR, OutputStatus.ABSTAIN}:
            status = determine_status(
                ranked,
                cutoff_violations=0,
                citation_all_resolve=True,
                coverage_degraded=bool(state.get("is_degraded", False)),
                context_quality_ok=bool(state.get("context_artifact", True)),
            )
    if status is None:
        if state.get("error_type") == "system_error":
            status = OutputStatus.SYSTEM_ERROR
        elif state.get("critic_decision") is not None:
            sufficiency = state["critic_decision"].sufficiency
            if sufficiency == "sufficient":
                status = OutputStatus.SUFFICIENT
            elif sufficiency == "partial":
                status = OutputStatus.PARTIAL
            else:
                status = OutputStatus.ABSTAIN
        elif state.get("causes"):
            status = OutputStatus.SUFFICIENT
        else:
            status = OutputStatus.ABSTAIN

    return {
        "output_status": status,
        "ranked_hypotheses": ranked_payload,
        "phase": Phase.FINALIZER,
    }
