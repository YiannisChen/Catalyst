from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from catalyst_agents.attribution.move_profile import AlignmentBand, MoveProfile
from catalyst_agents.retrieval.task import (
    EvidenceNeed,
    ResearchTask,
    ScenarioType,
    TimeScope,
)
from catalyst_agents.attribution.evidence_state import CapabilityGap
from catalyst_data.canonical.model import SourceClass

if TYPE_CHECKING:
    from catalyst_agents.runtime.manifest import ObservationPolicyConfig


MAX_LAYERS_P0 = 1
MAX_EXPANSIONS = 0
DEFAULT_TOP_K = 8
DEFAULT_CANDIDATE_DEPTH = 20


class Layer(str, Enum):
    DIRECT = "direct"
    MACRO = "macro"
    RELATED = "related"


@dataclass
class RetrievalMetadata:
    ticker: str
    trade_date: str
    date_range: tuple[str, str] | None = None
    cutoff: str | None = None
    db_path: Path | str | None = None
    lancedb_dir: Path | str | None = None
    table: Any = None
    embedding_fn: Any = None
    requested_manifest_id: str = "corpus-fixture-v1"
    retriever: Any = None
    top_k: int = DEFAULT_TOP_K
    candidate_depth: int = DEFAULT_CANDIDATE_DEPTH
    layers_attempted: list[Layer] = field(default_factory=list)
    expansion_reasons: list[str] = field(default_factory=list)
    stop_reason: Literal["sufficiency_reached", "expansions_exhausted", "layer3_not_implemented", "system_error"] | None = None
    hit_counts_per_layer: dict[Layer, int] = field(default_factory=dict)
    total_unique_evidence: int = 0


class RetrievalDependencyError(RuntimeError):
    pass


def check_sufficiency(chunks: list[dict[str, Any]], min_count: int = 1, min_mean_score: float = 0.0) -> bool:
    return len(chunks) >= min_count


def retrieve(query: str, layer: Layer, metadata: RetrievalMetadata, *, rerank: Any = None) -> list[Any]:
    if metadata.retriever is None:
        raise RetrievalDependencyError("B5 Miner requires an injected Retriever")
    metadata.layers_attempted.append(layer)
    if not metadata.expansion_reasons:
        metadata.expansion_reasons.append("initial")
    if metadata.cutoff is None:
        raise RetrievalDependencyError("B5 Miner requires a canonical cutoff")
    results = list(metadata.retriever.retrieve(
        query,
        ticker=metadata.ticker,
        cutoff=metadata.cutoff,
        requested_manifest_id=metadata.requested_manifest_id,
        top_k=metadata.top_k,
        candidate_depth=metadata.candidate_depth,
    ))
    metadata.hit_counts_per_layer[layer] = len(results)
    metadata.total_unique_evidence = len({getattr(item, "chunk_id", None) for item in results})
    metadata.stop_reason = "sufficiency_reached" if results else "expansions_exhausted"
    return results


# ---------------------------------------------------------------------------
# M4-2: deterministic InitialResearchPolicy (Frozen §6.2; Phase 3 TSD §6-7)
# ---------------------------------------------------------------------------

INITIAL_RETRIEVAL_POLICY_ID = "qp:v1"
MAX_INITIAL_TASKS = 3

# Default M4 executable needs: only company primary/news hybrid text retrieval
# is bound to the production backend. Sector/macro/fundamentals remain
# capability-gated until their Phase-2/3 adapters are healthy (Final TSD §12).
_DEFAULT_SUPPORTED_INITIAL_NEEDS: frozenset[EvidenceNeed] = frozenset(
    {EvidenceNeed.COMPANY_PRIMARY, EvidenceNeed.COMPANY_NEWS}
)

_SOURCE_CLASSES_BY_NEED: dict[EvidenceNeed, tuple[SourceClass, ...]] = {
    EvidenceNeed.COMPANY_PRIMARY: (
        SourceClass.ISSUER_DISCLOSURE,
        SourceClass.CORPORATE_PRESS_RELEASE,
        SourceClass.OFFICIAL_GOVERNMENT,
    ),
    EvidenceNeed.COMPANY_NEWS: (
        SourceClass.REPORTED_NEWS,
        SourceClass.ANALYSIS_OPINION,
        SourceClass.AGGREGATED_UNKNOWN,
    ),
}


