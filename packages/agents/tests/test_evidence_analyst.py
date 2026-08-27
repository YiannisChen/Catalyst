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
from catalyst_agents.runtime.pack_persistence import (
    InMemoryPackStore,
    PackNotPersistedError,
)
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


def _build_pack(
    *,
    run_id: str = "run:1",
    included: tuple[str, ...] = ("e1", "e2"),
    pack_sha256: str | None = None,
) -> EvidenceAnalystContextPack:
    """Build a minimal, valid persisted EvidenceAnalystContextPack (M4 schema)."""
    from catalyst_agents.attribution.context_pack import (
        ContextBudget,
        EvidenceAnalystContextPack,
        EvidencePayloadItem,
        TokenCountReport,
    )
    from catalyst_agents.attribution.coverage import CoverageSummary
    from catalyst_agents.attribution.move_profile import MoveProfile
    from catalyst_data.canonical.identity import DataRuntimeIdentity
    from catalyst_data.canonical.temporal import TemporalIdentity

    temporal = TemporalIdentity(
        session_date="2026-01-15",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-15T14:30:00Z"),
        session_close_at=_utc("2026-01-15T21:00:00Z"),
        information_window_start_at=_utc("2026-01-14T21:00:00Z"),
        cutoff_at=_utc("2026-01-15T21:00:00Z"),
    )
    runtime = DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id="m" * 64,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )
    budget = ContextBudget(
        model_context_limit=8000,
        reserved_output_tokens=300,
        reserved_system_instruction_tokens=200,
        observation_tokens=200,
        coverage_summary_tokens=200,
        research_history_tokens=100,
        inventory_tokens=200,
        evidence_payload_tokens=2000,
        per_news_item_max_tokens=200,
        per_sec_chunk_max_tokens=250,
        lead_only_tokens=80,
        safety_margin_tokens=100,
    )
    coverage = CoverageSummary(
        eligible_item_count=0,
        eligible_asset_count=0,
        eligible_full_text_item_count=0,
        material_capable_item_count=0,
        material_capable_asset_count=0,
        primary_authority_asset_count=0,
        direct_primary_asset_count=0,
        reported_news_asset_count=0,
        commentary_lead_asset_count=0,
        unknown_role_asset_count=0,
        eligible_reported_news_group_count=0,
        unknown_independence_asset_count=0,
        known_duplicate_or_syndicated_asset_count=0,
        parse_degraded_item_count=0,
        content_state_counts=(),
    )
    inventory_items = tuple(
        EvidencePayloadItem(
            evidence_id=evidence_id,
            canonical_asset_id=f"asset:{evidence_id}",
            canonical_content_version_id=f"version:{evidence_id}",
            corpus_document_id=f"doc:{evidence_id}",
            chunk_id=evidence_id,
            section_key="body",
            chunk_ordinal=1,
            source_class="issuer_disclosure",
            evidence_role="DIRECT_PRIMARY",
            eligible_at=temporal.cutoff_at,
            content_state="FULL_TEXT",
            material_capability="MATERIAL_CAPABLE",
            independence_status="KNOWN_GROUP",
            content_hash="c" * 64,
        )
        for evidence_id in included
    )
    return EvidenceAnalystContextPack(
        schema_version="1.0",
        packing_policy_version="p1",
        run_id=run_id,
        round=1,
        temporal_identity=temporal,
        data_runtime_identity=runtime,
        context_budget=budget,
        token_count_report=TokenCountReport(
            tokenizer_identity="test-tokenizer",
            rendered_messages_tokens=0,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
            remaining_payload_tokens=7600,
        ),
        observation=MoveProfile(),
        coverage_summary=coverage,
        research_history=(),
        evidence_inventory=inventory_items,
        direct_primary_evidence=(),
        primary_authority_evidence=(),
        independent_reports=(),
        lead_only_evidence=(),
        structured_context=(),
        deterministic_conflict_signals=(),
        data_coverage_gaps=(),
        capability_gaps=(),
        retrieval_degradations=(),
        included_evidence_ids=included,
        excluded_evidence_ids=(),
        truncation_metadata=(),
        delta_evidence_ids=(),
        context_pack_sha256=pack_sha256 or "a" * 64,
        prompt_template_version="tmpl:v1",
        prompt_template_sha256="c" * 64,
        rendered_messages_sha256="b" * 64,
    )


