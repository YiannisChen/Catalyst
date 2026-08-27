"""M4-3: bounded deterministic research executor.

One thin executor over the production retrieval and structured-data seams
(Frozen §6.2; Phase 3 TSD §8). Independent initial ResearchTasks run with
bounded parallelism under one shared absolute stage deadline and merge
deterministically by task priority -> task ID -> within-task rank ->
evidence ID; completion timing never determines order. The executor owns no
run admission or lifecycle (Final TSD §15); it never creates a second
retrieval engine and structured facts never receive fabricated
lexical/dense scores.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
import time
from typing import Callable, Protocol

from catalyst_agents.attribution.evidence_state import (
    CapabilityGap,
    EvidenceStateItem,
    ResearchTaskResult,
    ResearchTaskResultStatus,
    RetrievalContribution,
    RetrievalDegradation,
)
from catalyst_agents.attribution.provider import RetrievedEvidence
from catalyst_agents.retrieval.task import (
    ResearchTask,
    RetrievalStrategy,
    retrieval_strategy_for,
)
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval.result import RetrievalContractError


class ResearchIntegrityError(RuntimeError):
    """An identity/integrity failure that fails the run (never ABSTAIN)."""


class ResearchDeadlineError(RuntimeError):
    """The shared research stage deadline expired (technical failure)."""


class ResearchRetriever(Protocol):
    def retrieve(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
        temporal_identity: TemporalIdentity | None = None,
        top_k: int = 8,
        candidate_depth: int = 20,
    ) -> tuple[RetrievedEvidence, ...]:
        ...


class StructuredFactProvider(Protocol):
    def __call__(self, task: ResearchTask) -> tuple[EvidenceStateItem, ...]:
        ...


QueryBuilder = Callable[[ResearchTask, str], str]


@dataclass(frozen=True)
class ResearchExecution:
    """Deterministic stage output: ordered task results + typed gaps."""

    task_results: tuple[ResearchTaskResult, ...]
    capability_gaps: tuple[CapabilityGap, ...]
    degradations: tuple[RetrievalDegradation, ...]
    deadline_exhausted: bool = False


def _default_query(task: ResearchTask, ticker: str) -> str:
    label = task.evidence_need.value.lower().replace("_", " ")
    return f"{ticker} {label}"


class _Accumulator:
    """Thread-safe collection of stage outputs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.results: list[ResearchTaskResult] = []
        self.gaps: list[CapabilityGap] = []
        self.degradations: list[RetrievalDegradation] = []

    def add_result(self, result: ResearchTaskResult) -> None:
        with self._lock:
            self.results.append(result)

    def add_gap(self, gap: CapabilityGap) -> None:
        with self._lock:
            self.gaps.append(gap)

    def add_degradation(self, degradation: RetrievalDegradation) -> None:
        with self._lock:
            self.degradations.append(degradation)


