"""Thin terminal wrapper (M5-8/M5-11).

Final TSD §13/§29: the V1.1 Finalizer persists only an assured terminal result
envelope via the injected protocol (production envelope is M6). It never
ranks, repairs, rewrites, infers a status, or turns a failure into ABSTAIN.
The legacy ranked Finalizer was archived at M5-11.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

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
    envelope = {
        "run_id": state.get("run_id"),
        "answer_text": answer_text,
        "answer_text_sha256": hashlib.sha256(
            answer_text.encode("utf-8")
        ).hexdigest(),
        "final_status": validated_plan.status.value,
        "attribution_type": validated_plan.attribution_type.value,
        "provisional_label": PROVISIONAL_RENDERING,
        "assured": True,
        "terminal_status": terminal_status,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    sink.commit_assured_envelope(
        answer_id=f"answer:{state.get('run_id')}",
        final_status=envelope["final_status"],
        attribution_type=envelope["attribution_type"],
        checks=tuple(check.check_name for check in assurance_checks),
        completed_at=envelope["completed_at"],
    )
    return {"terminal_envelope": envelope, "assured": True}


__all__ = ["AssuranceFailed", "PROVISIONAL_RENDERING", "thin_finalizer"]
