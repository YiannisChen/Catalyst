"""Judge node — synthesizes attribution causes from graded evidence.

One LLM call. Produces causes, summary, grounding self-check,
and Arm C runtime instrumentation (judge_evidence — SECONDARY fidelity check).

Spec reference: Section 4.3 — Judge Node.  S1 §0.5 — Arm-B fidelity.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from catalyst_agents.state import AttributionState, Phase
from catalyst_agents.cost_tracker import track_cost
from catalyst_agents.nodes.critic import insufficient_handler
from catalyst_agents.backoff import invoke_with_retries, MAX_RETRIES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "judge.md"
MAX_CAUSES = 5


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _load_prompt() -> str:
    """Read the judge prompt template from disk."""
    return _PROMPT_PATH.read_text()


def _format_evidence(graded_evidence: list[dict], reranked_chunks: list[dict]) -> str:
    """Format graded evidence with their original content for the prompt.

    Builds a lookup from asset_id → content_md using reranked_chunks, then
    renders each graded evidence entry with its critic metadata and raw content.

    Args:
        graded_evidence: List of graded dicts from the Critic node. Each must
                         have at minimum a ``chunk_id`` key.
        reranked_chunks: List of retrieval result dicts with ``asset_id`` and
                         ``content_md`` keys used to look up raw content.

    Returns:
        Formatted multi-section string separated by horizontal rules, or an
        empty string when graded_evidence is empty.
    """
    if not graded_evidence:
        return ""

    # Build a lookup from chunk_id to content
    content_lookup = {c.get("asset_id", ""): c.get("content_md", "") for c in reranked_chunks}

    parts = []
    for ev in graded_evidence:
        chunk_id = ev.get("chunk_id", "unknown")
        content = content_lookup.get(chunk_id, "[content not found]")
        parts.append(
            f"### Evidence {chunk_id}\n"
            f"Relevance: {ev.get('relevance', 0):.2f} | "
            f"Category: {ev.get('category', 'unknown')} | "
            f"Temporal match: {ev.get('temporal_match', False)}\n"
            f"Critic reasoning: {ev.get('reasoning', '')}\n\n"
            f"{content}"
        )
    return "\n\n---\n\n".join(parts)


def _build_judge_evidence(graded_evidence: list[dict], reranked_chunks: list[dict]) -> dict:
    """Build the judge_evidence artifact — SECONDARY fidelity check.

    Returns a dict with:
      - "per_asset": {asset_id: content_md} — Critic fields STRIPPED
      - "sha256": SHA-256 of the assembled evidence block (the exact string
        _format_evidence_for_arm_b produces)

    Arm B consumes arm_b_evidence (pre-Critic, from miner). judge_evidence is a SUBSET fidelity check: the Judge only sees chunks the Critic graded, so judge_evidence.per_asset ⊆ arm_b_evidence.per_asset.
    """
    if not graded_evidence:
        return {"per_asset": {}, "sha256": ""}

    content_lookup = {c.get("asset_id", ""): c.get("content_md", "") for c in reranked_chunks}
    per_asset: dict[str, str] = {}

    for ev in graded_evidence:
        chunk_id = ev.get("chunk_id", "unknown")
        content = content_lookup.get(chunk_id, "[content not found]")
        per_asset[chunk_id] = content

    # Build the CRITIC-FREE evidence block (same template Arm B reconstructs)
    block = _format_evidence_for_arm_b(per_asset)
    sha = hashlib.sha256(block.encode("utf-8")).hexdigest()

    return {"per_asset": per_asset, "sha256": sha}


def _format_evidence_for_arm_b(per_asset: dict[str, str]) -> str:
    """Format evidence block with Critic fields STRIPPED (§0.5).

    Uses the SAME structural template as _format_evidence but OMITS Critic fields. The block template matches Arm B's _reconstruct_evidence_block for the subset fidelity check.
    - Relevance, Category, Temporal match lines
    - Critic reasoning line

    This block is compared against Arm B's reconstruction for the subset
    fidelity check (judge_evidence content must match arm_b_evidence
    for shared asset_ids).
    """
    if not per_asset:
        return ""

    parts = []
    for chunk_id, content in per_asset.items():
        parts.append(f"### Evidence {chunk_id}\n\n{content}")
    return "\n\n---\n\n".join(parts)


def _parse_judge_response(text: str) -> dict:
    """Parse LLM JSON response, handling optional Markdown fences.

    Args:
        text: Raw LLM output, optionally wrapped in ```json ... ``` or ``` ... ```.

    Returns:
        Parsed dict from the JSON payload.

    Raises:
        json.JSONDecodeError: If the content after fence stripping is not valid JSON.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Drop the opening fence line and any trailing closing fence
        inner_lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        cleaned = "\n".join(inner_lines)
    return json.loads(cleaned)


