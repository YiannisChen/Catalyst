"""M5-3: corrective capability registry, batch construction, locked deadlines.

Phase 4 TSD §§16–20; Final TSD §12; M5 plan M5-3. Capability registry maps
EvidenceNeed -> typed local backend + health; MARKET_STRUCTURE is always
non-recoverable. build_corrective_batch emits at most one action, one gap ->
one action, research fingerprints from trusted fields + sanitized hints, a
shared UTC audit deadline, and a monotonic internal enforcement deadline.
Locked formulas: remaining_seconds = max(0, (shared_deadline_utc - now_utc));
internal_deadline_monotonic = now_monotonic + remaining_seconds; expired iff
now_monotonic >= internal_deadline_monotonic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from catalyst_agents.attribution.analyst import AnalystDecision
from catalyst_agents.attribution.assessment import EvidenceAssessment
from catalyst_agents.retrieval.corrective import (
    BackendCapability,
    BackendHealth,
    CorrectiveCapabilityRegistry,
    CorrectivePolicy,
    CorrectiveResearchBatch,
    GapReasonCode,
    MissingEvidence,
    build_corrective_batch,
    compute_internal_deadline_monotonic,
    compute_remaining_seconds,
    compute_research_fingerprint,
    is_expired,
    sanitize_query_hints,
)
from catalyst_agents.retrieval.task import EvidenceNeed, TimeScope


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _registry(healthy: tuple[EvidenceNeed, ...] = ()) -> CorrectiveCapabilityRegistry:
    capabilities = {}
    for need in EvidenceNeed:
        if need is EvidenceNeed.MARKET_STRUCTURE:
            continue
        capabilities[need] = BackendCapability(
            backend=f"backend:{need.value.lower()}",
            health=BackendHealth.HEALTHY if need in healthy else BackendHealth.UNAVAILABLE,
        )
    return CorrectiveCapabilityRegistry(capabilities)


def _gap(**overrides: Any) -> MissingEvidence:
    base: dict[str, Any] = {
        "gap_id": "gap:1",
        "evidence_need": "COMPANY_PRIMARY",
        "time_scope": "PRIOR_SESSION",
        "expected_information": "issuer filing confirmation",
        "reason_code": "MISSING_PRIMARY_CONFIRMATION",
        "recoverable": True,
    }
    base.update(overrides)
    return MissingEvidence(**base)


def _assessment(*, gaps: tuple[MissingEvidence, ...], hints: tuple[tuple[str, ...], ...] = ()) -> EvidenceAssessment:
    return EvidenceAssessment(
        analyst_decision=AnalystDecision(
            schema_version="1.0",
            research_decision="FOLLOW_UP",
            recommended_status="PARTIAL",
            proposed_attribution_type="EVIDENCE_BACKED_CAUSAL",
        ),
        validated_missing_evidence=gaps,
        status_ceiling="PARTIAL",
        decision_hash="d" * 64,
        context_pack_sha256="a" * 64,
        rendered_messages_sha256="b" * 64,
        normalization_policy_version="n1",
        run_id="run:1",
        round=1,
        evidence_state_hash="e" * 64,
        assessment_hash="f" * 64,
        research_decision="FOLLOW_UP",
        normalized_corrective_intents=tuple(
            {
                "proposal_ref": f"p{i+1}",
                "gap_id": gaps[i].gap_id,
                "query_hints": hints[i] if i < len(hints) else (),
            }
            for i in range(len(gaps))
        ),
    )


def test_capability_registry_market_structure_always_non_recoverable() -> None:
    registry = _registry()
    assert registry.is_recoverable(EvidenceNeed.MARKET_STRUCTURE) is False
    assert registry.capability_for(EvidenceNeed.MARKET_STRUCTURE) is None


def test_capability_registry_gates_until_backend_healthy() -> None:
    registry = _registry(healthy=(EvidenceNeed.COMPANY_PRIMARY,))
    assert registry.is_recoverable(EvidenceNeed.COMPANY_PRIMARY) is True
    assert registry.is_recoverable(EvidenceNeed.SECTOR_NEWS) is False
    assert registry.is_recoverable(EvidenceNeed.MACRO_SERIES) is False
    assert registry.is_recoverable(EvidenceNeed.FUNDAMENTALS) is False


def test_build_corrective_batch_one_gap_one_action() -> None:
    assessment = _assessment(
        gaps=(_gap(),),
        hints=(("8-K",),),
    )
    policy = CorrectivePolicy(shared_deadline_utc=_utc("2030-01-06T21:00:00Z"))
    batch = build_corrective_batch(assessment, _registry(healthy=(EvidenceNeed.COMPANY_PRIMARY,)), policy)
    assert batch is not None
    assert len(batch.actions) == 1
    action = batch.actions[0]
    assert action.gap_id == "gap:1"
    assert action.evidence_need is EvidenceNeed.COMPANY_PRIMARY
    assert action.time_scope is TimeScope.PRIOR_SESSION
    assert action.query_hints == ("8-K",)
    assert batch.total_result_budget == policy.total_result_budget
    assert batch.shared_deadline == policy.shared_deadline_utc


def test_market_structure_gap_never_creates_action() -> None:
    assessment = _assessment(
        gaps=(_gap(
            gap_id="gap:ms",
            evidence_need="MARKET_STRUCTURE",
            reason_code="MARKET_STRUCTURE_UNSUPPORTED",
            recoverable=False,
        ),),
    )
    batch = build_corrective_batch(assessment, _registry(), CorrectivePolicy())
    assert batch is None


def test_unavailable_backend_yields_no_batch() -> None:
    assessment = _assessment(
        gaps=(_gap(),),
        hints=(("8-K",),),
    )
    batch = build_corrective_batch(assessment, _registry(), CorrectivePolicy())
    assert batch is None


def test_repeated_fingerprint_is_refused() -> None:
    assessment = _assessment(
        gaps=(_gap(),),
        hints=(("8-K",),),
    )
    policy = CorrectivePolicy(shared_deadline_utc=_utc("2030-01-06T21:00:00Z"))
    registry = _registry(healthy=(EvidenceNeed.COMPANY_PRIMARY,))
    first = build_corrective_batch(assessment, registry, policy)
    assert first is not None
    repeated = build_corrective_batch(
        assessment,
        registry,
        policy,
        attempted_fingerprints=frozenset({first.actions[0].research_fingerprint}),
    )
    assert repeated is None


def test_exhausted_round_yields_no_batch() -> None:
    assessment = _assessment(
        gaps=(_gap(),),
        hints=(("8-K",),),
    )
    policy = CorrectivePolicy(max_corrective_rounds=1)
    batch = build_corrective_batch(assessment, _registry(healthy=(EvidenceNeed.COMPANY_PRIMARY,)), policy, round=2)
    assert batch is None


def test_expired_deadline_yields_no_batch() -> None:
    assessment = _assessment(
        gaps=(_gap(),),
        hints=(("8-K",),),
    )
    past = _utc("2020-01-01T00:00:00Z")
    policy = CorrectivePolicy(shared_deadline_utc=past)
    batch = build_corrective_batch(
        assessment,
        _registry(healthy=(EvidenceNeed.COMPANY_PRIMARY,)),
        policy,
        now_utc=_utc("2026-01-06T00:00:00Z"),
    )
    assert batch is None


def test_deadline_remaining_seconds_is_floor_zero() -> None:
    now = _utc("2026-01-06T21:00:30Z")
    deadline = _utc("2026-01-06T21:00:00Z")
    assert compute_remaining_seconds(deadline, now) == 0.0
    assert compute_internal_deadline_monotonic(deadline, now, now_monotonic=100.0) == 100.0
    assert is_expired(100.0, now_monotonic=100.0) is True


def test_deadline_clock_jump_does_not_extend_enforcement() -> None:
    deadline = _utc("2026-01-06T21:00:10Z")
    # remaining time derives from the persisted UTC deadline only.
    remaining = compute_remaining_seconds(deadline, _utc("2026-01-06T21:00:00Z"))
    assert remaining == 10.0
    # A wall-clock jump between checks cannot extend the monotonic deadline.
    internal = compute_internal_deadline_monotonic(deadline, _utc("2026-01-06T21:00:00Z"), now_monotonic=500.0)
    assert internal == 510.0
    assert is_expired(internal, now_monotonic=509.999) is False
    assert is_expired(internal, now_monotonic=510.0) is True


def test_deadline_boundary_equality_is_expired() -> None:
    assert is_expired(42.0, now_monotonic=42.0) is True
    assert is_expired(42.0, now_monotonic=41.999) is False


def test_sanitize_query_hints_bounds_and_strips() -> None:
    hints = sanitize_query_hints(
        (
            "  SELECT * FROM filings WHERE ticker='AAPL' https://evil.example source:sec date:2026-01-06 8-K ",
            "二 次 合并",
            "  ",
        )
    )
    assert len(hints) <= 3
    assert all(len(hint) <= 200 for hint in hints)
    assert all(hint == " ".join(hint.split()) for hint in hints)
    assert not any("SELECT" in hint.upper() for hint in hints)
    assert not any("http" in hint for hint in hints)
    assert not any("source:" in hint for hint in hints)
    assert not any("date:" in hint for hint in hints)
    assert any("8-K" in hint for hint in hints)


def test_research_fingerprint_is_deterministic_and_sensitive() -> None:
    fp1 = compute_research_fingerprint(
        evidence_need=EvidenceNeed.COMPANY_PRIMARY,
        time_scope=TimeScope.PRIOR_SESSION,
        lookback_sessions=None,
        sanitized_hints=("8-K",),
        policy_version="cp:v1",
    )
    fp2 = compute_research_fingerprint(
        evidence_need=EvidenceNeed.COMPANY_PRIMARY,
        time_scope=TimeScope.PRIOR_SESSION,
        lookback_sessions=None,
        sanitized_hints=("8-K",),
        policy_version="cp:v1",
    )
    fp3 = compute_research_fingerprint(
        evidence_need=EvidenceNeed.COMPANY_NEWS,
        time_scope=TimeScope.PRIOR_SESSION,
        lookback_sessions=None,
        sanitized_hints=("8-K",),
        policy_version="cp:v1",
    )
    assert fp1 == fp2
    assert fp1 != fp3
    assert len(fp1) == 64


def test_batch_uses_monotonic_internal_deadline_float() -> None:
    assessment = _assessment(
        gaps=(_gap(),),
        hints=(("8-K",),),
    )
    deadline = _utc("2026-01-06T21:00:10Z")
    batch = build_corrective_batch(
        assessment,
        _registry(healthy=(EvidenceNeed.COMPANY_PRIMARY,)),
        CorrectivePolicy(shared_deadline_utc=deadline),
        now_utc=_utc("2026-01-06T21:00:00Z"),
        now_monotonic=1000.0,
    )
    assert batch is not None
    assert isinstance(batch.internal_deadline_monotonic, float)
    assert batch.internal_deadline_monotonic == 1010.0
    assert not hasattr(batch, "internal_deadline")
