"""Validator node — enforce output grounding and schema checks before finalization."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from catalyst_agents.backoff import invoke_with_retries
from catalyst_agents.cost_tracker import track_cost
from catalyst_agents.nodes.critic import M_THRESHOLD
from catalyst_agents.nodes.judge import _parse_judge_response
from catalyst_agents.nodes.miner import DATE_WINDOW_DAYS, _compute_date_range
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
        return OutputStatus.SUFFICIENT if state.get("causes") else OutputStatus.INSUFFICIENT

    if decision.sufficiency == "sufficient":
        return OutputStatus.SUFFICIENT
    if decision.sufficiency == "partial":
        return OutputStatus.PARTIAL
    return OutputStatus.INSUFFICIENT


def _evidence_lookup(state: AttributionState) -> dict[str, dict]:
    chunks = state.get("reranked_chunks") or state.get("retrieved_chunks") or []
    return {chunk.get("asset_id", ""): chunk for chunk in chunks if chunk.get("asset_id")}


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

    start, end = _compute_date_range(state["trade_date"], window_days=DATE_WINDOW_DAYS)
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    for cause in causes:
        for evidence_id in cause.get("evidence_ids", []):
            evidence_dt = _parse_date(
                lookup[evidence_id].get("reference_date") or lookup[evidence_id].get("published_utc")
            )
            if evidence_dt is None or evidence_dt < start_dt or evidence_dt > end_dt:
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


def validator(state: AttributionState, *, llm: Any = None) -> dict:
    """Validate Judge output and optionally request one correction pass."""
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
