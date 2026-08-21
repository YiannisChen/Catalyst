"""V1.1 canonical data contracts (M2-1).

Singular ownership: data-core owns canonical asset identity, source taxonomy,
the sole text-evidence chain, and the separately typed text/structured evidence
identities (Final Migration TSD §4.2, §5.2).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

ContentState = Literal[
    "FULL_TEXT",
    "TITLE_ONLY",
    "METADATA_ONLY",
    "EMPTY",
    "FAILED",
]

AssetType = Literal["NEWS", "FILING", "OFFICIAL_RELEASE", "STRUCTURED_CONTEXT"]


class SourceClass(str, Enum):
    """Frozen V1.1 source taxonomy (Frozen §5.1)."""

    STRUCTURED_MARKET_DATA = "structured_market_data"
    OFFICIAL_GOVERNMENT = "official_government"
    ISSUER_DISCLOSURE = "issuer_disclosure"
    CORPORATE_PRESS_RELEASE = "corporate_press_release"
    REPORTED_NEWS = "reported_news"
    ANALYSIS_OPINION = "analysis_opinion"
    AGGREGATED_UNKNOWN = "aggregated_unknown"


class SourceRole(str, Enum):
    """Evidence-role ceiling derived deterministically from source class."""

    STRUCTURED_CONTEXT = "STRUCTURED_CONTEXT"
    PRIMARY_AUTHORITY = "PRIMARY_AUTHORITY"
    DIRECT_PRIMARY = "DIRECT_PRIMARY"
    INDEPENDENT_REPORT = "INDEPENDENT_REPORT"
    COMMENTARY_LEAD = "COMMENTARY_LEAD"
    UNKNOWN = "UNKNOWN"


_SOURCE_ROLE_MAPPING: dict[SourceClass, SourceRole] = {
    SourceClass.STRUCTURED_MARKET_DATA: SourceRole.STRUCTURED_CONTEXT,
    SourceClass.OFFICIAL_GOVERNMENT: SourceRole.PRIMARY_AUTHORITY,
    SourceClass.ISSUER_DISCLOSURE: SourceRole.DIRECT_PRIMARY,
    SourceClass.CORPORATE_PRESS_RELEASE: SourceRole.DIRECT_PRIMARY,
    SourceClass.REPORTED_NEWS: SourceRole.INDEPENDENT_REPORT,
    SourceClass.ANALYSIS_OPINION: SourceRole.COMMENTARY_LEAD,
    SourceClass.AGGREGATED_UNKNOWN: SourceRole.UNKNOWN,
}


def source_role_for(source_class: SourceClass) -> SourceRole:
    """Return the frozen evidence-role ceiling for a source class.

    The mapping is derived from source class only. A missing publisher is never
    used to infer a role (Final Migration TSD §5.2).
    """
    return _SOURCE_ROLE_MAPPING[source_class]


CanonicalMetadataScalar = str | int | float | bool | None


class CanonicalMetadataEntry(BaseModel):
    """One immutable scalar subtype-metadata field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    value: CanonicalMetadataScalar

    @model_validator(mode="after")
    def _non_empty_key(self) -> "CanonicalMetadataEntry":
        if not self.key:
            raise ValueError("subtype metadata key must not be empty")
        return self


class CanonicalAsset(BaseModel):
    """Frozen V1.1 canonical asset record (Frozen §5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: str
    asset_type: AssetType
    issuer_id: str
    tickers: tuple[str, ...]
    provider: str
    publisher: str | None = None
    canonical_url: str | None = None
    source_class: SourceClass
    source_published_at: datetime | None = None
    eligible_at: datetime
    ingested_at: datetime
    temporal_precision: str
    content_state: ContentState
    serving_status: str
    title: str | None = None
    content_ref: str | None = None
    content_hash: str | None = None
    dedup_cluster_id: str | None = None
    parse_quality: str
    subtype_metadata: tuple[CanonicalMetadataEntry, ...]

    @field_validator("subtype_metadata", mode="before")
    @classmethod
    def _canonical_metadata_entries(cls, value: object) -> object:
        if isinstance(value, dict):
            return tuple(
                CanonicalMetadataEntry(key=key, value=item)
                for key, item in sorted(value.items())
            )
        return value


class CanonicalContentVersion(BaseModel):
    """A content version belongs to exactly one canonical asset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_content_version_id: str
    asset_id: str


class CanonicalEvidenceChain(BaseModel):
    """The sole text-evidence chain (Final Migration TSD §5.2).

    asset_id -> canonical_content_version_id -> corpus_document_id -> chunk_id
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: str
    canonical_content_version_id: str
    corpus_document_id: str
    chunk_id: str


class TextEvidenceIdentity(BaseModel):
    """Text evidence identity: evidence_id equals the stable chunk_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    chunk_id: str

    @model_validator(mode="after")
    def _evidence_is_chunk_id(self) -> "TextEvidenceIdentity":
        if self.evidence_id != self.chunk_id:
            raise ValueError("text evidence_id must equal chunk_id")
        return self


class StructuredEvidenceIdentity(BaseModel):
    """Structured evidence identity: evidence_id equals the stable fact_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    fact_id: str

    @model_validator(mode="after")
    def _evidence_is_fact_id(self) -> "StructuredEvidenceIdentity":
        if self.evidence_id != self.fact_id:
            raise ValueError("structured evidence_id must equal fact_id")
        return self


__all__ = [
    "AssetType",
    "CanonicalAsset",
    "CanonicalMetadataEntry",
    "CanonicalContentVersion",
    "CanonicalEvidenceChain",
    "ContentState",
    "SourceClass",
    "SourceRole",
    "StructuredEvidenceIdentity",
    "TextEvidenceIdentity",
    "source_role_for",
]
