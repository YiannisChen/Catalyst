"""V1.1 thin orchestration state (M5-11).

The sealed MCJ semantic fields were archived at M5-11. ``FoundationGraphState``
is the only new-write graph state: run/stage, immutable artifact refs/hashes,
round/attempt counters, deadline/cancel, and terminal error only. No copied
evidence lists, prose, or hidden reasoning in V1.1 fields
lives in V1.1 fields. Legacy semantic fields remain readable only through
historical readers.
"""
from __future__ import annotations

from typing import TypedDict


class FoundationGraphState(TypedDict, total=False):
    """Thin orchestration state: refs/hashes and counters, never payloads."""

    run_id: str
    round: int
    attempt: int
    deadline_epoch_ms: int | None
    cancel_requested: bool
    terminal_error: str | None

    # Artifact references and hashes only (never duplicated payloads).
    observation_ref: str | None
    observation_hash: str | None
    research_tasks_ref: str | None
    research_results_ref: str | None
    evidence_state_ref: str | None
    evidence_state_hash: str | None
    coverage_summary_ref: str | None
    coverage_summary_hash: str | None
    context_pack_ref: str | None
    context_pack_hash: str | None
    rendered_messages_ref: str | None
    rendered_messages_hash: str | None
    prompt_template_version: str | None
    prompt_template_sha256: str | None
    policy_version: str | None