class ScenarioClassification(BaseModel):
    """Deterministic classification record (Phase 3 TSD §6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: ScenarioType
    scenario_policy_version: str
    matched_predicate: str
    input_field_names: tuple[str, ...]
    reason_codes: tuple[str, ...]
    tasks: tuple[ResearchTask, ...] = ()
    capability_gaps: tuple["CapabilityGap", ...] = ()

    @model_validator(mode="after")
    def _bounded_and_unique_tasks(self) -> "ScenarioClassification":
        if len(self.tasks) > MAX_INITIAL_TASKS:
            raise ValueError(f"initial policy emits at most {MAX_INITIAL_TASKS} tasks")
        fingerprints = [task.task_fingerprint for task in self.tasks]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("initial policy tasks must be unique by fingerprint")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("reason_codes must be sorted and unique")
        return self


def _task_fingerprint(payload: dict[str, object]) -> str:
    from catalyst_data.canonical.ids import canonical_json_bytes

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _build_task(
    *,
    priority: int,
    scenario: ScenarioType,
    evidence_need: EvidenceNeed,
    time_scope: TimeScope,
    retrieval_policy_id: str,
    source_classes: tuple[SourceClass, ...],
) -> ResearchTask:
    semantics: dict[str, object] = {
        "schema_version": "research_task_v1",
        "round": 1,
        "priority": priority,
        "scenario": scenario.value,
        "evidence_need": evidence_need.value,
        "time_scope": time_scope.value,
        "lookback_sessions": None,
        "ticker_scope": [],
        "source_classes": [source_class.value for source_class in source_classes],
        "evidence_types": [],
        "query_hints": [],
        "retrieval_policy_id": retrieval_policy_id,
    }
    fingerprint = _task_fingerprint(semantics)
    return ResearchTask(
        schema_version="research_task_v1",
        task_id=f"research:1:{priority}:{fingerprint[:12]}",
        round=1,
        priority=priority,
        scenario=scenario,
        evidence_need=evidence_need,
        time_scope=time_scope,
        lookback_sessions=None,
        ticker_scope=(),
        source_classes=source_classes,
        evidence_types=(),
        query_hints=(),
        retrieval_policy_id=retrieval_policy_id,
        task_fingerprint=fingerprint,
    )


class InitialResearchPolicy:
    """Deterministic scenario classification and initial research task policy.

    Uses only the MoveProfile and pinned ObservationPolicyConfig (Frozen §6.2);
    CONTINUATION never consults news/evidence. Scenario precedence is exact:
    SCHEDULED_MACRO → CONTINUATION → BROAD_SECTOR → COMPANY_SPECIFIC →
    QUIET_OR_UNCLASSIFIED. Unsupported evidence needs emit typed CapabilityGap
    records and never widen to generic company retrieval.
    """

    def __init__(
        self,
        config: "ObservationPolicyConfig",
        *,
        supported_evidence_needs: frozenset[EvidenceNeed] | None = None,
    ) -> None:
        self.config = config
        self.supported_needs = (
            _DEFAULT_SUPPORTED_INITIAL_NEEDS
            if supported_evidence_needs is None
            else frozenset(supported_evidence_needs)
        )

    def classify(self, move_profile: MoveProfile) -> ScenarioClassification:
        scenario, predicate, fields, reason_codes = self._predicate(move_profile)
        tasks, gaps = self._tasks_for(scenario, move_profile)
        return ScenarioClassification(
            scenario=scenario,
            scenario_policy_version=self.config.scenario_policy_version,
            matched_predicate=predicate,
            input_field_names=tuple(sorted(fields)),
            reason_codes=tuple(sorted(set(reason_codes))),
            tasks=tasks,
            capability_gaps=gaps,
        )

    def capability_gap_for(self, evidence_need: EvidenceNeed) -> "CapabilityGap":
        """Typed capability gap: the evidence need has no V1.1 backend."""
        from catalyst_agents.attribution.evidence_state import CapabilityGap

        gap_id = f"cap:{evidence_need.value.lower()}"
        return CapabilityGap(
            gap_id=gap_id,
            evidence_need=evidence_need,
            reason_code="BACKEND_NOT_IMPLEMENTED",
        )

    # -- predicates ---------------------------------------------------------

    def _predicate(
        self, profile: MoveProfile
    ) -> tuple[ScenarioType, str, set[str], list[str]]:
        cfg = self.config
        fields: set[str] = set()
        reason_codes: list[str] = []

        macro_match = bool(profile.scheduled_macro_flags)
        market_aligned = (
            profile.market_comove is not None
            and profile.market_comove.band is AlignmentBand.ALIGNED
        )
        sector_aligned = (
            profile.sector_comove is not None
            and profile.sector_comove.band is AlignmentBand.ALIGNED
        )
        fields.update(
            {
                "target_return",
                "prior_session_return",
                "scheduled_macro_flags",
                "market_comove",
                "sector_comove",
                "peer_comove",
                "peer_summary",
            }
        )
        if macro_match and (market_aligned or sector_aligned):
            return (
                ScenarioType.SCHEDULED_MACRO,
                "SCHEDULED_MACRO",
                fields,
                ["macro_flag_present", "broad_co_move"],
            )

        target = profile.target_return
        prior = profile.prior_session_return
        target_material = (
            target is not None and abs(target) >= cfg.material_target_return_pct
        )
        prior_material = (
            prior is not None and abs(prior) >= cfg.material_prior_return_pct
        )
        same_sign = (
            target is not None
            and prior is not None
            and target != 0
            and prior != 0
            and (target > 0) == (prior > 0)
        )
        if target_material and prior_material and same_sign:
            return (
                ScenarioType.CONTINUATION,
                "CONTINUATION",
                fields,
                ["material_prior_move", "consistent_direction"],
            )

        peer_ok = (
            profile.peer_comove is not None
            and profile.peer_comove.band is AlignmentBand.ALIGNED
            and profile.peer_summary is not None
            and profile.peer_summary.available_peer_count >= cfg.minimum_peer_count
        )
        broad_sector = target_material and sector_aligned
        if cfg.require_sector_and_peer_for_broad_sector:
            broad_sector = broad_sector and peer_ok
        if broad_sector:
            return (
                ScenarioType.BROAD_SECTOR,
                "BROAD_SECTOR",
                fields,
                ["sector_aligned", "peer_aligned"],
            )

        alignments = [
            alignment
            for alignment in (profile.market_comove, profile.sector_comove, profile.peer_comove)
            if alignment is not None and alignment.band is AlignmentBand.DIVERGENT
        ]
        if target_material and alignments:
            return (
                ScenarioType.COMPANY_SPECIFIC,
                "COMPANY_SPECIFIC",
                fields,
                ["comparator_divergent"],
            )

        if target is None:
            reason_codes.append("target_return_unavailable")
        if target is not None and abs(target) < cfg.quiet_target_return_pct:
            reason_codes.append("quiet_target_return")
        return (
            ScenarioType.QUIET_OR_UNCLASSIFIED,
            "QUIET_OR_UNCLASSIFIED",
            fields,
            reason_codes,
        )

    # -- task construction --------------------------------------------------

    def _tasks_for(
        self, scenario: ScenarioType, profile: MoveProfile
    ) -> tuple[tuple[ResearchTask, ...], tuple[CapabilityGap, ...]]:
        session = TimeScope.SESSION_INFORMATION_WINDOW
        prior = TimeScope.PRIOR_SESSION
        definitions: list[tuple[EvidenceNeed, TimeScope]] = {
            ScenarioType.SCHEDULED_MACRO: [
                (EvidenceNeed.MACRO_EVENT, session),
                (EvidenceNeed.MACRO_SERIES, session),
                (EvidenceNeed.COMPANY_NEWS, session),
            ],
            ScenarioType.CONTINUATION: [
                (EvidenceNeed.COMPANY_PRIMARY, prior),
                (EvidenceNeed.COMPANY_NEWS, prior),
                (EvidenceNeed.COMPANY_NEWS, session),
            ],
            ScenarioType.BROAD_SECTOR: [
                (EvidenceNeed.SECTOR_NEWS, session),
                (EvidenceNeed.MACRO_EVENT, session),
                (EvidenceNeed.COMPANY_NEWS, session),
            ],
            ScenarioType.COMPANY_SPECIFIC: [
                (EvidenceNeed.COMPANY_PRIMARY, session),
                (EvidenceNeed.COMPANY_NEWS, session),
            ],
            ScenarioType.QUIET_OR_UNCLASSIFIED: [
                (EvidenceNeed.COMPANY_NEWS, session),
                (EvidenceNeed.COMPANY_PRIMARY, session),
            ],
        }[scenario]

        tasks: list[ResearchTask] = []
        gaps: list[CapabilityGap] = []
        seen: set[tuple[object, ...]] = set()
        for priority, (need, time_scope) in enumerate(definitions):
            if need not in self.supported_needs:
                gaps.append(self.capability_gap_for(need))
                continue
            source_classes = _SOURCE_CLASSES_BY_NEED.get(need, ())
            dedup_key = (
                need,
                time_scope,
                None,
                (),
                source_classes,
                (),
                INITIAL_RETRIEVAL_POLICY_ID,
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            tasks.append(
                _build_task(
                    priority=priority,
                    scenario=scenario,
                    evidence_need=need,
                    time_scope=time_scope,
                    retrieval_policy_id=INITIAL_RETRIEVAL_POLICY_ID,
                    source_classes=source_classes,
                )
            )
        return tuple(tasks), tuple(gaps)



__all__ = [
    "INITIAL_RETRIEVAL_POLICY_ID",
    "InitialResearchPolicy",
    "Layer",
    "MAX_INITIAL_TASKS",
    "RetrievalMetadata",
    "ScenarioClassification",
    "check_sufficiency",
    "retrieve",
]
