"""M7-5: Stage-1 retrieval metrics over identity-bound PoolManifest.

Ground truth is human GoldenCase.evidence_judgments + the pinned
PoolManifest; runtime grouping is never its own oracle. Every metric
publishes numerator/denominator/eligible/excluded/non-scorable counts and
case ids. Initial Stage-1 gates: Recall@8 >= 0.75, primary-source hit >=
0.80 where primary evidence exists, duplicate-adjusted Precision@8 >= 0.60,
ticker/cutoff violations == 0.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from catalyst_eval.benchmark.pool_manifest import PoolArm, PoolManifest
from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.retrieval_metrics import (
    RetrievalMetrics,
    RetrievalResult,
    compute_retrieval_metrics,
)

from tests.v1_1_fixtures import make_case, make_stage1_cases

TOP_K = 8


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _gold_rows() -> list[GoldenCase]:
    return [GoldenCase.model_validate(row) for row in make_stage1_cases()]


def _pool(case_id: str, inventory: tuple[str, ...]) -> PoolManifest:
    data = dict(
        schema_version="1.0.0",
        case_id=case_id,
        arms=(
            PoolArm(arm="lexical", version="1.0.0", top_k=TOP_K),
            PoolArm(arm="dense", version="1.0.0", top_k=TOP_K),
            PoolArm(arm="hybrid", version="1.0.0", top_k=TOP_K),
            PoolArm(arm="reranked", version="1.0.0", top_k=TOP_K),
        ),
        chunk_inventory=tuple(sorted(set(inventory))),
        corpus_manifest_id="c" * 64,
        index_manifest_id="e" * 64,
        source_artifact_id="f" * 64,
        created_at=_utc("2026-08-19T00:00:00Z"),
    )
    pool_id = hashlib.sha256(
        json.dumps(
            {
                "schema_version": data["schema_version"],
                "case_id": data["case_id"],
                "arms": [
                    {"arm": a.arm, "version": a.version, "top_k": a.top_k}
                    for a in data["arms"]
                ],
                "chunk_inventory": list(data["chunk_inventory"]),
                "corpus_manifest_id": data["corpus_manifest_id"],
                "index_manifest_id": data["index_manifest_id"],
                "source_artifact_id": data["source_artifact_id"],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return PoolManifest(pool_id=pool_id, **data)


def _result(
    gold: GoldenCase,
    *,
    ranked: tuple[str, ...],
    reranker_contributed: bool = False,
    latency_ms: int | None = 120,
    degraded: bool = False,
    ticker_violations: tuple[str, ...] = (),
    cutoff_violations: tuple[str, ...] = (),
    pool: PoolManifest | None = None,
) -> RetrievalResult:
    inventory = tuple(dict.fromkeys((*ranked, *gold.expected_primary_evidence)))
    return RetrievalResult(
        case_id=gold.case_id,
        pool=pool or _pool(gold.case_id, inventory),
        ranked_evidence_ids=ranked,
        reranker_contributed=reranker_contributed,
        latency_ms=latency_ms,
        degraded=degraded,
        ticker_violations=ticker_violations,
        cutoff_violations=cutoff_violations,
    )


def _top_results(gold_cases: list[GoldenCase]):
    """Returned top-K includes every human-judged evidence unit."""
    results = []
    for gold in gold_cases:
        judged = tuple(judgment.evidence_id for judgment in gold.evidence_judgments)
        ranked = (
            judged + ("fixture-not-judged-1", "fixture-not-judged-2")
            if judged
            else ("fixture-not-judged-1",)
        )
        results.append(_result(gold, ranked=ranked))
    return results


def test_metrics_report_denominators_and_case_ids():
    gold_cases = _gold_rows()
    metrics = compute_retrieval_metrics(_top_results(gold_cases), gold_cases, top_k=TOP_K)
    assert isinstance(metrics, RetrievalMetrics)
    recall = metrics.recall_at_k
    assert recall.metric_id == "recall_at_8"
    # ABSTAIN/no-judgment cases are non-scorable for recall (no relevant units).
    assert recall.eligible_count == 8
    # Integer count contract: hits over relevant units across eligible cases.
    assert recall.denominator == 9
    assert len(recall.case_ids) == 8
    assert recall.excluded_count == 0
    assert recall.non_scorable_count == 4


def test_recall_at_eight_gate_passes_on_full_hits():
    gold_cases = _gold_rows()
    metrics = compute_retrieval_metrics(_top_results(gold_cases), gold_cases, top_k=TOP_K)
    assert metrics.recall_at_k.value >= 0.75
    assert metrics.gates["recall_at_8"] is True


def test_recall_degrades_when_hits_missing():
    gold_cases = _gold_rows()
    results = []
    for index, gold in enumerate(gold_cases):
        ranked = ("fixture-not-judged-1", "fixture-not-judged-2") if index % 2 == 0 else (
            gold.expected_primary_evidence[0] if gold.expected_primary_evidence else "fixture-ev-009",
            "fixture-not-judged-3",
        )
        results.append(_result(gold, ranked=ranked))
    metrics = compute_retrieval_metrics(results, gold_cases, top_k=TOP_K)
    assert metrics.recall_at_k.value < 1.0
    assert metrics.recall_at_k.denominator == 9


def test_mrr_uses_first_relevant_rank():
    gold_cases = _gold_rows()
    gold = gold_cases[0]
    # relevant unit at rank 3 (0-based 2).
    ranked = ("fixture-x", "fixture-y", gold.expected_primary_evidence[0])
    metrics = compute_retrieval_metrics([_result(gold, ranked=ranked)], [gold], top_k=TOP_K)
    assert metrics.mrr.value is not None
    assert metrics.mrr.value == pytest.approx(1 / 3)


def test_primary_source_hit_denominator_is_cases_with_primary_evidence():
    gold_cases = _gold_rows()
    metrics = compute_retrieval_metrics(_top_results(gold_cases), gold_cases, top_k=TOP_K)
    primary = metrics.primary_source_hit
    expected_primary_cases = sum(1 for g in gold_cases if g.expected_primary_evidence)
    assert primary.denominator == expected_primary_cases
    assert primary.numerator == expected_primary_cases
    assert primary.value >= 0.80


def test_duplicate_adjusted_precision_counts_independence_groups():
    gold_cases = _gold_rows()
    gold = next(g for g in gold_cases if g.expected_primary_evidence)
    evidence = gold.evidence_judgments[0].evidence_id
    ranked = (evidence, "fixture-not-judged-1", "fixture-not-judged-1")
    metrics = compute_retrieval_metrics([_result(gold, ranked=ranked)], [gold], top_k=TOP_K)
    # unique returned groups: gold group + one ungrouped unit -> 2 groups,
    # both acceptable? only gold group is acceptable; ungrouped is not.
    precision = metrics.duplicate_adjusted_precision
    assert precision.denominator == 2
    assert precision.value == pytest.approx(0.5)


def test_empty_returned_groups_are_non_scorable_not_zero():
    gold_cases = _gold_rows()
    gold = gold_cases[0]
    metrics = compute_retrieval_metrics([_result(gold, ranked=())], [gold], top_k=TOP_K)
    assert metrics.duplicate_adjusted_precision.non_scorable_count == 1
    assert metrics.duplicate_adjusted_precision.value is None


def test_ticker_and_cutoff_violations_fail_gate():
    gold_cases = _gold_rows()
    results = _top_results(gold_cases)
    results[0] = _result(
        gold_cases[0],
        ranked=results[0].ranked_evidence_ids,
        ticker_violations=("fixture-wrong-ticker",),
    )
    metrics = compute_retrieval_metrics(results, gold_cases, top_k=TOP_K)
    assert metrics.violations.numerator >= 1
    assert metrics.gates["no_ticker_or_cutoff_violations"] is False


def test_latency_and_degradation_metrics():
    gold_cases = _gold_rows()
    results = _top_results(gold_cases)
    results[0] = _result(
        gold_cases[0], ranked=results[0].ranked_evidence_ids, latency_ms=500, degraded=True
    )
    metrics = compute_retrieval_metrics(results, gold_cases, top_k=TOP_K)
    assert metrics.latency_ms.value is not None
    assert metrics.degraded_cases.eligible_count == 12
    assert metrics.degraded_cases.numerator == 1


def test_reranker_contribution_counts_eligible_cases():
    gold_cases = _gold_rows()
    results = _top_results(gold_cases)
    results[1] = _result(
        gold_cases[1], ranked=results[1].ranked_evidence_ids, reranker_contributed=True
    )
    metrics = compute_retrieval_metrics(results, gold_cases, top_k=TOP_K)
    assert metrics.reranker_contribution.numerator == 1
    assert metrics.reranker_contribution.denominator == 12


def test_pool_identity_binding_mismatch_is_rejected():
    gold_cases = _gold_rows()
    gold = gold_cases[0]
    with pytest.raises(ValueError, match="pool"):
        compute_retrieval_metrics(
            [_result(gold, ranked=("fixture-x",), pool=_pool("other-case", ("fixture-x",)))],
            [gold],
            top_k=TOP_K,
        )
