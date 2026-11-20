"""V1.1 EvidenceAssessment contract and deterministic normalization (M2-6, M5-2).

Final Migration TSD §8.1; Phase 4 TSD §§13–15; M5 plan M5-2. The assessment
binds the raw AnalystDecision, the persisted pack/render hashes, the
code-owned status ceiling, normalized hypotheses/evidence decisions/conflicts,
typed gaps, an optional code-owned corrective batch, the normalized research
decision, and final status/type. ``normalize_decision`` is the one versioned
pure contract: strict reference validation, deterministic ID assignment,
disposition normalization without creating support, gap/capability
validation, hint sanitization, the locked status-rank formula
(ABSTAIN=0, PARTIAL=1, SUFFICIENT=2; final = lower(model recommendation, code
ceiling)), and FOLLOW_UP/round-exhaustion normalization. No second normalizer
module exists.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Protocol, Sequence

from catalyst_data.canonical.identity import DataRuntimeIdentity
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    AttributionStatus,
    AttributionType,
    CauseType,
    EvidenceDisposition,
    HypothesisRole,
    MagnitudeFit,
    ResearchDecision,
)
from catalyst_agents.attribution.ceilings import (  # noqa: F401  (moved; re-exported)
    _GATE_GROUP,
    _RENDERED_QUOTE,
    PRE_OPEN_MAX_GAP_SHARE,
    PRE_OPEN_MIN_MOVE_PCT,
    _comove_ceiling,
    _inventory_index,
    _stored_titles,
    _subject_ceiling,
    _timing_ceiling,
)
from catalyst_agents.attribution.no_material_gates import (
    CONTEXT_ADEQUACY_FIELD,
    _context_adequate,
    _has_publishable_supported_claim,
    _in_pack_failed_parse,
    _material_support_ids,
    _no_material_gate_failures,
    _no_material_gates_pass,
    _unavailable_gap_fields,
    resolve_no_material_outcome,
)
from catalyst_agents.retrieval.corrective import (
    CorrectivePolicy,
    CorrectiveResearchBatch,
    GapReasonCode,
    MissingEvidence,
    build_corrective_batch,
    compute_research_fingerprint,
    sanitize_query_hints,
)
from catalyst_agents.retrieval.task import EvidenceNeed

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_STATUS_RANK: dict[AttributionStatus, int] = {
    AttributionStatus.ABSTAIN: 0,
    AttributionStatus.PARTIAL: 1,
    AttributionStatus.SUFFICIENT: 2,
}

_NORMALIZATION_POLICY_VERSION = "assessment-normalizer-v1"


class AssessmentIdentityFailure(Exception):
    """Identity/integrity failure at normalization (integrity/system class).

    Missing or malformed run/evidence/context identity fails the run closed;
    it never becomes an epistemic ABSTAIN.
    """


class ContextPackSurface(Protocol):
    """The EvidenceAnalystContextPack fields the normalizer reads."""

    run_id: str
    round: int
    included_evidence_ids: tuple[str, ...]
    evidence_inventory: tuple[Any, ...]
    research_history: tuple[Any, ...]
    coverage_summary: Any | None
    data_coverage_gaps: tuple[Any, ...]
    capability_gaps: tuple[Any, ...]
    retrieval_degradations: tuple[Any, ...]
    context_pack_sha256: str
    rendered_messages_sha256: str


class CapabilityRegistrySurface(Protocol):
    """The corrective capability-registry surface the normalizer needs."""

    def is_recoverable(self, need: EvidenceNeed) -> bool: ...


class NormalizedEvidenceDecision(BaseModel):
    """Normalized evidence decision with runtime hypothesis IDs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    disposition: EvidenceDisposition
    supports_hypothesis_ids: tuple[str, ...] = ()
    contradicts_hypothesis_ids: tuple[str, ...] = ()
    reason_code: str = ""
    note: str | None = None


class NormalizedHypothesis(BaseModel):
    """Normalized candidate hypothesis with deterministic runtime ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: str
    source_hypothesis_ref: str
    cause_type: CauseType
    statement: str
    mechanism: str | None = None
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    magnitude_fit: MagnitudeFit
    proposed_role: HypothesisRole
    normalized_role: HypothesisRole
    unresolved_gap_ids: tuple[str, ...] = ()


class NormalizedCorrectiveIntent(BaseModel):
    """Sanitized corrective intent bound to one validated proposal/gap."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_ref: str
    gap_id: str | None = None
    query_hints: tuple[str, ...] = ()
    research_fingerprint: str | None = None


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _hash_prefix(*parts: str, length: int = 12) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:length]


class EvidenceAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    analyst_decision: AnalystDecision
    validated_missing_evidence: tuple[MissingEvidence, ...] = ()
    status_ceiling: AttributionStatus
    corrective_batch: CorrectiveResearchBatch | None = None
    normalization_violations: tuple[str, ...] = ()
    decision_hash: str
    context_pack_sha256: str
    rendered_messages_sha256: str
    normalization_policy_version: str

    # --- M5-2 normalized surface -------------------------------------------
    # Identity is required and validated; no synthetic defaults are accepted.
    run_id: str
    round: int
    data_runtime_identity: DataRuntimeIdentity
    normalized_hypotheses: tuple[NormalizedHypothesis, ...] = ()
    normalized_evidence_decisions: tuple[NormalizedEvidenceDecision, ...] = ()
    normalized_conflicts: tuple[str, ...] = ()
    normalized_corrective_intents: tuple[NormalizedCorrectiveIntent, ...] = ()
    research_decision: ResearchDecision = ResearchDecision.READY
    final_status: AttributionStatus = AttributionStatus.ABSTAIN
    final_attribution_type: AttributionType = AttributionType.EVIDENCE_BACKED_CAUSAL
    # Frozen-A4 §2/§4: an *explicitly recorded* fact that the deterministic
    # NO_MATERIAL gates passed. WriterInput accepts ABSTAIN +
    # NO_MATERIAL_PUBLIC_CATALYST only against this flag, never by inferring
    # from the attribution type alone.
    no_material_gates_passed: bool = False
    normalization_diagnostics: tuple[str, ...] = ()
    evidence_state_hash: str
    assessment_hash: str

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        if not value or value == "run:unknown":
            raise ValueError("run_id must be a real run identity, never 'run:unknown'")
        return value

    @field_validator("round")
    @classmethod
    def _round_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("round must be positive")
        return value

    @field_validator(
        "decision_hash",
        "context_pack_sha256",
        "rendered_messages_sha256",
        "evidence_state_hash",
        "assessment_hash",
    )
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        if value == "0" * 64:
            raise ValueError("all-zero SHA-256 placeholder is not a valid identity")
        return value

    @model_validator(mode="after")
    def _corrective_batch_matches_validated_gaps(self) -> "EvidenceAssessment":
        if self.corrective_batch is None:
            return self
        gaps = {gap.gap_id: gap for gap in self.validated_missing_evidence}
        for action in self.corrective_batch.actions:
            gap = gaps.get(action.gap_id)
            if gap is None:
                raise ValueError(
                    "corrective action references a gap not in validated_missing_evidence"
                )
            if not gap.recoverable:
                raise ValueError("corrective action requires a recoverable gap")
            if gap.evidence_need != action.evidence_need or gap.time_scope != action.time_scope:
                raise ValueError("corrective action must match its gap need and time scope")
        return self


# ---------------------------------------------------------------------------
# normalize_decision (Final TSD §8.1 steps 1-8; M5 plan M5-2)
# ---------------------------------------------------------------------------

_GAP_REASON_NEED_MAPPING: dict[GapReasonCode, set[EvidenceNeed]] = {
    GapReasonCode.MISSING_PRIMARY_CONFIRMATION: {EvidenceNeed.COMPANY_PRIMARY},
    GapReasonCode.MISSING_INDEPENDENT_CORROBORATION: {EvidenceNeed.COMPANY_NEWS},
    GapReasonCode.MISSING_PRIOR_SESSION_CONTEXT: {
        EvidenceNeed.COMPANY_PRIMARY,
        EvidenceNeed.COMPANY_NEWS,
    },
    GapReasonCode.MISSING_SECTOR_CONTEXT: {EvidenceNeed.SECTOR_NEWS},
    GapReasonCode.MISSING_MACRO_CONTEXT: {EvidenceNeed.MACRO_EVENT, EvidenceNeed.MACRO_SERIES},
    GapReasonCode.MISSING_FUNDAMENTAL_CONTEXT: {EvidenceNeed.FUNDAMENTALS},
    GapReasonCode.CONFLICT_REQUIRES_RESOLUTION: set(EvidenceNeed) - {EvidenceNeed.MARKET_STRUCTURE},
    GapReasonCode.MARKET_STRUCTURE_UNSUPPORTED: {EvidenceNeed.MARKET_STRUCTURE},
    GapReasonCode.LOCAL_COVERAGE_GAP: set(EvidenceNeed) - {EvidenceNeed.MARKET_STRUCTURE},
}


