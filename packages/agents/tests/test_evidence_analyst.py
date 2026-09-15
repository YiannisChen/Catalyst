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
    AttributionType,
    CandidateHypothesis,
    EvidenceDecision,
    ResearchDecision,
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

    def with_structured_output(self, schema):
        """Real test structured-output surface: invoke receives dict messages."""
        outer = self

        class Surface:
            def invoke(self, messages):
                return outer.invoke(messages)

        return Surface()


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


def _abstain_decision_dict() -> dict:
    return {
        "schema_version": "1.0",
        "evidence_decisions": [],
        "candidate_hypotheses": [],
        "research_decision": "ABSTAIN",
        "recommended_status": "ABSTAIN",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
    }


def test_empty_inventory_valid_abstain_succeeds_one_logical_call() -> None:
    """An empty authoritative inventory is valid: an evidence-free ABSTAIN
    AnalystDecision succeeds with exactly one logical call."""
    store = InMemoryPackStore()
    _persist_pair(store, pack=_build_pack(included=()))
    llm = FakeAnalystProvider(_abstain_decision_dict)
    result = evidence_analyst({"run_id": "run:1"}, llm=llm, persistence=store,
                              prompt_template="t", schema=AnalystDecision)
    assert llm.calls == 1
    assert result["analyst_logical_calls"] == 1
    assert result["analyst_provider_attempts"] == 1
    assert result["analyst_decision"].research_decision.value == "ABSTAIN"


def test_empty_inventory_evidence_ref_retries_once_then_model_schema_failure() -> None:
    """Any evidence reference against an empty inventory enters the bounded
    schema retry (max 2 provider attempts) and exhausts as MODEL_SCHEMA_FAILURE."""
    store = InMemoryPackStore()
    _persist_pair(store, pack=_build_pack(included=()))
    llm = FakeAnalystProvider(_valid_decision_dict)
    with pytest.raises(ModelRoleCallError, match="MODEL_SCHEMA_FAILURE"):
        evidence_analyst({"run_id": "run:1"}, llm=llm, persistence=store,
                         prompt_template="t", schema=AnalystDecision)
    assert llm.calls == 2


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


# ---------------------------------------------------------------------------
# MERGE-BLOCKER: native structured-output dict messages + fail-closed admission
# ---------------------------------------------------------------------------

def test_native_surface_receives_dict_messages_not_render_objects() -> None:
    """structured_surface.invoke must receive standard role/content dicts."""
    store = InMemoryPackStore()
    _persist_pair(store)

    class DictOnlyProvider(FakeAnalystProvider):
        def __init__(self, decision_factory):
            super().__init__(decision_factory)
            self.surface_calls = 0

        def with_structured_output(self, schema):
            outer = self

            class Surface:
                def invoke(self, messages):
                    outer.surface_calls += 1
                    outer.calls += 1
                    assert all(
                        isinstance(message, dict)
                        and set(message) == {"role", "content"}
                        for message in messages
                    ), f"non-dict message passed to native surface: {messages!r}"
                    return outer.decision_factory()

            return Surface()

    llm = DictOnlyProvider(_valid_decision_dict)
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.surface_calls == 1
    assert llm.calls == 1
    assert result["analyst_provider_attempts"] == 1


def test_langchain_message_conversion_compatibility_offline() -> None:
    """RenderMessage -> role/content dict conversion matches LangChain's
    standard message dict conversion (offline; no network call)."""
    from langchain_core.messages import SystemMessage

    from catalyst_agents.nodes.evidence_analyst import render_messages_to_dicts

    messages = (
        RenderMessage(role="system", content="You are the Evidence Analyst."),
        RenderMessage(role="system", content="observation={}"),
    )
    converted = render_messages_to_dicts(messages)
    for message, expected in zip(
        converted,
        (
            {"role": "system", "content": "You are the Evidence Analyst."},
            {"role": "system", "content": "observation={}"},
        ),
    ):
        assert message == expected
        langchain_message = SystemMessage(content=message["content"])
        assert {"role": langchain_message.type, "content": langchain_message.content} == message


