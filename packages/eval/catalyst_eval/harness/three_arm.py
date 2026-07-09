"""
Three-Arm Experiment Harness (S1 §0.5).

Arm A: Closed-book baseline — ticker + date + move only, zero evidence.
Arm B: Same-evidence single call — consumes the PERSISTED arm_b_evidence
       artifact from the MINER node (pre-Critic top-8, full content_md).
       Reconstructs evidence block through INDEPENDENT code path, feeds to
       a single Judge call using the judge.md instruction block.
Arm C: Full MCJ run — standard Catalyst pipeline with runtime instrumentation.

Semantics:
  A→B delta = retrieval value (adding pre-Critic top-8 evidence).
  B→C delta = Critic filtering + annotations + guards + Validator = MCJ
              structural value.

Fidelity contracts:
  1. SHA-256(Arm B reconstructed evidence block) == persisted arm_b_evidence.sha256
  2. judge_evidence.per_asset is a SUBSET of arm_b_evidence.per_asset
     with byte-equal values per shared asset_id.

Arm B code path: packages/eval/catalyst_eval/harness/three_arm.py
Arm C miner path:  packages/agents/catalyst_agents/nodes/miner.py
Arm C judge path:  packages/agents/catalyst_agents/nodes/judge.py
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult, PredictedCause


# ---------------------------------------------------------------------------
# Judge prompt (from agents package — ensures instruction parity)
# ---------------------------------------------------------------------------

_JUDGE_PROMPT_PATH = (
    Path(__file__).resolve().parents[4] / "packages" / "agents"
    / "catalyst_agents" / "prompts" / "judge.md"
)

def _load_judge_instruction_block() -> str:
    """Load the judge.md instruction block (rules + output format).

    The instruction block is everything after the '## Task' section header,
    excluding the template placeholder lines ({ticker}, {price_move_pct},
    {trade_date}, {evidence_formatted}) which are filled per-case.
    Returns the shared instruction text.
    """
    if not _JUDGE_PROMPT_PATH.exists():
        return _fallback_instruction_block()
    text = _JUDGE_PROMPT_PATH.read_text()
    # Extract from "## Rules" to end (the instruction block)
    rules_idx = text.find("## Rules")
    if rules_idx == -1:
        return _fallback_instruction_block()
    return text[rules_idx:].strip()


def _fallback_instruction_block() -> str:
    """Fallback if judge.md is unavailable."""
    return (
        "## Rules\n"
        "1. Maximum 5 causes — identify the most impactful factors only.\n"
        "2. Every claim MUST cite evidence — reference chunks by their ID as [chunk_id].\n"
        "3. Do NOT fabricate — if evidence is weak, assign low confidence.\n"
        "4. Category must be one of: earnings, macro, geopolitical, sector, technical, regulatory.\n"
        "5. Direction must be one of: positive, negative, neutral.\n\n"
        "## Output Format\n"
        'Return JSON: {"causes": [...], "summary_md": "..."}'
    )


# ---------------------------------------------------------------------------
# Arm A: Closed-book baseline
# ---------------------------------------------------------------------------

def arm_a_closed_book(
    golden: GoldenEvent,
    *,
    judge_llm: Callable[[str], dict],
) -> AttributionResult:
    """Closed-book prediction: ticker + trade_date + price_move_pct ONLY.

    Uses the judge.md instruction block for prompt parity.
    A→B delta measures retrieval value.
    """
    instruction = _load_judge_instruction_block()
    prompt = (
        f"You are an attribution analyst. Given a stock move with NO evidence, "
        f"identify the most likely causes from your knowledge.\n\n"
        f"Ticker: {golden.ticker}\n"
        f"Trade date: {golden.trade_date}\n"
        f"Price move: {golden.price_move_pct:+.2f}%\n\n"
        f"{instruction}\n\n"
        f"Only output valid JSON. No markdown fences."
    )
    response = judge_llm(prompt)
    return _parse_arm_response(response, golden)


# ---------------------------------------------------------------------------
# Arm B: Same-evidence single call
# ---------------------------------------------------------------------------

def arm_b_same_evidence(
    golden: GoldenEvent,
    arm_b_evidence_artifact: dict[str, Any],
    *,
    judge_llm: Callable[[str], dict],
    judge_evidence_artifact: dict[str, Any] | None = None,
) -> AttributionResult:
    """Same-evidence single call using PERSISTED arm_b_evidence artifact.

    Consumes the PRE-CRITIC top-8 evidence from the miner node.
    Reconstructs the evidence block through an INDEPENDENT code path
    (this module, NOT miner.py).

    On Critic-refuse cases, arm_b_evidence still exists → Arm B attempts
    an answer → refusal_correctness becomes a real measurement.

    If judge_evidence_artifact is provided, performs a SECONDARY fidelity
    check: judge_evidence.per_asset must be a byte-equal SUBSET of
    arm_b_evidence.per_asset.
    """
    per_asset = arm_b_evidence_artifact.get("per_asset", {})
    if not per_asset:
        return _empty_attribution(golden)

    # Reconstruct evidence block — INDEPENDENT of miner.py's assembly
    evidence_block = _reconstruct_evidence_block(per_asset)
    reconstructed_sha = hashlib.sha256(evidence_block.encode("utf-8")).hexdigest()

    # Load judge.md instruction block for prompt parity
    instruction = _load_judge_instruction_block()

    prompt = (
        f"You are an attribution analyst synthesizing the final explanation "
        f"for why a stock moved.\n\n"
        f"## Task\n"
        f"Given evidence about **{golden.ticker}** moving **{golden.price_move_pct:+.2f}%** "
        f"on **{golden.trade_date}**, produce a structured attribution report.\n\n"
        f"## Evidence\n{evidence_block}\n\n"
        f"{instruction}\n\n"
        f"Only output valid JSON. No markdown fences."
    )
    response = judge_llm(prompt)
    result = _parse_arm_response(response, golden)

    # Attach fidelity metadata
    result.evidence_block_sha256 = reconstructed_sha

    # Secondary fidelity check: judge_evidence subset of arm_b_evidence
    if judge_evidence_artifact:
        _verify_subset_fidelity(arm_b_evidence_artifact, judge_evidence_artifact)

    return result


def _reconstruct_evidence_block(per_asset: dict[str, str]) -> str:
    """Reconstruct evidence block from per-asset content_md.

    INDEPENDENT code path from miner.py's _build_arm_b_evidence.
    Same template: "### Evidence {asset_id}\n\n{content}"
    Separator: "\n\n---\n\n"
    """
    if not per_asset:
        return ""

    parts = []
    for asset_id, content in per_asset.items():
        parts.append(f"### Evidence {asset_id}\n\n{content}")
    return "\n\n---\n\n".join(parts)


def _verify_subset_fidelity(
    arm_b_evidence: dict[str, Any],
    judge_evidence: dict[str, Any],
) -> None:
    """Verify judge_evidence.per_asset is a SUBSET of arm_b_evidence.per_asset.

    Raises AssertionError if any judge_evidence asset_id is missing from
    arm_b_evidence or has different content (byte-inequality).
    """
    arm_b_per = arm_b_evidence.get("per_asset", {})
    judge_per = judge_evidence.get("per_asset", {})

    for asset_id, judge_content in judge_per.items():
        if asset_id not in arm_b_per:
            raise AssertionError(
                f"Subset fidelity FAILED: judge_evidence asset {asset_id} "
                f"not found in arm_b_evidence (pre-Critic top-8)"
            )
        if judge_content != arm_b_per[asset_id]:
            raise AssertionError(
                f"Subset fidelity FAILED: asset {asset_id} has different content "
                f"in judge_evidence vs arm_b_evidence. "
                f"Judge: {judge_content[:80]}... | Arm B: {arm_b_per[asset_id][:80]}..."
            )


# ---------------------------------------------------------------------------
# Arm C: Full MCJ
# ---------------------------------------------------------------------------

def arm_c_full_mcj(
    golden: GoldenEvent,
    *,
    catalyst_predict: Callable[[str, str], AttributionResult],
) -> AttributionResult:
    """Full MCJ run — standard Catalyst pipeline with runtime instrumentation.

    The miner node persists arm_b_evidence; the judge node persists
    judge_evidence. Both are available in node_artifacts after the run.
    """
    return catalyst_predict(golden.ticker, golden.trade_date)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_arm_response(response: dict, golden: GoldenEvent) -> AttributionResult:
    """Parse a judge LLM response dict into an AttributionResult."""
    causes_raw = response.get("causes", [])
    causes = []
    for c in causes_raw[:5]:
        causes.append(PredictedCause(
            text=c.get("text", ""),
            category=c.get("category", "unknown"),
            confidence=float(c.get("confidence", 0.5)),
            evidence_ids=c.get("evidence_ids", []),
            direction=c.get("direction", "neutral"),
        ))
    return AttributionResult(
        ticker=golden.ticker,
        trade_date=golden.trade_date,
        causes=causes,
        summary=response.get("summary_md", ""),
    )


def _empty_attribution(golden: GoldenEvent) -> AttributionResult:
    """Return an empty AttributionResult for edge cases."""
    return AttributionResult(
        ticker=golden.ticker,
        trade_date=golden.trade_date,
        causes=[],
        summary="",
    )