def _referenced_evidence_ids(decision: AnalystDecision) -> set[str]:
    ids: set[str] = set()
    for item in decision.evidence_decisions:
        ids.add(item.evidence_id)
    for hypothesis in decision.candidate_hypotheses:
        ids.update(hypothesis.supporting_evidence_ids)
        ids.update(hypothesis.contradicting_evidence_ids)
    return ids


def _normalize_hypotheses(
    decision: AnalystDecision,
    *,
    hypothesis_id_map: dict[str, str],
    gap_id_by_proposal: dict[str, str],
    violations: list[str],
) -> tuple[NormalizedHypothesis, ...]:
    normalized: list[NormalizedHypothesis] = []
    for candidate in decision.candidate_hypotheses:
        hypothesis_id = hypothesis_id_map[candidate.hypothesis_ref]
        # Defensive: drop any same-pair ambiguity without raising status.
        supporting = tuple(candidate.supporting_evidence_ids)
        contradicting = tuple(candidate.contradicting_evidence_ids)
        overlap = set(supporting) & set(contradicting)
        if overlap:
            supporting = tuple(eid for eid in supporting if eid not in overlap)
            contradicting = tuple(eid for eid in contradicting if eid not in overlap)
            violations.append(
                f"ambiguous_support_contradict_pair:{candidate.hypothesis_ref}:{sorted(overlap)}"
            )
        unresolved_gap_ids = tuple(
            gap_id_by_proposal.get(ref, f"unresolved:{ref}") for ref in candidate.unresolved_gap_refs
        )
        normalized.append(
            NormalizedHypothesis(
                hypothesis_id=hypothesis_id,
                source_hypothesis_ref=candidate.hypothesis_ref,
                cause_type=candidate.cause_type,
                statement=candidate.statement,
                mechanism=candidate.mechanism,
                supporting_evidence_ids=supporting,
                contradicting_evidence_ids=contradicting,
                magnitude_fit=candidate.magnitude_fit,
                proposed_role=candidate.proposed_role,
                normalized_role=candidate.proposed_role,
                unresolved_gap_ids=unresolved_gap_ids,
            )
        )
    return tuple(normalized)


def _normalize_evidence_decisions(
    decision: AnalystDecision,
    hypothesis_id_map: dict[str, str],
    violations: list[str],
) -> tuple[NormalizedEvidenceDecision, ...]:
    normalized: list[NormalizedEvidenceDecision] = []
    for item in decision.evidence_decisions:
        supports = tuple(
            hypothesis_id_map[ref] for ref in item.supports_hypothesis_refs
        )
        contradicts = tuple(
            hypothesis_id_map[ref] for ref in item.contradicts_hypothesis_refs
        )
        overlap = set(supports) & set(contradicts)
        if overlap:
            supports = tuple(hid for hid in supports if hid not in overlap)
            contradicts = tuple(hid for hid in contradicts if hid not in overlap)
            violations.append(
                f"ambiguous_support_contradict_pair:{item.evidence_id}:{sorted(overlap)}"
            )
        normalized.append(
            NormalizedEvidenceDecision(
                evidence_id=item.evidence_id,
                disposition=item.disposition,
                supports_hypothesis_ids=supports,
                contradicts_hypothesis_ids=contradicts,
                reason_code=item.reason_code,
                note=item.note,
            )
        )
    return tuple(normalized)


def _validate_gaps(
    decision: AnalystDecision,
    *,
    run_id: str,
    round: int,
    decision_hash: str,
    capability_registry: CapabilityRegistrySurface,
    violations: list[str],
) -> dict[str, MissingEvidence]:
    """Validate proposed gaps and return proposal_ref -> MissingEvidence.

    Binding is by ``proposal_ref`` so an invalid earlier proposal can never
    rebind a later valid gap (C4). The caller derives the canonical tuple from
    the mapping.
    """
    gap_by_proposal: dict[str, MissingEvidence] = {}
    for proposal in decision.proposed_missing_evidence:
        allowed_needs = _GAP_REASON_NEED_MAPPING.get(proposal.reason_code)
        if allowed_needs is None or proposal.evidence_need not in allowed_needs:
            violations.append(
                f"invalid_gap_reason_need:{proposal.proposal_ref}:{proposal.reason_code.value}:{proposal.evidence_need.value}"
            )
            continue
        if (
            proposal.reason_code is GapReasonCode.CONFLICT_REQUIRES_RESOLUTION
            and not proposal.related_conflict_refs
        ):
            violations.append(
                f"conflict_gap_without_conflict_ref:{proposal.proposal_ref}"
            )
            continue
        recoverable = capability_registry.is_recoverable(proposal.evidence_need)
        gap_id = (
            f"gap:{run_id}:{round}:"
            + _hash_prefix(
                decision_hash,
                proposal.proposal_ref,
                proposal.evidence_need.value,
                proposal.time_scope.value,
                proposal.reason_code.value,
            )
        )
        gap_by_proposal[proposal.proposal_ref] = MissingEvidence(
            gap_id=gap_id,
            evidence_need=proposal.evidence_need,
            time_scope=proposal.time_scope,
            expected_information=proposal.expected_information,
            reason_code=proposal.reason_code,
            recoverable=recoverable,
        )
    return gap_by_proposal


