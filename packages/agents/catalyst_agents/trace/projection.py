"""Project node state/results into artifact payloads suitable for persistence."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from typing import Any


_NODE_ARTIFACT_TYPES: dict[str, list[str]] = {
    "context_builder": ["context_artifact", "state_snapshot"],
    "miner": ["arm_b_evidence", "retrieved_chunks", "reranked_chunks", "state_snapshot"],
    "critic": ["graded_evidence", "all_graded_chunks", "critic_decision", "raw_llm_response", "state_snapshot"],
    "decision_router": ["state_snapshot"],
    "expand_macro": ["state_snapshot"],
    "judge": ["judge_causes", "judge_summary", "judge_evidence", "raw_llm_response", "state_snapshot"],
    "validator": ["validator_decision", "raw_llm_response", "state_snapshot"],
    "finalizer": ["state_snapshot"],
    "insufficient_handler": ["state_snapshot"],
    "system_error_handler": ["error_snapshot"],
    "baseline_prepare_evidence": ["state_snapshot"],
}

_STATE_EXCLUDE_KEYS = {
    "table",
    "embedding_fn",
    "reranker",
    "retrieval_metadata",
    "retrieved_chunks",
    "reranked_chunks",
    "graded_evidence",
    "all_graded_chunks",
    "causes",
    "summary_md",
    "critic_raw_llm_response",
    "judge_raw_llm_response",
    "validator_raw_llm_response",
}
_STATE_COMPACT_KEYS = {
    "phase",
    "output_status",
    "validation_error",
    "error_type",
    "router_edge",
    "router_reason",
    "expansions_used",
    "max_expansions",
    "current_layer",
    "grounding_rate",
    "total_cost_usd",
    "total_tokens",
    "validator_attempts",
    "critic_reasoning",
}
_MAX_RETRIEVED = 20
_MAX_RERANKED = 8
_MAX_SNIPPET = 200
_MAX_RAW_RESPONSE = 4000


def _is_jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _headline_and_snippet(content_md: str) -> tuple[str, str]:
    lines = (content_md or "").splitlines()
    headline = ""
    if lines and lines[0].startswith("## "):
        first = lines[0][3:].strip()
        headline = first.split(":", 1)[1].strip() if ":" in first else first
    body = "\n".join(lines[1:] if headline else lines).strip()
    snippet = body[:_MAX_SNIPPET]
    return headline, snippet


def _project_chunk(chunk: dict[str, Any], *, include_rerank: bool) -> dict[str, Any]:
    headline, snippet = _headline_and_snippet(str(chunk.get("content_md", "") or ""))
    payload = {
        "rank": chunk.get("rank"),
        "asset_id": chunk.get("asset_id"),
        "ticker": chunk.get("ticker"),
        "source_type": chunk.get("source_type"),
        "reference_date": chunk.get("reference_date"),
        "score": chunk.get("rrf_score"),
        "headline": headline,
        "snippet": snippet,
    }
    if include_rerank:
        payload["rerank_score"] = chunk.get("rerank_score")
    return payload


def _project_graded_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": item.get("chunk_id"),
        "relevance": item.get("relevance"),
        "category": item.get("category"),
        "temporal_match": bool(item.get("temporal_match")),
        "reasoning": item.get("reasoning"),
        "event_specificity": item.get("event_specificity"),
        "temporal_alignment": item.get("temporal_alignment"),
        "evidence_granularity": item.get("evidence_granularity"),
        "conflict_signal": item.get("conflict_signal"),
        "original_relevance": item.get("original_relevance"),
    }


def _clean_value(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            cleaned = _clean_value(v)
            if _is_jsonable(cleaned):
                out[k] = cleaned
        return out
    if isinstance(value, list):
        return [_clean_value(v) for v in value if _is_jsonable(_clean_value(v))]
    if _is_jsonable(value):
        return value
    return None


def _state_snapshot(merged_state: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "retrieved_chunk_count": len(merged_state.get("retrieved_chunks", []) or []),
        "reranked_chunk_count": len(merged_state.get("reranked_chunks", []) or []),
        "graded_evidence_count": len(merged_state.get("graded_evidence", []) or []),
        "all_graded_chunks_count": len(merged_state.get("all_graded_chunks", []) or []),
        "causes_count": len(merged_state.get("causes", []) or []),
    }
    for key in _STATE_COMPACT_KEYS:
        if key not in merged_state:
            continue
        cleaned = _clean_value(merged_state.get(key))
        if cleaned is not None and _is_jsonable(cleaned):
            snapshot[key] = cleaned
    if merged_state.get("critic_decision") is not None:
        snapshot["critic_decision"] = _clean_value(merged_state["critic_decision"])
    if merged_state.get("retrieval_metadata") is not None:
        metadata = merged_state["retrieval_metadata"]
        snapshot["retrieval_metadata"] = {
            "layers_attempted": [
                getattr(layer, "value", str(layer))
                for layer in getattr(metadata, "layers_attempted", [])
            ],
            "stop_reason": getattr(metadata, "stop_reason", None),
            "expansion_reasons": list(getattr(metadata, "expansion_reasons", [])),
        }
    # Include arm_b_evidence_sha256 in state snapshot for observability
    if merged_state.get("arm_b_evidence") is not None:
        abe = merged_state["arm_b_evidence"]
        if isinstance(abe, dict) and abe.get("sha256"):
            snapshot["arm_b_evidence_sha256"] = abe["sha256"]
    # Include judge_evidence_sha256 in state snapshot for observability
    if merged_state.get("judge_evidence_sha256") is not None:
        snapshot["judge_evidence_sha256"] = merged_state["judge_evidence_sha256"]
    return {"state": snapshot}


def _project_payload(artifact_type: str, merged_state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if artifact_type == "arm_b_evidence":
        abe = merged_state.get("arm_b_evidence")
        if abe is None:
            return {"per_asset": {}, "sha256": ""}
        return {
            "per_asset": abe.get("per_asset", {}),
            "sha256": abe.get("sha256", ""),
        }
    if artifact_type == "context_artifact":
        return {"artifact": _clean_value(merged_state.get("context_artifact", {})), "sha256": merged_state.get("context_artifact_sha256")}
    if artifact_type == "retrieved_chunks":
        chunks = merged_state.get("retrieved_chunks", [])[:_MAX_RETRIEVED]
        return {"chunks": [_project_chunk(chunk, include_rerank=False) for chunk in chunks]}
    if artifact_type == "reranked_chunks":
        chunks = merged_state.get("reranked_chunks", [])[:_MAX_RERANKED]
        return {"chunks": [_project_chunk(chunk, include_rerank=True) for chunk in chunks]}
    if artifact_type == "graded_evidence":
        items = merged_state.get("graded_evidence", [])
        return {"items": [_project_graded_item(item) for item in items]}
    if artifact_type == "all_graded_chunks":
        items = merged_state.get("all_graded_chunks", [])
        return {"items": [_project_graded_item(item) for item in items]}
    if artifact_type == "critic_decision":
        decision = merged_state.get("critic_decision")
        return {"decision": _clean_value(decision) if decision is not None else None}
    if artifact_type == "raw_llm_response":
        raw = (
            result.get("critic_raw_llm_response")
            or result.get("judge_raw_llm_response")
            or result.get("validator_raw_llm_response")
            or ""
        )
        return {"text": str(raw)[:_MAX_RAW_RESPONSE]}
    if artifact_type == "judge_causes":
        return {"causes": _clean_value(merged_state.get("causes", []))}
    if artifact_type == "judge_summary":
        return {"summary_md": merged_state.get("summary_md", ""), "grounding_rate": merged_state.get("grounding_rate")}
    if artifact_type == "judge_evidence":
        je = merged_state.get("judge_evidence")
        sha = merged_state.get("judge_evidence_sha256")
        return {
            "per_asset": je if je is not None else {},
            "sha256": sha if sha is not None else "",
        }
    if artifact_type == "validator_decision":
        return {
            "output_status": str(merged_state.get("output_status")) if merged_state.get("output_status") is not None else None,
            "validation_error": merged_state.get("validation_error"),
            "validator_attempts": merged_state.get("validator_attempts"),
        }
    if artifact_type == "error_snapshot":
        return {
            "error_type": merged_state.get("error_type"),
            "validation_error": merged_state.get("validation_error"),
            "summary_md": merged_state.get("summary_md"),
        }
    if artifact_type == "state_snapshot":
        return _state_snapshot(merged_state)
    return {}


def project_node_artifacts(node_name: str, merged_state: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return artifact payloads owned by one node."""
    artifact_types = _NODE_ARTIFACT_TYPES.get(node_name, [])
    artifacts: list[dict[str, Any]] = []
    for artifact_type in artifact_types:
        payload = _project_payload(artifact_type, merged_state, result)
        artifacts.append({"artifact_type": artifact_type, "payload_json": payload})
    return artifacts
