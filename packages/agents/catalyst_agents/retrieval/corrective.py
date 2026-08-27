"""V1.1 corrective research contracts (M2-6, corrective).

Code-owned executable artifacts normalized from model proposals (Phase 4 TSD
§16–19; Frozen §6.4). Production freezes: max_corrective_rounds = 1,
max_actions_per_batch = 1, and a production batch contains exactly one action.
MARKET_STRUCTURE always normalizes to MARKET_STRUCTURE_UNSUPPORTED with
recoverable=false and never creates a retrieval action.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import re
import time
import unicodedata
from typing import Iterable

from pydantic import BaseModel, ConfigDict, model_validator

from catalyst_agents.retrieval.task import EvidenceNeed, TimeScope


class GapReasonCode(str, Enum):
    """The nine frozen V1.1 gap/capability reason codes (Frozen §6.4)."""

    MISSING_PRIMARY_CONFIRMATION = "MISSING_PRIMARY_CONFIRMATION"
    MISSING_INDEPENDENT_CORROBORATION = "MISSING_INDEPENDENT_CORROBORATION"
    MISSING_PRIOR_SESSION_CONTEXT = "MISSING_PRIOR_SESSION_CONTEXT"
    MISSING_SECTOR_CONTEXT = "MISSING_SECTOR_CONTEXT"
    MISSING_MACRO_CONTEXT = "MISSING_MACRO_CONTEXT"
    MISSING_FUNDAMENTAL_CONTEXT = "MISSING_FUNDAMENTAL_CONTEXT"
    CONFLICT_REQUIRES_RESOLUTION = "CONFLICT_REQUIRES_RESOLUTION"
    MARKET_STRUCTURE_UNSUPPORTED = "MARKET_STRUCTURE_UNSUPPORTED"
    LOCAL_COVERAGE_GAP = "LOCAL_COVERAGE_GAP"


MAX_CORRECTIVE_ROUNDS = 1
MAX_ACTIONS_PER_BATCH = 1


class MissingEvidence(BaseModel):
    """Code-normalized gap record; recoverability is code-owned (Phase 4 §16)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_id: str
    evidence_need: EvidenceNeed
    time_scope: TimeScope
    expected_information: str
    reason_code: GapReasonCode
    recoverable: bool

    @model_validator(mode="after")
    def _market_structure_normalization(self) -> "MissingEvidence":
        is_market_structure = self.evidence_need is EvidenceNeed.MARKET_STRUCTURE
        is_unsupported = (
            self.reason_code is GapReasonCode.MARKET_STRUCTURE_UNSUPPORTED
        )
        if is_market_structure or is_unsupported:
            if not (is_market_structure and is_unsupported):
                raise ValueError(
                    "MARKET_STRUCTURE must normalize to "
                    "MARKET_STRUCTURE_UNSUPPORTED"
                )
            if self.recoverable:
                raise ValueError(
                    "MARKET_STRUCTURE_UNSUPPORTED must be a non-recoverable gap"
                )
        return self


class CorrectiveResearchAction(BaseModel):
    """One code-owned corrective action (Phase 4 §17)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str
    gap_id: str
    evidence_need: EvidenceNeed
    time_scope: TimeScope
    lookback_sessions: int | None = None
    query_hints: tuple[str, ...] = ()
    research_fingerprint: str

    @model_validator(mode="after")
    def _no_market_structure_and_lookback_consistency(self) -> "CorrectiveResearchAction":
        if self.evidence_need is EvidenceNeed.MARKET_STRUCTURE:
            raise ValueError(
                "MARKET_STRUCTURE must never create a corrective retrieval action"
            )
        if (
            self.lookback_sessions is not None
            and self.time_scope is not TimeScope.LOOKBACK_SESSIONS
        ):
            raise ValueError("lookback_sessions requires LOOKBACK_SESSIONS time scope")
        if self.lookback_sessions is not None and self.lookback_sessions < 1:
            raise ValueError("lookback_sessions must be a positive session count")
        return self


class CorrectiveResearchBatch(BaseModel):
    """Code-owned batch; production freezes at exactly one action (Phase 4 §19)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    run_id: str
    round: int
    actions: tuple[CorrectiveResearchAction, ...]
    shared_deadline: datetime
    internal_deadline_monotonic: float
    total_result_budget: int
    policy_version: str
    cancellation_token_ref: str | None = None

    @model_validator(mode="after")
    def _production_freeze(self) -> "CorrectiveResearchBatch":
        if self.round < 1:
            raise ValueError("round must be positive")
        if len(self.actions) == 0:
            raise ValueError("a corrective research batch cannot be empty")
        if len(self.actions) > MAX_ACTIONS_PER_BATCH:
            raise ValueError(
                "production freeze: at most one corrective action per batch"
            )
        if self.total_result_budget < 1:
            raise ValueError("total_result_budget must be positive")
        gap_ids = [action.gap_id for action in self.actions]
        if len(gap_ids) != len(set(gap_ids)):
            raise ValueError("one gap ID maps to at most one action")
        return self


