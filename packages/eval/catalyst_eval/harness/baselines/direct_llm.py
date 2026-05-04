"""Direct-LLM baseline for eval comparisons."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any, MutableMapping

from pydantic import BaseModel, Field

from catalyst_eval.schema.result import PredictedCause


DEFAULT_DIRECT_LLM_MODEL = "claude-sonnet-4-6"
FALLBACK_DIRECT_LLM_MODEL = "claude-haiku-4-5"
DIRECT_LLM_COST_CAP_USD = 20.0
DIRECT_LLM_PROJECTION_SAFETY_FACTOR = 1.5
DIRECT_LLM_WARMUP_CASES = 2
DIRECT_LLM_FULL_RUN_CASES = 10

MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.8, "output": 4.0},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.8, "output": 4.0},
}


class DirectLLMResponse(BaseModel):
    causes: list[PredictedCause] = Field(default_factory=list)
    summary_md: str = ""


def _build_prompt(case: dict[str, Any]) -> str:
    query = case.get("query") or f"Explain the price move for {case['ticker']} on {case['trade_date']}."
    return (
        "Return JSON only.\n"
        "Schema: {\"causes\": [{\"text\": str, \"category\": str, \"confidence\": float, "
        "\"evidence_ids\": [str], \"direction\": str}], \"summary_md\": str}\n"
        f"Ticker: {case['ticker']}\n"
        f"Trade date: {case['trade_date']}\n"
        f"Price move pct: {case.get('price_move_pct')}\n"
        f"Question: {query}\n"
        "Use evidence_ids only if you are confident they map to corpus asset IDs."
    )


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```json"):
        stripped = stripped[7:]
    elif stripped.startswith("```"):
        stripped = stripped[3:]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def _parse_response(text: str) -> DirectLLMResponse:
    payload = json.loads(_strip_json_fences(text))
    return DirectLLMResponse.model_validate(payload)


def _build_default_llm(model_id: str) -> Any:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:  # pragma: no cover - exercised via injected test llm
        raise RuntimeError("langchain-anthropic is required when llm is not injected") from exc

    return ChatAnthropic(model=model_id)


def _usage_value(response: Any, field: str) -> int:
    usage = getattr(response, "usage", None)
    return int(getattr(usage, field, 0) or 0)


def _compute_cost(model_id: str, tokens_in: int, tokens_out: int) -> float:
    pricing = MODEL_PRICING.get(model_id)
    if pricing is None:
        return 0.0
    return (tokens_in * pricing["input"] + tokens_out * pricing["output"]) / 1_000_000


def _fetch_evidence(db_path: Path, evidence_ids: list[str]) -> list[dict[str, Any]]:
    if not evidence_ids:
        return []

    conn = sqlite3.connect(str(db_path))
    placeholders = ",".join("?" for _ in evidence_ids)
    rows = conn.execute(
        f"""
        SELECT asset_id, content_md, source_type
        FROM clean_assets
        WHERE asset_id IN ({placeholders})
        ORDER BY asset_id ASC
        """,
        evidence_ids,
    ).fetchall()
    conn.close()

    return [
        {
            "asset_id": row[0],
            "content_md": row[1],
            "source_type": row[2],
            "rrf_score": 0.0,
        }
        for row in rows
    ]


def _sanitize_causes(causes: list[PredictedCause], valid_ids: set[str]) -> tuple[list[dict[str, Any]], bool]:
    sanitized: list[dict[str, Any]] = []
    missing_evidence = False

    for cause in causes:
        valid_evidence_ids = [evidence_id for evidence_id in cause.evidence_ids if evidence_id in valid_ids]
        if len(valid_evidence_ids) != len(cause.evidence_ids):
            missing_evidence = True
        sanitized.append(
            {
                "text": cause.text,
                "category": cause.category,
                "confidence": cause.confidence,
                "evidence_ids": valid_evidence_ids,
                "direction": cause.direction,
            }
        )

    return sanitized, missing_evidence


def _system_error_output(
    case: dict[str, Any],
    *,
    model_id: str,
    latency_ms: int,
    tokens_in: int,
    tokens_out: int,
    error_type: str,
    error_message: str,
) -> dict[str, Any]:
    total_tokens = tokens_in + tokens_out
    cost_usd = _compute_cost(model_id, tokens_in, tokens_out)
    return {
        "ticker": case["ticker"],
        "trade_date": case["trade_date"],
        "status": "SYSTEM_ERROR",
        "output_status": "SYSTEM_ERROR",
        "summary": f"System error: {error_message}",
        "summary_md": f"System error: {error_message}",
        "causes": [],
        "retrieved_evidence": [],
        "retrieval_metadata": {"layers_used": [], "expansion_count": 0, "mode": "direct_llm"},
        "trace_id": None,
        "validation_error": error_type,
        "error_type": error_type,
        "model_id": model_id,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "total_tokens": total_tokens,
        "cost_breakdown": [
            {
                "node": "direct_llm",
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "model_id": model_id,
                "cost_usd": cost_usd,
            }
        ],
        "total_cost_usd": cost_usd,
    }


def run_direct_llm(
    case: dict[str, Any],
    model_id: str,
    db_path: Path,
    *,
    llm: Any = None,
) -> dict[str, Any]:
    """Run the direct baseline on one case against a caller-supplied DB path."""
    started = time.perf_counter()
    llm = llm or _build_default_llm(model_id)

    try:
        response = llm.invoke(_build_prompt(case))
    except Exception as exc:
        return _system_error_output(
            case,
            model_id=model_id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            tokens_in=0,
            tokens_out=0,
            error_type="model_failure",
            error_message=str(exc),
        )

    tokens_in = _usage_value(response, "input_tokens")
    tokens_out = _usage_value(response, "output_tokens")
    latency_ms = int((time.perf_counter() - started) * 1000)

    try:
        parsed = _parse_response(response.content)
    except Exception as exc:
        return _system_error_output(
            case,
            model_id=model_id,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            error_type="model_failure",
            error_message=f"parse_failed: {exc}",
        )

    evidence_ids = sorted({evidence_id for cause in parsed.causes for evidence_id in cause.evidence_ids})
    retrieved_evidence = _fetch_evidence(Path(db_path), evidence_ids)
    valid_ids = {entry["asset_id"] for entry in retrieved_evidence}
    causes, missing_evidence = _sanitize_causes(parsed.causes, valid_ids)

    cost_usd = _compute_cost(model_id, tokens_in, tokens_out)
    if not causes:
        output_status = "INSUFFICIENT"
        validation_error = None
    elif missing_evidence:
        output_status = "PARTIAL"
        validation_error = "evidence_id_missing"
    else:
        output_status = "SUFFICIENT"
        validation_error = None
    total_tokens = tokens_in + tokens_out

    return {
        "ticker": case["ticker"],
        "trade_date": case["trade_date"],
        "status": output_status,
        "output_status": output_status,
        "summary": parsed.summary_md,
        "summary_md": parsed.summary_md,
        "causes": causes,
        "retrieved_evidence": retrieved_evidence,
        "retrieval_metadata": {"layers_used": [], "expansion_count": 0, "mode": "direct_llm"},
        "trace_id": None,
        "validation_error": validation_error,
        "error_type": None,
        "model_id": model_id,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "total_tokens": total_tokens,
        "cost_breakdown": [
            {
                "node": "direct_llm",
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "model_id": model_id,
                "cost_usd": cost_usd,
            }
        ],
        "total_cost_usd": cost_usd,
    }


def apply_direct_llm_cost_guard(
    *,
    current_model_id: str,
    observed_costs: list[float],
    header: MutableMapping[str, Any],
) -> str:
    """Apply the OD-6 projected-cost guard after the first two direct-baseline cases."""
    if current_model_id != DEFAULT_DIRECT_LLM_MODEL:
        return current_model_id
    if len(observed_costs) < DIRECT_LLM_WARMUP_CASES:
        return current_model_id

    observed_total = sum(observed_costs[:DIRECT_LLM_WARMUP_CASES])
    projected_total = (
        observed_total
        * (DIRECT_LLM_FULL_RUN_CASES / DIRECT_LLM_WARMUP_CASES)
        * DIRECT_LLM_PROJECTION_SAFETY_FACTOR
    )
    if projected_total <= DIRECT_LLM_COST_CAP_USD:
        return current_model_id

    header["direct_llm_model_substitution"] = {
        "from": DEFAULT_DIRECT_LLM_MODEL,
        "to": FALLBACK_DIRECT_LLM_MODEL,
        "projected_total_cost_usd": projected_total,
    }
    return FALLBACK_DIRECT_LLM_MODEL
