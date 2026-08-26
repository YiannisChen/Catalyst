"""M4-6: deterministic ContextPack draft/finalizer pipeline (amendment §5).

Phase A: ``ContextPackBuilder`` creates an immutable ``PackedContextDraft``
(deterministic inventory, role views, selected excerpts, truncation records,
research history, observation, coverage, identities, budgets) over bounded
run-scoped artifacts only. Phase B: ``ContextPackFinalizer`` receives the
draft, the exact prompt-template UTF-8 bytes/version, a deterministic
text-message renderer, and a registered ``TokenCounter``; it computes the
template/render/pack hashes, the labelled token report, and the final
``EvidenceAnalystContextPack``. Replay repeats the pipeline and requires all
hashes to match. The renderer never includes ``context_pack_sha256``,
avoiding a hash cycle.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, model_validator

from catalyst_agents.attribution.context_pack import (
    ContextBudget,
    EvidenceAnalystContextPack,
    EvidencePayloadItem,
    PriorAssessmentContext,
    TokenCountReport,
    TruncationRecord,
)
from catalyst_agents.attribution.coverage import (
    CapabilityGap,
    CoverageSummary,
    DataCoverageGap,
)
from catalyst_agents.attribution.evidence_state import (
    EvidenceState,
    EvidenceStateItem,
    RetrievalDegradation,
)
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.retrieval.task import ResearchTask
from catalyst_agents.runtime.token_budget import TokenCounter, UTF8ByteUpperBoundCounter
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity

CONTEXT_PACK_SCHEMA_VERSION = "context_pack_v1"

_ROLE_ORDER = {
    "DIRECT_PRIMARY": 0,
    "PRIMARY_AUTHORITY": 1,
    "INDEPENDENT_REPORT": 2,
    "COMMENTARY_LEAD": 3,
    "STRUCTURED_CONTEXT": 4,
    "UNKNOWN": 5,
}


def canonical_context_pack_json(value: object) -> bytes:
    """Locked V1.1 canonical JSON serializer (sorted keys, compact separators,
    UTF-8, ensure_ascii=False, allow_nan=False)."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class RenderMessage(BaseModel):
    """Strict text record: only role and content are allowed in M4."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    content: str

    @model_validator(mode="after")
    def _non_empty_role(self) -> "RenderMessage":
        if not self.role:
            raise ValueError("render message role must not be empty")
        return self


class PackedContextDraft(BaseModel):
    """Immutable internal draft; does not fabricate the three final hashes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    packing_policy_version: str
    run_id: str
    round: int
    temporal_identity: TemporalIdentity
    data_runtime_identity: DataRuntimeIdentity
    context_budget: ContextBudget
    observation: MoveProfile
    coverage_summary: CoverageSummary
    research_history: tuple[ResearchTask, ...] = ()
    evidence_inventory: tuple[EvidencePayloadItem, ...] = ()
    direct_primary_evidence: tuple[EvidencePayloadItem, ...] = ()
    primary_authority_evidence: tuple[EvidencePayloadItem, ...] = ()
    independent_reports: tuple[EvidencePayloadItem, ...] = ()
    lead_only_evidence: tuple[EvidencePayloadItem, ...] = ()
    structured_context: tuple[EvidencePayloadItem, ...] = ()
    deterministic_conflict_signals: tuple[str, ...] = ()
    data_coverage_gaps: tuple[DataCoverageGap, ...] = ()
    capability_gaps: tuple[CapabilityGap, ...] = ()
    retrieval_degradations: tuple[RetrievalDegradation, ...] = ()
    included_evidence_ids: tuple[str, ...] = ()
    excluded_evidence_ids: tuple[str, ...] = ()
    truncation_metadata: tuple[TruncationRecord, ...] = ()
    delta_evidence_ids: tuple[str, ...] = ()
    prior_assessment_context: PriorAssessmentContext | None = None

    @model_validator(mode="after")
    def _partition_and_inventory(self) -> "PackedContextDraft":
        ids = {item.evidence_id for item in self.evidence_inventory}
        if len(ids) != len(self.evidence_inventory):
            raise ValueError("evidence_inventory must be unique by evidence_id")
        overlap = set(self.included_evidence_ids) & set(self.excluded_evidence_ids)
        if overlap:
            raise ValueError("included and excluded evidence IDs cannot overlap")
        unknown = (
            set(self.included_evidence_ids)
            | set(self.excluded_evidence_ids)
            | set(self.delta_evidence_ids)
        ) - ids
        if unknown:
            raise ValueError(f"draft references unknown inventory IDs: {sorted(unknown)}")
        return self


