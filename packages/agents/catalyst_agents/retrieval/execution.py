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

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import SourceClass
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.read.market_scope import MAX_PEER_SEARCHES
from catalyst_data.retrieval.lead_terms import LEAD_TERMS_POLICY
from catalyst_data.retrieval.lexical_display import (
    CANDIDATE_DEPTH_CAP,
    DISPLAY_TOP_K,
)
from catalyst_data.retrieval.result import (
    RetrievalArmUnavailableError,
    RetrievalContractError,
)
from catalyst_data.trading_calendar import latest_closed_trading_day_for_date

from catalyst_agents.attribution.evidence_state import (
    CapabilityGap,
    EvidenceStateItem,
    ResearchTaskResult,
    ResearchTaskResultStatus,
    RetrievalContribution,
    RetrievalDegradation,
)
from catalyst_agents.attribution.provider import RetrievedEvidence
from catalyst_agents.retrieval.corrective import (
    compute_research_fingerprint,
    sanitize_query_hints,
)
from catalyst_agents.retrieval.task import (
    EvidenceNeed,
    ResearchTask,
    RetrievalStrategy,
    TimeScope,
    retrieval_strategy_for,
)
from catalyst_agents.runtime.retrieval_adapter import (
    RetrievalCallObservation,
    RetrievalDegradedError,
)

# Full-text primary sources a headline lead may point at.
LEAD_SOURCE_CLASSES = (
    SourceClass.ISSUER_DISCLOSURE,
    SourceClass.CORPORATE_PRESS_RELEASE,
    SourceClass.OFFICIAL_GOVERNMENT,
)

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
        top_k: int,
        candidate_depth: int,
        source_classes: tuple[str, ...] | None = None,
        temporal_centre_date: str | None = None,
    ) -> tuple[RetrievedEvidence, ...]:
        ...


class StructuredFactProvider(Protocol):
    def __call__(self, task: ResearchTask) -> tuple[EvidenceStateItem, ...]:
        ...


QueryBuilder = Callable[[ResearchTask, str], str]


@dataclass(frozen=True)
class ObservedRetrievalCall:
    """One retrieval call's observed facts, bound to its research task."""

    task_id: str
    task_fingerprint: str
    observation: RetrievalCallObservation


@dataclass(frozen=True)
class ResearchExecution:
    """Deterministic stage output: ordered task results + typed gaps."""

    task_results: tuple[ResearchTaskResult, ...]
    capability_gaps: tuple[CapabilityGap, ...]
    degradations: tuple[RetrievalDegradation, ...]
    deadline_exhausted: bool = False
    # Observed retrieval facts for the tasks whose retriever exposed them.
    # Tasks served by structured/backends with no retrieval call contribute
    # nothing here (their absence is not a "no results" observation).
    observed_retrieval_calls: tuple[ObservedRetrievalCall, ...] = ()
    tasks: tuple[ResearchTask, ...] = ()


def _default_query(task: ResearchTask, ticker: str) -> str:
    """The V1.1 shipped query: the ticker plus the task's sanitized hints.

    Frozen-A3 §2.1 / C6: the generic evidence-need label (for example
    ``company``) is never a ``MATCH`` term. Only ``source_classes``,
    ``time_scope`` and ``query_hints`` enter the query and its filters. Initial
    tasks carry no hints (R5), so the shipped initial query is the ticker alone,
    which is what keeps it eligible (an empty string short-circuits to
    ``match_mode="none"``, R3).
    """
    return " ".join([ticker, *sanitize_query_hints(task.query_hints)])


def _source_classes_filter(task: ResearchTask) -> tuple[str, ...] | None:
    """Task source classes as the lexical arm's values, or ``None`` (R11).

    ``None`` means "no source-class filter"; an empty tuple is rejected by
    ``retrieve_lexical``, so an empty ``task.source_classes`` maps to ``None``.
    """
    if not task.source_classes:
        return None
    return tuple(source_class.value for source_class in task.source_classes)


