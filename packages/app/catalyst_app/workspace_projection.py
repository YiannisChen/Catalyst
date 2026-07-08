"""Project live-run artifacts into a structured WorkspaceResponse.

Joins retrieved_chunks, reranked_chunks, graded_evidence, all_graded_chunks,
judge_causes, judge_summary, and validator_decision into a single coherent
workspace view for the v4 workbench UI.

Does not fabricate metrics.  Preserves original cause confidence values.
"""

from __future__ import annotations

import json
from typing import Any


# ── Stage labels for v4 presentation ──
_NODE_LABELS: dict[str, str] = {
    "miner": "Retriever",
    "critic": "Critic",
    "decision_router": "Readiness",
    "expand_macro": "Macro Expansion",
    "judge": "Attribution Model",
    "validator": "Validator",
    "finalizer": "Finalizer",
    "insufficient_handler": "Insufficient Handler",
    "system_error_handler": "Error Handler",
}

# Nodes that count as "resolved"
_RESOLVED_STATUSES = {"complete", "warning", "skipped", "error"}


def project_workspace(
    run_summary: dict[str, Any],
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a workspace projection from run summary, events, and artifacts."""

    run_id = run_summary.get("run_id", "")
    status = run_summary.get("status", "UNKNOWN")
    ticker = run_summary.get("ticker")
    trade_date = run_summary.get("trade_date")
    model_id = run_summary.get("model_id")
    started_at = run_summary.get("started_at")
    ended_at = run_summary.get("ended_at")

    # Compute runtime from timestamps
    runtime_ms = _compute_runtime_ms(started_at, ended_at)

    # Build stages from events
    stages = _build_stages(events, artifacts)

    # Build evidence by joining artifacts
    evidence, cited_ids = _build_evidence(artifacts)

    # Build result from judge artifacts
    result = _build_result(artifacts, cited_ids)

    # Build diagnostics
    diagnostics = _build_diagnostics(events, artifacts, cited_ids)

    return {
        "run_id": run_id,
        "status": status,
        "ticker": ticker,
        "trade_date": trade_date,
        "model_id": model_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "runtime_ms": runtime_ms,
        "last_completed_node": run_summary.get("last_completed_node"),
        "predicted_next_node": run_summary.get("predicted_next_node"),
        "result": result,
        "evidence": evidence,
        "stages": stages,
        "diagnostics": diagnostics,
        "failure": _build_failure(run_summary),
    }


def _compute_runtime_ms(started_at: str | None, ended_at: str | None) -> int | None:
    """Compute runtime from run timestamps, not max(stage durations)."""
    if not started_at or not ended_at:
        return None
    try:
        from datetime import datetime, timezone
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start = datetime.strptime(started_at, fmt).replace(tzinfo=timezone.utc)
        end = datetime.strptime(ended_at, fmt).replace(tzinfo=timezone.utc)
        return int((end - start).total_seconds() * 1000)
    except (ValueError, OSError):
        pass
    return None


def _build_stages(
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build stages from events, grouping artifact types per node."""
    # Map node -> artifact types
    node_artifact_types: dict[str, set[str]] = {}
    for a in artifacts:
        node = a.get("node", "")
        atype = a.get("artifact_type", "")
        if node not in node_artifact_types:
            node_artifact_types[node] = set()
        node_artifact_types[node].add(atype)

    # Derive a summary note from the most informative artifact
    artifact_summaries = _collect_artifact_summaries(artifacts)

    stages = []
    for evt in events:
        node = evt.get("node", "")
        status_after = evt.get("status_after")
        # Map trace status to stage status
        stage_status = _map_stage_status(status_after, node, events)

        stages.append({
            "id": node,
            "label": _NODE_LABELS.get(node, node),
            "status": stage_status,
            "started_at": evt.get("started_at"),
            "ended_at": evt.get("ended_at"),
            "duration_ms": evt.get("latency_ms"),
            "input_tokens": evt.get("input_tokens"),
            "output_tokens": evt.get("output_tokens"),
            "cost_usd": evt.get("cost_usd"),
            "summary": artifact_summaries.get(node),
            "artifact_types": sorted(node_artifact_types.get(node, set())),
        })

    return stages


def _map_stage_status(
    status_after: str | None,
    node: str,
    events: list[dict[str, Any]],
) -> str:
    """Map trace event status_after to a v4 stage status."""
    if status_after is None:
        # Not yet started — check if any event after this one has started
        node_indices = [i for i, e in enumerate(events) if e.get("node") == node]
        for idx in node_indices:
            evt = events[idx]
            if evt.get("started_at"):
                # Has started but no status_after means still running
                # Check if a later event exists
                if idx < len(events) - 1 and events[idx + 1].get("started_at"):
                    return "complete"
                return "active"
        return "pending"

    mapping = {
        "SUFFICIENT": "complete",
        "SUCCEEDED": "complete",
        "PARTIAL": "warning",
        "INSUFFICIENT": "skipped",
        "FAILED_SYSTEM": "error",
        "FAILED_REQUEST": "error",
        "SYSTEM_ERROR": "error",
        "RUNNING": "active",
        "QUEUED": "pending",
    }
    return mapping.get(status_after, "pending")


def _collect_artifact_summaries(
    artifacts: list[dict[str, Any]],
) -> dict[str, str]:
    """Extract a short summary from the most relevant artifact per node."""
    summaries: dict[str, str] = {}
    for a in artifacts:
        node = a.get("node", "")
        if node in summaries:
            continue  # first one wins per node
        payload = a.get("payload_json", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        atype = a.get("artifact_type", "")
        summary = _extract_artifact_summary(atype, payload)
        if summary:
            summaries[node] = summary
    return summaries


def _extract_artifact_summary(atype: str, payload: dict[str, Any]) -> str | None:
    """Extract a one-line summary from an artifact payload."""
    if atype == "retrieved_chunks":
        chunks = payload.get("chunks", [])
        return f"Retrieved {len(chunks)} candidate chunks"
    if atype == "reranked_chunks":
        chunks = payload.get("chunks", [])
        return f"Reranked {len(chunks)} chunks"
    if atype == "graded_evidence":
        items = payload.get("items", [])
        accepted = sum(1 for i in items if i.get("relevance", 0) >= 0.7)
        return f"Graded {len(items)} items, {accepted} accepted"
    if atype == "critic_decision":
        decision = payload.get("decision", "")
        return f"Decision: {decision}"[:80]
    if atype == "judge_causes":
        causes = payload.get("causes", [])
        return f"Generated {len(causes)} attributed causes"
    if atype == "judge_summary":
        rate = payload.get("grounding_rate", 0)
        return f"Grounding rate: {rate}"
    if atype == "validator_decision":
        status = payload.get("output_status", "unknown")
        return f"Validation: {status}"
    return None


def _build_evidence(
    artifacts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Build evidence items by joining retrieval, reranking, and grading artifacts."""

    # Parse all artifacts
    parsed = {}
    for a in artifacts:
        payload = a.get("payload_json", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        key = (a.get("node", ""), a.get("artifact_type", ""))
        parsed[key] = payload

    # Get cited IDs from judge_causes
    cited_ids: set[str] = set()
    judge_causes = parsed.get(("judge", "judge_causes"), {})
    for cause in judge_causes.get("causes", []):
        for eid in cause.get("evidence_ids", []):
            cited_ids.add(eid)

    # Build evidence index from retrieved chunks (has asset_id)
    retrieved = parsed.get(("miner", "retrieved_chunks"), {})
    reranked = parsed.get(("miner", "reranked_chunks"), {})
    graded = parsed.get(("critic", "graded_evidence"), {})
    all_graded = parsed.get(("critic", "all_graded_chunks"), {})

    # Index chunks by asset_id
    chunk_index: dict[str, dict[str, Any]] = {}
    for chunk in retrieved.get("chunks", []):
        aid = chunk.get("asset_id", "")
        if aid:
            chunk_index[aid] = {
                "id": aid,
                "ticker": chunk.get("ticker"),
                "headline": chunk.get("headline") or "",
                "snippet": chunk.get("snippet") or "",
                "source_type": chunk.get("source_type"),
                "reference_date": chunk.get("reference_date"),
                "retrieval_score": chunk.get("score"),
                "rerank_score": None,
                "cited": aid in cited_ids,
            }

    # Merge rerank scores
    for chunk in reranked.get("chunks", []):
        aid = chunk.get("asset_id", "")
        if aid and aid in chunk_index:
            chunk_index[aid]["rerank_score"] = chunk.get("rerank_score") or chunk.get("score")

    # Merge graded evidence
    for item in graded.get("items", []):
        cid = item.get("chunk_id", "")
        if cid and cid in chunk_index:
            chunk_index[cid].update({
                "critic_relevance": item.get("relevance"),
                "critic_category": item.get("category"),
                "critic_reasoning": item.get("reasoning"),
                "temporal_match": item.get("temporal_match"),
                "event_specificity": item.get("event_specificity"),
                "temporal_alignment": item.get("temporal_alignment"),
                "evidence_granularity": item.get("evidence_granularity"),
                "conflict_signal": item.get("conflict_signal"),
            })
            # Read critic_decision directly from the Critic node output.
            # The Critic now sets this field explicitly per item.
            critic_decision = item.get("critic_decision", "ungraded")
            chunk_index[cid]["critic_decision"] = critic_decision

    # Also include items from all_graded_chunks that weren't in graded_evidence
    for item in all_graded.get("items", []):
        cid = item.get("chunk_id", "")
        if cid and cid not in chunk_index:
            chunk_index[cid] = {
                "id": cid,
                "snippet": item.get("snippet", ""),
                "critic_relevance": item.get("relevance"),
                "critic_category": item.get("category"),
                "critic_decision": item.get("critic_decision", "ungraded"),
                "cited": cid in cited_ids,
            }

    return list(chunk_index.values()), cited_ids


def _build_result(
    artifacts: list[dict[str, Any]],
    cited_ids: set[str],
) -> dict[str, Any] | None:
    """Build the result section from judge_causes, judge_summary, and validator_decision."""

    parsed = {}
    for a in artifacts:
        payload = a.get("payload_json", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        key = (a.get("node", ""), a.get("artifact_type", ""))
        parsed[key] = payload

    judge_causes = parsed.get(("judge", "judge_causes"), {})
    judge_summary = parsed.get(("judge", "judge_summary"), {})
    validator = parsed.get(("validator", "validator_decision"), {})

    causes = []
    for cause in judge_causes.get("causes", []):
        causes.append({
            "text": cause.get("text", ""),
            "category": cause.get("category"),
            "direction": cause.get("direction"),
            "confidence": cause.get("confidence"),
            "evidence_ids": cause.get("evidence_ids", []),
        })

    result = {
        "output_status": validator.get("output_status"),
        "summary_md": judge_summary.get("summary_md"),
        "grounding_rate": judge_summary.get("grounding_rate"),
        "causes": causes,
        "validation_error": validator.get("validation_error"),
        "validator_attempts": validator.get("validator_attempts"),
    }

    # If no result data at all, return None
    if not any([result["causes"], result["summary_md"], result["output_status"]]):
        return None

    return result


def _build_diagnostics(
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    cited_ids: set[str],
) -> dict[str, Any]:
    """Build diagnostics summary."""
    resolved = sum(1 for e in events if _map_stage_status(e.get("status_after"), e.get("node", ""), events) in _RESOLVED_STATUSES)

    total_tokens = 0
    total_cost = 0.0
    for e in events:
        total_tokens += (e.get("input_tokens") or 0) + (e.get("output_tokens") or 0)
        total_cost += e.get("cost_usd") or 0.0

    # Count evidence
    retrieved_count = 0
    reranked_count = 0
    graded_count = 0
    top_score = None
    for a in artifacts:
        payload = a.get("payload_json", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        atype = a.get("artifact_type", "")
        if atype == "retrieved_chunks":
            retrieved_count = len(payload.get("chunks", []))
        elif atype == "reranked_chunks":
            reranked_count = len(payload.get("chunks", []))
        elif atype == "graded_evidence":
            items = payload.get("items", [])
            graded_count = len(items)
            for item in items:
                rel = item.get("relevance")
                if rel is not None and (top_score is None or rel > top_score):
                    top_score = rel

    return {
        "resolved_stage_count": resolved,
        "total_stage_count": len(events),
        "retrieved_count": retrieved_count,
        "reranked_count": reranked_count,
        "graded_count": graded_count,
        "cited_count": len(cited_ids),
        "top_evidence_score": top_score,
        "total_tokens": total_tokens if total_tokens > 0 else None,
        "total_cost_usd": round(total_cost, 6) if total_cost > 0 else None,
    }


def _build_failure(run_summary: dict[str, Any]) -> dict[str, Any] | None:
    """Extract failure information from the run summary."""
    failure = run_summary.get("failure")
    if not failure:
        return None
    return {
        "status": failure.get("status"),
        "message": failure.get("message"),
        "source": failure.get("source"),
        "node": failure.get("node"),
    }