Renderer = Callable[[PackedContextDraft], tuple[RenderMessage, ...]]


def _to_payload_item(item: EvidenceStateItem) -> EvidencePayloadItem:
    return EvidencePayloadItem(
        evidence_id=item.evidence_id,
        canonical_asset_id=item.canonical_asset_id,
        canonical_content_version_id=item.canonical_content_version_id,
        corpus_document_id=item.corpus_document_id,
        chunk_id=item.chunk_id,
        fact_id=item.fact_id,
        section_key=item.section_key,
        chunk_ordinal=item.chunk_ordinal,
        source_class=item.source_class,
        evidence_role=item.evidence_role,
        eligible_at=item.eligible_at,
        content_state=item.content_state,
        material_capability=item.material_capability,
        independence_group_id=item.independence_group_id,
        independence_status=item.independence_status,
        content_hash=item.content_hash,
        excerpt_text=item.excerpt_text,
        source_start_offset=None,
        source_end_offset=None,
        tokenizer_identity=None,
    )


class ContextPackBuilder:
    """Deterministic packing over bounded run-scoped artifacts only (no corpus
    scan, no retrieval, no LLM)."""

    def __init__(
        self,
        *,
        packing_policy_version: str,
        schema_version: str = CONTEXT_PACK_SCHEMA_VERSION,
        budget: ContextBudget,
        token_counter: TokenCounter | None = None,
    ) -> None:
        if not packing_policy_version:
            raise ValueError("packing_policy_version must not be empty")
        self.packing_policy_version = packing_policy_version
        self.schema_version = schema_version
        self.budget = budget
        self.token_counter = token_counter or UTF8ByteUpperBoundCounter(
            provider="catalyst", model_id="analyst-default",
        )

    def build_draft(
        self,
        *,
        run_id: str,
        round: int,
        temporal_identity: TemporalIdentity,
        data_runtime_identity: DataRuntimeIdentity,
        evidence_state: EvidenceState,
        move_profile: MoveProfile,
        coverage_summary: CoverageSummary,
        research_history: tuple[ResearchTask, ...] = (),
        prior_assessment_context: PriorAssessmentContext | None = None,
    ) -> PackedContextDraft:
        if evidence_state.run_id != run_id:
            raise ValueError("evidence state run_id does not match the pack run")
        if evidence_state.round != round:
            raise ValueError("evidence state round does not match the pack round")
        if evidence_state.temporal_identity != temporal_identity:
            raise ValueError("evidence state TemporalIdentity does not match the pack")
        if evidence_state.data_runtime_identity != data_runtime_identity:
            raise ValueError(
                "evidence state DataRuntimeIdentity does not match the pack"
            )
        priorities = {
            result.task_id: result.priority for result in evidence_state.task_results
        }
        raw_entries = [
            (item, _to_payload_item(item))
            for item in (*evidence_state.evidence_items, *evidence_state.structured_facts)
        ]
        ordered = sorted(
            raw_entries,
            key=lambda entry: self._utility_key(entry[0], priorities, round),
        )

        records: dict[str, TruncationRecord] = {}
        excluded: set[str] = set()
        for item, payload in ordered:
            if payload.eligible_at > temporal_identity.cutoff_at:
                records[payload.evidence_id] = self._record(
                    payload, "EXCLUDED_INELIGIBLE", "post_cutoff", 0
                )
                excluded.add(payload.evidence_id)
            elif payload.content_state in {"EMPTY", "FAILED"}:
                records[payload.evidence_id] = self._record(
                    payload, "EXCLUDED_MATERIALITY", "never_retrieved", 0
                )
                excluded.add(payload.evidence_id)
            elif payload.content_state == "METADATA_ONLY":
                records[payload.evidence_id] = self._record(
                    payload, "METADATA_ONLY", "metadata_only_never_body", 0
                )

        # Novelty pass: one representative body per known independence group and
        # per exact duplicate document; distinct SEC sections survive.
        representatives: set[str] = set()
        seen_group: dict[str, str] = {}
        seen_document: dict[str, str] = {}
        seen_section: dict[str, str] = {}
        for item, payload in ordered:
            if payload.evidence_id in excluded:
                continue
            group = (
                payload.independence_group_id
                if payload.independence_status == "KNOWN_GROUP"
                and payload.independence_group_id
                else None
            )
            document_key = f"{payload.corpus_document_id}:{payload.content_hash}"
            section_key = f"{payload.canonical_asset_id}:{payload.section_key or ''}"
            if group is not None:
                if group in seen_group:
                    records[payload.evidence_id] = self._record(
                        payload, "EXCLUDED_DUPLICATE", "syndicated_group_member",
                        self._truncate_to_zero(payload),
                    )
                    excluded.add(payload.evidence_id)
                    continue
                seen_group[group] = payload.evidence_id
            if document_key in seen_document and payload.fact_id is None:
                records[payload.evidence_id] = self._record(
                    payload, "EXCLUDED_DUPLICATE", "exact_duplicate_document",
                    self._truncate_to_zero(payload),
                )
                excluded.add(payload.evidence_id)
                continue
            seen_document[document_key] = payload.evidence_id
            if section_key in seen_section and payload.fact_id is None:
                # Additional same-section chunks are only admitted within the
                # remaining payload budget (distinct sections always survive).
                records[payload.evidence_id] = self._record(
                    payload, "EXCLUDED_DUPLICATE", "additional_section_chunk",
                    self._truncate_to_zero(payload),
                )
                excluded.add(payload.evidence_id)
                continue
            seen_section[section_key] = payload.evidence_id
            representatives.add(payload.evidence_id)

        # Payload allocation with tokenizer-offset truncation. Payload items are
        # immutable; truncation produces rebuilt items used consistently in the
        # inventory and role views.
        final_payloads: dict[str, EvidencePayloadItem] = {}
        included: list[str] = []
        payload_budget_remaining = self.budget.evidence_payload_tokens
        for item, payload in ordered:
            if payload.evidence_id in excluded:
                final_payloads[payload.evidence_id] = payload
                continue
            per_item_cap = self._per_item_cap(payload)
            truncated, original, included_count = self.token_counter.truncate_with_offsets(
                payload.excerpt_text or "", per_item_cap
            )
            if included_count > payload_budget_remaining:
                records[payload.evidence_id] = self._record(
                    payload, "EXCLUDED_BUDGET", "payload_budget_exhausted", included_count
                )
                excluded.add(payload.evidence_id)
                final_payloads[payload.evidence_id] = payload
                continue
            payload_budget_remaining -= included_count
            included.append(payload.evidence_id)
            truncated_payload = payload.model_copy(
                update={
                    "excerpt_text": truncated or None,
                    "source_start_offset": 0 if truncated else None,
                    "source_end_offset": len(truncated) if truncated else None,
                    "tokenizer_identity": self.token_counter.counter_id,
                }
            )
            final_payloads[payload.evidence_id] = truncated_payload
            if truncated != (payload.excerpt_text or ""):
                records[payload.evidence_id] = self._record(
                    payload, "INCLUDED_TRUNCATED", "per_item_token_cap",
                    included_count, truncated,
                )

        inventory = tuple(final_payloads[payload.evidence_id] for _, payload in ordered)
        delta_ids = tuple(
            payload.evidence_id
            for _, payload in ordered
            if payload.evidence_id in included
            and self._first_seen_round(_, priorities) == round
        )
        role_views = self._role_views(inventory, set(included))

        return PackedContextDraft(
            schema_version=self.schema_version,
            packing_policy_version=self.packing_policy_version,
            run_id=run_id,
            round=round,
            temporal_identity=temporal_identity,
            data_runtime_identity=data_runtime_identity,
            context_budget=self.budget,
            observation=move_profile,
            coverage_summary=coverage_summary,
            research_history=tuple(research_history),
            evidence_inventory=inventory,
            direct_primary_evidence=role_views["DIRECT_PRIMARY"],
            primary_authority_evidence=role_views["PRIMARY_AUTHORITY"],
            independent_reports=role_views["INDEPENDENT_REPORT"],
            lead_only_evidence=role_views["LEAD_ONLY"],
            structured_context=role_views["STRUCTURED_CONTEXT"],
            deterministic_conflict_signals=(),
            data_coverage_gaps=coverage_summary.data_coverage_gaps,
            capability_gaps=coverage_summary.capability_gaps,
            retrieval_degradations=coverage_summary.retrieval_degradations,
            included_evidence_ids=tuple(included),
            excluded_evidence_ids=tuple(sorted(excluded)),
            truncation_metadata=tuple(
                records[key] for key in sorted(records)
            ),
            delta_evidence_ids=delta_ids,
            prior_assessment_context=prior_assessment_context,
        )

    # -- helpers ------------------------------------------------------------

    def _first_seen_round(self, item: EvidenceStateItem, priorities: dict[str, int]) -> int:
        del priorities
        return item.first_seen_round

    def _utility_key(
        self, item: EvidenceStateItem, priorities: dict[str, int], round: int
    ) -> tuple[object, ...]:
        best_task_priority = min(
            (priorities.get(task_id, 0) for task_id in item.contributing_task_ids),
            default=0,
        )
        reranker = [
            c.reranker_rank
            for c in item.retrieval_contributions
            if c.reranker_rank is not None
        ]
        fusion = [
            c.fusion_rank
            for c in item.retrieval_contributions
            if c.fusion_rank is not None
        ]
        lexical = [
            c.lexical_rank
            for c in item.retrieval_contributions
            if c.lexical_rank is not None
        ]
        dense = [
            c.dense_rank
            for c in item.retrieval_contributions
            if c.dense_rank is not None
        ]
        if reranker:
            stage_order, rank = 0, min(reranker)
        elif fusion:
            stage_order, rank = 1, min(fusion)
        else:
            candidates = []
            if lexical:
                candidates.append((2, min(lexical)))
            if dense:
                candidates.append((3, min(dense)))
            stage_order, rank = min(candidates) if candidates else (4, 0)
        role_order = _ROLE_ORDER.get(item.evidence_role, 99)
        delta = 0 if item.first_seen_round == round else 1
        return (
            delta,
            best_task_priority,
            stage_order,
            rank,
            role_order,
            -item.eligible_at.timestamp(),
            item.canonical_asset_id,
            item.corpus_document_id,
            item.section_key or "",
            item.chunk_ordinal or 0,
            item.evidence_id,
        )

    def _per_item_cap(self, payload: EvidencePayloadItem) -> int:
        if payload.fact_id is not None or payload.evidence_role == "STRUCTURED_CONTEXT":
            return self.budget.per_news_item_max_tokens
        if payload.section_key not in (None, "", "body"):
            return self.budget.per_sec_chunk_max_tokens
        if payload.material_capability in {"LEAD_ONLY", "NOT_CAPABLE"}:
            return self.budget.lead_only_tokens
        return self.budget.per_news_item_max_tokens

    def _record(
        self,
        payload: EvidencePayloadItem,
        action: str,
        reason: str,
        included_count: int,
        truncated: str | None = None,
    ) -> TruncationRecord:
        return TruncationRecord(
            evidence_id=payload.evidence_id,
            action=action,
            original_token_count=(
                self.token_counter.count_text(payload.excerpt_text or "")
                if payload.excerpt_text
                else None
            ),
            included_token_count=included_count,
            source_start_offset=0 if truncated is not None else None,
            source_end_offset=len(truncated) if truncated is not None else None,
            tokenizer_identity=self.token_counter.counter_id,
            reason_code=reason,
        )

    def _truncate_to_zero(self, payload: EvidencePayloadItem) -> int:
        return 0

    def _role_views(
        self,
        inventory: tuple[EvidencePayloadItem, ...],
        included: set[str],
    ) -> dict[str, tuple[EvidencePayloadItem, ...]]:
        views: dict[str, list[EvidencePayloadItem]] = {
            "DIRECT_PRIMARY": [],
            "PRIMARY_AUTHORITY": [],
            "INDEPENDENT_REPORT": [],
            "LEAD_ONLY": [],
            "STRUCTURED_CONTEXT": [],
        }
        for payload in inventory:
            if payload.evidence_id not in included:
                continue
            if payload.fact_id is not None and payload.evidence_role == "STRUCTURED_CONTEXT":
                views["STRUCTURED_CONTEXT"].append(payload)
            elif payload.evidence_role in {"COMMENTARY_LEAD", "UNKNOWN"}:
                views["LEAD_ONLY"].append(payload)
            elif payload.evidence_role in views:
                views[payload.evidence_role].append(payload)
        return {key: tuple(values) for key, values in views.items()}


