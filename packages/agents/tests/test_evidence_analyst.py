"""M5-1: EvidenceAnalyst structured-output boundary with technical retry.

Frozen §6.3; Phase 4 TSD §§6–12; M5 plan M5-1. The node reads the persisted
EvidenceAnalystContextPack/render pair (M4), renders the exact model messages,
calls the injected LLM once through the M5-0 capability protocol, parses a
strict AnalystDecision (extra="forbid"), and emits the decision plus the
pack/render hashes. Exactly one technical retry for transport/timeout/invalid
structured schema with identical semantic input; valid ABSTAIN/PARTIAL is
never retried; a second schema failure is MODEL_SCHEMA_FAILURE (system
failure, never ABSTAIN). Non-capable providers are rejected at admission.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    AttributionStatus,
    CandidateHypothesis,
    EvidenceDecision,
)
from catalyst_agents.attribution.context_pack import EvidenceAnalystContextPack
from catalyst_agents.attribution.context_pack_builder import (
    RenderMessage,
    canonical_context_pack_json,
)
from catalyst_agents.nodes.evidence_analyst import evidence_analyst
from catalyst_agents.runtime.pack_persistence import InMemoryPackStore
from catalyst_agents.runtime.provider_capability import (
    ModelRoleCallError,
    ModelSchemaFailure,
    ModelTimeoutFailure,
    ProviderCapabilityError,
)


def _sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FakeAnalystProvider:
    def __init__(self, decision_factory, *, capable: bool = True):
        self.decision_factory = decision_factory
        self.calls = 0
        self.captured_messages: list[list[dict]] = []
        self.capability_metadata = (
            {
                "supports_structured_output": True,
                "supports_true_streaming": True,
                "declares_token_accounting": True,
                "normalizes_timeout_errors": True,
                "capability_revision": "test-capability-1",
            }
            if capable
            else {
                "supports_structured_output": False,
                "supports_true_streaming": False,
                "declares_token_accounting": False,
                "normalizes_timeout_errors": False,
                "capability_revision": "test-capability-1",
            }
        )

    def invoke(self, messages):
        self.calls += 1
        self.captured_messages.append(
            [
                dict(message)
                if isinstance(message, dict)
                else {"role": message.role, "content": message.content}
                for message in messages
            ]
        )
        return self.decision_factory()


def _rendered_messages() -> tuple[RenderMessage, ...]:
    return (
        RenderMessage(role="system", content="observation={}"),
        RenderMessage(role="system", content="inventory=e1,e2"),
        RenderMessage(role="system", content="coverage={}"),
    )


def _persist_pair(persistence: InMemoryPackStore, run_id: str = "run:1") -> None:
    """Persist the authoritative render pair.

    The persisted pair (rendered messages + refs) is the authoritative
    model-input evidence (Final TSD §7); the M4 protocol accepts a None pack
    body while the render/hash pair stays authoritative for the node gate.
    """
    messages = _rendered_messages()
    render_sha256 = _sha256(
        canonical_context_pack_json(
            [m.model_dump(mode="json") for m in messages]
        ).decode("utf-8")
    )
    persistence.persist_pack_and_render(
        run_id=run_id,
        pack=None,
        pack_sha256="a" * 64,
        rendered_messages=messages,
        rendered_messages_sha256=render_sha256,
        prompt_template_version="tmpl:v1",
        prompt_template_sha256="c" * 64,
    )
    return render_sha256


def _valid_decision_dict() -> dict:
    return {
        "schema_version": "1.0",
        "evidence_decisions": [
            {
                "evidence_id": "e1",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ["h1"],
                "reason_code": "material_support",
            }
        ],
        "candidate_hypotheses": [
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "AAPL rose on record guidance.",
                "supporting_evidence_ids": ["e1"],
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            }
        ],
        "research_decision": "READY",
        "recommended_status": "PARTIAL",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
    }


def _node_kwargs(store: InMemoryPackStore, llm: FakeAnalystProvider) -> dict:
    return {
        "llm": llm,
        "persistence": store,
        "prompt_template": "You are the Evidence Analyst. Emit the strict schema.",
        "schema": AnalystDecision,
    }


def test_evidence_analyst_parses_strict_decision_and_emits_refs() -> None:
    store = InMemoryPackStore()
    render_sha256 = _persist_pair(store)
    llm = FakeAnalystProvider(_valid_decision_dict)
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))

    assert llm.calls == 1
    decision = result["analyst_decision"]
    assert isinstance(decision, AnalystDecision)
    assert decision.research_decision.value == "READY"
    assert result["context_pack_sha256"] == "a" * 64
    assert result["rendered_messages_sha256"] == render_sha256
    assert len(result["decision_hash"]) == 64
    assert result["analyst_logical_calls"] == 1
    assert result["analyst_provider_attempts"] == 1


def test_evidence_analyst_renders_exact_persisted_messages() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)
    llm = FakeAnalystProvider(_valid_decision_dict)
    evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    rendered = llm.captured_messages[0]
    contents = [message["content"] for message in rendered]
    assert any("observation={}" in content for content in contents)
    assert any("inventory=e1,e2" in content for content in contents)
    assert any("Evidence Analyst" in content for content in contents)


def test_status_ceiling_is_a_schema_violation_never_trusted() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)

    def bad() -> dict:
        payload = _valid_decision_dict()
        payload["status_ceiling"] = "SUFFICIENT"
        return payload

    llm = FakeAnalystProvider(bad)
    with pytest.raises(ModelRoleCallError) as excinfo:
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert "MODEL_SCHEMA_FAILURE" in str(excinfo.value)
    assert not isinstance(excinfo.value, AttributionStatus)
    assert llm.calls == 2  # one technical retry, never a third call


def test_executable_corrective_batch_is_a_schema_violation() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)

    def bad() -> dict:
        payload = _valid_decision_dict()
        payload["corrective_batch"] = {"batch_id": "batch:1"}
        return payload

    llm = FakeAnalystProvider(bad)
    with pytest.raises(ModelRoleCallError, match="MODEL_SCHEMA_FAILURE"):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 2


def test_more_than_three_hypotheses_fails_schema() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)

    def bad() -> dict:
        payload = _valid_decision_dict()
        payload["candidate_hypotheses"] = [
            {
                "hypothesis_ref": f"h{i}",
                "cause_type": "SECTOR_MOVE",
                "statement": f"hypothesis {i}",
                "magnitude_fit": "WEAK",
                "proposed_role": "CONTEXT",
            }
            for i in range(4)
        ]
        return payload

    llm = FakeAnalystProvider(bad)
    with pytest.raises(ModelRoleCallError, match="MODEL_SCHEMA_FAILURE"):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))


def test_duplicate_evidence_refs_fail_schema() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)

    def bad() -> dict:
        payload = _valid_decision_dict()
        payload["evidence_decisions"] = [
            {
                "evidence_id": "e1",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ["h1"],
            },
            {
                "evidence_id": "e1",
                "disposition": "WEAK",
            },
        ]
        return payload

    llm = FakeAnalystProvider(bad)
    with pytest.raises(ModelRoleCallError, match="MODEL_SCHEMA_FAILURE"):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))


def test_unknown_evidence_ref_fails_schema() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)

    def bad() -> dict:
        payload = _valid_decision_dict()
        payload["candidate_hypotheses"][0]["supporting_evidence_ids"] = ["ghost"]
        return payload

    llm = FakeAnalystProvider(bad)
    with pytest.raises(ModelRoleCallError, match="MODEL_SCHEMA_FAILURE"):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))


def test_no_material_as_cause_fails_schema() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)

    def bad() -> dict:
        payload = _valid_decision_dict()
        payload["candidate_hypotheses"][0]["cause_type"] = "NO_MATERIAL_PUBLIC_CATALYST"
        return payload

    llm = FakeAnalystProvider(bad)
    with pytest.raises(ModelRoleCallError, match="MODEL_SCHEMA_FAILURE"):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))


def test_market_structure_as_cause_fails_schema() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)

    def bad() -> dict:
        payload = _valid_decision_dict()
        payload["candidate_hypotheses"][0]["cause_type"] = "MARKET_STRUCTURE_UNSUPPORTED"
        return payload

    llm = FakeAnalystProvider(bad)
    with pytest.raises(ModelRoleCallError, match="MODEL_SCHEMA_FAILURE"):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))


def test_transport_failure_retries_once_with_identical_input_then_succeeds() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)
    flaky_state = {"calls": 0}

    def flaky_decision() -> dict:
        flaky_state["calls"] += 1
        if flaky_state["calls"] == 1:
            raise ModelTimeoutFailure("timed out")
        return _valid_decision_dict()

    llm = FakeAnalystProvider(flaky_decision)
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 2
    assert result["analyst_logical_calls"] == 1
    assert result["analyst_provider_attempts"] == 2
    attempts = result["analyst_attempts"]
    assert len(attempts) == 2
    assert attempts[0].semantic_input_hash == attempts[1].semantic_input_hash
    assert len(llm.captured_messages) == 2
    assert llm.captured_messages[0] == llm.captured_messages[1]


def test_valid_abstain_is_never_retried() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)

    def abstain() -> dict:
        payload = _valid_decision_dict()
        payload["research_decision"] = "ABSTAIN"
        payload["recommended_status"] = "ABSTAIN"
        payload["candidate_hypotheses"] = []
        payload["evidence_decisions"] = []
        return payload

    llm = FakeAnalystProvider(abstain)
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 1
    assert result["analyst_decision"].research_decision.value == "ABSTAIN"


def test_incapable_provider_rejected_at_admission_before_any_call() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)
    llm = FakeAnalystProvider(_valid_decision_dict, capable=False)
    with pytest.raises(ProviderCapabilityError):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 0


def test_missing_persisted_pair_fails_closed_before_provider_call() -> None:
    store = InMemoryPackStore()
    llm = FakeAnalystProvider(_valid_decision_dict)
    from catalyst_agents.runtime.pack_persistence import PackNotPersistedError

    with pytest.raises(PackNotPersistedError):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 0
