"""V1.1 canonical contract tests (M2-1).

Covers the singular data-core canonical asset/content/evidence identity
contracts: source taxonomy, canonical asset shape, the sole text-evidence
chain, TemporalIdentity windows, and DataRuntimeIdentity.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from catalyst_data.trading_calendar import session_close_utc


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _valid_asset(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "asset_id": "issuer:AAPL:filing:0000320193-26-000001",
        "asset_type": "FILING",
        "issuer_id": "issuer:AAPL",
        "tickers": ["AAPL"],
        "provider": "sec",
        "publisher": None,
        "canonical_url": None,
        "source_class": "official_government",
        "source_published_at": None,
        "eligible_at": _utc("2026-01-05T21:05:00Z"),
        "ingested_at": _utc("2026-01-05T21:06:00Z"),
        "temporal_precision": "accepted_time",
        "content_state": "FULL_TEXT",
        "serving_status": "active",
        "title": None,
        "content_ref": "doc-1",
        "content_hash": "a" * 64,
        "dedup_cluster_id": None,
        "parse_quality": "full",
        "subtype_metadata": {"accession": "0000320193-26-000001"},
    }
    base.update(overrides)
    return base


def test_content_state_is_exactly_the_five_frozen_values() -> None:
    from catalyst_data.canonical.model import ContentState

    assert get_args(ContentState) == (
        "FULL_TEXT",
        "TITLE_ONLY",
        "METADATA_ONLY",
        "EMPTY",
        "FAILED",
    )


def test_source_class_enum_is_exactly_the_seven_frozen_values() -> None:
    from catalyst_data.canonical.model import SourceClass

    assert [member.value for member in SourceClass] == [
        "structured_market_data",
        "official_government",
        "issuer_disclosure",
        "corporate_press_release",
        "reported_news",
        "analysis_opinion",
        "aggregated_unknown",
    ]


def test_source_role_mapping_is_frozen() -> None:
    from catalyst_data.canonical.model import SourceClass, SourceRole, source_role_for

    assert source_role_for(SourceClass.OFFICIAL_GOVERNMENT) is SourceRole.PRIMARY_AUTHORITY
    assert source_role_for(SourceClass.ISSUER_DISCLOSURE) is SourceRole.DIRECT_PRIMARY
    assert source_role_for(SourceClass.CORPORATE_PRESS_RELEASE) is SourceRole.DIRECT_PRIMARY
    assert source_role_for(SourceClass.REPORTED_NEWS) is SourceRole.INDEPENDENT_REPORT
    assert source_role_for(SourceClass.ANALYSIS_OPINION) is SourceRole.COMMENTARY_LEAD
    assert source_role_for(SourceClass.AGGREGATED_UNKNOWN) is SourceRole.UNKNOWN
    assert source_role_for(SourceClass.STRUCTURED_MARKET_DATA) is SourceRole.STRUCTURED_CONTEXT


def test_missing_publisher_stays_none_and_is_never_inferred() -> None:
    from catalyst_data.canonical.model import CanonicalAsset

    asset = CanonicalAsset(**_valid_asset())
    assert asset.publisher is None
    assert asset.model_dump()["publisher"] is None


def test_canonical_asset_is_frozen_forbids_extra_and_serializes_asset_id() -> None:
    from catalyst_data.canonical.model import CanonicalAsset

    asset = CanonicalAsset(**_valid_asset())
    with pytest.raises(ValidationError):
        asset.asset_id = "other"  # frozen
    with pytest.raises(ValidationError):
        CanonicalAsset(**_valid_asset(), unknown_field=True)  # extra forbidden
    # Serialized field is exactly asset_id; no canonical_asset_id alias/field.
    dumped = asset.model_dump()
    assert dumped["asset_id"] == asset.asset_id
    assert "canonical_asset_id" not in dumped
    assert "canonical_asset_id" not in CanonicalAsset.model_fields


def test_canonical_asset_rejects_unknown_content_state_and_asset_type() -> None:
    from catalyst_data.canonical.model import CanonicalAsset

    with pytest.raises(ValidationError):
        CanonicalAsset(**_valid_asset(content_state="FULL"))
    with pytest.raises(ValidationError):
        CanonicalAsset(**_valid_asset(asset_type="TWEET"))


def test_evidence_chain_enforces_sole_text_chain_shape() -> None:
    from catalyst_data.canonical.model import (
        CanonicalContentVersion,
        CanonicalEvidenceChain,
    )

    version = CanonicalContentVersion(
        canonical_content_version_id="content:v1:0001", asset_id="asset:1"
    )
    chain = CanonicalEvidenceChain(
        asset_id=version.asset_id,
        canonical_content_version_id=version.canonical_content_version_id,
        corpus_document_id="corpus:doc:0001",
        chunk_id="corpus:chunk:0001",
    )
    assert chain.asset_id == "asset:1"
    assert chain.canonical_content_version_id == "content:v1:0001"
    # A chain cannot carry a parallel fifth ID or the structured fact id.
    with pytest.raises(ValidationError):
        CanonicalEvidenceChain(
            asset_id="asset:1",
            canonical_content_version_id="content:v1:0001",
            corpus_document_id="corpus:doc:0001",
            chunk_id="corpus:chunk:0001",
            fact_id="fact:1",
        )


def test_text_and_structured_evidence_identity_are_separately_typed() -> None:
    from catalyst_data.canonical.model import (
        StructuredEvidenceIdentity,
        TextEvidenceIdentity,
    )

    text = TextEvidenceIdentity(
        evidence_id="corpus:chunk:0001", chunk_id="corpus:chunk:0001"
    )
    assert text.evidence_id == text.chunk_id
    with pytest.raises(ValidationError):
        TextEvidenceIdentity(evidence_id="corpus:chunk:0001", chunk_id="other-chunk")

    structured = StructuredEvidenceIdentity(
        evidence_id="fact:1", fact_id="fact:1"
    )
    assert structured.evidence_id == structured.fact_id
    with pytest.raises(ValidationError):
        StructuredEvidenceIdentity(evidence_id="fact:1", fact_id="other-fact")


def test_temporal_identity_contains_required_fields_and_rejects_post_cutoff() -> None:
    from catalyst_data.canonical.temporal import TemporalIdentity

    monday_close = _utc(session_close_utc("2026-01-05"))
    tuesday_close = _utc(session_close_utc("2026-01-06"))
    temporal = TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-06T14:30:00Z"),
        session_close_at=tuesday_close,
        information_window_start_at=monday_close,
        cutoff_at=tuesday_close,
    )
    assert temporal.session_date == "2026-01-06"
    assert temporal.market_timezone == "America/New_York"
    # Eligible at exactly the cutoff is inside the window; after cutoff is not.
    assert temporal.contains(tuesday_close) is True
    assert temporal.contains(_utc("2026-01-06T21:05:00Z")) is False
    assert "information_window_start_at" in TemporalIdentity.model_fields
    assert "cutoff_at" in TemporalIdentity.model_fields


def test_monday_1605_earnings_eligible_for_tuesday_window_via_calendar() -> None:
    from catalyst_data.canonical.temporal import TemporalIdentity

    # Attribution session is Tuesday 2026-01-06; Monday 2026-01-05 is the
    # previous regular session. 16:05 ET == 21:05 UTC on Monday.
    monday_close = _utc(session_close_utc("2026-01-05"))
    tuesday_close = _utc(session_close_utc("2026-01-06"))
    temporal = TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-06T14:30:00Z"),
        session_close_at=tuesday_close,
        information_window_start_at=monday_close,
        cutoff_at=tuesday_close,
    )
    earnings_at = _utc("2026-01-05T21:05:00Z")  # Monday 16:05 ET
    assert monday_close < earnings_at <= tuesday_close
    assert temporal.contains(earnings_at) is True


def test_temporal_identity_rejects_impossible_window_order() -> None:
    from catalyst_data.canonical.temporal import TemporalIdentity

    monday_close = _utc(session_close_utc("2026-01-05"))
    tuesday_close = _utc(session_close_utc("2026-01-06"))
    with pytest.raises(ValidationError):
        TemporalIdentity(
            session_date="2026-01-06",
            market_timezone="America/New_York",
            session_open_at=_utc("2026-01-06T14:30:00Z"),
            session_close_at=tuesday_close,
            information_window_start_at=tuesday_close,
            cutoff_at=monday_close,
        )


def test_data_runtime_identity_fields_are_exact_and_strict() -> None:
    from catalyst_data.canonical.identity import DataRuntimeIdentity

    identity = DataRuntimeIdentity(
        data_snapshot_id="snapshot:7a004",
        corpus_manifest_id="c" * 64,
        fts_index_version="fts:v3",
        query_policy_version="qp:v1",
    )
    assert identity.dense_index_version is None
    assert identity.embedding_model_revision is None
    assert identity.reranker_revision is None
    dumped = identity.model_dump()
    assert set(dumped) == {
        "data_snapshot_id",
        "corpus_manifest_id",
        "fts_index_version",
        "dense_index_version",
        "embedding_model_revision",
        "reranker_revision",
        "query_policy_version",
    }
    with pytest.raises(ValidationError):
        DataRuntimeIdentity(
            data_snapshot_id="snapshot:7a004",
            corpus_manifest_id="c" * 64,
            fts_index_version="fts:v3",
            query_policy_version="qp:v1",
            invented_field=True,
        )
