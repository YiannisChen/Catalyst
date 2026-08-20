"""Catalyst V1.1 canonical data contracts (data-core owned)."""

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import (
    CanonicalAsset,
    CanonicalContentVersion,
    CanonicalEvidenceChain,
    ContentState,
    SourceClass,
    SourceRole,
    StructuredEvidenceIdentity,
    TextEvidenceIdentity,
    source_role_for,
)
from catalyst_data.canonical.temporal import TemporalIdentity

__all__ = [
    "CanonicalAsset",
    "CanonicalContentVersion",
    "CanonicalEvidenceChain",
    "ContentState",
    "DataRuntimeIdentity",
    "SourceClass",
    "SourceRole",
    "StructuredEvidenceIdentity",
    "TemporalIdentity",
    "TextEvidenceIdentity",
    "source_role_for",
]
