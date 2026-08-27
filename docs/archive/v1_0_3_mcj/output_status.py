from __future__ import annotations

from enum import Enum
from typing import Any


class OutputStatus(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    ABSTAIN = "ABSTAIN"
    SYSTEM_ERROR = "SYSTEM_ERROR"


def decode_output_status(value: Any) -> OutputStatus | None:
    if value is None:
        return None
    raw = getattr(value, "value", None) or getattr(value, "name", None) or str(value)
    if raw == "INSUFFICIENT":
        raw = "ABSTAIN"
    return OutputStatus(raw)


def determine_status(
    hypotheses: list[Any] | tuple[Any, ...] | None,
    *,
    cutoff_violations: int,
    citation_all_resolve: bool,
    coverage_degraded: bool,
    context_quality_ok: bool,
    error_occurred: bool = False,
) -> OutputStatus:
    if error_occurred:
        return OutputStatus.SYSTEM_ERROR
    if not context_quality_ok or cutoff_violations > 0 or not citation_all_resolve:
        return OutputStatus.ABSTAIN
    gate_passed = [h for h in (hypotheses or []) if bool(getattr(h, "prerequisite_gate_passed", False))]
    if not gate_passed:
        return OutputStatus.ABSTAIN
    degraded = coverage_degraded or any(int(getattr(h, "source_support_degradation_count", 0) or 0) > 0 for h in gate_passed)
    return OutputStatus.PARTIAL if degraded else OutputStatus.SUFFICIENT
