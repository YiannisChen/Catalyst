"""Validator node — enforce output grounding and schema checks before finalization."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from catalyst_agents.backoff import invoke_with_retries
from catalyst_agents.attribution.gates import check_prerequisite
from catalyst_agents.attribution.hypothesis import EvidenceRef, Hypothesis, HypothesisDraft
from catalyst_agents.attribution.output_status import determine_status
from catalyst_agents.runtime.assurance.checks import compute_source_flags
from catalyst_agents.cost_tracker import track_cost
from catalyst_agents.nodes.critic import M_THRESHOLD
from catalyst_agents.nodes.judge import _parse_judge_response
from catalyst_agents.state import AttributionState, OutputStatus, Phase


def _effective_m_threshold(default: float = M_THRESHOLD) -> float:
    raw = os.getenv("CATALYST_M_THRESHOLD")
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class ValidatedCause(BaseModel):
    text: str = Field(min_length=1)
    category: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    direction: str = Field(min_length=1)


class ValidatedJudgeOutput(BaseModel):
    causes: list[ValidatedCause]
    summary_md: str = Field(min_length=1)


def _status_from_state(state: AttributionState) -> OutputStatus:
    decision = state.get("critic_decision")
    if decision is None:
        return OutputStatus.SUFFICIENT if state.get("causes") else OutputStatus.ABSTAIN

    if decision.sufficiency == "sufficient":
        return OutputStatus.SUFFICIENT
    if decision.sufficiency == "partial":
        return OutputStatus.PARTIAL
    return OutputStatus.ABSTAIN


def _evidence_lookup(state: AttributionState) -> dict[str, dict]:
    chunks = state.get("reranked_chunks") or state.get("retrieved_chunks") or []
    return {chunk.get("asset_id", ""): chunk for chunk in chunks if chunk.get("asset_id")}


def _critic_lookup(state: AttributionState) -> dict[str, dict]:
    return {item.get("chunk_id", ""): item for item in state.get("graded_evidence", []) if item.get("chunk_id")}


def _evidence_ref(chunk_id: str, state: AttributionState) -> EvidenceRef | None:
    chunk = _evidence_lookup(state).get(chunk_id)
    if chunk is None:
        return None
    grade = _critic_lookup(state).get(chunk_id, {})
    return EvidenceRef(
        chunk_id=chunk_id,
        document_id=chunk.get("document_id"),
        available_at=chunk.get("available_at") or chunk.get("reference_date"),
        source_class=chunk.get("source_class") or chunk.get("source_type"),
        ticker_associations=tuple(chunk.get("ticker_associations", ())),
        dedup_cluster_id=chunk.get("dedup_cluster_id"),
        is_novel=bool(chunk.get("is_novel", False)),
        critic_category=grade.get("category"),
        relevance=float(grade.get("relevance", 0.0) or 0.0),
        temporal_match=bool(grade.get("temporal_match", False)),
    )


DIRECT_SOURCE_CLASSES = {"official_government", "issuer_disclosure", "corporate_press_release", "reported_news"}


def _enrich_drafts(state: AttributionState, *, cutoff_policy: Any = None) -> tuple[list[Hypothesis], list[str], str | None]:
    drafts = [HypothesisDraft.model_validate(item) for item in state.get("hypothesis_drafts", [])]
    violations: list[str] = []
    hypotheses: list[Hypothesis] = []
    cutoff = state.get("cutoff")
    if cutoff_policy is not None:
        cutoff = cutoff_policy.compute_cutoff(ticker=state["ticker"], session_date=state["trade_date"], mode="attribution")
    for draft in drafts:
        draft_violations: list[str] = []
        supporting: list[EvidenceRef] = []
        counter: list[EvidenceRef] = []
        for chunk_id in draft.supporting_evidence_ids:
            ref = _evidence_ref(chunk_id, state)
            if ref is None:
                draft_violations.append("evidence_id_missing")
            else:
                supporting.append(ref)
                if cutoff and (not ref.available_at or ref.available_at > cutoff):
                    draft_violations.append("cutoff_violation")
        for chunk_id in draft.counter_evidence_ids:
            ref = _evidence_ref(chunk_id, state)
            if ref is not None:
                counter.append(ref)
        valid_support = [
            ref for ref in supporting
            if ref.relevance > 0.5 and ref.temporal_match and cutoff is not None
            and ref.available_at is not None and ref.available_at <= cutoff
        ]
        context_artifact = state.get("context_artifact") or {}
        gate_passed, gate_reason = check_prerequisite(
            cause=draft.cause_label,
            context={
                "ticker": state["ticker"],
                "session_date": state["trade_date"],
                "cutoff": cutoff,
                "benchmark_return_pct": context_artifact.get("benchmark_return_pct"),
                "sector_return_pct": context_artifact.get("sector_return_pct"),
                "peer_returns_by_ticker": dict(context_artifact.get("peer_returns") or ()),
                "context_quality_ok": bool(context_artifact),
            },
            evidence=[ref.model_dump(mode="json") for ref in valid_support],
            edges=state.get("relationship_manifest") or state.get("relationship_edges") or {},
        )
        direction_value = None
        if draft.cause_label == "market":
            direction_value = context_artifact.get("market_component", context_artifact.get("benchmark_return_pct"))
        elif draft.cause_label == "sector":
            direction_value = context_artifact.get("sector_excess")
            if direction_value is None and context_artifact.get("sector_return_pct") is not None and context_artifact.get("benchmark_return_pct") is not None:
                direction_value = context_artifact["sector_return_pct"] - context_artifact["benchmark_return_pct"]
        elif draft.cause_label == "peer_propagation":
            direction_value = context_artifact.get("target_vs_peers")
        if isinstance(direction_value, (int, float)) and direction_value != 0:
            expected_direction = "positive" if direction_value > 0 else "negative"
            if draft.direction != expected_direction:
                gate_passed = False
                gate_reason = f"structured direction requires {expected_direction}"
                draft_violations.append("direction_mismatch")
        flags = compute_source_flags([ref.model_dump(mode="json") for ref in valid_support])
        clusters = {ref.dedup_cluster_id or f"chunk:{ref.chunk_id}" for ref in valid_support}
        hypotheses.append(Hypothesis(
            **draft.model_dump(mode="json"),
            supporting_evidence=tuple(supporting),
            counter_evidence=tuple(counter),
            prerequisite_gate_passed=gate_passed,
            prerequisite_gate_reason=gate_reason,
            direct_support_exists=any((ref.source_class in DIRECT_SOURCE_CLASSES) for ref in valid_support),
            independent_supporting_cluster_count=min(len(clusters), 2),
            max_supporting_critic_relevance=max((ref.relevance for ref in valid_support), default=0.0),
            source_support_degradation_count=sum(1 for value in flags.values() if value),
            max_counter_evidence_relevance=max(
                (
                    ref.relevance for ref in counter
                    if ref.relevance > 0.5 and ref.temporal_match and cutoff is not None
                    and ref.available_at is not None and ref.available_at <= cutoff
                ),
                default=0.0,
            ),
            is_novel=any(ref.is_novel for ref in valid_support),
            source_support_flags=flags,
            validation_violations=tuple(sorted(set(draft_violations))),
        ))
        violations.extend(draft_violations)
    passed_causes = {
        hypothesis.cause_label
        for hypothesis in hypotheses
        if hypothesis.prerequisite_gate_passed and hypothesis.cause_label not in {"mixed", "unexplained"}
    }
    for index, hypothesis in enumerate(hypotheses):
        if hypothesis.cause_label not in {"mixed", "unexplained"}:
            continue
        gate_passed, gate_reason = check_prerequisite(
            cause=hypothesis.cause_label,
            context={
                "gate_passed_causes": tuple(sorted(passed_causes)),
                "context_quality_ok": bool(state.get("context_artifact")),
                "any_other_gate_passed": bool(passed_causes),
            },
            evidence=[],
            edges={},
        )
        hypotheses[index] = hypothesis.model_copy(update={
            "prerequisite_gate_passed": gate_passed,
            "prerequisite_gate_reason": gate_reason,
        })
    return hypotheses, sorted(set(violations)), cutoff


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value[:10]
    try:
        return datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError:
        return None


def _validation_failure(state: AttributionState) -> str | None:
    lookup = _evidence_lookup(state)
    causes = state.get("causes", [])

    for cause in causes:
        for evidence_id in cause.get("evidence_ids", []):
            if evidence_id not in lookup:
                return "evidence_id_missing"

    cutoff = state.get("cutoff")
    for cause in causes:
        for evidence_id in cause.get("evidence_ids", []):
            evidence_dt = _parse_date(
                lookup[evidence_id].get("reference_date") or lookup[evidence_id].get("published_utc")
            )
            if cutoff and (evidence_dt is None or str(lookup[evidence_id].get("available_at") or lookup[evidence_id].get("reference_date")) > cutoff):
                return "time_window_violation"

    try:
        ValidatedJudgeOutput.model_validate(
            {
                "causes": causes,
                "summary_md": state.get("summary_md", ""),
            }
        )
    except ValidationError:
        return "schema_invalid"

    decision = state.get("critic_decision")
    if decision and _status_from_state(state) == OutputStatus.SUFFICIENT and decision.magnitude_coverage < _effective_m_threshold():
        return "magnitude_sanity_failed"

    return None


def _correction_prompt(state: AttributionState, failure: str) -> str:
    evidence_lines = []
    for chunk in state.get("reranked_chunks", []):
        evidence_lines.append(
            f"- {chunk.get('asset_id', '')}: date={chunk.get('reference_date', chunk.get('published_utc', 'unknown'))}"
        )

    return (
        "Return corrected JSON only.\n"
        "Schema: {\"causes\": [...], \"summary_md\": \"...\", \"self_grounding_check\": {...}}\n"
        f"Ticker: {state['ticker']}\n"
        f"Trade date: {state['trade_date']}\n"
        f"Validation failure: {failure}\n"
        "Available evidence ids:\n"
        f"{chr(10).join(evidence_lines)}\n"
        "Current output:\n"
        f"causes={state.get('causes', [])}\n"
        f"summary_md={state.get('summary_md', '')}\n"
        "Fix the output so all cited evidence ids exist, dates stay within the allowed window, "
        "and the payload matches the schema."
    )


def _downgraded_status(failure: str) -> OutputStatus:
    if failure == "model_timeout":
        return OutputStatus.SYSTEM_ERROR
    return OutputStatus.PARTIAL


def validator(state: AttributionState, *, llm: Any = None, cutoff_policy: Any = None) -> dict:
    """Validate Judge output and optionally request one correction pass."""
    if state.get("hypothesis_drafts"):
        try:
            hypotheses, violations, cutoff = _enrich_drafts(state, cutoff_policy=cutoff_policy)
        except Exception:
            hypotheses, violations, cutoff = [], ["schema_invalid"], state.get("cutoff")
        validator_attempts = 0
        repair_count = 0
        summary_md = state.get("summary_md", "")
        if violations and llm is not None:
            validator_attempts = 1
            try:
                response = llm.invoke(
                    "Return corrected JSON using the Judge hypothesis schema only. "
                    f"Validation violations: {sorted(set(violations))}. "
                    f"Available evidence ids: {sorted(_evidence_lookup(state))}."
                )
                parsed = _parse_judge_response(str(response.content))
                track_cost(state, "validator", response)
                repaired_state = {
                    **state,
                    "hypothesis_drafts": parsed["hypotheses"],
                    "summary_md": parsed["summary_md"],
                    "cutoff": cutoff,
                }
                hypotheses, violations, _ = _enrich_drafts(repaired_state, cutoff_policy=None)
                summary_md = parsed["summary_md"]
                repair_count = 1
            except Exception:
                violations = sorted(set([*violations, "repair_failed"]))
        status = determine_status(
            hypotheses,
            cutoff_violations=violations.count("cutoff_violation"),
            citation_all_resolve="evidence_id_missing" not in violations,
            coverage_degraded=bool(state.get("is_degraded", False)),
            context_quality_ok=bool(state.get("context_artifact")),
            error_occurred=bool(state.get("error_type") == "system_error"),
        )
        return {
            "hypotheses": [h.model_dump(mode="json") for h in hypotheses],
            "causes": [
                {"text": h.transmission_mechanism, "category": h.cause_label, "evidence_ids": list(h.supporting_evidence_ids), "direction": h.direction}
                for h in hypotheses
            ],
            "summary_md": summary_md,
            "output_status": status,
            "validation_error": violations[0] if violations else None,
            "validator_attempts": validator_attempts,
            "repair_count": repair_count,
            "source_support_flags": {
                name: any(h.source_support_flags.get(name, False) for h in hypotheses)
                for name in ("opinion_only_support", "unknown_origin_support", "issuer_claim_only_support")
            },
            "validator_cutoff": cutoff,
            "cost_breakdown": state.get("cost_breakdown", []),
            "total_cost_usd": state.get("total_cost_usd"),
            "cost_status": state.get("cost_status", "unknown"),
            "total_tokens": state.get("total_tokens", 0),
            "phase": Phase.VALIDATOR,
        }

    failure = _validation_failure(state)
    if failure is None:
        return {
            "causes": state.get("causes", []),
            "summary_md": state.get("summary_md", ""),
            "output_status": _status_from_state(state),
            "validation_error": None,
            "validator_attempts": 0,
            "cost_breakdown": state.get("cost_breakdown", []),
            "total_cost_usd": state.get("total_cost_usd", 0.0),
            "total_tokens": state.get("total_tokens", 0),
            "phase": Phase.VALIDATOR,
        }

    if llm is None:
        return {
            "output_status": _downgraded_status(failure),
            "validation_error": failure,
            "validator_attempts": 0,
            "phase": Phase.VALIDATOR,
        }

    try:
        response, parsed = invoke_with_retries(
            llm,
            _correction_prompt(state, failure),
            parse_fn=_parse_judge_response,
            node_name="Validator",
        )
        track_cost(state, "validator", response)
        corrected_state = {
            **state,
            "causes": parsed.get("causes", []),
            "summary_md": parsed.get("summary_md", ""),
        }
        second_failure = _validation_failure(corrected_state)
        if second_failure is None:
            return {
                "causes": corrected_state["causes"],
                "summary_md": corrected_state["summary_md"],
                "output_status": _status_from_state(corrected_state),
                "validation_error": None,
                "validator_attempts": 1,
                "validator_raw_llm_response": str(response.content),
                "cost_breakdown": state.get("cost_breakdown", []),
                "total_cost_usd": state.get("total_cost_usd", 0.0),
                "total_tokens": state.get("total_tokens", 0),
                "phase": Phase.VALIDATOR,
            }

        return {
            "causes": corrected_state["causes"],
            "summary_md": corrected_state["summary_md"],
            "output_status": _downgraded_status(second_failure),
            "validation_error": second_failure,
            "validator_attempts": 1,
            "validator_raw_llm_response": str(response.content),
            "cost_breakdown": state.get("cost_breakdown", []),
            "total_cost_usd": state.get("total_cost_usd", 0.0),
            "total_tokens": state.get("total_tokens", 0),
            "phase": Phase.VALIDATOR,
        }
    except RuntimeError:
        return {
            "output_status": OutputStatus.SYSTEM_ERROR,
            "validation_error": "model_timeout",
            "validator_attempts": 1,
            "cost_breakdown": state.get("cost_breakdown", []),
            "total_cost_usd": state.get("total_cost_usd", 0.0),
            "total_tokens": state.get("total_tokens", 0),
            "phase": Phase.VALIDATOR,
        }