__all__ = [
    "BackendCapability",
    "corrective_research_tasks",
    "BackendHealth",
    "CorrectiveCapabilityRegistry",
    "CorrectivePolicy",
    "CorrectiveResearchAction",
    "CorrectiveResearchBatch",
    "GapReasonCode",
    "MAX_ACTIONS_PER_BATCH",
    "MAX_CORRECTIVE_ROUNDS",
    "MissingEvidence",
    "build_corrective_batch",
    "compute_internal_deadline_monotonic",
    "compute_remaining_seconds",
    "compute_research_fingerprint",
    "is_expired",
    "sanitize_query_hints",
]


# ---------------------------------------------------------------------------
# M5-3: capability registry, corrective policy, deadlines, batch construction
# (Phase 4 TSD §§16-20; Final TSD §12; M5 plan M5-3)
# ---------------------------------------------------------------------------

class BackendHealth(str, Enum):
    HEALTHY = "HEALTHY"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True)
class BackendCapability:
    """Typed local backend for one EvidenceNeed plus health state."""

    backend: str
    health: BackendHealth


class CorrectiveCapabilityRegistry:
    """Maps each EvidenceNeed to a typed local backend and health state.

    MARKET_STRUCTURE has no V1.1 backend and is always non-recoverable
    (Phase 4 §19; Frozen §6.4).
    """

    def __init__(self, capabilities: dict[EvidenceNeed, BackendCapability]):
        self._capabilities = dict(capabilities)

    def capability_for(self, need: EvidenceNeed) -> BackendCapability | None:
        return self._capabilities.get(need)

    def is_recoverable(self, need: EvidenceNeed) -> bool:
        if need is EvidenceNeed.MARKET_STRUCTURE:
            return False
        capability = self._capabilities.get(need)
        return capability is not None and capability.health is BackendHealth.HEALTHY


@dataclass(frozen=True)
class CorrectivePolicy:
    """Versioned corrective research policy (production freezes applied)."""

    max_corrective_rounds: int = MAX_CORRECTIVE_ROUNDS
    max_actions_per_batch: int = MAX_ACTIONS_PER_BATCH
    total_result_budget: int = 20
    deadline_seconds: float = 60.0
    shared_deadline_utc: datetime | None = None
    policy_version: str = "corrective-policy-v1"


# --- Locked deadline formulas (M5 plan §4 / M5-3) ----------------------------

def compute_remaining_seconds(
    shared_deadline_utc: datetime,
    now_utc: datetime,
) -> float:
    """Remaining seconds to a persisted UTC audit deadline (floor at zero)."""
    if shared_deadline_utc.tzinfo is None:
        raise ValueError("shared_deadline_utc must be timezone-aware UTC")
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware UTC")
    return max(0.0, (shared_deadline_utc - now_utc).total_seconds())


def compute_internal_deadline_monotonic(
    shared_deadline_utc: datetime,
    now_utc: datetime,
    now_monotonic: float,
) -> float:
    """Monotonic runtime enforcement deadline derived from the UTC deadline.

    A wall-clock jump between checks cannot extend or contract enforcement
    because remaining time derives from the persisted UTC deadline and is
    enforced on ``time.monotonic()``.
    """
    return now_monotonic + compute_remaining_seconds(shared_deadline_utc, now_utc)


def is_expired(internal_deadline_monotonic: float, now_monotonic: float) -> bool:
    """Boundary equality counts as expired (plan M5-3 TDD case c)."""
    return now_monotonic >= internal_deadline_monotonic