def _normalize_intents(
    decision: AnalystDecision,
    *,
    gap_by_proposal: dict[str, MissingEvidence],
    policy_version: str,
) -> tuple[NormalizedCorrectiveIntent, ...]:
    """Sanitize corrective intents and bind each to its OWN validated gap.

    Binding is strictly by proposal_ref: an invalid earlier proposal never
    rebinds a later valid gap (C4).
    """
    proposal_refs = {proposal.proposal_ref: proposal for proposal in decision.proposed_missing_evidence}
    intents: list[NormalizedCorrectiveIntent] = []
    for intent in decision.proposed_corrective_intents:
        hints = sanitize_query_hints(intent.query_hints)
        proposal = proposal_refs.get(intent.proposal_ref)
        gap = gap_by_proposal.get(intent.proposal_ref)
        fingerprint = None
        if proposal is not None and gap is not None:
            fingerprint = compute_research_fingerprint(
                evidence_need=gap.evidence_need,
                time_scope=gap.time_scope,
                lookback_sessions=proposal.lookback_sessions,
                sanitized_hints=hints,
                policy_version=policy_version,
            )
        intents.append(
            NormalizedCorrectiveIntent(
                proposal_ref=intent.proposal_ref,
                gap_id=gap.gap_id if gap is not None else None,
                query_hints=hints,
                research_fingerprint=fingerprint,
            )
        )
    return tuple(intents)


def _any_weak_support(
    decision: AnalystDecision,
    accepted: Sequence[NormalizedHypothesis],
) -> bool:
    """True when any accepted hypothesis is supported by a WEAK-marked item."""
    refs = {hypothesis.source_hypothesis_ref for hypothesis in accepted}
    for item in decision.evidence_decisions:
        if getattr(item.disposition, "value", item.disposition) != "WEAK":
            continue
        if refs & set(item.supports_hypothesis_refs):
            return True
    return False


