"""Stage-1 retrieval metrics over identity-bound PoolManifest (M7-5).

Ground truth is human ``GoldenCase.evidence_judgments`` plus the pinned
PoolManifest; runtime grouping is never its own oracle. Metrics follow the
Final TSD §22.2 / Frozen §10 formulas and publish integer
numerator/denominator/eligible/excluded/non-scorable counts plus case ids.

Metric formula lock (M7 plan):
- Recall@K is macro over eligible cases: sum(hit/relevant units)/cases.
- MRR uses the first relevant unique unit rank.
- nDCG@K uses gains primary_support=3, secondary_support=2, contradiction=1,
  all other roles 0, binary temporal eligibility, log2(rank+1) discount.
- Primary-source hit: cases with >=1 expected primary unit at rank<=K over
  cases with expected primary evidence.
- Duplicate-adjusted Precision@K: acceptable unique human independence groups
  returned at rank<=K over all unique returned groups; an ungrouped unit is
  its own group; empty denominators are non-scorable.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

from catalyst_eval.benchmark.pool_manifest import PoolManifest
from catalyst_eval.v1_1.case import GoldenCase

RELEVANT_ROLES = frozenset({"primary_support", "secondary_support", "contradiction"})
GAIN_BY_ROLE = {"primary_support": 3, "secondary_support": 2, "contradiction": 1}

STAGE1_RETRIEVAL_GATES = {
    "recall_at_8": 0.75,
    "primary_source_hit": 0.80,
    "duplicate_adjusted_precision": 0.60,
    "no_ticker_or_cutoff_violations": 0.0,
}


@dataclass(frozen=True)
class RetrievalResult:
    """One served retrieval result bound to its pinned pool."""

    case_id: str
    pool: PoolManifest
    ranked_evidence_ids: tuple[str, ...]
    reranker_contributed: bool = False
    latency_ms: int | None = None
    degraded: bool = False
    ticker_violations: tuple[str, ...] = ()
    cutoff_violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetricResult:
    """Every metric carries locked counts and identities."""

    metric_id: str
    numerator: int
    denominator: int
    eligible_count: int
    excluded_count: int
    non_scorable_count: int
    value: float | None
    case_ids: tuple[str, ...]
    pool_ids: tuple[str, ...]
    hard_gate: bool
    gate_passed: bool | None = None
    # exercised=False means the metric had no eligible/denominator stratum, so
    # a zero-violation "pass" is a frozen formula artifact, not an empirically
    # validated check.
    exercised: bool | None = None

    def passed(self, *, threshold: float | None = None) -> bool | None:
        if self.non_scorable_count and self.denominator == 0:
            return None
        if self.value is None:
            return None
        if threshold is None:
            return self.gate_passed
        return bool(self.value >= threshold)


@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_k: MetricResult
    mrr: MetricResult
    ndcg_at_k: MetricResult
    primary_source_hit: MetricResult
    duplicate_adjusted_precision: MetricResult
    independent_group_recall: MetricResult
    source_diversity: MetricResult
    reranker_contribution: MetricResult
    violations: MetricResult
    latency_ms: MetricResult
    degraded_cases: MetricResult
    gates: Mapping[str, bool | None]

    def as_dict(self) -> dict:
        return {
            name: {
                "metric_id": getattr(self, name).metric_id,
                "numerator": getattr(self, name).numerator,
                "denominator": getattr(self, name).denominator,
                "eligible_count": getattr(self, name).eligible_count,
                "excluded_count": getattr(self, name).excluded_count,
                "non_scorable_count": getattr(self, name).non_scorable_count,
                "value": getattr(self, name).value,
                "case_ids": list(getattr(self, name).case_ids),
                "pool_ids": list(getattr(self, name).pool_ids),
                "hard_gate": getattr(self, name).hard_gate,
                "gate_passed": getattr(self, name).gate_passed,
            }
            for name in (
                "recall_at_k", "mrr", "ndcg_at_k", "primary_source_hit",
                "duplicate_adjusted_precision", "independent_group_recall",
                "source_diversity", "reranker_contribution", "violations",
                "latency_ms", "degraded_cases",
            )
        }


def _judgments_by_evidence(gold: GoldenCase) -> dict[str, object]:
    return {judgment.evidence_id: judgment for judgment in gold.evidence_judgments}


def _relevant_unit_ids(gold: GoldenCase) -> set[str]:
    return {
        judgment.evidence_id
        for judgment in gold.evidence_judgments
        if judgment.role in RELEVANT_ROLES and judgment.temporal_eligible
    }


def _group_of(gold: GoldenCase, evidence_id: str) -> str:
    judgment = _judgments_by_evidence(gold).get(evidence_id)
    if judgment is None:
        return f"unit:{evidence_id}"
    return judgment.independence_group or f"unit:{evidence_id}"


def _ndcg_at_k(ranked: Sequence[str], gold: GoldenCase, *, top_k: int) -> float | None:
    relevant = _relevant_unit_ids(gold)
    if not relevant:
        return None
    gains: dict[str, int] = {}
    for judgment in gold.evidence_judgments:
        if judgment.evidence_id in relevant:
            gains[judgment.evidence_id] = GAIN_BY_ROLE.get(judgment.role, 0)
    ideal = sorted(gains.values(), reverse=True)[:top_k]
    idcg = sum(g / math.log2(idx + 2) for idx, g in enumerate(ideal))
    if idcg <= 0:
        return 0.0
    dcg = 0.0
    for rank, evidence_id in enumerate(ranked[:top_k], start=1):
        dcg += gains.get(evidence_id, 0) / math.log2(rank + 1)
    return dcg / idcg


def compute_retrieval_metrics(
    results: Sequence[RetrievalResult],
    gold_cases: Sequence[GoldenCase],
    *,
    top_k: int = 8,
) -> RetrievalMetrics:
    """Compute Stage-1 retrieval metrics over the identity-bound pool results."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    gold_by_case = {case.case_id: case for case in gold_cases}
    results_by_case = {result.case_id: result for result in results}
    if set(gold_by_case) != set(results_by_case):
        raise ValueError(
            "results must cover exactly the gold cases one-to-one "
            f"(missing={sorted(set(gold_by_case) - set(results_by_case))}, "
            f"extra={sorted(set(results_by_case) - set(gold_by_case))})"
        )

    recall_parts: list[float] = []
    recall_cases: list[str] = []
    recall_pool_ids: list[str] = []
    recall_hits_total = 0
    recall_units_total = 0
    mrr_parts: list[float] = []
    mrr_cases: list[str] = []
    ndcg_parts: list[float] = []
    ndcg_cases: list[str] = []
    primary_hit = 0
    primary_denom = 0
    primary_cases: list[str] = []
    group_acceptable = 0
    group_total = 0
    group_precision_cases: list[str] = []
    group_precision_non_scorable = 0
    ig_hit = 0
    ig_denom = 0
    ig_cases: list[str] = []
    source_assets = 0
    source_units = 0
    source_cases: list[str] = []
    source_non_scorable = 0
    rerank_numerator = 0
    rerank_denom = 0
    rerank_cases: list[str] = []
    violations_total = 0
    violations_cases: list[str] = []
    latencies: list[int] = []
    latency_cases: list[str] = []
    degraded_numerator = 0
    degraded_denom = 0
    degraded_cases: list[str] = []

    for case_id, gold in gold_by_case.items():
        result = results_by_case[case_id]
        if result.pool.case_id != case_id:
            raise ValueError(
                f"pool identity binding mismatch for {case_id!r}: pool case_id "
                f"{result.pool.case_id!r}"
            )
        ranked = result.ranked_evidence_ids[:top_k]
        ranked_set = set(ranked)
        pool_id = result.pool.pool_id
        relevant = _relevant_unit_ids(gold)
        judgments = _judgments_by_evidence(gold)

        # Recall@K (macro over cases with relevant units; integer totals kept
        # for the count contract: hits over relevant units across cases).
        if relevant:
            hits = relevant & ranked_set
            recall_parts.append(len(hits) / len(relevant))
            recall_hits_total += len(hits)
            recall_units_total += len(relevant)
            recall_cases.append(case_id)
            recall_pool_ids.append(pool_id)
        # MRR (first relevant unique unit rank).
        if relevant:
            first_rank = next(
                (idx for idx, eid in enumerate(ranked, start=1) if eid in relevant),
                None,
            )
            mrr_parts.append(1.0 / first_rank if first_rank is not None else 0.0)
            mrr_cases.append(case_id)
        # nDCG@K.
        ndcg = _ndcg_at_k(ranked, gold, top_k=top_k)
        if ndcg is not None:
            ndcg_parts.append(ndcg)
            ndcg_cases.append(case_id)

        # Primary-source hit (denominator = cases with expected primary evidence).
        if gold.expected_primary_evidence:
            primary_denom += 1
            primary_cases.append(case_id)
            if any(eid in ranked_set for eid in gold.expected_primary_evidence):
                primary_hit += 1

        # Duplicate-adjusted Precision@K over unique returned groups.
        returned_groups = {_group_of(gold, eid) for eid in ranked}
        if not returned_groups:
            group_precision_non_scorable += 1
        else:
            acceptable_groups = set()
            for eid in ranked:
                group = _group_of(gold, eid)
                if eid in relevant:
                    acceptable_groups.add(group)
            group_acceptable += len(acceptable_groups)
            group_total += len(returned_groups)
            group_precision_cases.append(case_id)

        # Independent-group recall (denominator = human-labelled required groups).
        required_groups = {
            _group_of(gold, eid) for eid in relevant
        }
        if required_groups:
            hit_groups = {
                group
                for eid, group in (
                    (_group_of(gold, eid), _group_of(gold, eid)) for eid in ranked
                )
            } & required_groups
            ig_hit += len(hit_groups)
            ig_denom += len(required_groups)
            ig_cases.append(case_id)

        # Source diversity: unique canonical assets / returned units.
        if ranked:
            assets = {
                judgments[eid].canonical_asset_id
                for eid in ranked
                if eid in judgments
            }
            source_assets += len(assets)
            source_units += len(ranked)
            source_cases.append(case_id)
        else:
            source_non_scorable += 1

        # Reranker contribution / violations / latency / degraded (case-level).
        rerank_denom += 1
        rerank_cases.append(case_id)
        if result.reranker_contributed:
            rerank_numerator += 1
        violations_total += len(result.ticker_violations) + len(result.cutoff_violations)
        violations_cases.append(case_id)
        if result.latency_ms is not None:
            latencies.append(result.latency_ms)
            latency_cases.append(case_id)
        degraded_denom += 1
        degraded_cases.append(case_id)
        if result.degraded:
            degraded_numerator += 1

    def _result(
        metric_id: str,
        *,
        numerator: int,
        denominator: int,
        eligible: int,
        non_scorable: int,
        value: float | None,
        case_ids: Sequence[str],
        pool_ids: Sequence[str] = (),
        hard_gate: bool = False,
        threshold: float | None = None,
    ) -> MetricResult:
        gate_passed: bool | None = None
        if value is not None and denominator > 0:
            gate_passed = bool(value >= threshold) if threshold is not None else None
        return MetricResult(
            metric_id=metric_id,
            numerator=numerator,
            denominator=denominator,
            eligible_count=eligible,
            excluded_count=len(gold_cases) - eligible - non_scorable,
            non_scorable_count=non_scorable,
            value=value,
            case_ids=tuple(case_ids),
            pool_ids=tuple(pool_ids),
            hard_gate=hard_gate,
            gate_passed=gate_passed,
        )

    recall_value = (
        sum(recall_parts) / len(recall_parts) if recall_parts else None
    )
    mrr_value = sum(mrr_parts) / len(mrr_parts) if mrr_parts else None
    ndcg_value = sum(ndcg_parts) / len(ndcg_parts) if ndcg_parts else None
    primary_value = primary_hit / primary_denom if primary_denom else None
    precision_value = group_acceptable / group_total if group_total else None
    ig_value = ig_hit / ig_denom if ig_denom else None
    diversity_value = source_assets / source_units if source_units else None
    rerank_value = rerank_numerator / rerank_denom if rerank_denom else None
    latency_value = sum(latencies) / len(latencies) if latencies else None
    degraded_value = degraded_numerator / degraded_denom if degraded_denom else None

    recall = _result(
        "recall_at_8", numerator=recall_hits_total,
        denominator=recall_units_total, eligible=len(recall_parts),
        non_scorable=len(gold_cases) - len(recall_parts),
        value=recall_value, case_ids=recall_cases, pool_ids=recall_pool_ids,
        hard_gate=True, threshold=STAGE1_RETRIEVAL_GATES["recall_at_8"],
    )
    mrr = _result(
        "mrr", numerator=0, denominator=len(mrr_cases), eligible=len(mrr_cases),
        non_scorable=len(gold_cases) - len(mrr_cases), value=mrr_value,
        case_ids=mrr_cases,
    )
    ndcg = _result(
        "ndcg_at_8", numerator=0, denominator=len(ndcg_cases),
        eligible=len(ndcg_cases), non_scorable=len(gold_cases) - len(ndcg_cases),
        value=ndcg_value, case_ids=ndcg_cases,
    )
    primary = _result(
        "primary_source_hit", numerator=primary_hit, denominator=primary_denom,
        eligible=primary_denom,
        non_scorable=len(gold_cases) - primary_denom,
        value=primary_value, case_ids=primary_cases,
        hard_gate=True, threshold=STAGE1_RETRIEVAL_GATES["primary_source_hit"],
    )
    precision = _result(
        "duplicate_adjusted_precision", numerator=group_acceptable,
        denominator=group_total, eligible=len(group_precision_cases),
        non_scorable=group_precision_non_scorable,
        value=precision_value, case_ids=group_precision_cases,
        hard_gate=True, threshold=STAGE1_RETRIEVAL_GATES["duplicate_adjusted_precision"],
    )
    ig = _result(
        "independent_group_recall", numerator=ig_hit, denominator=ig_denom,
        eligible=len(ig_cases), non_scorable=len(gold_cases) - len(ig_cases),
        value=ig_value, case_ids=ig_cases,
    )
    diversity = _result(
        "source_diversity", numerator=source_assets, denominator=source_units,
        eligible=len(source_cases), non_scorable=source_non_scorable,
        value=diversity_value, case_ids=source_cases,
    )
    rerank = _result(
        "reranker_contribution", numerator=rerank_numerator, denominator=rerank_denom,
        eligible=rerank_denom, non_scorable=0, value=rerank_value,
        case_ids=rerank_cases,
    )
    violations = _result(
        "ticker_or_cutoff_violations", numerator=violations_total,
        denominator=len(violations_cases) if violations_cases else 1,
        eligible=len(violations_cases), non_scorable=0,
        value=float(violations_total),
        case_ids=violations_cases,
        hard_gate=True, threshold=STAGE1_RETRIEVAL_GATES["no_ticker_or_cutoff_violations"],
    )
    # Violations are an upper-bound gate: pass only at exactly zero.
    violations = MetricResult(
        metric_id=violations.metric_id,
        numerator=violations.numerator,
        denominator=violations.denominator,
        eligible_count=violations.eligible_count,
        excluded_count=violations.excluded_count,
        non_scorable_count=violations.non_scorable_count,
        value=violations.value,
        case_ids=violations.case_ids,
        pool_ids=violations.pool_ids,
        hard_gate=violations.hard_gate,
        gate_passed=violations_total == 0,
    )
    latency = _result(
        "mean_latency_ms", numerator=0, denominator=len(latency_cases),
        eligible=len(latency_cases), non_scorable=len(gold_cases) - len(latency_cases),
        value=latency_value, case_ids=latency_cases,
    )
    degraded = _result(
        "degraded_cases", numerator=degraded_numerator, denominator=degraded_denom,
        eligible=degraded_denom, non_scorable=0, value=degraded_value,
        case_ids=degraded_cases,
    )

    gates = {
        "recall_at_8": recall.gate_passed,
        "primary_source_hit": primary.gate_passed,
        "duplicate_adjusted_precision": precision.gate_passed,
        "no_ticker_or_cutoff_violations": violations.gate_passed,
    }
    return RetrievalMetrics(
        recall_at_k=recall,
        mrr=mrr,
        ndcg_at_k=ndcg,
        primary_source_hit=primary,
        duplicate_adjusted_precision=precision,
        independent_group_recall=ig,
        source_diversity=diversity,
        reranker_contribution=rerank,
        violations=violations,
        latency_ms=latency,
        degraded_cases=degraded,
        gates=gates,
    )


__all__ = [
    "MetricResult",
    "RetrievalMetrics",
    "RetrievalResult",
    "STAGE1_RETRIEVAL_GATES",
    "compute_retrieval_metrics",
]
