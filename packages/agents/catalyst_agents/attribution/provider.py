from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Mapping, Protocol

if TYPE_CHECKING:
    from catalyst_agents.attribution.move_profile import ScheduledMacroFlag


@dataclass(frozen=True)
class ContextInputs:
    """Point-in-time structured observation inputs (data-core owned facts).

    M4-1 extends the PIT fact contract with session open, a second prior close
    (for the prior-session return), and configured major macro release facts.
    Missing facts stay explicit nulls/unknowns; the builder never fetches or
    fabricates proxies (Q-008).
    """

    ticker: str
    session_date: date
    cutoff: str
    target_close: float | None
    previous_target_close: float | None
    target_volume: float | None
    expected_prior_sessions: tuple[str, ...]
    prior_volumes_by_session: Mapping[str, float | None]
    benchmark_ticker: str | None
    benchmark_return_pct: float | None
    sector_ticker: str | None
    sector_return_pct: float | None
    peer_returns_by_ticker: Mapping[str, float | None]
    target_open: float | None = None
    previous_2_target_close: float | None = None
    scheduled_macro_flags: tuple["ScheduledMacroFlag", ...] = ()
    macro_source_available: bool = False


class ContextProvider(Protocol):
    def load_context_inputs(self, *, ticker: str, session_date: str, cutoff: str) -> ContextInputs:
        ...


@dataclass(frozen=True)
class RetrievedEvidence:
    """Adapter evidence record (M4-0: V1.1 hit contract).

    Carries the complete data-owned metadata from a V1.1 ``RetrievalHit`` so an
    ``EvidenceStateItem`` can be constructed without re-querying data-core
    tables. Legacy fields (``lexical_rank`` etc.) remain for baseline callers;
    the production adapter fills every field from the hit.
    """

    chunk_id: str | None
    document_id: str
    content_text: str
    available_at: str
    source_class: str
    ticker_associations: tuple[str, ...]
    dedup_cluster_id: str | None
    cluster_first_available_at: str
    representative_document_id: str
    is_novel: bool
    lexical_raw_score: float | None
    lexical_rank: int | None
    corpus_manifest_id: str
    index_manifest_id: str | None
    mode_requested: str
    mode_served: str
    is_degraded: bool
    fallback_reason: str | None
    fusion_score: float | None = None
    reranker_score: float | None = None
    reranker_rank: int | None = None
    dense_score: float | None = None
    dense_rank: int | None = None
    # AMEND-5.2A: structured temporal identity on production evidence.
    temporal_center_date: str | None = None
    query_date: str | None = None
    query_date_conflict: bool = False
    query_date_decision: str | None = None
    # M4-0: V1.1 data-owned metadata (amendment §1.3).
    canonical_asset_id: str | None = None
    canonical_content_version_id: str | None = None
    corpus_document_id: str | None = None
    section_key: str | None = None
    chunk_ordinal: int | None = None
    asset_type: str | None = None
    content_hash: str | None = None
    material_capability: str | None = None
    serving_status: str | None = None
    temporal_precision: str | None = None
    independence_group_id: str | None = None
    independence_status: str | None = None
    canonical_url: str | None = None
    publisher: str | None = None
    evidence_role: str | None = None
    parse_quality: str | None = None
    fact_id: str | None = None
    provider: str | None = None
    content_state: str | None = None
    eligible_at: object | None = None
    temporal_identity: object | None = None
    data_runtime_identity: object | None = None

    def to_evidence_state_item(
        self,
        *,
        first_seen_round: int,
        contributing_task_ids: tuple[str, ...],
        retrieval_contribution: "object | None" = None,
    ) -> "object":
        """Build the V1.1 EvidenceStateItem from this evidence record.

        The adapter output carries every data-owned metadata field; the state
        item is constructed without querying data-core tables again (M4-0 §1.3).
        """
        from datetime import datetime, timezone

        from catalyst_agents.attribution.evidence_state import EvidenceStateItem
        from catalyst_data.canonical.model import SourceClass, source_role_for

        is_fact = self.fact_id is not None
        chunk_id = None if is_fact else self.chunk_id
        fact_id = self.fact_id
        if is_fact:
            if fact_id is None:
                raise ValueError("structured fact evidence requires a fact_id")
            evidence_id = fact_id
        else:
            if self.chunk_id is None:
                raise ValueError("text evidence requires a chunk_id")
            evidence_id = self.chunk_id
        source_class = SourceClass(self.source_class)
        role = self.evidence_role or source_role_for(source_class).value
        material = self.material_capability
        if material is None:
            material = {
                "FULL_TEXT": "MATERIAL_CAPABLE",
                "TITLE_ONLY": "LEAD_ONLY",
                "METADATA_ONLY": "NOT_CAPABLE",
            }.get(self.content_state or "FULL_TEXT", "NOT_CAPABLE")
        independence_status = self.independence_status or (
            "KNOWN_GROUP"
            if source_class.value == "reported_news" and self.independence_group_id
            else "UNKNOWN"
        )
        asset_type = self.asset_type or ("STRUCTURED_CONTEXT" if is_fact else "NEWS")
        if self.eligible_at is not None:
            eligible_at = self.eligible_at
        else:
            raw = self.available_at
            text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            eligible_at = datetime.fromisoformat(text)
            if eligible_at.tzinfo is None:
                eligible_at = eligible_at.replace(tzinfo=timezone.utc)
        return EvidenceStateItem(
            evidence_id=evidence_id,
            canonical_asset_id=self.canonical_asset_id or self.document_id,
            canonical_content_version_id=self.canonical_content_version_id or "",
            corpus_document_id=self.corpus_document_id or self.document_id,
            chunk_id=chunk_id,
            fact_id=fact_id,
            section_key=self.section_key,
            chunk_ordinal=self.chunk_ordinal,
            asset_type=asset_type,
            provider=self.provider or "unknown",
            publisher=self.publisher,
            canonical_url=self.canonical_url,
            source_class=source_class,
            evidence_role=role,
            eligible_at=eligible_at,
            temporal_precision=self.temporal_precision or "unknown",
            content_state=self.content_state or "FULL_TEXT",
            material_capability=material,
            serving_status=self.serving_status or "body_candidate",
            parse_quality=self.parse_quality or "not_applicable",
            independence_group_id=self.independence_group_id,
            independence_status=independence_status,
            content_hash=self.content_hash,
            text_ref=evidence_id,
            excerpt_text=self.content_text or None,
            first_seen_round=first_seen_round,
            contributing_task_ids=tuple(contributing_task_ids),
            retrieval_contributions=(
                (retrieval_contribution,) if retrieval_contribution is not None else ()
            ),
        )

class Retriever(Protocol):
    def retrieve(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
        top_k: int = 8,
        candidate_depth: int = 20,
    ) -> tuple[RetrievedEvidence, ...]:
        ...
