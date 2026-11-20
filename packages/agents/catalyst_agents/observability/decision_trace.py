"""Decision trace: what each stage of one attribution run decided, and why.

A side recorder. The graph and the assessment normalizer report into it; it never
changes a decision. The payload is persisted as the run's ``decision_trace``
artifact so a run can be audited (and later replayed in the UI) without
re-running anything.

It separates two kinds of record:
- ``model_reported``: what the Analyst and the verifier said (dispositions,
  reason codes, their short notes, proposed hypotheses and quotes). These are
  the models' own statements, not hidden reasoning.
- ``code_checks``: what code enforced (quote rules R1-R6, the co-movement,
  subject and pre-open timing ceilings, the final status).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, Iterator

DECISION_TRACE_SCHEMA_VERSION = "decision_trace_v1"
GATES = ("comove", "subject", "timing")


class AbstentionReasonCode(StrEnum):
    NO_STOCK_NEWS = "NO_STOCK_NEWS"
    NO_PRIMARY_PROPOSED = "NO_PRIMARY_PROPOSED"
    PRIMARY_QUOTES_FAILED = "PRIMARY_QUOTES_FAILED"
    GATE_FAILED = "GATE_FAILED"
    VERIFIER_REJECTED = "VERIFIER_REJECTED"
    VERIFIER_NO_VERDICT = "VERIFIER_NO_VERDICT"
    CEILING_ABSTAIN = "CEILING_ABSTAIN"
    ALREADY_ABSTAIN = "ALREADY_ABSTAIN"
    STALE_PRIMARY = "STALE_PRIMARY"  # v2 recency veto: the top cause happened before the window


ABSTENTION_REASON_CODES = tuple(code.value for code in AbstentionReasonCode)
_NOTE_CHARS = 400


def _value(x: Any) -> Any:
    return getattr(x, "value", x)


class DecisionTrace:
    def __init__(self, run_id: str) -> None:
        self._t0 = time.monotonic()
        self.payload: dict[str, Any] = {
            "schema_version": DECISION_TRACE_SCHEMA_VERSION,
            "run_id": run_id,
            "stages": [],
            "retrieval": None,
            "rounds": [],
        }

    def _ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    @contextmanager
    def stage(self, name: str, **detail: Any) -> Iterator[dict]:
        record = {"stage": name, "started_ms": self._ms(), **detail}
        self.payload["stages"].append(record)
        try:
            yield record
        finally:
            record["duration_ms"] = self._ms() - record["started_ms"]

    def begin(self, name: str, **detail: Any) -> dict:
        record = {"stage": name, "started_ms": self._ms(), **detail}
        self.payload["stages"].append(record)
        return record

    def end(self, record: dict) -> None:
        record["duration_ms"] = self._ms() - record["started_ms"]

    def _round(self, round_no: int) -> dict:
        for record in self.payload["rounds"]:
            if record["round"] == round_no:
                return record
        record = {"round": round_no, "model_reported": {}, "code_checks": {"gates": []}}
        self.payload["rounds"].append(record)
        return record

    # ---------------------------------------------------------------- retrieval
    def retrieval(
        self, *, evidence_state: Any, context_pack: Any, research_tasks: Any = (),
    ) -> None:
        task_metadata = {
            task.task_id: task for task in research_tasks
            if getattr(task, "task_id", None) is not None
        }
        tasks = [
            {
                "task_id": result.task_id,
                "evidence_need": (
                    "LEAD_FOLLOW_UP" if ":lead:" in result.task_id else
                    _value(task_metadata[result.task_id].evidence_need)
                    if result.task_id in task_metadata else None
                ),
                "time_scope": (
                    _value(task_metadata[result.task_id].time_scope)
                    if result.task_id in task_metadata else None
                ),
                "status": _value(result.status),
                "latency_ms": result.latency_ms,
                "items": len(result.evidence_items),
                "facts": len(result.structured_facts),
                "mode_served": result.mode_served,
                "degradation_reasons": list(result.degradation_reasons),
            }
            for result in getattr(evidence_state, "task_results", ())
        ]
        inventory = tuple(getattr(context_pack, "evidence_inventory", ()))
        included = set(getattr(context_pack, "included_evidence_ids", ()))
        by_state: dict[str, int] = {}
        for item in inventory:
            if item.evidence_id in included:
                key = str(_value(item.content_state))
                by_state[key] = by_state.get(key, 0) + 1
        self.payload["retrieval"] = {
            "tasks": tasks,
            "evidence_in_state": len(getattr(evidence_state, "evidence_items", ())),
            "facts_in_state": len(getattr(evidence_state, "structured_facts", ())),
            "inventory": len(inventory),
            "included_in_pack": len(included),
            "excluded_from_pack": len(getattr(context_pack, "excluded_evidence_ids", ())),
            "included_by_content_state": by_state,
            "lead_only": len(getattr(context_pack, "lead_only_evidence", ())),
        }

    # ---------------------------------------------------------------- analyst
    def analyst(self, round_no: int, decision: Any) -> None:
        """The Analyst's raw decision, before quote rules and ceilings."""
        if decision is None:
            return
        self._round(round_no)["model_reported"]["analyst"] = {
            "evidence_decisions": [
                {
                    "evidence_id": d.evidence_id,
                    "disposition": str(_value(d.disposition)),
                    "reason_code": d.reason_code,
                    "note": (d.note or "")[:_NOTE_CHARS] or None,
                    "supports": list(d.supports_hypothesis_refs),
                    "contradicts": list(d.contradicts_hypothesis_refs),
                }
                for d in decision.evidence_decisions
            ],
            "hypotheses": [
                {
                    "ref": h.hypothesis_ref,
                    "cause_type": str(_value(h.cause_type)),
                    "proposed_role": str(_value(getattr(h, "proposed_role", None))),
                    "magnitude_fit": str(_value(getattr(h, "magnitude_fit", None))),
                    "statement": h.statement,
                    "supporting_evidence_ids": list(getattr(h, "supporting_evidence_ids", ())),
                    "quotes": [{"evidence_id": q.evidence_id, "text": q.text} for q in h.quotes],
                }
                for h in decision.candidate_hypotheses
            ],
            "research_decision": str(_value(getattr(decision, "research_decision", None))),
            "recommended_status": str(_value(getattr(decision, "recommended_status", None))),
        }

    # ---------------------------------------------------------------- code checks
    def quote_checks(self, round_no: int, checks: tuple) -> None:
        rows = []
        for c in checks:
            row = {"hypothesis_ref": c.hypothesis_ref, "evidence_id": c.evidence_id,
                   "kept": c.kept, "rule": c.rule, "text": c.text}
            if getattr(c, "r4a", None) is not None:
                row.update({"r4a": c.r4a, "r4b": c.r4b,
                            "r4b_path": c.r4b_path, "r8": c.r8})
                row.update({
                    "publisher": c.publisher,
                    "publisher_family": c.publisher_family,
                    "event_time": c.event_time,
                    "source_class": c.source_class,
                    "document_id": c.document_id,
                    "document_key": c.document_key,
                    "provenance_fingerprint": list(c.provenance_fingerprint or ()),
                })
            if getattr(c, "roles", None) is not None:
                row.update({
                    "roles": list(c.roles),
                    "role_reasons": {
                        role: list(reasons)
                        for role, reasons in (c.role_reasons or {}).items()
                    },
                })
            rows.append(row)
        self._round(round_no)["code_checks"]["quotes"] = rows

    def gate(self, round_no: int, name: str, ceiling: Any, diagnostic: str) -> None:
        self._round(round_no)["code_checks"]["gates"].append({
            "gate": name,
            "result": "no_change" if ceiling is None else f"ceiling:{_value(ceiling)}",
            "detail": diagnostic or None,
        })

    def verifier(self, round_no: int, *, called: bool, verdict: Any = None, reason: str = "",
                 skipped_because: str | None = None, cause_type: str | None = None,
                 primary_proposal_state: str = "NO_PRIMARY_PROPOSED", cause_quote: str | None = None) -> None:
        record = {"called": called}
        record["primary_proposal_state"] = primary_proposal_state
        if called:
            category, _, sentence = (reason or "").partition(":")
            record.update({
                "verdict": "SUPPORTED" if verdict is True else "REJECT" if verdict is False else "NO_VERDICT",
                "category": category or None,
                "reason": sentence or reason or None,
                "cause_type": cause_type,
            })
            if cause_quote:
                record["cause_quote"] = cause_quote
        else:
            record["skipped_because"] = skipped_because
        self._round(round_no)["model_reported"]["verifier"] = record

    def outcome(self, round_no: int, assessment: Any) -> None:
        ceiling_codes = {
            str(getattr(item, "reason_code", ""))
            for item in getattr(assessment, "validated_missing_evidence", ())
            if getattr(item, "reason_code", None)
        }
        ceiling_codes.update(
            str(item) for item in getattr(assessment, "normalization_diagnostics", ())
            if isinstance(item, str) and item and item.upper() == item
        )
        self._round(round_no)["code_checks"]["outcome"] = {
            "status_ceiling": str(_value(assessment.status_ceiling)),
            "ceiling_codes": sorted(ceiling_codes),
            "final_status": str(_value(assessment.final_status)),
            "attribution_type": str(_value(assessment.final_attribution_type)),
            "research_decision": str(_value(assessment.research_decision)),
            "hypotheses": [
                {"id": h.hypothesis_id, "role": str(_value(h.normalized_role)),
                 "cause_type": str(_value(h.cause_type))}
                for h in assessment.normalized_hypotheses
            ],
        }

    def grade_decision(self, round_no: int, decision: dict[str, Any]) -> None:
        """Record the code-derived event-time grade and its reason codes."""
        self._round(round_no)["code_checks"]["grade"] = decision

    def abstention_reason(self, reason: dict[str, Any] | None) -> None:
        """Record the code-derived, additive public abstention classification."""
        if reason is None:
            self.payload.pop("abstention_reason", None)
            return
        code = reason.get("reason_code")
        if code not in ABSTENTION_REASON_CODES:
            raise ValueError(f"unsupported abstention reason code: {code!r}")
        self.payload["abstention_reason"] = {
            "reason_code": code,
            "details": reason.get("details", {}),
        }

    # ---------------------------------------------------------------- result
    def finish(self, *, final_status: Any, calls: dict[str, int]) -> dict:
        self.payload["final_status"] = str(_value(final_status))
        self.payload["model_calls"] = calls
        self.payload["total_ms"] = self._ms()
        return self.payload


__all__ = ["ABSTENTION_REASON_CODES", "AbstentionReasonCode", "DECISION_TRACE_SCHEMA_VERSION", "DecisionTrace"]
