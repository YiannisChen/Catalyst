"""Finalizer node — assign the final output status for the current run."""
from __future__ import annotations

from catalyst_agents.state import AttributionState, OutputStatus, Phase


def finalizer(state: AttributionState) -> dict:
    """Normalize the final status after Judge/Validator or fallback handlers."""
    status = state.get("output_status")
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
                status = OutputStatus.INSUFFICIENT
        elif state.get("causes"):
            status = OutputStatus.SUFFICIENT
        else:
            status = OutputStatus.INSUFFICIENT

    return {
        "output_status": status,
        "phase": Phase.FINALIZER,
    }