def _utc(iso: str):
    from datetime import datetime, timezone

    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _persist_pair(
    persistence: InMemoryPackStore,
    run_id: str = "run:1",
    pack: EvidenceAnalystContextPack | None = "default",
) -> None:
    """Persist the authoritative render pair plus (by default) the real pack.

    The persisted pair (rendered messages + pack + refs) is the authoritative
    model-input evidence (Final TSD §7); the provider dispatch gate requires
    the persisted pack body so the Analyst inventory is never caller-supplied.
    """
    messages = _rendered_messages()
    render_sha256 = _sha256(
        canonical_context_pack_json(
            [m.model_dump(mode="json") for m in messages]
        ).decode("utf-8")
    )
    resolved_pack = _build_pack(run_id=run_id) if pack == "default" else pack
    persistence.persist_pack_and_render(
        run_id=run_id,
        pack=resolved_pack,
        pack_sha256=(
            resolved_pack.context_pack_sha256
            if resolved_pack is not None
            else "a" * 64
        ),
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
        "pack_inventory_ids": ("e1", "e2"),
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


def test_pack_external_evidence_ref_retries_once_then_model_schema_failure() -> None:
    """C6: reference-integrity against the pack inventory is structured-output
    validation inside the bounded technical retry boundary: a pack-external
    evidence ref gets at most one identical-semantic-input retry, then
    MODEL_SCHEMA_FAILURE (never ABSTAIN, never an extra logical call)."""
    store = InMemoryPackStore()
    _persist_pair(store)

    def bad() -> dict:
        payload = _valid_decision_dict()
        payload["evidence_decisions"].append(
            {
                "evidence_id": "e9",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ["h1"],
                "reason_code": "material_support",
            }
        )
        payload["candidate_hypotheses"][0]["supporting_evidence_ids"] = ["e1", "e9"]
        return payload

    llm = FakeAnalystProvider(bad)
    with pytest.raises(ModelRoleCallError, match="MODEL_SCHEMA_FAILURE") as excinfo:
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 2  # exactly one technical retry; never a third call
    assert not isinstance(excinfo.value, AttributionStatus)
    assert "e9" in str(excinfo.value)


def test_pack_internal_evidence_refs_pass_without_retry() -> None:
    store = InMemoryPackStore()
    _persist_pair(store)
    llm = FakeAnalystProvider(_valid_decision_dict)
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 1
    assert result["analyst_logical_calls"] == 1


# ---------------------------------------------------------------------------
# FINAL NARROW PASS: authoritative persisted inventory + native structured output
# ---------------------------------------------------------------------------

def test_missing_persisted_pack_fails_closed_before_provider_dispatch() -> None:
    """The provider dispatch gate requires the persisted pack body."""
    store = InMemoryPackStore()
    _persist_pair(store, pack=None)
    llm = FakeAnalystProvider(_valid_decision_dict)
    with pytest.raises(PackNotPersistedError):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 0


def test_empty_persisted_inventory_fails_closed() -> None:
    """An omitted/empty persisted inventory must fail closed before dispatch."""
    store = InMemoryPackStore()
    _persist_pair(store, pack=_build_pack(included=()))
    llm = FakeAnalystProvider(_valid_decision_dict)
    with pytest.raises(PackNotPersistedError):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 0


def test_caller_supplied_inventory_superset_fails_closed() -> None:
    """A caller-supplied inventory that is not byte-for-byte equal to the
    persisted pack inventory must fail closed."""
    store = InMemoryPackStore()
    _persist_pair(store)
    llm = FakeAnalystProvider(_valid_decision_dict)
    with pytest.raises(PackNotPersistedError):
        evidence_analyst(
            {"run_id": "run:1"},
            llm=llm,
            persistence=store,
            prompt_template="You are the Evidence Analyst. Emit the strict schema.",
            schema=AnalystDecision,
            pack_inventory_ids=("e1", "e2", "e9"),
        )
    assert llm.calls == 0


def test_native_structured_output_surface_invoked() -> None:
    """When the provider admits with_structured_output(AnalystDecision), the
    node must invoke that native surface; fakes without it still work."""
    store = InMemoryPackStore()
    _persist_pair(store)

    class NativeProvider(FakeAnalystProvider):
        def __init__(self, decision_factory):
            super().__init__(decision_factory)
            self.structured_calls = 0
            self.surface_invoke_calls = 0

        def with_structured_output(self, schema):
            assert schema is AnalystDecision
            self.structured_calls += 1
            outer = self

            class Surface:
                def invoke(self, messages):
                    outer.surface_invoke_calls += 1
                    outer.calls += 1
                    return outer.decision_factory()

            return Surface()

    llm = NativeProvider(_valid_decision_dict)
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.structured_calls == 1
    assert llm.surface_invoke_calls == 1
    assert llm.calls == 1
    assert result["analyst_logical_calls"] == 1
    assert result["analyst_provider_attempts"] == 1