def test_native_factory_raise_fails_closed_typed() -> None:
    """An admitted with_structured_output whose factory raises must fail closed
    with a typed capability error; no silent fallback to raw invoke."""
    store = InMemoryPackStore()
    _persist_pair(store)

    class BrokenNativeProvider(FakeAnalystProvider):
        def with_structured_output(self, schema):
            raise RuntimeError("factory exploded")

    llm = BrokenNativeProvider(_valid_decision_dict)
    with pytest.raises(ProviderCapabilityError):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 0


def test_native_invalid_surface_fails_closed_typed() -> None:
    """An admitted with_structured_output returning an unusable surface must
    fail closed with a typed capability error; no fallback."""
    store = InMemoryPackStore()
    _persist_pair(store)

    class InvalidSurfaceProvider(FakeAnalystProvider):
        def with_structured_output(self, schema):
            return object()  # no invoke callable

    llm = InvalidSurfaceProvider(_valid_decision_dict)
    with pytest.raises(ProviderCapabilityError):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 0


def test_fallback_invoke_path_used_when_no_native_surface() -> None:
    """Fakes without with_structured_output still use the strict invoke+parse
    fallback (one logical call)."""
    store = InMemoryPackStore()
    _persist_pair(store)

    class LegacyFake(FakeAnalystProvider):
        def __init__(self, decision_factory):
            super().__init__(decision_factory)
            self.with_structured_output = None  # not admitted

    llm = LegacyFake(_valid_decision_dict)
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 1
    assert result["analyst_logical_calls"] == 1


# ---------------------------------------------------------------------------
# DeepSeek structured-output method admission (json_object via langchain
# json_mode). The declared method travels in the capability metadata.
# ---------------------------------------------------------------------------


def test_declared_structured_output_method_is_forwarded_to_the_factory() -> None:
    """A provider that declares ``structured_output_method`` must have that
    method forwarded to ``with_structured_output``; the schema stays the
    positional argument."""
    store = InMemoryPackStore()
    _persist_pair(store)
    captured: dict = {}

    class MethodAwareProvider(FakeAnalystProvider):
        def __init__(self, decision_factory):
            super().__init__(decision_factory)
            self.capability_metadata = dict(
                self.capability_metadata, structured_output_method="json_mode"
            )

        def with_structured_output(self, schema, method=None):
            captured["schema"] = schema
            captured["method"] = method
            outer = self

            class Surface:
                def invoke(self, messages):
                    outer.calls += 1
                    return outer.decision_factory()

            return Surface()

    llm = MethodAwareProvider(_valid_decision_dict)
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert captured["method"] == "json_mode"
    assert captured["schema"] is AnalystDecision
    assert result["analyst_provider_attempts"] == 1
    assert result["analyst_logical_calls"] == 1


def test_provider_without_declared_method_keeps_schema_only_factory() -> None:
    """Fakes and providers whose factory only accepts ``schema`` keep working:
    no method keyword is passed when the metadata declares none."""
    store = InMemoryPackStore()
    _persist_pair(store)

    class SchemaOnlyProvider(FakeAnalystProvider):
        def with_structured_output(self, schema):  # no method kwarg at all
            outer = self

            class Surface:
                def invoke(self, messages):
                    outer.calls += 1
                    return outer.decision_factory()

            return Surface()

    llm = SchemaOnlyProvider(_valid_decision_dict)
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert result["analyst_provider_attempts"] == 1
    assert result["analyst_logical_calls"] == 1


def test_declared_method_rejected_by_factory_fails_closed() -> None:
    """A declared method the factory cannot accept is a typed capability
    failure, never a silent method-less fallback."""
    store = InMemoryPackStore()
    _persist_pair(store)

    class LyingMethodProvider(FakeAnalystProvider):
        def __init__(self, decision_factory):
            super().__init__(decision_factory)
            self.capability_metadata = dict(
                self.capability_metadata, structured_output_method="json_mode"
            )

        def with_structured_output(self, schema):
            return None

    llm = LyingMethodProvider(_valid_decision_dict)
    with pytest.raises(ProviderCapabilityError):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 0