# --- Query hint sanitization --------------------------------------------------

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SQL_RE = re.compile(
    r"\b(select|insert|update|delete|drop|alter|create|union|from|where|join)\b",
    re.IGNORECASE,
)
_TOOL_RE = re.compile(
    r"(?:source:|site:|filetype:|date:|ticker=|index:|query:|"
    r"\btool\b|\bretrieve\b|\bsearch\b|\bsql\b|\bcurl\b|\bwget\b)",
    re.IGNORECASE,
)
_OPERATOR_RE = re.compile(r"\b(and|or|not|nearest|limit)\b", re.IGNORECASE)
_HINT_MAX_LENGTH = 200
_HINT_MAX_COUNT = 3


def sanitize_query_hints(hints: Iterable[str]) -> tuple[str, ...]:
    """Unicode/whitespace-normalize, bound, and strip unsafe hint content.

    Strips URLs, SQL/tool syntax, source selectors, explicit ticker/date
    controls, and retrieval operators (Phase 4 §17). Hints may carry only
    issuer/event concepts; trusted runtime fields own ticker/cutoff/filters.
    """
    cleaned: list[str] = []
    for raw in hints:
        value = unicodedata.normalize("NFC", str(raw))
        value = _URL_RE.sub(" ", value)
        value = _SQL_RE.sub(" ", value)
        value = _TOOL_RE.sub(" ", value)
        value = _OPERATOR_RE.sub(" ", value)
        value = " ".join(value.split())
        if not value:
            continue
        if len(value) > _HINT_MAX_LENGTH:
            value = value[:_HINT_MAX_LENGTH].rstrip()
        if value and value not in cleaned:
            cleaned.append(value)
        if len(cleaned) >= _HINT_MAX_COUNT:
            break
    return tuple(cleaned)


# --- Research fingerprint -----------------------------------------------------

