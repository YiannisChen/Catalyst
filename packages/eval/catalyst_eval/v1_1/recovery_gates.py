"""M8 causal-recovery quality gates (M8-C / M8-D).

Separate from the frozen M7 ``STAGE1_RETRIEVAL_GATES``: this block never
reinterprets, relaxes, or replaces the M7 metrics. It consumes existing metric
objects plus the ordered per-case attribution results and decides whether the
M8 recovery attribution is admissible.

Every gate is reported as ``PASS`` / ``FAIL`` / ``NOT_EXERCISED``. A gate whose
denominator was never exercised is ``NOT_EXERCISED`` and fails the recovery
seal — an empty denominator is never a pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

RECOVERY_GATE_VERSION = "m8_v1"

# M8-C: FTS-only attribution probe.
RECOVERY_FTS_GATES: dict[str, Any] = {
    "answerable_non_abstain": (6, 11),
    "c01_non_abstain": True,
    "c04_abstain": True,
    "citation_correctness": 1.0,
    "unsupported_primary_claims": 0,
    "false_sufficient_max": 1,
}

# M8-D: hybrid attribution after the dense recovery + atomic promotion.
RECOVERY_HYBRID_GATES: dict[str, Any] = {
    "sufficient_non_abstain": (7, 9),
    "status_match_min": (8, 12),
    "c04_abstain": True,
    "citation_correctness": 1.0,
    "unsupported_primary_claims": 0,
    "false_sufficient_max": 1,
}

GATE_SETS = {"fts": RECOVERY_FTS_GATES, "hybrid": RECOVERY_HYBRID_GATES}

ANSWERABLE_GOLD = frozenset({"SUFFICIENT", "PARTIAL"})
ABSTAIN = "ABSTAIN"

PASS = "PASS"
FAIL = "FAIL"
NOT_EXERCISED = "NOT_EXERCISED"


class RecoveryGateError(ValueError):
    pass


@dataclass(frozen=True)
class RecoveryCaseResult:
    case_id: str
    gold_status: str
    observed_status: str
    citation_total: int = 0
    citation_correct: int = 0
    unsupported_primary_claims: int = 0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RecoveryCaseResult":
        try:
            return cls(
                case_id=str(payload["case_id"]),
                gold_status=str(payload["gold_status"]),
                observed_status=str(payload["observed_status"]),
                citation_total=int(payload.get("citation_total") or 0),
                citation_correct=int(payload.get("citation_correct") or 0),
                unsupported_primary_claims=int(
                    payload.get("unsupported_primary_claims") or 0
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RecoveryGateError(
                f"invalid recovery case result: {exc}"
            ) from exc


@dataclass(frozen=True)
class RecoveryGateReport:
    recovery_gate_version: str
    mode: str
    gates: Mapping[str, str]
    counts: Mapping[str, int | float | None]
    sealed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "recovery_gate_version": self.recovery_gate_version,
            "mode": self.mode,
            "sealed": self.sealed,
            "gates": dict(sorted(self.gates.items())),
            "counts": dict(sorted(self.counts.items())),
        }


def _by_id(case_results: Sequence[RecoveryCaseResult]) -> dict[str, RecoveryCaseResult]:
    return {case.case_id: case for case in case_results}


def _ordered_gate(
    *,
    numerator: int,
    denominator: int,
    required_numerator: int,
    required_denominator: int,
) -> str:
    if denominator == 0:
        return NOT_EXERCISED
    if denominator < required_denominator:
        return FAIL
    return PASS if numerator >= required_numerator else FAIL


def evaluate_recovery_gates(
    *,
    mode: str,
    case_results: Sequence[RecoveryCaseResult],
    gates: Mapping[str, Any] | None = None,
) -> RecoveryGateReport:
    """Evaluate the M8-C/M8-D gate set over ordered per-case results."""
    if mode not in GATE_SETS:
        raise RecoveryGateError(f"unknown recovery mode {mode!r}")
    spec = dict(gates or GATE_SETS[mode])
    cases = tuple(case_results)
    indexed = _by_id(cases)

    gold_answerable = [case for case in cases if case.gold_status in ANSWERABLE_GOLD]
    answerable_non_abstain = sum(
        1 for case in gold_answerable if case.observed_status != ABSTAIN
    )
    gold_sufficient = [case for case in cases if case.gold_status == "SUFFICIENT"]
    sufficient_non_abstain = sum(
        1 for case in gold_sufficient if case.observed_status != ABSTAIN
    )
    status_match = sum(
        1 for case in cases if case.observed_status == case.gold_status
    )
    false_sufficient = sum(
        1
        for case in cases
        if case.observed_status == "SUFFICIENT" and case.gold_status != "SUFFICIENT"
    )
    citation_total = sum(case.citation_total for case in cases)
    citation_correct = sum(case.citation_correct for case in cases)
    unsupported_total = sum(case.unsupported_primary_claims for case in cases)

    status: dict[str, str] = {}
    for gate in spec:
        if gate == "answerable_non_abstain":
            required, denominator = spec[gate]
            status[gate] = _ordered_gate(
                numerator=answerable_non_abstain,
                denominator=len(gold_answerable),
                required_numerator=required,
                required_denominator=denominator,
            )
        elif gate == "sufficient_non_abstain":
            required, denominator = spec[gate]
            status[gate] = _ordered_gate(
                numerator=sufficient_non_abstain,
                denominator=len(gold_sufficient),
                required_numerator=required,
                required_denominator=denominator,
            )
        elif gate == "status_match_min":
            required, denominator = spec[gate]
            status[gate] = _ordered_gate(
                numerator=status_match,
                denominator=len(cases),
                required_numerator=required,
                required_denominator=denominator,
            )
        elif gate in {"c01_non_abstain", "c04_abstain"}:
            case_id = gate.split("_")[0]
            case = indexed.get(case_id)
            if case is None:
                status[gate] = NOT_EXERCISED
            elif gate.endswith("non_abstain"):
                status[gate] = PASS if case.observed_status != ABSTAIN else FAIL
            else:
                status[gate] = PASS if case.observed_status == ABSTAIN else FAIL
        elif gate == "citation_correctness":
            if citation_total == 0:
                status[gate] = NOT_EXERCISED
            else:
                status[gate] = (
                    PASS
                    if citation_correct / citation_total >= float(spec[gate])
                    else FAIL
                )
        elif gate == "unsupported_primary_claims":
            if citation_total == 0:
                status[gate] = NOT_EXERCISED
            else:
                status[gate] = PASS if unsupported_total == 0 else FAIL
        elif gate == "false_sufficient_max":
            if not cases:
                status[gate] = NOT_EXERCISED
            else:
                status[gate] = PASS if false_sufficient <= int(spec[gate]) else FAIL
        else:  # pragma: no cover - unknown gate in an explicit override
            status[gate] = NOT_EXERCISED

    counts = {
        "cases": len(cases),
        "gold_answerable": len(gold_answerable),
        "answerable_non_abstain": answerable_non_abstain,
        "gold_sufficient": len(gold_sufficient),
        "sufficient_non_abstain": sufficient_non_abstain,
        "status_match": status_match,
        "false_sufficient": false_sufficient,
        "citation_total": citation_total,
        "citation_correct": citation_correct,
        "unsupported_primary_claims": unsupported_total,
    }
    return RecoveryGateReport(
        recovery_gate_version=RECOVERY_GATE_VERSION,
        mode=mode,
        gates=status,
        counts=counts,
        sealed=bool(status) and all(value == PASS for value in status.values()),
    )


def recovery_report_section(
    *,
    mode: str,
    case_results: Iterable[Mapping[str, Any] | RecoveryCaseResult],
) -> dict[str, Any]:
    """Report section keyed by ``recovery_gate_version`` (never M7 formulas)."""
    parsed = [
        item
        if isinstance(item, RecoveryCaseResult)
        else RecoveryCaseResult.from_mapping(item)
        for item in case_results
    ]
    return evaluate_recovery_gates(mode=mode, case_results=parsed).as_dict()


__all__ = [
    "ANSWERABLE_GOLD",
    "FAIL",
    "GATE_SETS",
    "NOT_EXERCISED",
    "PASS",
    "RECOVERY_FTS_GATES",
    "RECOVERY_GATE_VERSION",
    "RECOVERY_HYBRID_GATES",
    "RecoveryCaseResult",
    "RecoveryGateError",
    "RecoveryGateReport",
    "evaluate_recovery_gates",
    "recovery_report_section",
]