class ContextPackFinalizer:
    """Phase B: hashes, token report, budget invariant, final pack."""

    def __init__(
        self,
        *,
        template_bytes: bytes,
        template_version: str,
        renderer: Renderer,
        token_counter: TokenCounter,
    ) -> None:
        if not template_bytes:
            raise ValueError("prompt template bytes must not be empty")
        if not template_version:
            raise ValueError("prompt template version must not be empty")
        self.template_bytes = bytes(template_bytes)
        self.template_version = template_version
        self.renderer = renderer
        self.token_counter = token_counter
        self.prompt_template_sha256 = hashlib.sha256(self.template_bytes).hexdigest()

    def finalize(self, draft: PackedContextDraft) -> EvidenceAnalystContextPack:
        messages = tuple(self.renderer(draft))
        normalized = tuple(
            RenderMessage.model_validate(
                message if isinstance(message, RenderMessage) else dict(message)
            )
            for message in messages
        )
        rendered_messages_sha256 = hashlib.sha256(
            canonical_context_pack_json(
                [message.model_dump(mode="json") for message in normalized]
            )
        ).hexdigest()
        rendered_tokens = sum(
            self.token_counter.count_text(message.content) for message in normalized
        )
        budget = draft.context_budget
        used = (
            rendered_tokens
            + budget.reserved_output_tokens
            + budget.safety_margin_tokens
        )
        if used > budget.model_context_limit:
            raise ValueError(
                "rendered messages exceed the model context limit; packing "
                "fails closed before any Analyst call"
            )
        report = TokenCountReport(
            tokenizer_identity=self.token_counter.counter_id,
            rendered_messages_tokens=rendered_tokens,
            reserved_output_tokens=budget.reserved_output_tokens,
            safety_margin_tokens=budget.safety_margin_tokens,
            remaining_payload_tokens=budget.model_context_limit - used,
        )
        payload: dict[str, Any] = {
            "schema_version": draft.schema_version,
            "packing_policy_version": draft.packing_policy_version,
            "run_id": draft.run_id,
            "round": draft.round,
            "temporal_identity": draft.temporal_identity,
            "data_runtime_identity": draft.data_runtime_identity,
            "context_budget": budget,
            "token_count_report": report,
            "hard_constraints": (
                "materiality_ceiling",
                "metadata_only_never_body",
                "cutoff_eligibility",
            ),
            "observation": draft.observation,
            "coverage_summary": draft.coverage_summary,
            "research_history": draft.research_history,
            "evidence_inventory": draft.evidence_inventory,
            "direct_primary_evidence": draft.direct_primary_evidence,
            "primary_authority_evidence": draft.primary_authority_evidence,
            "independent_reports": draft.independent_reports,
            "lead_only_evidence": draft.lead_only_evidence,
            "structured_context": draft.structured_context,
            "deterministic_conflict_signals": draft.deterministic_conflict_signals,
            "data_coverage_gaps": draft.data_coverage_gaps,
            "capability_gaps": draft.capability_gaps,
            "retrieval_degradations": draft.retrieval_degradations,
            "included_evidence_ids": draft.included_evidence_ids,
            "excluded_evidence_ids": draft.excluded_evidence_ids,
            "truncation_metadata": draft.truncation_metadata,
            "delta_evidence_ids": draft.delta_evidence_ids,
            "prior_assessment_context": draft.prior_assessment_context,
            "context_pack_sha256": "0" * 64,
            "prompt_template_version": self.template_version,
            "prompt_template_sha256": self.prompt_template_sha256,
            "rendered_messages_sha256": rendered_messages_sha256,
        }
        # Compute the pack hash over canonical JSON excluding context_pack_sha256.
        hash_payload = {
            key: value
            for key, value in payload.items()
            if key != "context_pack_sha256"
        }
        context_pack_sha256 = hashlib.sha256(
            canonical_context_pack_json(
                EvidenceAnalystContextPack.model_validate(
                    payload
                ).model_dump(mode="json", exclude={"context_pack_sha256"})
            )
        ).hexdigest()
        payload["context_pack_sha256"] = context_pack_sha256
        return EvidenceAnalystContextPack.model_validate(payload)


__all__ = [
    "CONTEXT_PACK_SCHEMA_VERSION",
    "ContextPackBuilder",
    "ContextPackFinalizer",
    "PackedContextDraft",
    "RenderMessage",
    "canonical_context_pack_json",
]