class ResearchExecutor:
    """Bounded within-run research fan-out with injected resource limits."""

    def __init__(
        self,
        *,
        retriever: ResearchRetriever,
        concurrency: int,
        stage_timeout_seconds: float,
        structured_provider: StructuredFactProvider | None = None,
        query_builder: QueryBuilder | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("research concurrency must be at least 1")
        if stage_timeout_seconds <= 0:
            raise ValueError("stage timeout must be positive")
        self.retriever = retriever
        self.concurrency = concurrency
        self.stage_timeout_seconds = stage_timeout_seconds
        self.structured_provider = structured_provider
        self.query_builder = query_builder or _default_query

    def execute(
        self,
        *,
        tasks: tuple[ResearchTask, ...],
        run_id: str,
        round: int,
        temporal_identity: TemporalIdentity,
        data_runtime_identity: DataRuntimeIdentity,
        research_policy_version: str,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
    ) -> ResearchExecution:
        del run_id, research_policy_version  # state assembly is downstream
        if not tasks:
            return ResearchExecution(
                task_results=(),
                capability_gaps=(),
                degradations=(),
            )
        # One absolute monotonic stage deadline, computed once (FIX 3B).
        deadline = time.monotonic() + self.stage_timeout_seconds
        accumulator = _Accumulator()
        pool = ThreadPoolExecutor(max_workers=min(self.concurrency, len(tasks)))
        try:
            futures = {}
            for task in tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResearchDeadlineError(
                        "research stage deadline expired before task dispatch"
                    )
                future = pool.submit(
                    self._run_task,
                    task,
                    round=round,
                    temporal_identity=temporal_identity,
                    data_runtime_identity=data_runtime_identity,
                    ticker=ticker,
                    cutoff=cutoff,
                    requested_manifest_id=requested_manifest_id,
                    accumulator=accumulator,
                )
                futures[future] = task
            for future, task in futures.items():
                remaining = deadline - time.monotonic()
                try:
                    future.result(timeout=max(remaining, 0.0))
                except TimeoutError:
                    raise ResearchDeadlineError(
                        f"research stage deadline expired for task {task.task_id}"
                    ) from None
            # All futures completed before the deadline.
            pool.shutdown(wait=True)
        except ResearchDeadlineError:
            # Never wait for the timed-out worker: cancel pending futures and
            # leave the pool; late results can never become accepted stage
            # output because the accumulator is discarded on the raise path.
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        except ResearchIntegrityError:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        ordered = tuple(
            sorted(accumulator.results, key=lambda item: (item.priority, item.task_id))
        )
        return ResearchExecution(
            task_results=ordered,
            capability_gaps=_dedup_gaps(accumulator.gaps),
            degradations=_sorted_degradations(accumulator.degradations),
        )

    # -- per-task execution --------------------------------------------------

    def _run_task(
        self,
        task: ResearchTask,
        *,
        round: int,
        temporal_identity: TemporalIdentity,
        data_runtime_identity: DataRuntimeIdentity,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
        accumulator: _Accumulator,
    ) -> None:
        started = datetime.now(timezone.utc)
        started_mono = time.monotonic()
        strategy = retrieval_strategy_for(task.evidence_need)
        if strategy is RetrievalStrategy.NO_BACKEND:
            accumulator.add_gap(self._capability_gap(task, "BACKEND_NOT_IMPLEMENTED"))
            accumulator.add_result(
                self._result(
                    task, started, started_mono,
                    ResearchTaskResultStatus.FAILED_CAPABILITY,
                    data_runtime_identity, error_code="BACKEND_NOT_IMPLEMENTED",
                )
            )
            return
        if strategy is RetrievalStrategy.DETERMINISTIC_STRUCTURED:
            if self.structured_provider is None:
                accumulator.add_gap(self._capability_gap(task, "SOURCE_NOT_INDEXED"))
                accumulator.add_result(
                    self._result(
                        task, started, started_mono,
                        ResearchTaskResultStatus.FAILED_CAPABILITY,
                        data_runtime_identity, error_code="SOURCE_NOT_INDEXED",
                    )
                )
                return
            try:
                facts = tuple(self.structured_provider(task))
            except Exception as exc:
                reason = "structured_provider_error"
                accumulator.add_degradation(
                    RetrievalDegradation(
                        task_id=task.task_id,
                        component="structured",
                        reason_code=reason,
                        requested_mode=_mode_requested(task),
                        served_mode=None,
                    )
                )
                accumulator.add_result(
                    self._degraded_result(
                        task, started, started_mono, data_runtime_identity,
                        (reason,), str(exc),
                    )
                )
                return
            accumulator.add_result(
                self._result(
                    task, started, started_mono,
                    ResearchTaskResultStatus.SUCCEEDED,
                    data_runtime_identity, structured_facts=facts,
                )
            )
            return
        query = self.query_builder(task, ticker)
        try:
            evidence = tuple(
                self.retriever.retrieve(
                    query,
                    ticker=ticker,
                    cutoff=cutoff,
                    requested_manifest_id=requested_manifest_id,
                    temporal_identity=temporal_identity,
                    top_k=8,
                    candidate_depth=20,
                )
            )
        except RetrievalContractError as exc:
            raise ResearchIntegrityError(
                f"research integrity failure in task {task.task_id}: {exc.code}"
            ) from exc
        except Exception as exc:
            reason = "retrieval_backend_unavailable"
            accumulator.add_degradation(
                RetrievalDegradation(
                    task_id=task.task_id,
                    component="retrieval",
                    reason_code=reason,
                    requested_mode=_mode_requested(task),
                    served_mode=None,
                )
            )
            accumulator.add_result(
                self._degraded_result(
                    task, started, started_mono, data_runtime_identity,
                    (reason,), str(exc),
                )
            )
            return
        items = tuple(self._item_for(task, item, round) for item in evidence)
        accumulator.add_result(
            self._result(
                task, started, started_mono, ResearchTaskResultStatus.SUCCEEDED,
                data_runtime_identity, evidence_items=items,
            )
        )

    def _item_for(
        self, task: ResearchTask, evidence: RetrievedEvidence, round: int
    ) -> EvidenceStateItem:
        contribution = RetrievalContribution(
            task_id=task.task_id,
            requested_mode=evidence.mode_requested,
            served_mode=evidence.mode_served,
            lexical_rank=evidence.lexical_rank,
            lexical_score=evidence.lexical_raw_score,
            dense_rank=evidence.dense_rank,
            dense_score=evidence.dense_score,
            fusion_rank=None,
            fusion_score=evidence.fusion_score,
            reranker_rank=evidence.reranker_rank,
            reranker_score=evidence.reranker_score,
            original_rank=evidence.reranker_rank or evidence.lexical_rank,
            degradation_flags=(
                ("retrieval_degraded",) if evidence.is_degraded else ()
            ),
        )
        return evidence.to_evidence_state_item(
            first_seen_round=round,
            contributing_task_ids=(task.task_id,),
            retrieval_contribution=contribution,
        )

    # -- result helpers ------------------------------------------------------

    def _capability_gap(self, task: ResearchTask, reason: str) -> CapabilityGap:
        return CapabilityGap(
            gap_id=f"cap:{task.evidence_need.value.lower()}",
            evidence_need=task.evidence_need,
            reason_code=reason,
        )

    def _result(
        self,
        task: ResearchTask,
        started: datetime,
        started_mono: float,
        status: ResearchTaskResultStatus,
        data_runtime_identity: DataRuntimeIdentity,
        *,
        evidence_items: tuple[EvidenceStateItem, ...] = (),
        structured_facts: tuple[EvidenceStateItem, ...] = (),
        degradation_reasons: tuple[str, ...] = (),
        error_code: str | None = None,
    ) -> ResearchTaskResult:
        elapsed_seconds = max(0.0, time.monotonic() - started_mono)
        ended = started + timedelta(seconds=elapsed_seconds)
        return ResearchTaskResult(
            task_id=task.task_id,
            task_fingerprint=task.task_fingerprint,
            priority=task.priority,
            status=status,
            started_at=started,
            ended_at=ended,
            latency_ms=int(elapsed_seconds * 1000),
            deadline_exhausted=False,
            evidence_items=evidence_items,
            structured_facts=structured_facts,
            mode_requested=_mode_requested(task),
            mode_served=None,
            degradation_reasons=degradation_reasons,
            error_code=error_code,
            data_runtime_identity=data_runtime_identity,
        )

    def _degraded_result(
        self,
        task: ResearchTask,
        started: datetime,
        started_mono: float,
        data_runtime_identity: DataRuntimeIdentity,
        reasons: tuple[str, ...],
        error_code: str,
    ) -> ResearchTaskResult:
        return self._result(
            task, started, started_mono, ResearchTaskResultStatus.DEGRADED,
            data_runtime_identity, degradation_reasons=reasons, error_code=error_code,
        )


def _mode_requested(task: ResearchTask) -> str:
    strategy = retrieval_strategy_for(task.evidence_need)
    if strategy is RetrievalStrategy.DETERMINISTIC_STRUCTURED:
        return "structured"
    if strategy is RetrievalStrategy.NO_BACKEND:
        return "none"
    return "reranked"


def _dedup_gaps(gaps: list[CapabilityGap]) -> tuple[CapabilityGap, ...]:
    seen: dict[tuple[str, str], CapabilityGap] = {}
    for gap in gaps:
        seen.setdefault((gap.evidence_need.value, gap.reason_code), gap)
    return tuple(
        sorted(
            seen.values(), key=lambda gap: (gap.evidence_need.value, gap.reason_code)
        )
    )


def _sorted_degradations(
    degradations: list[RetrievalDegradation],
) -> tuple[RetrievalDegradation, ...]:
    return tuple(
        sorted(
            {degradation: None for degradation in degradations}.keys(),
            key=lambda d: (d.task_id, d.component, d.reason_code),
        )
    )


__all__ = [
    "ResearchDeadlineError",
    "ResearchExecution",
    "ResearchExecutor",
    "ResearchIntegrityError",
]