def _compute_status_ceiling(
    decision: AnalystDecision,
    normalized_hypotheses: tuple[NormalizedHypothesis, ...],
    inventory: dict[str, Any],
) -> AttributionStatus:
    accepted = [
        hypothesis
        for hypothesis in normalized_hypotheses
        if hypothesis.normalized_role is not HypothesisRole.REJECTED
        and hypothesis.supporting_evidence_ids
    ]
    if not accepted:
        return AttributionStatus.ABSTAIN

    material = {
        evidence_id
        for evidence_id, item in inventory.items()
        if getattr(item, "material_capability", None) == "MATERIAL_CAPABLE"
    }

    # Material evidence against the primary explanation -> at most PARTIAL.
    # Until 2026-09-30 this returned ABSTAIN. Mixed results are the norm in
    # earnings (revenue beat, EPS miss): a quote-only answer shows both sides,
    # so contradiction means "uncertain" (ADR-005 PARTIAL), not "no cause".
    for hypothesis in accepted:
        if hypothesis.normalized_role is HypothesisRole.PRIMARY:
            if any(eid in material for eid in hypothesis.contradicting_evidence_ids):
                return AttributionStatus.PARTIAL

    # Unresolved material conflict -> at most PARTIAL.
    if decision.conflicts:
        return AttributionStatus.PARTIAL

    # Degraded support -> at most PARTIAL. ADR-005 puts support that is
    # "limited to opinion, unknown-origin aggregation, or uncorroborated issuer
    # claims" at PARTIAL, and a WEAK disposition is the Analyst saying this item
    # does not carry the claim on its own.
    #
    # Until 2026-09-22 claims.py demoted the whole hypothesis to CONTEXT on any
    # WEAK supporting item, which made ABSTAIN the only outcome for a hypothesis
    # with three good citations and one boilerplate chunk. Removing that veto
    # without replacing it let a pack of uncorroborated issuer text -- three
    # quarters of it WEAK -- publish SUFFICIENT while citing the boilerplate.
    # This is the veto at the right strength: a WEAK item no longer blocks a
    # causal claim, it caps how strongly that claim may be stated.
    if _any_weak_support(decision, accepted):
        return AttributionStatus.PARTIAL

    support_ids: set[str] = set()
    for hypothesis in accepted:
        support_ids.update(hypothesis.supporting_evidence_ids)

    roles = {
        getattr(inventory[eid], "evidence_role", "UNKNOWN")
        for eid in support_ids
        if eid in inventory
    }
    statuses = {
        getattr(inventory[eid], "independence_status", "UNKNOWN")
        for eid in support_ids
        if eid in inventory
    }

    # Commentary-only/unknown-role support -> at most PARTIAL.
    if any(role in {"COMMENTARY_LEAD", "UNKNOWN"} for role in roles):
        return AttributionStatus.PARTIAL
    # Unknown-lineage support caps to PARTIAL only when it is not primary-grade:
    # direct primary/primary-authority evidence is authoritative regardless of
    # lineage, while unknown-lineage news never counts as independent support.
    non_primary_support = {
        eid
        for eid in support_ids
        if eid in inventory
        and getattr(inventory[eid], "evidence_role", None)
        not in {"DIRECT_PRIMARY", "PRIMARY_AUTHORITY"}
    }
    if any(
        getattr(inventory[eid], "independence_status", None) == "UNKNOWN"
        for eid in non_primary_support
    ):
        return AttributionStatus.PARTIAL

    known_news_groups = {
        getattr(inventory[eid], "independence_group_id", None)
        for eid in support_ids
        if eid in inventory
        and getattr(inventory[eid], "evidence_role", None) == "INDEPENDENT_REPORT"
        and getattr(inventory[eid], "independence_status", None) == "KNOWN_GROUP"
    }
    known_news_groups.discard(None)
    has_primary_grade = any(
        role in {"DIRECT_PRIMARY", "PRIMARY_AUTHORITY"} for role in roles
    )
    if not has_primary_grade and len(known_news_groups) < 2:
        return AttributionStatus.PARTIAL

    # SUFFICIENT additionally requires STRONG or PLAUSIBLE magnitude fit on
    # an accepted supported hypothesis.
    best_fit = min(
        (
            _FIT_RANK[hypothesis.magnitude_fit]
            for hypothesis in accepted
            if hypothesis.magnitude_fit in _FIT_RANK
        ),
        default=2,
    )
    if best_fit > _FIT_RANK[MagnitudeFit.PLAUSIBLE]:
        return AttributionStatus.PARTIAL
    return AttributionStatus.SUFFICIENT


_FIT_RANK = {
    MagnitudeFit.STRONG: 0,
    MagnitudeFit.PLAUSIBLE: 1,
    MagnitudeFit.WEAK: 2,
    MagnitudeFit.UNKNOWN: 3,
}


def _lower_status(
    model_recommendation: AttributionStatus,
    ceiling: AttributionStatus,
) -> AttributionStatus:
    """Locked formula: final_status = lower-ranked(model, ceiling)."""
    if _STATUS_RANK[model_recommendation] <= _STATUS_RANK[ceiling]:
        return model_recommendation
    return ceiling


def _normalize_follow_up(
    decision: AnalystDecision,
    *,
    batch: CorrectiveResearchBatch | None,
    round: int,
    max_corrective_rounds: int,
    publishable_claim: bool,
    diagnostics: list[str],
) -> ResearchDecision:
    if decision.research_decision is not ResearchDecision.FOLLOW_UP:
        return decision.research_decision
    if round > max_corrective_rounds:
        diagnostics.append("round_exhausted")
    if batch is None or round > max_corrective_rounds:
        if publishable_claim:
            diagnostics.append("READY_WITH_PARTIAL")
        else:
            diagnostics.append("READY_WITH_ABSTAIN")
        return ResearchDecision.READY
    return ResearchDecision.FOLLOW_UP


