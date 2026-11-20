"""Task 2.9: required evidence metadata fails closed at the provider seam.

``RetrievedEvidence.to_evidence_state_item`` is the third call site that derives
``material_capability`` from ``content_state`` (the others are the production
hybrid hit builder and the eval-only candidate adapter). Before Task 2.9 it held
its own copy of the mapping and defaulted twice -- ``content_state or
"FULL_TEXT"`` and ``.get(..., "NOT_CAPABLE")`` -- so an absent content state
became the most permissive state and a capability was substituted silently. The
recovery design forbids both: METADATA_ONLY/TITLE_ONLY never become citable
body, and substituting a capability silently mis-grades materiality.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from catalyst_agents.attribution.provider import RetrievedEvidence

ELIGIBLE = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)


def _evidence(evidence_id: str = "chunk:1", **overrides) -> RetrievedEvidence:
    """A complete FULL_TEXT filing record; overrides punch one hole at a time."""
    base: dict[str, object] = dict(
        chunk_id=evidence_id,
        document_id="doc:1",
        content_text="The 8-K states the binding commitment.",
        available_at="2026-01-15T10:00:00Z",
        source_class="official_government",
        ticker_associations=("AAPL",),
        dedup_cluster_id=None,
        cluster_first_available_at="2026-01-15T10:00:00Z",
        representative_document_id="doc:1",
        is_novel=False,
        lexical_raw_score=-1.0,
        lexical_rank=1,
        corpus_manifest_id="m" * 64,
        index_manifest_id="i" * 64,
        mode_requested="reranked",
        mode_served="reranked",
        is_degraded=False,
        fallback_reason=None,
        canonical_asset_id="asset:1",
        canonical_content_version_id="version:1",
        corpus_document_id="doc:1",
        section_key="body",
        chunk_ordinal=1,
        asset_type="FILING",
        content_hash="c" * 64,
        material_capability=None,
        serving_status="body_candidate",
        temporal_precision="accepted_time",
        evidence_role="PRIMARY_AUTHORITY",
        provider="sec",
        content_state="FULL_TEXT",
        parse_quality="full",
        eligible_at=ELIGIBLE,
    )
    base.update(overrides)
    return RetrievedEvidence(**base)  # type: ignore[arg-type]


def _item(evidence: RetrievedEvidence):
    return evidence.to_evidence_state_item(
        first_seen_round=1, contributing_task_ids=("research:1:0",)
    )


def test_full_text_chunk_arrives_material_capable_through_the_provider():
    """Every remaining call site yields MATERIAL_CAPABLE for a FULL_TEXT chunk.

    The provider derives the value when the record carries none, so this asserts
    the derivation runs through the single canonical mapping rather than a local
    copy of it.
    """
    item = _item(_evidence())
    assert item.content_state == "FULL_TEXT"
    assert item.material_capability == "MATERIAL_CAPABLE"


def test_declared_capability_that_matches_the_derivation_is_kept():
    item = _item(_evidence(material_capability="MATERIAL_CAPABLE"))
    assert item.material_capability == "MATERIAL_CAPABLE"


@pytest.mark.parametrize("missing", [None, ""])
def test_absent_content_state_fails_closed(missing):
    """Absence must never become citable body ("FULL_TEXT").

    An absent content state is the sharpest form of the defect: defaulting it
    made a citable body out of an item whose state is unknown.
    """
    with pytest.raises(ValueError) as excinfo:
        _item(_evidence(content_state=missing))
    message = str(excinfo.value)
    assert "content_state" in message
    assert "chunk:1" in message


@pytest.mark.parametrize("unmapped", ["EMPTY", "FAILED", "metadata_only", "WAT"])
def test_unmapped_content_state_raises_instead_of_producing_a_capability(unmapped):
    with pytest.raises(ValueError) as excinfo:
        _item(_evidence(content_state=unmapped))
    message = str(excinfo.value)
    assert "content_state" in message
    assert "chunk:1" in message
    assert unmapped in message


def test_capability_contradicting_content_state_fails_closed():
    """A supplied capability that contradicts the state is a silent mis-grade."""
    with pytest.raises(ValueError) as excinfo:
        _item(_evidence(content_state="FULL_TEXT", material_capability="NOT_CAPABLE"))
    message = str(excinfo.value)
    assert "material_capability" in message
    assert "chunk:1" in message


def test_metadata_only_content_state_never_becomes_material_capable():
    item = _item(_evidence(content_state="METADATA_ONLY", content_text=""))
    assert item.material_capability == "NOT_CAPABLE"


@pytest.mark.parametrize("missing", [None, ""])
def test_absent_parse_quality_fails_closed(missing):
    """parse_quality feeds the parse-degraded gate (coverage -> assessment)."""
    with pytest.raises(ValueError) as excinfo:
        _item(_evidence(parse_quality=missing))
    message = str(excinfo.value)
    assert "parse_quality" in message
    assert "chunk:1" in message


def test_fact_evidence_names_the_evidence_id_in_the_error():
    """The fail-closed message names the fact id when the item is a fact."""
    evidence = _evidence(
        chunk_id=None,
        fact_id="fact:7",
        content_state=None,
    )
    with pytest.raises(ValueError) as excinfo:
        _item(evidence)
    message = str(excinfo.value)
    assert "content_state" in message
    assert "fact:7" in message


def test_non_projected_placeholders_stay_out_of_the_gated_surface():
    """provider/temporal_precision/serving_status/asset_type are not projected.

    They carry documented placeholders rather than gated values: this pins that
    the item still builds, and that the *graded* fields are the record's own.
    """
    item = _item(_evidence(provider=None, temporal_precision=None, asset_type=None))
    assert item.provider == "unknown"
    assert item.temporal_precision == "unknown"
    assert item.content_state == "FULL_TEXT"
    assert item.material_capability == "MATERIAL_CAPABLE"