def _temporal_centre_date(
    task: ResearchTask, temporal_identity: TemporalIdentity
) -> str:
    """Map ``time_scope`` to the structured temporal centre (WP-B B3, R13).

    * ``SESSION_INFORMATION_WINDOW`` -> the asked session date;
    * ``PRIOR_SESSION`` -> ``latest_closed_trading_day_for_date(session - 1)``,
      the same data-core calendar helper ``build_temporal_identity`` uses;
    * ``LOOKBACK_SESSIONS`` -> the asked session (the initial policy never emits
      it, so the value is recorded and not converted).
    """
    if task.time_scope is TimeScope.PRIOR_SESSION:
        prior_day = latest_closed_trading_day_for_date(
            date.fromisoformat(temporal_identity.session_date) - timedelta(days=1)
        )
        return prior_day.isoformat()
    return temporal_identity.session_date


class _Accumulator:
    """Thread-safe collection of stage outputs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.results: list[ResearchTaskResult] = []
        self.gaps: list[CapabilityGap] = []
        self.degradations: list[RetrievalDegradation] = []
        self.observations: list[ObservedRetrievalCall] = []

    def add_result(self, result: ResearchTaskResult) -> None:
        with self._lock:
            self.results.append(result)

    def add_gap(self, gap: CapabilityGap) -> None:
        with self._lock:
            self.gaps.append(gap)

    def add_degradation(self, degradation: RetrievalDegradation) -> None:
        with self._lock:
            self.degradations.append(degradation)

    def add_observation(self, observation: ObservedRetrievalCall) -> None:
        with self._lock:
            self.observations.append(observation)


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
        tasks = tasks + self._lead_followup(tasks, round, ticker, temporal_identity)
        task_tickers = self._expand_peer_scope(tasks, ticker)
        tasks = tuple(task for task, _ in task_tickers)
        ticker_by_task = {task.task_id: task_ticker for task, task_ticker in task_tickers}
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
            next_retrieval_index = 0
            for task in tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResearchDeadlineError(
                        "research stage deadline expired before task dispatch"
                    )
                retrieval_index = None
                if retrieval_strategy_for(task.evidence_need) is RetrievalStrategy.HYBRID_TEXT:
                    retrieval_index = next_retrieval_index
                    next_retrieval_index += 1
                future = pool.submit(
                    self._run_task_with_selection_lifecycle,
                    task,
                    research_submission_index=retrieval_index,
                    round=round,
                    temporal_identity=temporal_identity,
                    data_runtime_identity=data_runtime_identity,
                    ticker=ticker_by_task[task.task_id],
                    cutoff=cutoff,
                    requested_manifest_id=requested_manifest_id,
                    accumulator=accumulator,
                    research_deadline_monotonic=deadline,
                )
                futures[future] = task
            timed_out: list[ResearchTask] = []
            for future, task in futures.items():
                remaining = deadline - time.monotonic()
                try:
                    future.result(timeout=max(remaining, 0.0))
                except TimeoutError:
                    timed_out.append(task)
            if timed_out:
                # Product rule (2026-11-16): a slow task (often a peer or lead follow-up) no longer fails the whole
                # run. Tasks that finished in time are kept, as a frozen copy taken now; late workers can still
                # append to the accumulator but never reach this output. With nothing finished it stays a failure.
                with accumulator._lock:
                    results = list(accumulator.results)
                    gaps = list(accumulator.gaps)
                    degradations = list(accumulator.degradations)
                    observations = list(accumulator.observations)
                self._finish_research_selection_leader()
                pool.shutdown(wait=False, cancel_futures=True)
                finished = {r.task_id for r in results}
                if not finished:
                    raise ResearchDeadlineError(
                        f"research stage deadline expired for task {timed_out[0].task_id}"
                    )
                degradations += [
                    RetrievalDegradation(
                        task_id=task.task_id,
                        component="research_executor",
                        reason_code="task_deadline_exceeded",
                        requested_mode=_mode_requested(task),
                        served_mode=None,
                    )
                    for task in timed_out
                    if task.task_id not in finished
                ]
                ordered = tuple(sorted(results, key=lambda item: (item.priority, item.task_id)))
                observed_by_task = {item.task_id: item for item in observations}
                return ResearchExecution(
                    task_results=ordered,
                    capability_gaps=_dedup_gaps(gaps),
                    degradations=_sorted_degradations(degradations),
                    deadline_exhausted=True,
                    observed_retrieval_calls=tuple(
                        observed_by_task[result.task_id] for result in ordered if result.task_id in observed_by_task
                    ),
                    tasks=tasks,
                )
            # All futures completed before the deadline.
            pool.shutdown(wait=True)
        except ResearchDeadlineError:
            # Never wait for the timed-out worker: cancel pending futures and
            # leave the pool; late results can never become accepted stage
            # output because the accumulator is discarded on the raise path.
            self._finish_research_selection_leader()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        except ResearchIntegrityError:
            self._finish_research_selection_leader()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        except BaseException:
            self._finish_research_selection_leader()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        ordered = tuple(
            sorted(accumulator.results, key=lambda item: (item.priority, item.task_id))
        )
        observed_by_task = {item.task_id: item for item in accumulator.observations}
        return ResearchExecution(
            task_results=ordered,
            capability_gaps=_dedup_gaps(accumulator.gaps),
            degradations=_sorted_degradations(accumulator.degradations),
            observed_retrieval_calls=tuple(
                observed_by_task[result.task_id]
                for result in ordered
                if result.task_id in observed_by_task
            ),
            tasks=tasks,
        )

    def _run_task_with_selection_lifecycle(
        self, task: ResearchTask, *, research_submission_index: int | None, **task_kwargs: Any
    ) -> None:
        try:
            self._run_task(
                task,
                research_submission_index=research_submission_index,
                **task_kwargs,
            )
        finally:
            if research_submission_index == 0:
                self._finish_research_selection_leader()

    def _finish_research_selection_leader(self) -> None:
        finish = getattr(self.retriever, "finish_research_submission", None)
        if callable(finish):
            finish(0)

    @staticmethod
    def _expand_peer_scope(
        tasks: tuple[ResearchTask, ...], ticker: str
    ) -> tuple[tuple[ResearchTask, str], ...]:
        """A task scoped to peers runs once for the target and once per peer.

        Sector news names the peers, not the target (c04: the banks fell
        together). Each peer search is its own child task so its retrieval
        observation stays one call per task.
        """
        out: list[tuple[ResearchTask, str]] = []
        for task in tasks:
            out.append((task, ticker))
            for peer in task.ticker_scope[:MAX_PEER_SEARCHES]:
                if peer == ticker:
                    continue
                out.append(
                    (
                        task.model_copy(
                            update={
                                "task_id": f"{task.task_id}:peer:{peer}",
                                "ticker_scope": (peer,),
                            }
                        ),
                        peer,
                    )
                )
        return tuple(out)

    def _lead_followup(
        self,
        tasks: tuple[ResearchTask, ...],
        round: int,
        ticker: str,
        temporal_identity: TemporalIdentity,
    ) -> tuple[ResearchTask, ...]:
        """One primary-source query built from the session's headline terms.

        Headlines are leads: they only choose the query. What comes back is
        full-text primary/official evidence under the normal display and pack
        rules (ADR-011 §2.5, §3 C3).
        """
        lead_terms = getattr(self.retriever, "lead_terms", None)
        if round != 1 or not tasks or not callable(lead_terms):
            return ()
        hints = sanitize_query_hints(
            lead_terms(ticker=ticker, temporal_identity=temporal_identity)
        )
        if not hints:
            return ()
        base = next(
            (t for t in tasks if t.evidence_need is EvidenceNeed.COMPANY_PRIMARY),
            tasks[0],
        )
        fingerprint = compute_research_fingerprint(
            evidence_need=EvidenceNeed.COMPANY_PRIMARY,
            time_scope=base.time_scope,
            lookback_sessions=base.lookback_sessions,
            sanitized_hints=hints,
            policy_version=f"{base.retrieval_policy_id}+{LEAD_TERMS_POLICY}",
        )
        return (
            base.model_copy(
                update={
                    "task_id": f"research:{round}:lead:{fingerprint[:12]}",
                    "priority": max(t.priority for t in tasks) + 1,
                    "evidence_need": EvidenceNeed.COMPANY_PRIMARY,
                    "source_classes": LEAD_SOURCE_CLASSES,
                    "query_hints": hints,
                    "task_fingerprint": fingerprint,
                }
            ),
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
        research_submission_index: int | None = None,
        research_deadline_monotonic: float | None = None,
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
            except Exception:
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
                        (reason,), reason,
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
        source_classes = _source_classes_filter(task)
        temporal_centre_date = _temporal_centre_date(task, temporal_identity)
        retrieve_with_observations = getattr(
            self.retriever, "retrieve_with_observations", None
        )
        observation: RetrievalCallObservation | None = None
        selection_order = {}
        if getattr(self.retriever, "supports_research_submission_order", False):
            selection_order = {
                "research_submission_index": research_submission_index,
                "research_deadline_monotonic": research_deadline_monotonic,
            }
        try:
            if callable(retrieve_with_observations):
                evidence, observation = retrieve_with_observations(
                    query,
                    ticker=ticker,
                    cutoff=cutoff,
                    requested_manifest_id=requested_manifest_id,
                    temporal_identity=temporal_identity,
                    top_k=DISPLAY_TOP_K,
                    candidate_depth=CANDIDATE_DEPTH_CAP,
                    source_classes=source_classes,
                    temporal_centre_date=temporal_centre_date,
                    **selection_order,
                )
            else:
                evidence = tuple(
                    self.retriever.retrieve(
                        query,
                        ticker=ticker,
                        cutoff=cutoff,
                        requested_manifest_id=requested_manifest_id,
                        temporal_identity=temporal_identity,
                        top_k=DISPLAY_TOP_K,
                        candidate_depth=CANDIDATE_DEPTH_CAP,
                        source_classes=source_classes,
                        temporal_centre_date=temporal_centre_date,
                        **selection_order,
                    )
                )
        except RetrievalContractError as exc:
            raise ResearchIntegrityError(
                f"research integrity failure in task {task.task_id}: {exc.code}"
            ) from exc
        except RetrievalDegradedError as exc:
            observed = exc.observation
            reasons = tuple(observed.degradation_reasons) or (
                "retrieval_degraded_not_v1_representable",
            )
            accumulator.add_degradation(
                RetrievalDegradation(
                    task_id=task.task_id,
                    component="retrieval",
                    reason_code=reasons[0],
                    requested_mode=_mode_requested(task),
                    served_mode=observed.served_mode,
                )
            )
            accumulator.add_observation(
                ObservedRetrievalCall(
                    task_id=task.task_id,
                    task_fingerprint=task.task_fingerprint,
                    observation=RetrievalCallObservation(
                        requested_mode=_mode_requested(task),
                        served_mode=observed.served_mode,
                        ordered_candidate_evidence_ids=tuple(
                            observed.ordered_candidate_evidence_ids
                        ),
                        ordered_final_ranked_evidence_ids=tuple(
                            observed.ordered_final_ranked_evidence_ids
                        ),
                        rank_changes=dict(observed.rank_changes),
                        degradation_reasons=reasons,
                        arm_names=tuple(observed.arm_names),
                    ),
                )
            )
            accumulator.add_result(
                self._degraded_result(
                    task, started, started_mono, data_runtime_identity,
                    reasons, reasons[0], mode_served=observed.served_mode,
                )
            )
            return
        except RetrievalArmUnavailableError as exc:
            # The data retrieval boundary raises this only for a confirmed
            # backend availability failure. Keep its stable code, and never
            # surface backend exception text in run diagnostics.
            reason = exc.code
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
                    (reason,), reason,
                )
            )
            return
        items = tuple(self._item_for(task, item, round) for item in evidence)
        if observation is not None:
            accumulator.add_observation(
                ObservedRetrievalCall(
                    task_id=task.task_id,
                    task_fingerprint=task.task_fingerprint,
                    observation=observation,
                )
            )
        accumulator.add_result(
            self._result(
                task, started, started_mono, ResearchTaskResultStatus.SUCCEEDED,
                data_runtime_identity, evidence_items=items,
                mode_served=(observation.served_mode if observation else None),
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
        mode_served: str | None = None,
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
            mode_served=mode_served,
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
        mode_served: str | None = None,
    ) -> ResearchTaskResult:
        return self._result(
            task, started, started_mono, ResearchTaskResultStatus.DEGRADED,
            data_runtime_identity, degradation_reasons=reasons, error_code=error_code,
            mode_served=mode_served,
        )


def _mode_requested(task: ResearchTask) -> str:
    strategy = retrieval_strategy_for(task.evidence_need)
    if strategy is RetrievalStrategy.DETERMINISTIC_STRUCTURED:
        return "structured"
    if strategy is RetrievalStrategy.NO_BACKEND:
        return "none"
    return "lexical"  # V1.1 serves the lexical (FTS5) path only


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