def _compute_grounding_rate(causes: list[dict], evidence_ids_available: set[str]) -> float:
    """Compute what fraction of causes cite at least one available evidence chunk.

    This is computed independently from the LLM's self_grounding_check field to
    provide an objective, auditable grounding metric.

    Args:
        causes:                List of cause dicts, each with an ``evidence_ids`` list.
        evidence_ids_available: Set of chunk IDs that were actually graded by the Critic.

    Returns:
        Float in [0.0, 1.0]. Returns 0.0 for an empty causes list.
    """
    if not causes:
        return 0.0
    grounded = sum(
        1 for c in causes
        if any(eid in evidence_ids_available for eid in c.get("evidence_ids", []))
    )
    return grounded / len(causes)


# ---------------------------------------------------------------------------
# Public node
# ---------------------------------------------------------------------------

def judge(state: AttributionState, *, llm: Any = None) -> dict:
    """Judge node for the LangGraph attribution pipeline.

    Synthesizes the final attribution report from Critic-graded evidence using
    a single LLM call. Only chunks with relevance > 0.5 (already filtered by
    the Critic) are forwarded to this node.

    Args:
        state: Current attribution state. Must have ``graded_evidence`` populated
               by the Critic node.
        llm:   LLM client with a ``.invoke(prompt: str)`` interface that returns
               a response object with ``.content`` (str) and ``.usage`` attributes.

    Returns:
        Partial state dict containing:
          - ``causes``:        Up to MAX_CAUSES attribution causes with metadata.
          - ``summary_md``:    2-3 sentence Markdown summary with inline citations.
          - ``grounding_rate``: Fraction of causes that cite available evidence.
          - ``judge_evidence``: Per-asset content_md dict (Critic fields stripped;
            SECONDARY fidelity check — subset of arm_b_evidence).
          - ``judge_evidence_sha256``: SHA-256 of the judge evidence block.
    """
    graded = state.get("graded_evidence", [])
    reranked = state.get("reranked_chunks", [])
    if not graded:
        fallback = insufficient_handler(state)
        fallback["phase"] = Phase.JUDGE
        return fallback

    # Compute judge_evidence BEFORE the LLM call (evidence assembly is deterministic)
    je = _build_judge_evidence(graded, reranked)

    prompt_template = _load_prompt()
    prompt = prompt_template.format(
        ticker=state["ticker"],
        price_move_pct=state.get("price_move_pct", "unknown"),
        trade_date=state["trade_date"],
        evidence_formatted=_format_evidence(graded, reranked),
    )

    try:
        response, parsed = invoke_with_retries(
            llm, prompt, parse_fn=_parse_judge_response, node_name="Judge",
        )
        track_cost(state, "judge", response)
        causes = parsed.get("causes", [])[:MAX_CAUSES]

        # Compute grounding rate from our side (independent of LLM's self-check)
        available_ids = {ev.get("chunk_id", "") for ev in graded}
        grounding = _compute_grounding_rate(causes, available_ids)

        return {
            "causes": causes,
            "summary_md": parsed.get("summary_md", ""),
            "grounding_rate": grounding,
            "judge_raw_llm_response": str(response.content),
            "judge_evidence": je["per_asset"],
            "judge_evidence_sha256": je["sha256"],
            "cost_breakdown": state.get("cost_breakdown", []),
            "total_cost_usd": state.get("total_cost_usd", 0.0),
            "total_tokens": state.get("total_tokens", 0),
            "phase": Phase.JUDGE,
        }
    except RuntimeError as exc:
        fallback = insufficient_handler(state)
        fallback["summary_md"] = f"{fallback['summary_md']} {exc}"
        fallback["cost_breakdown"] = state.get("cost_breakdown", [])
        fallback["total_cost_usd"] = state.get("total_cost_usd", 0.0)
        fallback["total_tokens"] = state.get("total_tokens", 0)
        fallback["phase"] = Phase.JUDGE
        return fallback
