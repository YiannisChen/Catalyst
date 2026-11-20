"""ChunkProfile protocol — contract for all B3 chunk profile engines."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# --- Filing chunk-profile identity ------------------------------------------
#
# The filing profile version is identity-bearing: it participates in
# ``corpus_document_id`` (``canonical/ids.py``) and in every ``chunk_id``
# (``{document_id}:{profile_version}:{section_key}:{ordinal}``). A change to the
# filing extraction or chunk composition therefore bumps this value, not merely
# the parser version. ``LEGACY_*`` stays resolvable so sealed builds keep
# reading; a rebuild re-derives identity under ``FILING_CHUNK_PROFILE_VERSION``.
# ``filing_v2`` is a historical frozen artifact: the corpus schema still stores
# it, but current retrieval filters reject it.
LEGACY_FILING_CHUNK_PROFILE_VERSIONS: tuple[str, ...] = ("filing_v2",)
FILING_CHUNK_PROFILE_VERSION = "filing_v4"
# Profiles a current build may serve (sealed ``filing_v3`` plus the new profile).
SUPPORTED_FILING_CHUNK_PROFILE_VERSIONS: tuple[str, ...] = (
    "filing_v3",
    FILING_CHUNK_PROFILE_VERSION,
)
# Every profile the corpus schema admits (storage; includes the frozen artifact).
FILING_CHUNK_PROFILE_VERSIONS: tuple[str, ...] = (
    *LEGACY_FILING_CHUNK_PROFILE_VERSIONS,
    *SUPPORTED_FILING_CHUNK_PROFILE_VERSIONS,
)


@dataclass
class ChunkResult:
    """One output chunk from a chunk profile engine."""

    chunk_id: str
    document_id: str
    chunk_profile_version: str
    section_key: str
    ordinal: str  # 4-digit zero-padded

    content_text: str
    content_hash: str  # SHA-256 hex, lowercase
    metadata_hash: str  # SHA-256 hex, lowercase

    source_class: str
    available_at: str
    ticker_associations: str  # JSON array string

    eligibility: str = "eligible"

    boundary_kind: str = "document_end"
    body_token_start: int = 0
    body_token_end: int = 0
    body_overlap_tokens: int = 0
    prefix_token_count: int = 0
    prefix_truncated: bool = False
    section_parse_degraded: bool = False


@runtime_checkable
class ChunkProfile(Protocol):
    """Protocol for chunk profile engines.

    Each profile transforms one canonical document into zero or more
    searchable chunks with stable identities, content hash, and metadata hash.
    """

    @property
    def profile_version(self) -> str: ...

    def chunk(self, document: dict) -> list[ChunkResult]:
        """Transform one document into a list of searchable chunks."""
        ...


def should_chunk(source_type: str) -> bool:
    """Return True if *source_type* documents should be chunked.

    Raw JSON, OHLCV, FRED arrays, FMP raw statements, and SEC submissions
    manifests emit zero searchable chunks.
    """
    return source_type.strip().lower() not in {
        "ohlcv",
        "market_data",
        "fred",
        "fred_observations",
        "fmp_raw",
        "fmp_statement",
        "fmp_statements",
        "sec_submissions",
        "sec_submissions_manifest",
        "provider_envelope",
        "raw_json",
    }