_METHOD_NOT_CALLED = object()


class MethodAwareAnalystProvider(FakeAnalystProvider):
    """Fake provider whose structured-output factory accepts ``method``.

    Mirrors the production admission path: the declared capability metadata
    carries ``structured_output_method`` and the factory receives it as a
    keyword, so tests can exercise both the json_mode contract path and the
    production function_calling path without a network call.
    """

    def __init__(self, decision_factory, method: str | None = None) -> None:
        super().__init__(decision_factory)
        self.declared_method = method
        self.captured_method: object = _METHOD_NOT_CALLED
        if method is not None:
            self.capability_metadata = dict(
                self.capability_metadata, structured_output_method=method
            )

    def with_structured_output(self, schema, method=None):
        self.captured_method = method
        return super().with_structured_output(schema)


# ---------------------------------------------------------------------------
# M7 STAGE-1: json_mode field contract + typed parser failure
#
# The 2026-09-15 live c01 run terminalized SYSTEM_ERROR because DeepSeek's
# json_object answer used invented field names and langchain's
# OutputParserException was not a ModelRoleCallError (so it was neither
# retried nor typed). These tests pin the two corrections: the node maps
# parse failures onto the retryable ModelSchemaFailure, and the json_mode
# request carries the exact AnalystDecision field contract.
# ---------------------------------------------------------------------------

# A METADATA_ONLY coverage row from the live c01 run: visible in the coverage
# summary, never part of the citable pack inventory.
METADATA_ONLY_COVERAGE_ID = (
    "cf436246bcb70f5f6b0e6d5f8cbb4a4f4ac50d0f8cd17f0f4c5c0bb1d9d1b0a1"
    ":news_v2:body:0001"
)


