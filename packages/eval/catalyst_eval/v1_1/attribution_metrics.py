"""Stage-1 attribution metrics (M7-6).

Citation correctness, causal support, and unsupported-material decisions come
only from the sealed human output-audit artifact; LLM judges are separate
diagnostics. Hard gates: citation correctness = 100%, unsupported primary
causal claims = 0, unsupported material claims <= 5%, false SUFFICIENT <= 1
of 12, explicit ABSTAIN producing SUFFICIENT = 0,
NO_MATERIAL_PUBLIC_CATALYST producing SUFFICIENT = 0, NO_MATERIAL without
completed sanity tasks = 0.

DATA-02: a news case whose serving text is title-only/metadata-only/
unrecovered is coverage-limited / non-scorable for news-attribution
denominators and is NEVER reported as an Analyst or model failure;
``coverage_limited`` and ``model_limited`` denominators stay separate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.output_audit import (
    AuditClaimDecision,
    Stage1OutputAudit,
    supported_citation_ids_for,
)
from catalyst_eval.v1_1.retrieval_metrics import MetricResult

UNSUPPORTED_MATERIAL_CLAIMS_MAX = 0.05
FALSE_SUFFICIENT_MAX = 1
CITATION_CORRECTNESS_MIN = 1.0


@dataclass(frozen=True)
class RunClaimOutput:
    claim_id: str
    material: bool
    citation_ids: tuple[str, ...] = ()
    role: str = "PRIMARY"
    statement: str | None = None


@dataclass(frozen=True)
class RunAttributionOutput:
    case_id: str
    output_status: str
    attribution_type: str | None
    refusal_reason: str | None = None
    claims: tuple[RunClaimOutput, ...] = ()
    sanity_tasks_completed: tuple[str, ...] = ()
    latency_ms: int | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    coverage_limited: bool = False


@dataclass(frozen=True)
class AttributionMetrics:
    citation_correctness: MetricResult
    evidence_precision: MetricResult
    causal_relevance: MetricResult
    material_coverage: MetricResult
    unsupported_primary_claims: MetricResult
    unsupported_material_claims: MetricResult
    false_sufficient: MetricResult
    abstain_producing_sufficient: MetricResult
    no_material_producing_sufficient: MetricResult
    no_material_without_sanity: MetricResult
    status_confusion_matrix: dict[tuple[str, str], int]
    refusal_correctness: MetricResult
    latency_ms: MetricResult
    tokens: MetricResult
    cost_usd: MetricResult
    coverage_limited_count: int
    model_limited_count: int
    gates: dict[str, bool | None]

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
                "hard_gate": getattr(self, name).hard_gate,
                "gate_passed": getattr(self, name).gate_passed,
            }
            for name in (
                "citation_correctness", "evidence_precision", "causal_relevance",
                "material_coverage", "unsupported_primary_claims",
                "unsupported_material_claims", "false_sufficient",
                "abstain_producing_sufficient", "no_material_producing_sufficient",
                "no_material_without_sanity", "refusal_correctness",
                "latency_ms", "tokens", "cost_usd",
            )
        }


def _metric(
    metric_id: str,
    *,
    numerator: int,
    denominator: int,
    eligible: int,
    non_scorable: int,
    value: float | None,
    case_ids: Sequence[str],
    hard_gate: bool = False,
    threshold: float | None = None,
    upper_bound: bool = False,
) -> MetricResult:
    gate_passed: bool | None = None
    if value is not None and denominator > 0 and threshold is not None:
        gate_passed = bool(value <= threshold) if upper_bound else bool(value >= threshold)
    return MetricResult(
        metric_id=metric_id,
        numerator=numerator,
        denominator=denominator,
        eligible_count=eligible,
        excluded_count=0,
        non_scorable_count=non_scorable,
        value=value,
        case_ids=tuple(case_ids),
        pool_ids=(),
        hard_gate=hard_gate,
        gate_passed=gate_passed,
    )


def compute_attribution_metrics(
    run_outputs: Sequence[RunAttributionOutput],
    audits: Sequence[Stage1OutputAudit],
    gold_cases: Sequence[GoldenCase],
) -> AttributionMetrics:
    """Compute Stage-1 attribution metrics from sealed audits + run outputs."""
    gold_by_case = {case.case_id: case for case in gold_cases}
    run_by_case = {run.case_id: run for run in run_outputs}
    audit_by_case = {audit.case_id: audit for audit in audits}
    if set(gold_by_case) != set(run_by_case) or set(gold_by_case) != set(audit_by_case):
        raise ValueError("run outputs and audits must cover the gold cases one-to-one")

    total_cases = len(gold_cases)
    citation_numerator = 0
    citation_denominator = 0
    citation_cases: list[str] = []
    evidence_supported = 0
    evidence_total = 0
    causal_relevant = 0
    causal_total = 0
    material_total = 0
    material_audited = 0
    material_cases: list[str] = []
    unsupported_primary = 0
    unsupported_material = 0
    unsupported_material_cases: list[str] = []
    false_sufficient = 0
    false_sufficient_cases: list[str] = []
    abstain_sufficient = 0
    no_material_sufficient = 0
    no_material_no_sanity = 0
    refusal_ok = 0
    refusal_denom = 0
    confusion: dict[tuple[str, str], int] = {}
    latencies: list[int] = []
    tokens_total = 0
    token_cases = 0
    cost_total = 0.0
    cost_cases = 0
    coverage_limited_count = 0
    model_limited_count = 0

    for case_id, gold in gold_by_case.items():
        run = run_by_case[case_id]
        audit = audit_by_case[case_id]
        if run.coverage_limited:
            coverage_limited_count += 1
            # DATA-02: coverage-limited cases are non-scorable for
            # news-attribution denominators, never model failures.
            model_limited_count += 0
        else:
            model_limited_count += 1

        confusion[(gold.oracle_status, run.output_status)] = (
            confusion.get((gold.oracle_status, run.output_status), 0) + 1
        )
        # DATA-02: model-failure numerators/denominators exclude
        # coverage-limited cases (never reported as Analyst/model failures).
        if not run.coverage_limited:
            if gold.oracle_status == "ABSTAIN" and run.output_status == "SUFFICIENT":
                abstain_sufficient += 1
            if (
                gold.expected_attribution_type == "NO_MATERIAL_PUBLIC_CATALYST"
                and run.output_status == "SUFFICIENT"
            ):
                no_material_sufficient += 1
            if (
                run.attribution_type == "NO_MATERIAL_PUBLIC_CATALYST"
                and not run.sanity_tasks_completed
            ):
                no_material_no_sanity += 1
            if gold.oracle_status != "SUFFICIENT" and run.output_status == "SUFFICIENT":
                false_sufficient += 1
                false_sufficient_cases.append(case_id)

        # Refusal correctness over ABSTAIN oracle cases.
        if gold.oracle_status == "ABSTAIN":
            refusal_denom += 1
            if (
                gold.expected_refusal_reason
                and run.refusal_reason == gold.expected_refusal_reason
            ):
                refusal_ok += 1

        audit_decisions = {decision.claim_id: decision for decision in audit.claims}
        for claim in run.claims:
            decision = audit_decisions.get(claim.claim_id)
            if decision is None:
                continue
            # Citation correctness counts supported resolved citation UNITS
            # divided by all resolved citation units (Batch-B corrective),
            # never one count per claim.
            if decision.citation_ids:
                supported_units = supported_citation_ids_for(decision)
                citation_denominator += len(decision.citation_ids)
                citation_numerator += len(supported_units)
                citation_cases.append(case_id)
                evidence_supported += len(supported_units)
                evidence_total += len(decision.citation_ids)
            if decision.decision == "SUPPORT":
                causal_relevant += 1
            causal_total += 1
            if claim.material:
                material_total += 1
                material_audited += 1
                if case_id not in material_cases:
                    material_cases.append(case_id)
                if decision.decision == "UNSUPPORTED":
                    unsupported_material += 1
                    unsupported_material_cases.append(case_id)
                if claim.role == "PRIMARY" and decision.decision == "UNSUPPORTED":
                    unsupported_primary += 1

        if run.latency_ms is not None:
            latencies.append(run.latency_ms)
        if run.tokens is not None:
            tokens_total += run.tokens
            token_cases += 1
        if run.cost_usd is not None:
            cost_total += run.cost_usd
            cost_cases += 1

    citation_value = citation_numerator / citation_denominator if citation_denominator else None
    evidence_value = evidence_supported / evidence_total if evidence_total else None
    causal_value = causal_relevant / causal_total if causal_total else None
    material_value = material_audited / material_total if material_total else None
    unsupported_material_value = (
        unsupported_material / material_total if material_total else None
    )
    refusal_value = refusal_ok / refusal_denom if refusal_denom else None
    latency_value = sum(latencies) / len(latencies) if latencies else None
    tokens_value = tokens_total / token_cases if token_cases else None
    cost_value = cost_total / cost_cases if cost_cases else None

    citation = _metric(
        "citation_correctness", numerator=citation_numerator,
        denominator=citation_denominator, eligible=len(citation_cases),
        non_scorable=total_cases - len(citation_cases), value=citation_value,
        case_ids=citation_cases, hard_gate=True,
        threshold=CITATION_CORRECTNESS_MIN,
    )
    evidence = _metric(
        "evidence_precision", numerator=evidence_supported,
        denominator=evidence_total, eligible=len(citation_cases),
        non_scorable=total_cases - len(citation_cases), value=evidence_value,
        case_ids=citation_cases,
    )
    causal = _metric(
        "causal_relevance", numerator=causal_relevant, denominator=causal_total,
        eligible=len(citation_cases), non_scorable=total_cases - len(citation_cases),
        value=causal_value, case_ids=citation_cases,
    )
    material = _metric(
        "material_coverage", numerator=material_audited, denominator=material_total,
        eligible=len(material_cases), non_scorable=total_cases - len(material_cases),
        value=material_value, case_ids=material_cases,
    )
    unsupported_primary_res = _metric(
        "unsupported_primary_causal_claims", numerator=unsupported_primary,
        denominator=material_total or 1, eligible=len(material_cases),
        non_scorable=total_cases - len(material_cases),
        value=float(unsupported_primary), case_ids=unsupported_material_cases,
        hard_gate=True, threshold=0.0, upper_bound=True,
    )
    unsupported_material_res = _metric(
        "unsupported_material_claims", numerator=unsupported_material,
        denominator=material_total, eligible=len(material_cases),
        non_scorable=total_cases - len(material_cases),
        value=unsupported_material_value, case_ids=unsupported_material_cases,
        hard_gate=True, threshold=UNSUPPORTED_MATERIAL_CLAIMS_MAX, upper_bound=True,
    )
    model_eligible = total_cases - coverage_limited_count
    false_sufficient_res = _metric(
        "false_sufficient", numerator=false_sufficient, denominator=model_eligible,
        eligible=model_eligible, non_scorable=coverage_limited_count,
        value=float(false_sufficient),
        case_ids=false_sufficient_cases, hard_gate=True,
        threshold=FALSE_SUFFICIENT_MAX, upper_bound=True,
    )
    abstain_res = _metric(
        "abstain_producing_sufficient", numerator=abstain_sufficient,
        denominator=model_eligible, eligible=model_eligible,
        non_scorable=coverage_limited_count,
        value=float(abstain_sufficient), case_ids=[],
        hard_gate=True, threshold=0.0, upper_bound=True,
    )
    no_material_res = _metric(
        "no_material_producing_sufficient", numerator=no_material_sufficient,
        denominator=model_eligible, eligible=model_eligible,
        non_scorable=coverage_limited_count,
        value=float(no_material_sufficient), case_ids=[],
        hard_gate=True, threshold=0.0, upper_bound=True,
    )
    no_material_sanity_res = _metric(
        "no_material_without_sanity", numerator=no_material_no_sanity,
        denominator=model_eligible, eligible=model_eligible,
        non_scorable=coverage_limited_count,
        value=float(no_material_no_sanity), case_ids=[],
        hard_gate=True, threshold=0.0, upper_bound=True,
    )
    refusal = _metric(
        "refusal_correctness", numerator=refusal_ok, denominator=refusal_denom,
        eligible=refusal_denom, non_scorable=total_cases - refusal_denom,
        value=refusal_value, case_ids=[],
    )
    latency = _metric(
        "mean_latency_ms", numerator=0, denominator=len(latencies),
        eligible=len(latencies), non_scorable=total_cases - len(latencies),
        value=latency_value, case_ids=[],
    )
    tokens = _metric(
        "mean_tokens", numerator=0, denominator=token_cases,
        eligible=token_cases, non_scorable=total_cases - token_cases,
        value=tokens_value, case_ids=[],
    )
    cost = _metric(
        "mean_cost_usd", numerator=0, denominator=cost_cases,
        eligible=cost_cases, non_scorable=total_cases - cost_cases,
        value=cost_value, case_ids=[],
    )

    gates = {
        "citation_correctness": citation.gate_passed,
        "unsupported_primary_claims": unsupported_primary_res.gate_passed,
        "unsupported_material_claims": unsupported_material_res.gate_passed,
        "false_sufficient": false_sufficient_res.gate_passed,
        "abstain_producing_sufficient": abstain_res.gate_passed,
        "no_material_producing_sufficient": no_material_res.gate_passed,
        "no_material_without_sanity": no_material_sanity_res.gate_passed,
    }
    return AttributionMetrics(
        citation_correctness=citation,
        evidence_precision=evidence,
        causal_relevance=causal,
        material_coverage=material,
        unsupported_primary_claims=unsupported_primary_res,
        unsupported_material_claims=unsupported_material_res,
        false_sufficient=false_sufficient_res,
        abstain_producing_sufficient=abstain_res,
        no_material_producing_sufficient=no_material_res,
        no_material_without_sanity=no_material_sanity_res,
        status_confusion_matrix=confusion,
        refusal_correctness=refusal,
        latency_ms=latency,
        tokens=tokens,
        cost_usd=cost,
        coverage_limited_count=coverage_limited_count,
        model_limited_count=model_limited_count,
        gates=gates,
    )


__all__ = [
    "AttributionMetrics",
    "RunAttributionOutput",
    "RunClaimOutput",
    "compute_attribution_metrics",
]
