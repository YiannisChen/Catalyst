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


# ---------------------------------------------------------------------------
# M5-8: thin terminal wrapper (V1.1). The legacy finalizer above is retained
# as the sealed MCJ baseline instrument until M5-11 removes it.
# ---------------------------------------------------------------------------

PROVISIONAL_RENDERING = "PROVISIONAL_RENDERING"


class AssuranceFailed(RuntimeError):
    """Structural assurance failed: the provisional answer is invalidated and
    the run fails closed; no retry is performed to obtain a preferred status."""


def thin_finalizer(
    state: dict,
    *,
    answer_text: str,
    validated_plan: Any,
    assurance_checks: list[Any],
    sink: Any,
    terminal_status: str = "COMPLETED",
) -> dict:
    """Persist only an assured terminal result envelope.

    Never ranks, repairs, rewrites, or infers status. Assurance failure
    invalidates the provisional output and fails closed.
    """
    if not assurance_checks or not all(
        check.status == "pass" for check in assurance_checks
    ):
        sink.fail("ASSURANCE_FAILED")
        raise AssuranceFailed(
            "ASSURANCE_FAILED: structural assurance did not pass; the "
            "provisional answer is invalidated"
        )
    import time as _time

    envelope = {
        "run_id": state.get("run_id"),
        "answer_text": answer_text,
        "answer_text_sha256": __import__("hashlib").sha256(
            answer_text.encode("utf-8")
        ).hexdigest(),
        "final_status": validated_plan.status.value,
        "attribution_type": validated_plan.attribution_type.value,
        "provisional_label": PROVISIONAL_RENDERING,
        "assured": True,
        "terminal_status": terminal_status,
        "completed_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
    }
    sink.commit_assured_envelope(
        answer_id=f"answer:{state.get('run_id')}",
        final_status=envelope["final_status"],
        attribution_type=envelope["attribution_type"],
        checks=tuple(check.check_name for check in assurance_checks),
        completed_at=envelope["completed_at"],
    )
    return {"terminal_envelope": envelope, "assured": True}