def compute_research_fingerprint(
    *,
    evidence_need: EvidenceNeed,
    time_scope: TimeScope,
    lookback_sessions: int | None,
    sanitized_hints: tuple[str, ...],
    policy_version: str,
) -> str:
    """Deterministic fingerprint over trusted fields and sanitized hints."""
    payload = {
        "evidence_need": evidence_need.value,
        "time_scope": time_scope.value,
        "lookback_sessions": lookback_sessions,
        "sanitized_hints": sorted(sanitized_hints),
        "policy_version": policy_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _batch_deadline(
    policy: CorrectivePolicy,
    now_utc: datetime,
    now_monotonic: float | None = None,
) -> tuple[datetime, float, float]:
    """Return (shared_deadline_utc, remaining_seconds, internal_monotonic)."""
    shared = policy.shared_deadline_utc
    if shared is None:
        shared = now_utc + timedelta(seconds=policy.deadline_seconds)
    remaining = compute_remaining_seconds(shared, now_utc)
    internal_monotonic = compute_internal_deadline_monotonic(
        shared, now_utc, now_monotonic if now_monotonic is not None else time.monotonic()
    )
    return shared, remaining, internal_monotonic


def build_corrective_batch(
    assessment: "object",
    capabilities: CorrectiveCapabilityRegistry,
    policy: CorrectivePolicy,
    *,
    attempted_fingerprints: frozenset[str] = frozenset(),
    round: int = 1,
    now_utc: datetime | None = None,
    now_monotonic: float | None = None,
) -> CorrectiveResearchBatch | None:
    """Construct at most one code-owned corrective batch, or None.

    Refuses batch creation when: round is exhausted, the gap is non-recoverable
    or its backend is unavailable, the fingerprint repeats, the deadline is
    already expired, or no valid gap/intent pairing exists (Phase 4 §19/§20).
    """
    from catalyst_agents.attribution.assessment import EvidenceAssessment

    if not isinstance(assessment, EvidenceAssessment):
        raise TypeError("build_corrective_batch requires an EvidenceAssessment")

    if round > policy.max_corrective_rounds:
        return None
    if policy.max_actions_per_batch < 1:
        return None

    now = now_utc or datetime.now(timezone.utc)
    monotonic = now_monotonic if now_monotonic is not None else time.monotonic()
    shared_deadline, remaining, internal_monotonic = _batch_deadline(
        policy, now, now_monotonic=monotonic
    )
    if remaining <= 0:
        return None

    intents_by_gap = {
        intent.gap_id: intent
        for intent in getattr(assessment, "normalized_corrective_intents", ())
        if intent.gap_id is not None
    }

    action: CorrectiveResearchAction | None = None
    for gap in assessment.validated_missing_evidence:
        if not gap.recoverable:
            continue
        if not capabilities.is_recoverable(gap.evidence_need):
            continue
        if gap.evidence_need is EvidenceNeed.MARKET_STRUCTURE:
            continue
        intent = intents_by_gap.get(gap.gap_id)
        hints = sanitize_query_hints(intent.query_hints) if intent is not None else ()
        fingerprint = compute_research_fingerprint(
            evidence_need=gap.evidence_need,
            time_scope=gap.time_scope,
            lookback_sessions=None,
            sanitized_hints=hints,
            policy_version=policy.policy_version,
        )
        if fingerprint in attempted_fingerprints:
            return None
        action = CorrectiveResearchAction(
            action_id=f"action:{assessment.analyst_decision.schema_version}:{round}:{fingerprint[:12]}",
            gap_id=gap.gap_id,
            evidence_need=gap.evidence_need,
            time_scope=gap.time_scope,
            lookback_sessions=None,
            query_hints=hints,
            research_fingerprint=fingerprint,
        )
        break

    if action is None:
        return None

    return CorrectiveResearchBatch(
        batch_id=f"batch:{round}",
        run_id=getattr(assessment, "run_id", "run:unknown"),
        round=round,
        actions=(action,),
        shared_deadline=shared_deadline,
        internal_deadline_monotonic=internal_monotonic,
        total_result_budget=policy.total_result_budget,
        policy_version=policy.policy_version,
        cancellation_token_ref=None,
    )


def corrective_research_tasks(
    actions: tuple[CorrectiveResearchAction, ...],
    *,
    run_id: str,
    round: int,
    scenario: "object",
    retrieval_policy_id: str,
    source_classes_by_need: dict[object, tuple[object, ...]] | None = None,
) -> tuple[object, ...]:
    """Convert one corrective batch's actions into ResearchTasks (Phase 4 §17).

    Trusted runtime fields (ticker/cutoff/filters/limits) come from the
    executor call; only the typed need/scope/hints from the action enter the
    task. Task identity and fingerprint are deterministic.
    """
    from catalyst_agents.retrieval.policy import source_classes_for_need
    from catalyst_agents.retrieval.task import ResearchTask, ScenarioType
    from catalyst_data.canonical.ids import canonical_json_bytes

    tasks: list[ResearchTask] = []
    for priority, action in enumerate(actions):
        source_classes = tuple(
            source_classes_for_need(action.evidence_need)
            if source_classes_by_need is None
            else (source_classes_by_need or {}).get(action.evidence_need, ())
        )
        semantics = {
            "schema_version": "research_task_v1",
            "round": round,
            "priority": priority,
            "scenario": scenario.value if hasattr(scenario, "value") else str(scenario),
            "evidence_need": action.evidence_need.value,
            "time_scope": action.time_scope.value,
            "lookback_sessions": action.lookback_sessions,
            "ticker_scope": [],
            "source_classes": [c.value for c in source_classes],
            "evidence_types": [],
            "query_hints": list(action.query_hints),
            "retrieval_policy_id": retrieval_policy_id,
            "research_fingerprint": action.research_fingerprint,
        }
        fingerprint = hashlib.sha256(canonical_json_bytes(semantics)).hexdigest()
        tasks.append(
            ResearchTask(
                schema_version="research_task_v1",
                task_id=f"corrective:{run_id}:{round}:{priority}:{fingerprint[:12]}",
                round=round,
                priority=priority,
                scenario=scenario if isinstance(scenario, ScenarioType) else ScenarioType(ScenarioType.QUIET_OR_UNCLASSIFIED),
                evidence_need=action.evidence_need,
                time_scope=action.time_scope,
                lookback_sessions=action.lookback_sessions,
                ticker_scope=(),
                source_classes=source_classes,
                evidence_types=(),
                query_hints=action.query_hints,
                retrieval_policy_id=retrieval_policy_id,
                task_fingerprint=fingerprint,
            )
        )
    return tuple(tasks)
