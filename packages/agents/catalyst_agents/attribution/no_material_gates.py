"""Deterministic no-material attribution gates (Frozen-A4 / Final TSD §15).

These checks decide whether a NO_MATERIAL_PUBLIC_CATALYST result is
substantiated and how point-in-time context affects its status.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    AttributionStatus,
    AttributionType,
    HypothesisRole,
)
from catalyst_agents.retrieval.task import EvidenceNeed

if TYPE_CHECKING:
    from catalyst_agents.attribution.assessment import (
        ContextPackSurface,
        NormalizedHypothesis,
    )


@dataclass(frozen=True)
class NoMaterialOutcome:
    """Normalized result of applying the NO_MATERIAL gate policy."""

    passed: bool
    final_attribution_type: AttributionType
    status_ceiling: AttributionStatus
    diagnostics: tuple[str, ...] = ()


def _material_support_ids(
    normalized_hypotheses: tuple[NormalizedHypothesis, ...],
    inventory: dict[str, Any],
) -> set[str]:
    material = {
        evidence_id
        for evidence_id, item in inventory.items()
        if getattr(item, "material_capability", None) == "MATERIAL_CAPABLE"
    }
    support_ids: set[str] = set()
    for hypothesis in normalized_hypotheses:
        if hypothesis.normalized_role is HypothesisRole.REJECTED:
            continue
        support_ids.update(hypothesis.supporting_evidence_ids)
    return support_ids & material


def _has_publishable_supported_claim(
    normalized_hypotheses: tuple[NormalizedHypothesis, ...],
    inventory: dict[str, Any],
) -> bool:
    return bool(_material_support_ids(normalized_hypotheses, inventory))


# Frozen-A4 §2 / WP-C C2. These fields map to typed point-in-time coverage
# signals; every other UNAVAILABLE data gap still vetoes the no-material path.
CONTEXT_ADEQUACY_FIELD: tuple[str, ...] = (
    "market_return",
    "sector_return",
    "peer_summary",
)
_CONTEXT_ADEQUACY_FIELDS = frozenset(CONTEXT_ADEQUACY_FIELD)


def _unavailable_gap_fields(context_pack: ContextPackSurface) -> set[str]:
    """Fields with an UNAVAILABLE ``DataCoverageGap`` on the pack."""
    return {
        getattr(gap, "field", None)
        for gap in context_pack.data_coverage_gaps
        if getattr(gap, "availability", None) == "UNAVAILABLE"
    }


def _context_adequate(context_pack: ContextPackSurface) -> bool:
    """Point-in-time market/sector/peer context adequacy (Frozen-A4 §2).

    ADEQUATE only when all three context signals are available: each typed
    alignment is present and AVAILABLE, and none of the three context fields
    carries an UNAVAILABLE data-coverage gap. In V1.1 the corpus has no index,
    sector or peer series, so every alignment is UNAVAILABLE and this is False
    — the ABSTAIN pairing is the reachable one.
    """
    coverage = context_pack.coverage_summary
    if coverage is None:
        return False
    for alignment in (
        getattr(coverage, "market_alignment", None),
        getattr(coverage, "sector_alignment", None),
        getattr(coverage, "peer_alignment", None),
    ):
        if alignment is None:
            return False
        if getattr(alignment, "availability", None) == "UNAVAILABLE":
            return False
    if _unavailable_gap_fields(context_pack) & _CONTEXT_ADEQUACY_FIELDS:
        return False
    return True


def _in_pack_failed_parse(
    inventory: dict[str, Any],
    context_pack: ContextPackSurface,
) -> bool:
    """True when an in-pack, cutoff-valid, FULL_TEXT, material-capable item
    carries ``parse_quality == "failed"`` (WP-C C2 degraded-parse veto).

    Reads the per-item ``parse_quality`` the pack now carries; it never
    approximates with the corpus-wide ``parse_degraded_item_count``. A
    ``degraded`` item is reported through coverage and never vetoes.
    """
    cutoff_at = getattr(
        getattr(context_pack, "temporal_identity", None), "cutoff_at", None
    )
    for item in inventory.values():
        if getattr(item, "parse_quality", None) != "failed":
            continue
        if getattr(item, "content_state", None) != "FULL_TEXT":
            continue
        if getattr(item, "material_capability", None) != "MATERIAL_CAPABLE":
            continue
        eligible_at = getattr(item, "eligible_at", None)
        temporal = getattr(context_pack, "temporal_identity", None)
        publication_window = getattr(temporal, "publication_window", None) if temporal is not None else None
        allowed_end = publication_window()[1] if callable(publication_window) else (
            getattr(temporal, "evidence_window_end_at", None) or cutoff_at
        )
        if allowed_end is not None and eligible_at is not None and eligible_at > allowed_end:
            continue
        return True
    return False


def _no_material_gate_failures(
    decision: AnalystDecision,
    normalized_hypotheses: tuple[NormalizedHypothesis, ...],
    inventory: dict[str, Any],
    context_pack: ContextPackSurface,
) -> tuple[str, ...]:
    """Deterministic NO_MATERIAL_PUBLIC_CATALYST gate failures (Final TSD §15).

    COMPANY_PRIMARY **and** COMPANY_NEWS in the research history; no retrieval
    degradations; no capability gaps; a coverage summary; no in-pack
    FULL_TEXT material-capable ``failed`` parse; no UNAVAILABLE data gap
    outside the three context fields; and no material support. Every failing
    predicate is named so the diagnostics can label the cause.
    """
    failures: list[str] = []
    needs = {task.evidence_need for task in context_pack.research_history}
    if EvidenceNeed.COMPANY_PRIMARY not in needs or EvidenceNeed.COMPANY_NEWS not in needs:
        failures.append("sanity_tasks_incomplete")
    if context_pack.retrieval_degradations:
        failures.append("retrieval_degradation")
    if context_pack.capability_gaps:
        failures.append("capability_gap")
    if context_pack.coverage_summary is None:
        failures.append("coverage_unavailable")
    if _in_pack_failed_parse(inventory, context_pack):
        failures.append("in_pack_failed_parse")
    if _unavailable_gap_fields(context_pack) - _CONTEXT_ADEQUACY_FIELDS:
        failures.append("unavailable_data_gap")
    if _material_support_ids(normalized_hypotheses, inventory):
        failures.append("material_support")
    return tuple(failures)


def _no_material_gates_pass(
    decision: AnalystDecision,
    normalized_hypotheses: tuple[NormalizedHypothesis, ...],
    inventory: dict[str, Any],
    context_pack: ContextPackSurface,
) -> bool:
    """Final TSD §15 deterministic gates for NO_MATERIAL_PUBLIC_CATALYST."""
    return not _no_material_gate_failures(
        decision, normalized_hypotheses, inventory, context_pack
    )


def resolve_no_material_outcome(
    decision: AnalystDecision,
    normalized_hypotheses: tuple[NormalizedHypothesis, ...],
    inventory: dict[str, Any],
    context_pack: ContextPackSurface,
) -> NoMaterialOutcome:
    """Apply gate results to the attribution type, ceiling, and diagnostics."""
    gate_failures = _no_material_gate_failures(
        decision, normalized_hypotheses, inventory, context_pack
    )
    if not gate_failures:
        if _context_adequate(context_pack):
            return NoMaterialOutcome(
                passed=True,
                final_attribution_type=AttributionType.NO_MATERIAL_PUBLIC_CATALYST,
                status_ceiling=AttributionStatus.PARTIAL,
            )
        return NoMaterialOutcome(
            passed=True,
            final_attribution_type=AttributionType.NO_MATERIAL_PUBLIC_CATALYST,
            status_ceiling=AttributionStatus.ABSTAIN,
            diagnostics=("NO_MATERIAL_CONTEXT_INADEQUATE",),
        )

    diagnostics = ["NO_MATERIAL_GATES_FAILED"]
    # An abstention whose only gate failure is the degraded-parse veto is a
    # metadata-forced abstention, never a refusal judgement.
    if gate_failures == ("in_pack_failed_parse",):
        diagnostics.append("metadata_forced")
    return NoMaterialOutcome(
        passed=False,
        final_attribution_type=AttributionType.EVIDENCE_BACKED_CAUSAL,
        status_ceiling=AttributionStatus.ABSTAIN,
        diagnostics=tuple(diagnostics),
    )