def normalize_decision(
    decision: AnalystDecision,
    context_pack: ContextPackSurface,
    capability_registry: CapabilityRegistrySurface,
    policy_version: str,
    *,
    policy: CorrectivePolicy | None = None,
    evidence_state_hash: str | None = None,
    quote_rules: bool = False,
    evidence_window_policy: str = "pre_close_v1",
    filing_rule: bool = False,
    release_timings: Mapping[str, Mapping[str, Any]] | None = None,
    primary_verifier: Any | None = None,
    trace: Any | None = None,
) -> EvidenceAssessment:
    """Deterministically normalize an AnalystDecision into an EvidenceAssessment.

    Pure and versioned: no LLM call, no hidden state. Implements Final TSD
    §8.1 steps 1-8 and the M5-2 locked status-rank formula. The one exception
    is an injected ``primary_verifier`` (attribution/verifier.py): it may only
    lower the ceiling to ABSTAIN.
    """
    effective_policy = policy or CorrectivePolicy(policy_version=policy_version)
    # Q1c: only hypotheses backed by verified verbatim source sentences go on;
    # their published statement is the rendered quotes, never model prose.
    from catalyst_agents.attribution.quotes import apply_quote_rules

    _quote_checks: tuple = ()
    primary_refs = {
        h.hypothesis_ref for h in decision.candidate_hypotheses
        if getattr(h.proposed_role, "value", h.proposed_role) == "PRIMARY"
    }
    if trace is not None:  # side record of the raw decision; never affects the result
        trace.analyst(context_pack.round, decision)
    if quote_rules:
        quote_payload, _quote_checks = apply_quote_rules(
            decision.model_dump(mode="json"), context_pack,
            evidence_window_policy=evidence_window_policy,
            filing_rule=filing_rule,
            release_timings=release_timings or {},
        )
        decision = type(decision).model_validate(quote_payload)
    primary_kept = any(check.kept and check.hypothesis_ref in primary_refs for check in _quote_checks)
    primary_proposal_state = (
        "NO_PRIMARY_PROPOSED" if not primary_refs else
        "PROPOSED_QUOTES_KEPT" if primary_kept else "PROPOSED_QUOTES_FAILED"
    )
    run_id = context_pack.run_id
    round = context_pack.round
    if trace is not None and quote_rules:
        trace.quote_checks(round, _quote_checks)
    violations: list[str] = []
    diagnostics: list[str] = [
        f"QUOTE_{check.rule}:{check.hypothesis_ref}:{check.evidence_id[-28:]}:"
        f"{' '.join(check.text.split())}"[:256]
        for check in _quote_checks
    ]

    # Step 1/2: strict reference validation against the persisted pack inventory.
    pack_ids = set(context_pack.included_evidence_ids)
    referenced = _referenced_evidence_ids(decision)
    unknown = sorted(referenced - pack_ids)
    if unknown:
        raise ValueError(
            "AnalystDecision references evidence not in the pack inventory: "
            f"{unknown}"
        )

    if not context_pack.run_id or context_pack.run_id == "run:unknown":
        raise AssessmentIdentityFailure(
            "context pack run_id is missing or placeholder; identity must be supplied"
        )
    if getattr(context_pack, "data_runtime_identity", None) is None:
        raise AssessmentIdentityFailure(
            "context pack data_runtime_identity is missing; identity must be supplied"
        )
    if evidence_state_hash is None or _SHA256_RE.fullmatch(evidence_state_hash) is None:
        raise AssessmentIdentityFailure(
            "evidence_state_hash must be supplied as a lowercase SHA-256 hex digest"
        )
    resolved_evidence_state_hash = evidence_state_hash

    decision_hash = _sha256_hex(decision.model_dump(mode="json"))

    # Step 3: deterministic runtime IDs from decision hash + stable ordinals.
    hypothesis_id_map: dict[str, str] = {}
    for ordinal, candidate in enumerate(decision.candidate_hypotheses):
        hypothesis_id_map[candidate.hypothesis_ref] = (
            f"hyp:{run_id}:{round}:"
            + _hash_prefix(
                decision_hash,
                str(ordinal),
                candidate.hypothesis_ref,
                candidate.cause_type.value,
                candidate.statement,
            )
        )

    # Step 5a: validate gaps and compute recoverability (code-owned). Gaps are
    # bound by proposal_ref so filtering cannot rebind a later valid gap (C4).
    gap_by_proposal = _validate_gaps(
        decision,
        run_id=run_id,
        round=round,
        decision_hash=decision_hash,
        capability_registry=capability_registry,
        violations=violations,
    )
    validated_gaps = tuple(gap_by_proposal.values())
    gap_id_by_proposal: dict[str, str] = {
        proposal_ref: gap.gap_id
        for proposal_ref, gap in gap_by_proposal.items()
    }

    normalized_intents = _normalize_intents(
        decision,
        gap_by_proposal=gap_by_proposal,
        policy_version=policy_version,
    )

    # Step 4: normalize dispositions/links without creating support.
    normalized_evidence_decisions = _normalize_evidence_decisions(
        decision, hypothesis_id_map, violations
    )
    normalized_hypotheses = _normalize_hypotheses(
        decision,
        hypothesis_id_map=hypothesis_id_map,
        gap_id_by_proposal=gap_id_by_proposal,
        violations=violations,
    )

    inventory = _inventory_index(context_pack)

    # Step 5b: build the optional code-owned corrective batch.
    batch = None
    if (
        decision.research_decision is ResearchDecision.FOLLOW_UP
        and round <= effective_policy.max_corrective_rounds
    ):
        batch = build_corrective_batch(
            EvidenceAssessment(
                analyst_decision=decision,
                validated_missing_evidence=validated_gaps,
                status_ceiling=AttributionStatus.ABSTAIN,
                decision_hash=decision_hash,
                context_pack_sha256=context_pack.context_pack_sha256,
                rendered_messages_sha256=context_pack.rendered_messages_sha256,
                normalization_policy_version=policy_version,
                run_id=run_id,
                round=round,
                data_runtime_identity=context_pack.data_runtime_identity,
                evidence_state_hash=resolved_evidence_state_hash,
                assessment_hash=_sha256_hex(
                    {
                        "interim": decision_hash,
                        "run_id": run_id,
                        "round": round,
                    }
                ),
                normalized_corrective_intents=normalized_intents,
                research_decision=decision.research_decision,
            ),
            capabilities=capability_registry,  # type: ignore[arg-type]
            policy=effective_policy,
            round=round,
        )

    # Step 8: normalize FOLLOW_UP without an executable batch / round closure.
    publishable_claim = _has_publishable_supported_claim(
        normalized_hypotheses, inventory
    )
    research_decision = _normalize_follow_up(
        decision,
        batch=batch,
        round=round,
        max_corrective_rounds=effective_policy.max_corrective_rounds,
        publishable_claim=publishable_claim,
        diagnostics=diagnostics,
    )

    # Step 6: code-owned status ceiling.
    ceiling = _compute_status_ceiling(decision, normalized_hypotheses, inventory)
    no_material_gates_passed = False

    # Step 7: NO_MATERIAL gates and the locked rank formula.
    #
    # Frozen-A4 §2 (Option X). When the gates pass the result is
    # NO_MATERIAL_PUBLIC_CATALYST, and the point-in-time context decides the
    # status: adequate -> PARTIAL, inadequate -> ABSTAIN (the inadequate
    # context is disclosed as coverage gaps). When the gates do not pass the
    # result is ABSTAIN + EVIDENCE_BACKED_CAUSAL with NO_MATERIAL_GATES_FAILED.
    final_type = decision.proposed_attribution_type
    if decision.proposed_attribution_type is AttributionType.NO_MATERIAL_PUBLIC_CATALYST:
        no_material_outcome = resolve_no_material_outcome(
            decision, normalized_hypotheses, inventory, context_pack
        )
        no_material_gates_passed = no_material_outcome.passed
        final_type = no_material_outcome.final_attribution_type
        ceiling = no_material_outcome.status_ceiling
        diagnostics.extend(no_material_outcome.diagnostics)

    if research_decision is ResearchDecision.READY and "READY_WITH_PARTIAL" in diagnostics:
        ceiling = min((ceiling, AttributionStatus.PARTIAL), key=_STATUS_RANK.__getitem__)

    comove_ceiling, comove_diagnostic = _comove_ceiling(
        normalized_hypotheses, getattr(context_pack, "observation", None)
    )
    if comove_ceiling is not None:
        ceiling = min((ceiling, comove_ceiling), key=_STATUS_RANK.__getitem__)
        diagnostics.append(comove_diagnostic)
    if trace is not None:
        trace.gate(round, "comove", comove_ceiling, comove_diagnostic)

    subject_ceiling, subject_diagnostic = _subject_ceiling(
        normalized_hypotheses, context_pack
    )
    if subject_ceiling is not None:
        ceiling = min((ceiling, subject_ceiling), key=_STATUS_RANK.__getitem__)
        diagnostics.append(subject_diagnostic)
    if trace is not None:
        trace.gate(round, "subject", subject_ceiling, subject_diagnostic)

    timing_ceiling, timing_diagnostic = _timing_ceiling(normalized_hypotheses, context_pack)
    if timing_ceiling is not None:
        ceiling = min((ceiling, timing_ceiling), key=_STATUS_RANK.__getitem__)
        diagnostics.append(timing_diagnostic)
    if trace is not None:
        trace.gate(round, "timing", timing_ceiling, timing_diagnostic)

    primary = next(
        (h for h in normalized_hypotheses if h.normalized_role is HypothesisRole.PRIMARY), None
    )
    if (
        primary_verifier is not None
        and primary is not None
        and ceiling is not AttributionStatus.ABSTAIN
        and decision.recommended_status is not AttributionStatus.ABSTAIN
    ):
        verdict, reason = primary_verifier(primary, context_pack)
        cause = primary.cause_type.value
        if trace is not None:
            trace.verifier(round, called=True, verdict=verdict, reason=reason, cause_type=cause,
                           primary_proposal_state=primary_proposal_state,
                           cause_quote=(getattr(primary_verifier, "last", None) or {}).get("cause_quote"))
        if verdict is True:
            diagnostics.append(f"primary_verifier_supported:{cause}:{reason}")
        else:
            # A rejection, or no verdict at all: an unverified cause is not published.
            ceiling = AttributionStatus.ABSTAIN
            label = "reject" if verdict is False else "unavailable"
            diagnostics.append(f"primary_verifier_{label}:{cause}:{reason}")

    elif trace is not None:
        trace.verifier(round, called=False, skipped_because=(
            "verifier_off" if primary_verifier is None
            else "no_primary" if primary is None
            else "already_abstain"
        ), primary_proposal_state=primary_proposal_state)

    final_status = _lower_status(decision.recommended_status, ceiling)

    # AGENT-01: ABSTAIN carries no accepted cause and never adds causal
    # language. Frozen-A4 §3: this holds for both ABSTAIN types, including the
    # NO_MATERIAL_PUBLIC_CATALYST pairing.
    if final_status is AttributionStatus.ABSTAIN:
        downgraded: list[NormalizedHypothesis] = []
        for hypothesis in normalized_hypotheses:
            if hypothesis.normalized_role in (HypothesisRole.PRIMARY, HypothesisRole.SECONDARY):
                downgraded.append(
                    hypothesis.model_copy(
                        update={"normalized_role": HypothesisRole.REJECTED}
                    )
                )
                diagnostics.append(
                    f"abstain_role_downgraded:{hypothesis.hypothesis_id}"
                )
            else:
                downgraded.append(hypothesis)
        normalized_hypotheses = tuple(downgraded)

    assessment_hash = _sha256_hex(
        {
            "decision_hash": decision_hash,
            "validated_gaps": [gap.model_dump(mode="json") for gap in validated_gaps],
            "normalized_hypotheses": [
                hypothesis.model_dump(mode="json")
                for hypothesis in normalized_hypotheses
            ],
            "normalized_evidence_decisions": [
                item.model_dump(mode="json")
                for item in normalized_evidence_decisions
            ],
            "ceiling": ceiling.value,
            "final_status": final_status.value,
            "final_type": final_type.value,
            "research_decision": research_decision.value,
        }
    )

    return EvidenceAssessment(
        analyst_decision=decision,
        validated_missing_evidence=validated_gaps,
        status_ceiling=ceiling,
        corrective_batch=batch,
        normalization_violations=tuple(sorted(set(violations))),
        decision_hash=decision_hash,
        context_pack_sha256=context_pack.context_pack_sha256,
        rendered_messages_sha256=context_pack.rendered_messages_sha256,
        normalization_policy_version=policy_version,
        run_id=run_id,
        round=round,
        normalized_hypotheses=normalized_hypotheses,
        normalized_evidence_decisions=normalized_evidence_decisions,
        normalized_conflicts=tuple(decision.conflicts),
        normalized_corrective_intents=normalized_intents,
        research_decision=research_decision,
        final_status=final_status,
        final_attribution_type=final_type,
        no_material_gates_passed=no_material_gates_passed,
        normalization_diagnostics=tuple(diagnostics),
        evidence_state_hash=resolved_evidence_state_hash,
        data_runtime_identity=context_pack.data_runtime_identity,
        assessment_hash=assessment_hash,
    )


__all__ = [
    "AssessmentIdentityFailure",
    "CONTEXT_ADEQUACY_FIELD",
    "ContextPackSurface",
    "CapabilityRegistrySurface",
    "EvidenceAssessment",
    "NormalizedCorrectiveIntent",
    "NormalizedEvidenceDecision",
    "NormalizedHypothesis",
    "normalize_decision",
]
