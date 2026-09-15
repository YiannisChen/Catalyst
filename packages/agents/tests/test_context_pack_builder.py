"""M4-6: deterministic context pack draft/finalizer pipeline (amendment §5)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from catalyst_agents.attribution.context_pack import (
    ContextBudget,
    EvidenceAnalystContextPack,
    EvidencePayloadItem,
    PriorAssessmentContext,
    TokenCountReport,
    TruncationRecord,
)
from catalyst_agents.attribution.context_pack_builder import (
    ContextPackBuilder,
    ContextPackFinalizer,
    PackedContextDraft,
    RenderMessage,
    canonical_context_pack_json,
)
from catalyst_agents.attribution.coverage import CoverageSummary
from catalyst_agents.attribution.coverage_builder import build_coverage_summary
from catalyst_agents.attribution.evidence_state import EvidenceState, EvidenceStateItem
from catalyst_agents.attribution.evidence_state_builder import (
    EvidenceStateBuildContext,
    build_evidence_state,
)
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.runtime.token_budget import (
    TokenCounter,
    UTF8ByteUpperBoundCounter,
)
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-15",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-15T14:30:00Z"),
        session_close_at=_utc("2026-01-15T21:00:00Z"),
        information_window_start_at=_utc("2026-01-14T21:00:00Z"),
        cutoff_at=_utc("2026-01-15T21:00:00Z"),
    )


def _runtime() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id="m" * 64,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _budget(**overrides) -> ContextBudget:
    base = dict(
        model_context_limit=4_000,
        reserved_output_tokens=200,
        reserved_system_instruction_tokens=100,
        observation_tokens=100,
        coverage_summary_tokens=100,
        research_history_tokens=50,
        inventory_tokens=100,
        evidence_payload_tokens=1_000,
        per_news_item_max_tokens=120,
        per_sec_chunk_max_tokens=150,
        lead_only_tokens=60,
        safety_margin_tokens=50,
    )
    base.update(overrides)
    return ContextBudget(**base)


def _item(
    evidence_id: str,
    *,
    source_class: str,
    evidence_role: str,
    content_state: str = "FULL_TEXT",
    material_capability: str = "MATERIAL_CAPABLE",
    independence_group_id: str | None = None,
    section_key: str = "body",
    ordinal: int = 1,
    excerpt: str = "The company reported strong results.",
    eligible_at: str = "2026-01-15T10:00:00Z",
    first_seen_round: int = 1,
    task_id: str = "t:1",
    priority: int = 0,
    fact_id: str | None = None,
    canonical_asset_id: str | None = None,
    canonical_content_version_id: str | None = None,
    corpus_document_id: str | None = None,
    content_hash: str | None = None,
    asset_type: str | None = None,
) -> EvidenceStateItem:
    is_fact = fact_id is not None
    return EvidenceStateItem(
        evidence_id=evidence_id,
        canonical_asset_id=canonical_asset_id or f"asset:{evidence_id}",
        canonical_content_version_id=canonical_content_version_id or f"version:{evidence_id}",
        corpus_document_id=corpus_document_id or f"doc:{evidence_id}",
        chunk_id=None if is_fact else evidence_id,
        fact_id=fact_id,
        section_key=None if is_fact else section_key,
        chunk_ordinal=None if is_fact else ordinal,
        asset_type=asset_type or ("STRUCTURED_CONTEXT" if is_fact else "NEWS"),
        provider="polygon",
        publisher=None,
        canonical_url=None,
        source_class=source_class,
        evidence_role=evidence_role,
        eligible_at=_utc(eligible_at),
        temporal_precision="publication_time",
        content_state=content_state,
        material_capability=material_capability,
        serving_status="body_candidate",
        parse_quality="full",
        independence_group_id=independence_group_id,
        independence_status=(
            "KNOWN_GROUP" if independence_group_id else "UNKNOWN"
        ),
        content_hash=content_hash or "c" * 64,
        text_ref=evidence_id,
        excerpt_text=excerpt,
        first_seen_round=first_seen_round,
        contributing_task_ids=(task_id,),
        retrieval_contributions=(),
    )


def _empty_state() -> EvidenceState:
    return EvidenceState(
        schema_version="evidence_state_v1",
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        research_policy_version="sp:v1",
        task_results=(),
        evidence_items=(),
        structured_facts=(),
        degradations=(),
        capability_gaps=(),
        state_hash="0" * 64,
    )


def _profile() -> MoveProfile:
    return MoveProfile(
        target_return=3.0,
        prior_session_return=2.0,
        gap_return=0.5,
        market_return=2.9,
        sector_return=2.8,
        market_adjusted_return=0.1,
        sector_adjusted_return=0.2,
        volume_abnormality=None,
        scheduled_macro_flags=(),
        market_comove=None,
        sector_comove=None,
        peer_comove=None,
        coverage_flags=(),
        degraded_fields=(),
    )


def _coverage(state: EvidenceState) -> CoverageSummary:
    return build_coverage_summary(state, _profile(), (), ())


def _builder(**overrides) -> ContextPackBuilder:
    base = dict(
        packing_policy_version="pack:v1",
        schema_version="context_pack_v1",
        budget=_budget(),
        token_counter=UTF8ByteUpperBoundCounter(
            provider="test", model_id="test-model",
            message_overhead_tokens=0,
        ),
    )
    base.update(overrides)
    return ContextPackBuilder(**base)


def _draft(state: EvidenceState, *, round: int = 1, prior: EvidenceState | None = None, **kwargs):
    del prior
    builder = _builder()
    return builder.build_draft(
        run_id="run:1",
        round=round,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        evidence_state=state,
        move_profile=_profile(),
        coverage_summary=_coverage(state),
        research_history=(),
        **kwargs,
    )


def _finalizer(**overrides) -> ContextPackFinalizer:
    base = dict(
        template_bytes=b"system: analyse the evidence\n",
        template_version="prompt:v1",
        renderer=default_renderer,
        token_counter=UTF8ByteUpperBoundCounter(
            provider="test", model_id="test-model", message_overhead_tokens=0
        ),
    )
    base.update(overrides)
    return ContextPackFinalizer(**base)


def default_renderer(draft: PackedContextDraft) -> tuple[RenderMessage, ...]:
    """Deterministic test renderer: bounded section text; never includes the
    pack hash."""
    return (
        RenderMessage(
            role="system",
            content="observation=" + canonical_context_pack_json(
                draft.observation.model_dump(mode="json")
            ).decode("utf-8"),
        ),
        RenderMessage(
            role="system",
            content="inventory=" + ",".join(
                item.evidence_id for item in draft.evidence_inventory
            ),
        ),
        RenderMessage(
            role="system",
            content="direct_primary=" + ",".join(
                item.evidence_id for item in draft.direct_primary_evidence
            ),
        ),
        RenderMessage(
            role="system",
            content="lead_only=" + ",".join(
                item.evidence_id for item in draft.lead_only_evidence
            ),
        ),
    )


def test_draft_inventories_every_item_and_partitions_included_excluded():
    state = _empty_state()
    state = state.upsert(_item("a", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY"))
    state = state.upsert(_item("b", source_class="reported_news", evidence_role="INDEPENDENT_REPORT"))
    draft = _draft(state)
    assert isinstance(draft, PackedContextDraft)
    ids = {item.evidence_id for item in draft.evidence_inventory}
    assert ids == {"a", "b"}
    assert set(draft.included_evidence_ids) | set(draft.excluded_evidence_ids) == ids
    assert set(draft.included_evidence_ids) & set(draft.excluded_evidence_ids) == set()


def test_draft_excludes_empty_failed_and_post_cutoff_with_records():
    state = _empty_state()
    state = state.upsert(_item("a", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY"))
    state = state.upsert(
        _item("empty", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              content_state="EMPTY", material_capability="NOT_CAPABLE", excerpt="")
    )
    state = state.upsert(
        _item("failed", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              content_state="FAILED", material_capability="NOT_CAPABLE", excerpt="")
    )
    state = state.upsert(
        _item("late", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              eligible_at="2026-01-16T10:00:00Z")
    )
    draft = _draft(state)
    ids = {item.evidence_id for item in draft.evidence_inventory}
    assert ids == {"a", "empty", "failed", "late"}
    assert "empty" in draft.excluded_evidence_ids
    assert "failed" in draft.excluded_evidence_ids
    assert "late" in draft.excluded_evidence_ids
    actions = {record.evidence_id: record.action for record in draft.truncation_metadata}
    assert actions["empty"] == "EXCLUDED_MATERIALITY"
    assert actions["failed"] == "EXCLUDED_MATERIALITY"
    assert actions["late"] == "EXCLUDED_INELIGIBLE"


def test_draft_metadata_only_lead_no_body():
    state = _empty_state()
    state = state.upsert(
        _item("meta", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              content_state="METADATA_ONLY", material_capability="NOT_CAPABLE",
              excerpt="title lead only")
    )
    draft = _draft(state)
    actions = {record.evidence_id: record.action for record in draft.truncation_metadata}
    assert actions["meta"] == "METADATA_ONLY"
    # Inventory retains identity metadata; the item is not body evidence.
    inventory = {item.evidence_id: item for item in draft.evidence_inventory}
    assert inventory["meta"].material_capability == "NOT_CAPABLE"


def test_draft_order_identical_across_task_completion_orders():
    state_a = _empty_state()
    state_a = state_a.upsert(_item("a", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY", task_id="t:1", priority=0))
    state_a = state_a.upsert(_item("b", source_class="reported_news", evidence_role="INDEPENDENT_REPORT", task_id="t:2", priority=1))
    draft_a = _draft(state_a)
    state_b = _empty_state()
    state_b = state_b.upsert(_item("b", source_class="reported_news", evidence_role="INDEPENDENT_REPORT", task_id="t:2", priority=1))
    state_b = state_b.upsert(_item("a", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY", task_id="t:1", priority=0))
    draft_b = _draft(state_b)
    assert draft_a == draft_b
    assert [item.evidence_id for item in draft_a.evidence_inventory] == [
        item.evidence_id for item in draft_b.evidence_inventory
    ]


def test_draft_news_duplicate_compression_deterministic():
    state = _empty_state()
    state = state.upsert(
        _item("news:1", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              independence_group_id="g:1", excerpt="first wire copy")
    )
    state = state.upsert(
        _item("news:2", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              independence_group_id="g:1", excerpt="second wire copy")
    )
    draft = _draft(state)
    ids = {item.evidence_id for item in draft.evidence_inventory}
    assert ids == {"news:1", "news:2"}  # every member inventoried
    actions = {record.evidence_id: record.action for record in draft.truncation_metadata}
    duplicated = [evidence_id for evidence_id, action in actions.items() if action == "EXCLUDED_DUPLICATE"]
    assert len(duplicated) == 1
    payload_ids = {item.evidence_id for item in draft.independent_reports if item.excerpt_text}
    assert len(payload_ids) == 1  # one representative body per group


def test_draft_sec_sections_stay_distinct():
    state = _empty_state()
    state = state.upsert(
        _item("filing:item1.01", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY",
              section_key="item_1.01", excerpt="section 1.01 text")
    )
    state = state.upsert(
        _item("filing:item2.02", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY",
              section_key="item_2.02", excerpt="section 2.02 text")
    )
    draft = _draft(state)
    sections = {item.section_key for item in draft.evidence_inventory}
    assert sections == {"item_1.01", "item_2.02"}
    assert {item.evidence_id for item in draft.direct_primary_evidence} == {
        "filing:item1.01", "filing:item2.02"
    }


def test_draft_round_two_delta_priority_and_prior_refs():
    round_one = build_evidence_state(
        EvidenceStateBuildContext(
            run_id="run:1", round=1, temporal_identity=_temporal(),
            data_runtime_identity=_runtime(), research_policy_version="sp:v1",
            task_results=(),
        )
    )
    round_two = EvidenceState.model_validate({
        **round_one.model_dump(mode="python"),
        "round": 2,
        "evidence_items": (
            _item("old", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY", first_seen_round=1),
            _item("new", source_class="reported_news", evidence_role="INDEPENDENT_REPORT", first_seen_round=2),
        ),
        "state_hash": "0" * 64,
    })
    prior = PriorAssessmentContext(
        prior_counter_evidence_ids=("old",),
        prior_evidence_assessment_ref="assessment:1",
    )
    draft = _draft(round_two, round=2, prior_assessment_context=prior)
    assert draft.round == 2
    assert set(draft.delta_evidence_ids) == {"new"}
    assert draft.prior_assessment_context == prior
    ordered = [item.evidence_id for item in draft.evidence_inventory]
    assert ordered[0] == "new"  # round-two delta prioritized


def test_finalizer_computes_hashes_and_budget_invariant():
    state = _empty_state()
    state = state.upsert(_item("a", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY"))
    draft = _draft(state)
    pack = _finalizer().finalize(draft)
    assert isinstance(pack, EvidenceAnalystContextPack)
    assert len(pack.context_pack_sha256) == 64
    assert len(pack.prompt_template_sha256) == 64
    assert len(pack.rendered_messages_sha256) == 64
    assert pack.prompt_template_version == "prompt:v1"
    used = (
        pack.token_count_report.rendered_messages_tokens
        + pack.token_count_report.reserved_output_tokens
        + pack.token_count_report.safety_margin_tokens
    )
    assert used <= pack.context_budget.model_context_limit


def test_replay_is_byte_identical():
    state = _empty_state()
    state = state.upsert(_item("a", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY"))
    draft = _draft(state)
    pack_a = _finalizer().finalize(draft)
    draft_b = _draft(state)
    pack_b = _finalizer().finalize(draft_b)
    assert pack_a == pack_b
    assert pack_a.context_pack_sha256 == pack_b.context_pack_sha256
    assert pack_a.rendered_messages_sha256 == pack_b.rendered_messages_sha256


def test_renderer_messages_exclude_pack_hash_and_are_strict():
    state = _empty_state()
    state = state.upsert(_item("a", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY"))
    draft = _draft(state)
    messages = default_renderer(draft)
    assert messages
    for message in messages:
        assert set(RenderMessage.model_fields) == {"role", "content"}
        assert isinstance(message.content, str)
        assert "context_pack_sha256" not in message.content


def test_token_counter_modes_are_labeled():
    counter = UTF8ByteUpperBoundCounter(
        provider="p", model_id="m", message_overhead_tokens=2,
    )
    assert counter.counting_mode == "UTF8_BYTE_UPPER_BOUND"
    assert counter.count_text("héllo") == len("héllo".encode("utf-8")) + 2
    text, original, included = counter.truncate_with_offsets("héllo wörld", 6)
    assert original == len("héllo wörld".encode("utf-8"))
    assert included <= 6
    assert "�" not in text  # never splits a UTF-8 code point


def test_canonical_pack_json_uses_locked_serializer():
    payload = {"z": 1, "a": "héllo"}
    assert canonical_context_pack_json(payload) == json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Corrective pass FIX 1: materiality and filing-section preservation
# ---------------------------------------------------------------------------


def _filing_item(
    evidence_id: str,
    *,
    section_key: str,
    ordinal: int,
    content_hash: str,
    excerpt: str,
    **overrides,
) -> EvidenceStateItem:
    """Two filing chunks that share the canonical chain but differ by section."""
    base = dict(
        source_class="issuer_disclosure",
        evidence_role="DIRECT_PRIMARY",
        content_state="FULL_TEXT",
        material_capability="MATERIAL_CAPABLE",
        section_key=section_key,
        ordinal=ordinal,
        excerpt=excerpt,
        canonical_asset_id="asset:filing:1",
        canonical_content_version_id="version:filing:1",
        corpus_document_id="doc:filing:1",
        content_hash=content_hash,
    )
    base.update(overrides)
    return _item(evidence_id, **base)


def test_metadata_only_marker_never_appears_in_draft_or_pack_bytes():
    """METADATA_ONLY must never retain/expose excerpt body text: a non-empty
    secret-marker excerpt must not appear in the canonical draft or final pack
    bytes, and the item is excluded from payload inclusion."""
    secret = "SECRET-MARKER-METADATA-BODY"
    state = _empty_state()
    state = state.upsert(
        _item("meta", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              content_state="METADATA_ONLY", material_capability="NOT_CAPABLE",
              excerpt=secret)
    )
    draft = _draft(state)
    assert "meta" in draft.excluded_evidence_ids
    assert "meta" not in draft.included_evidence_ids
    actions = {record.evidence_id: record.action for record in draft.truncation_metadata}
    assert actions["meta"] == "METADATA_ONLY"
    record = next(r for r in draft.truncation_metadata if r.evidence_id == "meta")
    assert record.included_token_count == 0
    inventory = {item.evidence_id: item for item in draft.evidence_inventory}
    assert inventory["meta"].excerpt_text is None
    assert inventory["meta"].source_start_offset is None
    assert inventory["meta"].source_end_offset is None
    assert inventory["meta"].tokenizer_identity is None
    assert secret not in canonical_context_pack_json(
        draft.model_dump(mode="json")
    ).decode("utf-8")

    pack = _finalizer().finalize(draft)
    assert secret not in canonical_context_pack_json(
        pack.model_dump(mode="json")
    ).decode("utf-8")
    assert all(
        item.evidence_id != "meta"
        for array in (
            pack.direct_primary_evidence,
            pack.primary_authority_evidence,
            pack.independent_reports,
            pack.structured_context,
        )
        for item in array
    )


def test_title_only_never_included_as_citable_body():
    """Live c05: TITLE_ONLY / LEAD_ONLY titles are not FULL_TEXT and must not
    enter included_evidence_ids. Dispatching Analyst on a title-only pack
    produced FOLLOW_UP + a corrective round and then failed assurance.
    """
    title = "TSLA stock has given up its prior gain."
    state = _empty_state()
    state = state.upsert(
        _item(
            "title",
            source_class="reported_news",
            evidence_role="INDEPENDENT_REPORT",
            content_state="TITLE_ONLY",
            material_capability="LEAD_ONLY",
            excerpt=title,
        )
    )
    draft = _draft(state)
    assert "title" in draft.excluded_evidence_ids
    assert "title" not in draft.included_evidence_ids
    actions = {record.evidence_id: record.action for record in draft.truncation_metadata}
    assert actions["title"] == "TITLE_ONLY"
    inventory = {item.evidence_id: item for item in draft.evidence_inventory}
    assert inventory["title"].excerpt_text is None
    assert inventory["title"].content_state == "TITLE_ONLY"


def test_metadata_only_not_in_any_role_array():
    state = _empty_state()
    state = state.upsert(
        _item("meta", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              content_state="METADATA_ONLY", material_capability="NOT_CAPABLE",
              excerpt="lead")
    )
    draft = _draft(state)
    role_ids = set()
    for array in (
        draft.direct_primary_evidence,
        draft.primary_authority_evidence,
        draft.independent_reports,
        draft.lead_only_evidence,
        draft.structured_context,
    ):
        role_ids.update(item.evidence_id for item in array)
    assert "meta" not in role_ids


def test_distinct_filing_sections_survive_shared_canonical_chain():
    """Two chunks sharing canonical_asset_id, corpus_document_id,
    canonical_content_version_id, and content_hash but with distinct
    section_key values must both be included."""
    state = _empty_state()
    state = state.upsert(
        _filing_item("filing:1:item1.01", section_key="item_1.01", ordinal=1,
                     content_hash="c" * 64, excerpt="section 1.01 text")
    )
    state = state.upsert(
        _filing_item("filing:1:item2.02", section_key="item_2.02", ordinal=1,
                     content_hash="c" * 64, excerpt="section 2.02 text")
    )
    draft = _draft(state)
    assert {item.evidence_id for item in draft.evidence_inventory} == {
        "filing:1:item1.01", "filing:1:item2.02"
    }
    assert set(draft.included_evidence_ids) == {
        "filing:1:item1.01", "filing:1:item2.02"
    }
    sections = {
        item.section_key for item in draft.evidence_inventory
    }
    assert sections == {"item_1.01", "item_2.02"}
    actions = {
        record.evidence_id: record.action for record in draft.truncation_metadata
    }
    assert actions.get("filing:1:item1.01") != "EXCLUDED_DUPLICATE"
    assert actions.get("filing:1:item2.02") != "EXCLUDED_DUPLICATE"
    assert {item.evidence_id for item in draft.direct_primary_evidence} == {
        "filing:1:item1.01", "filing:1:item2.02"
    }


def test_distinct_ordinal_chunks_same_section_remain_eligible_within_budget():
    """Two chunks from one section with distinct (section_key, ordinal) are
    both eligible within budget; exact chunk identity is not erased."""
    state = _empty_state()
    state = state.upsert(
        _filing_item("filing:1:item1.01:0001", section_key="item_1.01", ordinal=1,
                     content_hash="a" * 64, excerpt="first paragraph of section 1.01")
    )
    state = state.upsert(
        _filing_item("filing:1:item1.01:0002", section_key="item_1.01", ordinal=2,
                     content_hash="b" * 64, excerpt="second paragraph of section 1.01")
    )
    draft = _draft(state)
    assert set(draft.included_evidence_ids) == {
        "filing:1:item1.01:0001", "filing:1:item1.01:0002"
    }
    actions = {
        record.evidence_id: record.action for record in draft.truncation_metadata
    }
    assert actions.get("filing:1:item1.01:0001") != "EXCLUDED_DUPLICATE"
    assert actions.get("filing:1:item1.01:0002") != "EXCLUDED_DUPLICATE"


def test_exact_duplicate_news_within_group_still_collapses():
    """News known-independence-group compression remains unchanged: one
    representative body per group, every member inventoried."""
    state = _empty_state()
    state = state.upsert(
        _item("news:1", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              independence_group_id="g:1", excerpt="wire copy one")
    )
    state = state.upsert(
        _item("news:2", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              independence_group_id="g:1", excerpt="wire copy two")
    )
    draft = _draft(state)
    assert {item.evidence_id for item in draft.evidence_inventory} == {"news:1", "news:2"}
    actions = {
        record.evidence_id: record.action for record in draft.truncation_metadata
    }
    duplicated = [
        evidence_id for evidence_id, action in actions.items()
        if action == "EXCLUDED_DUPLICATE"
    ]
    assert len(duplicated) == 1


# ---------------------------------------------------------------------------
# Final corrective pass: strip payload from every excluded inventory item and
# scope filing chunk dedup to the filing identity
# ---------------------------------------------------------------------------


_EXCLUSION_CASES = [
    (
        "late",
        "EXCLUDED_INELIGIBLE",
        "MARKER-LATE-BODY",
        lambda: _empty_state().upsert(
            _item("late", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
                  eligible_at="2026-01-16T10:00:00Z", excerpt="MARKER-LATE-BODY")
        ),
    ),
    (
        "empty",
        "EXCLUDED_MATERIALITY",
        "MARKER-EMPTY-BODY",
        lambda: _empty_state().upsert(
            _item("empty", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
                  content_state="EMPTY", material_capability="NOT_CAPABLE",
                  excerpt="MARKER-EMPTY-BODY")
        ),
    ),
    (
        "failed",
        "EXCLUDED_MATERIALITY",
        "MARKER-FAILED-BODY",
        lambda: _empty_state().upsert(
            _item("failed", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
                  content_state="FAILED", material_capability="NOT_CAPABLE",
                  excerpt="MARKER-FAILED-BODY")
        ),
    ),
    (
        "dup:group",
        "EXCLUDED_DUPLICATE",
        "MARKER-DUP-GROUP-BODY",
        lambda: _empty_state()
        .upsert(
            _item("aaa:group:rep", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
                  independence_group_id="g:marker", excerpt="representative wire body")
        )
        .upsert(
            _item("dup:group", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
                  independence_group_id="g:marker", excerpt="MARKER-DUP-GROUP-BODY")
        ),
    ),
    (
        "filing:z:dup",
        "EXCLUDED_DUPLICATE",
        "MARKER-DUP-FILING-BODY",
        lambda: _empty_state()
        .upsert(
            _filing_item("filing:a:rep", section_key="item_1.01", ordinal=1,
                         content_hash="c" * 64, excerpt="representative section")
        )
        .upsert(
            _filing_item("filing:z:dup", section_key="item_1.01", ordinal=1,
                         content_hash="c" * 64, excerpt="MARKER-DUP-FILING-BODY")
        ),
    ),
    (
        "dup:document",
        "EXCLUDED_DUPLICATE",
        "MARKER-DUP-DOC-BODY",
        lambda: _empty_state()
        .upsert(
            _item("aaa:doc:rep", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
                  corpus_document_id="doc:same", content_hash="c" * 64, excerpt="representative body")
        )
        .upsert(
            _item("dup:document", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
                  corpus_document_id="doc:same", content_hash="c" * 64, excerpt="MARKER-DUP-DOC-BODY")
        ),
    ),
]


@pytest.mark.parametrize(
    ("excluded_id", "action", "marker", "make_state"),
    _EXCLUSION_CASES,
)
def test_excluded_item_marker_never_serialized(excluded_id, action, marker, make_state):
    """Every excluded inventory class must retain identity/source/eligibility/
    content-state/role metadata but strip body payload: the secret marker must
    appear nowhere in canonical draft or final pack bytes."""
    state = make_state()
    draft = _draft(state)
    assert excluded_id in draft.excluded_evidence_ids
    assert excluded_id not in draft.included_evidence_ids
    actions = {
        record.evidence_id: record.action for record in draft.truncation_metadata
    }
    assert actions[excluded_id] == action
    inventory = {item.evidence_id: item for item in draft.evidence_inventory}
    excluded_item = inventory[excluded_id]
    assert excluded_item.excerpt_text is None
    assert excluded_item.source_start_offset is None
    assert excluded_item.source_end_offset is None
    assert excluded_item.tokenizer_identity is None
    # Identity/source/eligibility/state/role metadata is retained.
    assert excluded_item.canonical_asset_id
    assert excluded_item.corpus_document_id
    assert excluded_item.eligible_at is not None
    assert excluded_item.content_state is not None
    assert excluded_item.evidence_role
    draft_bytes = canonical_context_pack_json(
        draft.model_dump(mode="json")
    ).decode("utf-8")
    assert marker not in draft_bytes
    pack = _finalizer().finalize(draft)
    pack_bytes = canonical_context_pack_json(
        pack.model_dump(mode="json")
    ).decode("utf-8")
    assert marker not in pack_bytes


def test_budget_excluded_item_marker_never_serialized():
    """Budget-excluded records are identity-only inventory entries; their
    body/excerpt must not remain serialized inside the draft or pack."""
    marker = "MARKER-BUDGET-BODY"
    state = _empty_state()
    state = state.upsert(
        _item("budget:1", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              excerpt=marker + " " + "padding " * 60)
    )
    builder = _builder(budget=_budget(evidence_payload_tokens=2))
    draft = builder.build_draft(
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        evidence_state=state,
        move_profile=_profile(),
        coverage_summary=_coverage(state),
    )
    assert "budget:1" in draft.excluded_evidence_ids
    assert "budget:1" not in draft.included_evidence_ids
    actions = {
        record.evidence_id: record.action for record in draft.truncation_metadata
    }
    assert actions["budget:1"] == "EXCLUDED_BUDGET"
    inventory = {item.evidence_id: item for item in draft.evidence_inventory}
    assert inventory["budget:1"].excerpt_text is None
    assert inventory["budget:1"].source_start_offset is None
    assert inventory["budget:1"].source_end_offset is None
    assert inventory["budget:1"].tokenizer_identity is None
    assert marker not in canonical_context_pack_json(
        draft.model_dump(mode="json")
    ).decode("utf-8")
    pack = _finalizer().finalize(draft)
    assert marker not in canonical_context_pack_json(
        pack.model_dump(mode="json")
    ).decode("utf-8")


def test_cross_filing_chunks_identical_section_ordinal_hash_both_survive():
    """Two different filings (distinct canonical_asset_id and
    corpus_document_id) with identical section/ordinal/content_hash must both
    be included: filing chunk dedup is filing-scoped."""
    state = _empty_state()
    state = state.upsert(
        _filing_item("filing:a:chunk", section_key="item_1.01", ordinal=1,
                     content_hash="c" * 64, excerpt="filing a section text",
                     canonical_asset_id="asset:filing:a",
                     corpus_document_id="doc:filing:a")
    )
    state = state.upsert(
        _filing_item("filing:b:chunk", section_key="item_1.01", ordinal=1,
                     content_hash="c" * 64, excerpt="filing b section text",
                     canonical_asset_id="asset:filing:b",
                     corpus_document_id="doc:filing:b")
    )
    draft = _draft(state)
    assert {item.evidence_id for item in draft.evidence_inventory} == {
        "filing:a:chunk", "filing:b:chunk"
    }
    assert set(draft.included_evidence_ids) == {
        "filing:a:chunk", "filing:b:chunk"
    }
    actions = {
        record.evidence_id: record.action for record in draft.truncation_metadata
    }
    assert actions.get("filing:a:chunk") != "EXCLUDED_DUPLICATE"
    assert actions.get("filing:b:chunk") != "EXCLUDED_DUPLICATE"
    assert {item.evidence_id for item in draft.direct_primary_evidence} == {
        "filing:a:chunk", "filing:b:chunk"
    }


def test_true_duplicate_within_same_filing_section_chunk_collapses():
    """A true duplicate inside the same filing/section/chunk identity collapses
    to one included representative; the duplicate stays inventoried as
    identity-only."""
    state = _empty_state()
    state = state.upsert(
        _filing_item("filing:a:rep", section_key="item_1.01", ordinal=1,
                     content_hash="c" * 64, excerpt="representative chunk")
    )
    state = state.upsert(
        _filing_item("filing:z:dup", section_key="item_1.01", ordinal=1,
                     content_hash="c" * 64, excerpt="exact duplicate chunk")
    )
    draft = _draft(state)
    assert {item.evidence_id for item in draft.evidence_inventory} == {
        "filing:a:rep", "filing:z:dup"
    }
    assert "filing:z:dup" in draft.excluded_evidence_ids
    assert "filing:a:rep" in draft.included_evidence_ids
    actions = {
        record.evidence_id: record.action for record in draft.truncation_metadata
    }
    assert actions.get("filing:z:dup") == "EXCLUDED_DUPLICATE"
    inventory = {item.evidence_id: item for item in draft.evidence_inventory}
    assert inventory["filing:z:dup"].excerpt_text is None
    assert "exact duplicate chunk" not in canonical_context_pack_json(
        draft.model_dump(mode="json")
    ).decode("utf-8")
