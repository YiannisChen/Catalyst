"""C3: EvidenceAssessment must not silently accept synthetic identity defaults.

The corrective review found EvidenceAssessment defaulted run_id to
"run:unknown" and identity hashes to "0" * 64, and accepted assessment_hash as
a fake placeholder. Required run/evidence/context/assessment identity must be
supplied and validated; missing or malformed identity must fail closed with a
typed integrity/system failure; assessment_hash is deterministically computed
by normalize_decision.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.analyst import AnalystDecision
from catalyst_agents.attribution.assessment import (
    AssessmentIdentityFailure,
    EvidenceAssessment,
    normalize_decision,
)
from catalyst_agents.retrieval.task import EvidenceNeed



def _runtime_identity():
    import hashlib

    from catalyst_data.canonical.identity import DataRuntimeIdentity

    def h(part):
        return hashlib.sha256(f"1:{part}".encode("utf-8")).hexdigest()

    return DataRuntimeIdentity(
        data_snapshot_id=h("snapshot"), corpus_manifest_id=h("corpus"),
        fts_index_version="build:fts", dense_index_version=h("dense"),
        embedding_model_revision="emb:1", reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


class FakeContextPack:
    def __init__(self, *, run_id: str = "run:1", round: int = 1,
                 included_evidence_ids: tuple[str, ...] = ("e1",),
                 evidence_inventory: tuple[Any, ...] = (),
                 research_history: tuple[Any, ...] = (),
                 coverage_summary: Any = None,
                 data_coverage_gaps: tuple[Any, ...] = (),
                 capability_gaps: tuple[Any, ...] = (),
                 retrieval_degradations: tuple[Any, ...] = (),
                 context_pack_sha256: str = "a" * 64,
                 rendered_messages_sha256: str = "b" * 64,
                 data_runtime_identity: Any = None):
        self.run_id = run_id
        self.round = round
        self.included_evidence_ids = included_evidence_ids
        self.evidence_inventory = evidence_inventory
        self.research_history = research_history
        self.coverage_summary = coverage_summary
        self.data_coverage_gaps = data_coverage_gaps
        self.capability_gaps = capability_gaps
        self.retrieval_degradations = retrieval_degradations
        self.context_pack_sha256 = context_pack_sha256
        self.rendered_messages_sha256 = rendered_messages_sha256
        self.data_runtime_identity = (
            data_runtime_identity if data_runtime_identity is not None else _runtime_identity()
        )


class FakeRegistry:
    def is_recoverable(self, need: EvidenceNeed) -> bool:
        return False


def _decision() -> AnalystDecision:
    return AnalystDecision(
        schema_version="1.0",
        research_decision="READY",
        recommended_status="SUFFICIENT",
        proposed_attribution_type="EVIDENCE_BACKED_CAUSAL",
    )


def _base(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "analyst_decision": _decision(),
        "status_ceiling": "SUFFICIENT",
        "decision_hash": "d" * 64,
        "context_pack_sha256": "a" * 64,
        "rendered_messages_sha256": "b" * 64,
        "normalization_policy_version": "n1",
        "run_id": "run:1",
        "round": 1,
        "evidence_state_hash": "e" * 64,
        "assessment_hash": "f" * 64,
    }
    base.update(overrides)
    return base


def test_assessment_requires_run_identity() -> None:
    with pytest.raises(ValidationError):
        EvidenceAssessment(**_base(run_id=None))


def test_assessment_rejects_synthetic_run_unknown_placeholder() -> None:
    with pytest.raises(ValidationError):
        EvidenceAssessment(**_base(run_id="run:unknown"))


def test_assessment_rejects_placeholder_evidence_state_hash() -> None:
    with pytest.raises(ValidationError):
        EvidenceAssessment(**_base(evidence_state_hash="0" * 64))


def test_assessment_rejects_placeholder_assessment_hash() -> None:
    with pytest.raises(ValidationError):
        EvidenceAssessment(**_base(assessment_hash="0" * 64))


def test_assessment_rejects_non_positive_round() -> None:
    with pytest.raises(ValidationError):
        EvidenceAssessment(**_base(round=0))


def test_normalize_decision_requires_evidence_state_hash() -> None:
    with pytest.raises(AssessmentIdentityFailure):
        normalize_decision(
            _decision(),
            context_pack=FakeContextPack(),
            capability_registry=FakeRegistry(),
            policy_version="n1",
            evidence_state_hash=None,
        )


def test_normalize_decision_requires_run_identity() -> None:
    with pytest.raises(AssessmentIdentityFailure):
        normalize_decision(
            _decision(),
            context_pack=FakeContextPack(run_id=""),
            capability_registry=FakeRegistry(),
            policy_version="n1",
            evidence_state_hash="e" * 64,
        )


def test_normalize_decision_assessment_hash_is_deterministic() -> None:
    pack = FakeContextPack()
    first = normalize_decision(
        _decision(), context_pack=pack, capability_registry=FakeRegistry(),
        policy_version="n1", evidence_state_hash="e" * 64,
    )
    second = normalize_decision(
        _decision(), context_pack=pack, capability_registry=FakeRegistry(),
        policy_version="n1", evidence_state_hash="e" * 64,
    )
    assert first.assessment_hash == second.assessment_hash
    assert len(first.assessment_hash) == 64
    assert first.run_id == "run:1"
    assert first.evidence_state_hash == "e" * 64
