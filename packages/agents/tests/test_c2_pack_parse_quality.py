"""WP-C C2: in-pack parse_quality and the model-facing wire surface.

Frozen §5.1 lists ``parse_quality`` as canonical evidence metadata and Frozen
§6.3.1 keeps the pack inventory's retained metadata as a *minimum*, so the pack
gains ``parse_quality`` per item (reviewer condition i) under a bumped schema
version (condition ii). It must never reach the model: no renderer may emit
``parse_quality`` on the wire (condition iii), and a fixture that pins pack
bytes keeps its identity because the field is omitted when ``None``
(condition iv).
"""
from __future__ import annotations

from datetime import datetime, timezone

from catalyst_agents.attribution.context_pack import EvidencePayloadItem
from catalyst_agents.attribution.context_pack_builder import (
    CONTEXT_PACK_SCHEMA_VERSION,
    ContextPackBuilder,
    _to_payload_item,
    canonical_context_pack_json,
)
from catalyst_agents.attribution.coverage_builder import build_coverage_summary
from catalyst_agents.attribution.evidence_state import EvidenceState, EvidenceStateItem
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.attribution.pack_render import (
    critic_prefix_renderer,
    evidence_body_renderer,
    foundation_renderer,
)
from catalyst_agents.runtime.token_budget import UTF8ByteUpperBoundCounter
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


def _item(parse_quality: str) -> EvidenceStateItem:
    return EvidenceStateItem(
        evidence_id="e1",
        canonical_asset_id="asset:e1",
        canonical_content_version_id="version:e1",
        corpus_document_id="doc:e1",
        chunk_id="e1",
        section_key="body",
        chunk_ordinal=1,
        asset_type="NEWS",
        provider="polygon",
        source_class="issuer_disclosure",
        evidence_role="DIRECT_PRIMARY",
        eligible_at=_utc("2026-01-15T10:00:00Z"),
        temporal_precision="publication_time",
        content_state="FULL_TEXT",
        material_capability="MATERIAL_CAPABLE",
        serving_status="body_candidate",
        parse_quality=parse_quality,
        independence_status="UNKNOWN",
        content_hash="c" * 64,
        text_ref="e1",
        excerpt_text="The issuer disclosed a material event.",
        first_seen_round=1,
        contributing_task_ids=("t:1",),
    )


def _state(item: EvidenceStateItem) -> EvidenceState:
    return EvidenceState(
        schema_version="evidence_state_v1",
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        research_policy_version="sp:v1",
        evidence_items=(item,),
        state_hash="0" * 64,
    )


def _profile() -> MoveProfile:
    return MoveProfile(target_return=3.0, coverage_flags=())


def _draft(state: EvidenceState):
    builder = ContextPackBuilder(
        packing_policy_version="evidence_body_v1",
        budget=_budget(),
        token_counter=UTF8ByteUpperBoundCounter(
            provider="test", model_id="test-model", message_overhead_tokens=0
        ),
    )
    return builder.build_draft(
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        evidence_state=state,
        move_profile=_profile(),
        coverage_summary=build_coverage_summary(state, _profile(), (), ()),
        research_history=(),
    )


def _budget():
    from catalyst_agents.graphs.graph import _foundation_budget

    return _foundation_budget()


def test_context_pack_schema_version_is_bumped() -> None:
    # Reviewer condition (ii): the RunManifest binds context_pack_schema_version
    # through catalyst_app.runtime.composition._context_pack_schema_version(),
    # so bumping the constant is what binds it. The binding site is asserted
    # directly below.
    assert CONTEXT_PACK_SCHEMA_VERSION == "context_pack_v2"


def test_run_manifest_schema_binding_uses_the_bumped_constant() -> None:
    from catalyst_app.runtime.composition import _context_pack_schema_version

    assert (
        _context_pack_schema_version()
        == CONTEXT_PACK_SCHEMA_VERSION
        == "context_pack_v2"
    )


def test_to_payload_item_carries_parse_quality() -> None:
    payload = _to_payload_item(_item("degraded"))
    assert payload.parse_quality == "degraded"
    assert payload.content_state == "FULL_TEXT"
    assert payload.material_capability == "MATERIAL_CAPABLE"


def test_parse_quality_is_omitted_when_none_and_kept_when_set() -> None:
    without = EvidencePayloadItem(
        evidence_id="e1",
        canonical_asset_id="asset:e1",
        canonical_content_version_id="version:e1",
        corpus_document_id="doc:e1",
        chunk_id="e1",
        source_class="issuer_disclosure",
        evidence_role="DIRECT_PRIMARY",
        eligible_at=_utc("2026-01-15T10:00:00Z"),
        content_state="FULL_TEXT",
        material_capability="MATERIAL_CAPABLE",
        independence_status="UNKNOWN",
    )
    assert "parse_quality" not in without.model_dump(mode="json")
    with_value = without.model_copy(update={"parse_quality": "failed"})
    assert with_value.model_dump(mode="json")["parse_quality"] == "failed"


def test_renderers_never_emit_parse_quality_on_the_wire() -> None:
    draft = _draft(_state(_item("failed")))
    # The pack *does* carry the field...
    pack_json = canonical_context_pack_json(draft.model_dump(mode="json")).decode("utf-8")
    assert "parse_quality" in pack_json
    # ...but neither production renderer emits it to the model.
    for renderer in (
        evidence_body_renderer,
        foundation_renderer,
        critic_prefix_renderer,
    ):
        wire = "\n".join(
            message.content if hasattr(message, "content") else str(message)
            for message in renderer(draft)
        )
        assert "parse_quality" not in wire