class _RaisingSurfaceProvider(FakeAnalystProvider):
    """Native surface that raises the injected exception on every invoke."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(_valid_decision_dict)
        self.exc = exc

    def with_structured_output(self, schema):
        outer = self

        class Surface:
            def invoke(self, messages):
                outer.calls += 1
                outer.captured_messages.append(
                    [
                        dict(message)
                        if isinstance(message, dict)
                        else {"role": message.role, "content": message.content}
                        for message in messages
                    ]
                )
                raise outer.exc

        return Surface()


def _empty_inventory_example_dict() -> dict:
    from catalyst_agents.nodes.evidence_analyst import _empty_inventory_example

    return _empty_inventory_example(AnalystDecision)


def test_langchain_output_parser_exception_becomes_typed_schema_failure() -> None:
    """json_mode parses with langchain's PydanticOutputParser, which raises
    OutputParserException when the model invents field names. That failure is
    the retryable structured-schema failure: the node must retry once with the
    identical input and then raise typed MODEL_SCHEMA_FAILURE, never leak the
    parser error to the executor as an untyped SYSTEM_ERROR."""
    from langchain_core.exceptions import OutputParserException

    store = InMemoryPackStore()
    _persist_pair(store)
    llm = _RaisingSurfaceProvider(
        OutputParserException(
            "Failed to parse AnalystDecision from completion "
            '{"evidence_classifications": [{"evidence_id": "e1", '
            '"classification": "WEAK"}]}',
            llm_output=(
                '{"evidence_classifications": [{"evidence_id": "e1", '
                '"classification": "WEAK"}]}'
            ),
        )
    )
    with pytest.raises(
        ModelSchemaFailure, match="unparseable structured output JSON"
    ) as excinfo:
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert type(excinfo.value) is ModelSchemaFailure
    assert "MODEL_SCHEMA_FAILURE" in str(excinfo.value)
    assert llm.calls == 2


def test_unmapped_provider_error_is_not_retried_or_retyped() -> None:
    """Only structured-output parse failures are retyped: an unmapped provider
    error (an endpoint 400) propagates unchanged after exactly one attempt."""
    class BadRequestError(Exception):
        """Stand-in for a non-retryable provider HTTP error."""

    store = InMemoryPackStore()
    _persist_pair(store)
    llm = _RaisingSurfaceProvider(BadRequestError("400 invalid_request_error"))
    with pytest.raises(BadRequestError):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 1


def test_generated_empty_inventory_example_matches_the_pinned_contract() -> None:
    """The example must be generated from the Pydantic model (never
    hand-written) and must still carry the required exact fields."""
    example = _empty_inventory_example_dict()
    assert example == {
        "schema_version": "1.0",
        "evidence_decisions": [],
        "candidate_hypotheses": [],
        "conflicts": [],
        "proposed_missing_evidence": [],
        "research_decision": "ABSTAIN",
        "recommended_status": "ABSTAIN",
        "proposed_attribution_type": "NO_MATERIAL_PUBLIC_CATALYST",
        "proposed_corrective_intents": [],
    }
    # Drift guard: the example cannot acquire or drop a schema field.
    assert set(example) == set(AnalystDecision.model_fields)


def test_json_mode_contract_text_names_the_decision_fields_and_example() -> None:
    from catalyst_agents.nodes.evidence_analyst import _json_mode_field_contract

    text = _json_mode_field_contract(AnalystDecision)
    assert "evidence_decisions" in text
    assert "disposition" in text
    assert json.dumps(_empty_inventory_example_dict(), indent=2) in text
    for name in AnalystDecision.model_fields:
        assert name in text


def test_inventory_constraint_text_states_the_pack_inventory_rules() -> None:
    """No JSON schema can express "only IDs in the pack inventory are citable",
    so these rules are prompt text on every structured-output method."""
    from catalyst_agents.nodes.evidence_analyst import _inventory_constraint_text

    text = _inventory_constraint_text()
    assert "evidence inventory" in text
    assert "METADATA_ONLY" in text
    assert "evidence_decisions" in text and "MUST be" in text
    assert "ABSTAIN" in text


def test_json_mode_contract_is_appended_to_the_system_prompt_before_hashing() -> None:
    """Under the declared json_mode method the contract text must be part of
    the hashed prompt, not a side channel: json_object transmits no schema, so
    the semantic input hash covers exactly what the provider receives."""
    from catalyst_agents.nodes.evidence_analyst import _semantic_input_hash

    store = InMemoryPackStore()
    _persist_pair(store)
    llm = MethodAwareAnalystProvider(_abstain_decision_dict, method="json_mode")
    bare_prompt = "You are the Evidence Analyst. Emit the strict schema."
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))

    system_message = llm.captured_messages[0][0]
    assert system_message["role"] == "system"
    assert system_message["content"].startswith(bare_prompt)
    assert "evidence_decisions" in system_message["content"]
    assert "disposition" in system_message["content"]

    assert result["semantic_input_hash"] == _semantic_input_hash(
        system_message["content"], _rendered_messages(), "1.0"
    )
    assert result["semantic_input_hash"] != _semantic_input_hash(
        bare_prompt, _rendered_messages(), "1.0"
    )


def test_generated_empty_example_parses_as_abstain_in_one_logical_call() -> None:
    """The emitted example is itself a valid AnalystDecision: the live model
    constrained to that shape completes as an evidence-free ABSTAIN."""
    store = InMemoryPackStore()
    _persist_pair(store, pack=_build_pack(included=()))
    llm = FakeAnalystProvider(_empty_inventory_example_dict)
    result = evidence_analyst(
        {"run_id": "run:1"},
        llm=llm,
        persistence=store,
        prompt_template="t",
        schema=AnalystDecision,
    )
    assert llm.calls == 1
    assert result["analyst_decision"].research_decision is ResearchDecision.ABSTAIN
    assert result["analyst_decision"].recommended_status is AttributionStatus.ABSTAIN
    assert (
        result["analyst_decision"].proposed_attribution_type
        is AttributionType.NO_MATERIAL_PUBLIC_CATALYST
    )
    assert result["analyst_decision"].evidence_decisions == ()


def test_empty_inventory_metadata_only_coverage_id_is_a_schema_failure() -> None:
    """A METADATA_ONLY coverage row is not inventory: citing it is a bounded
    schema failure after exactly two provider attempts."""
    store = InMemoryPackStore()
    _persist_pair(store, pack=_build_pack(included=()))
    llm = FakeAnalystProvider(
        lambda: {
            "schema_version": "1.0",
            "evidence_decisions": [
                {
                    "evidence_id": METADATA_ONLY_COVERAGE_ID,
                    "disposition": "WEAK",
                    "reason_code": "metadata_only",
                }
            ],
            "candidate_hypotheses": [],
            "research_decision": "ABSTAIN",
            "recommended_status": "ABSTAIN",
            "proposed_attribution_type": "NO_MATERIAL_PUBLIC_CATALYST",
        }
    )
    with pytest.raises(ModelSchemaFailure, match="outside the pack inventory"):
        evidence_analyst(
            {"run_id": "run:1"},
            llm=llm,
            persistence=store,
            prompt_template="t",
            schema=AnalystDecision,
        )
    assert llm.calls == 2


def test_json_mode_outbound_payload_carries_the_field_contract_offline() -> None:
    """Transport-level check (mock httpx transport, dummy key, no network):
    the json_mode request body declares response_format json_object and its
    system message carries the AnalystDecision field names and example.

    The production declaration (provider deepseek -> method json_mode) is
    pinned by packages/app/tests/test_llm_factory_capability.py; the node is
    exercised here against a real langchain json_mode surface.
    """
    import httpx
    from langchain_openai import ChatOpenAI

    captured: dict = {}

    def handler(request: "httpx.Request") -> "httpx.Response":
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        captured["call_count"] = captured.get("call_count", 0) + 1
        body = {
            "id": "chatcmpl-offline-1",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(_empty_inventory_example_dict()),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        }
        return httpx.Response(200, json=body)

    client = ChatOpenAI(
        model="deepseek-flash",
        api_key="sk-test-dummy-key",
        base_url="https://offline.invalid/v1",
        temperature=0.0,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    object.__setattr__(
        client,
        "capability_metadata",
        {
            "supports_structured_output": True,
            "supports_true_streaming": True,
            "declares_token_accounting": True,
            "normalizes_timeout_errors": True,
            "capability_revision": "v1.1-capability-1",
            "structured_output_method": "json_mode",
        },
    )

    store = InMemoryPackStore()
    _persist_pair(store, pack=_build_pack(included=()))
    result = evidence_analyst(
        {"run_id": "run:1"},
        llm=client,
        persistence=store,
        prompt_template="You are the Evidence Analyst. Emit the strict schema.",
        schema=AnalystDecision,
    )

    assert captured["call_count"] == 1
    payload = captured["payload"]
    assert payload["response_format"] == {"type": "json_object"}
    system_contents = [
        message["content"]
        for message in payload["messages"]
        if message["role"] == "system"
    ]
    assert any(
        "evidence_decisions" in content and "disposition" in content
        for content in system_contents
    )
    assert any(
        json.dumps(_empty_inventory_example_dict(), indent=2) in content
        for content in system_contents
    )
    assert result["analyst_decision"].research_decision is ResearchDecision.ABSTAIN


# ---------------------------------------------------------------------------
# M7 STAGE-1 (function_calling): the AnalystDecision schema travels in
# tools[] with a forced tool_choice. json_object + prompt text is retained
# only as the documented json_mode contract; it is not the constraint
# mechanism on this path.
# ---------------------------------------------------------------------------


def _empty_inventory_tool_call_response() -> dict:
    return {
        "id": "chatcmpl-offline-tool-1",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_offline_1",
                            "type": "function",
                            "function": {
                                "name": "AnalystDecision",
                                "arguments": json.dumps(_empty_inventory_example_dict()),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _offline_capable_client(handler, method: str):
    """Real langchain client over a mock httpx transport (dummy key, no network)."""
    import httpx
    from langchain_openai import ChatOpenAI

    client = ChatOpenAI(
        model="deepseek-flash",
        api_key="sk-test-dummy-key",
        base_url="https://offline.invalid/v1",
        temperature=0.0,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    object.__setattr__(
        client,
        "capability_metadata",
        {
            "supports_structured_output": True,
            "supports_true_streaming": True,
            "declares_token_accounting": True,
            "normalizes_timeout_errors": True,
            "capability_revision": "v1.1-capability-1",
            "structured_output_method": method,
        },
    )
    return client


def test_function_calling_outbound_payload_carries_the_decision_tool_offline() -> None:
    """Transport-level check (mock httpx transport, dummy key, no network):
    the function_calling request carries the AnalystDecision schema in
    ``tools[0].function`` with a forced ``tool_choice``, and no
    ``response_format`` json_schema/json_object shape."""
    captured: dict = {}

    def handler(request):
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        captured["call_count"] = captured.get("call_count", 0) + 1
        import httpx

        return httpx.Response(200, json=_empty_inventory_tool_call_response())

    client = _offline_capable_client(handler, method="function_calling")
    store = InMemoryPackStore()
    _persist_pair(store, pack=_build_pack(included=()))
    result = evidence_analyst(
        {"run_id": "run:1"},
        llm=client,
        persistence=store,
        prompt_template="You are the Evidence Analyst. Emit the strict schema.",
        schema=AnalystDecision,
    )

    assert captured["call_count"] == 1
    payload = captured["payload"]
    tool = payload["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "AnalystDecision"
    parameters = tool["function"]["parameters"]
    assert set(parameters["properties"]) == set(AnalystDecision.model_fields)
    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["evidence_decisions"]["items"]["properties"][
        "disposition"
    ]["enum"] == ["SUPPORT", "CONTRADICT", "WEAK", "LEAD_ONLY", "IRRELEVANT"]
    assert payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "AnalystDecision"},
    }
    assert payload["parallel_tool_calls"] is False
    assert payload.get("response_format") is None
    assert "json_schema" not in json.dumps(payload)
    assert result["analyst_decision"].research_decision is ResearchDecision.ABSTAIN
    assert result["analyst_logical_calls"] == 1


def test_function_calling_declared_method_is_forwarded_to_the_factory() -> None:
    """The declared production method reaches the factory; the schema stays the
    positional argument."""
    store = InMemoryPackStore()
    _persist_pair(store)
    llm = MethodAwareAnalystProvider(_valid_decision_dict, method="function_calling")
    result = evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))

    assert llm.captured_method == "function_calling"
    assert llm.calls == 1
    assert result["analyst_logical_calls"] == 1


def test_function_calling_prompt_carries_no_field_contract_only_inventory_rules() -> None:
    """The schema travels in tools[] on this path, so the prompt must not claim
    a schema-less json_object call nor restate the field example. The evidence
    inventory rules stay: no JSON schema can constrain pack membership."""
    store = InMemoryPackStore()
    _persist_pair(store)
    llm = MethodAwareAnalystProvider(_valid_decision_dict, method="function_calling")
    bare_prompt = "You are the Evidence Analyst. Emit the strict schema."
    evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))

    content = llm.captured_messages[0][0]["content"]
    assert content.startswith(bare_prompt)
    assert "no schema is transmitted" not in content
    assert json.dumps(_empty_inventory_example_dict(), indent=2) not in content
    assert "METADATA_ONLY" in content


def test_inventory_rules_are_injected_for_every_structured_output_method() -> None:
    """The live c01 run proved it: the model cited a METADATA_ONLY coverage row
    as evidence because the pack-inventory rules had been dropped from the
    prompt. They must reach the provider whatever the declared method is."""
    for method in ("json_mode", "function_calling"):
        store = InMemoryPackStore()
        _persist_pair(store)
        llm = MethodAwareAnalystProvider(_valid_decision_dict, method=method)
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))

        content = llm.captured_messages[0][0]["content"]
        assert "Only evidence IDs listed in the evidence inventory" in content
        assert "METADATA_ONLY coverage rows are NOT inventory" in content
        assert "evidence_decisions` MUST be" in content


def test_tool_argument_validation_error_becomes_typed_schema_failure() -> None:
    """langchain's PydanticToolsParser validates tool-call arguments with the
    schema and lets pydantic's ValidationError escape. That is the retryable
    structured-schema failure: one identical-input retry, then typed
    MODEL_SCHEMA_FAILURE carrying a bounded parser excerpt — never an untyped
    SYSTEM_ERROR."""
    from pydantic import ValidationError

    try:
        AnalystDecision.model_validate(
            {"schema_version": "1.0", "evidence_decisions": [{"evidence_id": "e1"}]}
        )
    except ValidationError as exc:
        validation_error = exc
    else:  # pragma: no cover - the schema must reject a disposition-less item
        raise AssertionError("expected a ValidationError")

    store = InMemoryPackStore()
    _persist_pair(store)
    llm = _RaisingSurfaceProvider(validation_error)
    with pytest.raises(ModelSchemaFailure) as excinfo:
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))

    assert type(excinfo.value) is ModelSchemaFailure
    assert "MODEL_SCHEMA_FAILURE" in str(excinfo.value)
    assert "disposition" in str(excinfo.value)
    assert llm.calls == 2


def test_unrelated_pydantic_validation_error_is_not_retyped() -> None:
    """Only the AnalystDecision schema's own validation error is the retryable
    structured-schema failure. An unrelated pydantic ValidationError from the
    provider call propagates unchanged after exactly one attempt."""
    from pydantic import BaseModel, ValidationError

    class Unrelated(BaseModel):
        value: int

    try:
        Unrelated(value="not-an-int")
    except ValidationError as exc:
        unrelated_error = exc
    else:  # pragma: no cover - the model must reject a non-int
        raise AssertionError("expected a ValidationError")

    store = InMemoryPackStore()
    _persist_pair(store)
    llm = _RaisingSurfaceProvider(unrelated_error)
    with pytest.raises(ValidationError):
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))
    assert llm.calls == 1


def test_schema_failure_excerpt_is_bounded_single_line_and_secret_free() -> None:
    """The excerpt is public-diagnostic text: single line, <=200 chars, and
    redacted of credential/raw-provider shaped content."""
    from catalyst_agents.nodes.evidence_analyst import _schema_failure_excerpt

    excerpt = _schema_failure_excerpt(
        "1 validation error for AnalystDecision\n"
        "  api_key=sk-abcdefghijklmnopqrstuvwxyz012345\n"
        + "y" * 500
    )
    assert len(excerpt) <= 200
    assert "\n" not in excerpt
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in excerpt
    assert "api_key=sk-" not in excerpt
    assert excerpt


def test_schema_failure_excerpt_is_carried_by_the_mapped_parser_failure() -> None:
    """The OutputParserException mapping keeps a failure detail so a live
    schema failure is diagnosable without persisting the raw completion."""
    from langchain_core.exceptions import OutputParserException

    store = InMemoryPackStore()
    _persist_pair(store)
    llm = _RaisingSurfaceProvider(
        OutputParserException(
            "Failed to parse AnalystDecision from completion "
            '{"evidence_classifications": []}',
        )
    )
    with pytest.raises(ModelSchemaFailure) as excinfo:
        evidence_analyst({"run_id": "run:1"}, **_node_kwargs(store, llm))

    message = str(excinfo.value)
    assert "unparseable structured output JSON" in message
    assert "Failed to parse AnalystDecision" in message
    assert len(message) < 1000
    assert llm.calls == 2
