"""M7-6: Stage-1 attribution metrics + sealed human output audit.

Citation correctness, causal support, and unsupported-material decisions come
only from the sealed human output-audit artifact; LLM judges remain
diagnostics. DATA-02: coverage-limited news cases are non-scorable for
news-attribution denominators. News-body absence does not exclude
filing-backed metrics: coverage-limited cases with bound
expected_primary_evidence remain eligible for model-failure
denominators. News-only coverage gaps are never reported as
Analyst/model failures. coverage_limited and model_limited
denominators stay separate.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalyst_eval.v1_1.attribution_metrics import (
    AttributionMetrics,
    RunAttributionOutput,
    RunClaimOutput,
    compute_attribution_metrics,
)
from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.output_audit import (
    AuditClaimDecision,
    Stage1OutputAudit,
    load_output_audit,
    validate_output_audit,
)

from tests.v1_1_fixtures import make_case, make_stage1_cases

REFUSAL = "insufficient_public_evidence"


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _gold_rows() -> list[GoldenCase]:
    return [GoldenCase.model_validate(row) for row in make_stage1_cases()]


def _run_output(
    gold: GoldenCase,
    *,
    output_status: str | None = None,
    attribution_type: str | None = None,
    refusal_reason: str | None = None,
    claims: tuple[RunClaimOutput, ...] = (),
    sanity_tasks: tuple[str, ...] = (),
    coverage_limited: bool = False,
) -> RunAttributionOutput:
    status = output_status or gold.oracle_status
    return RunAttributionOutput(
        case_id=gold.case_id,
        output_status=status,
        attribution_type=attribution_type
        if attribution_type is not None
        else (gold.expected_attribution_type or "EVIDENCE_BACKED_CAUSAL"),
        refusal_reason=refusal_reason if refusal_reason is not None else gold.expected_refusal_reason,
        claims=claims,
        sanity_tasks_completed=sanity_tasks,
        latency_ms=100,
        tokens=200,
        cost_usd=0.01,
        coverage_limited=coverage_limited,
    )


def _claim(claim_id: str, *, material: bool = True, citations: tuple[str, ...] = ("fixture-ev-001",)) -> RunClaimOutput:
    return RunClaimOutput(
        claim_id=claim_id,
        material=material,
        citation_ids=citations,
        role="PRIMARY",
    )


def _audit(
    gold: GoldenCase,
    run: RunAttributionOutput,
    *,
    decisions: tuple[AuditClaimDecision, ...] = (),
    adjudication_state: str = "resolved",
) -> Stage1OutputAudit:
    if not decisions:
        decisions = tuple(
            AuditClaimDecision(
                claim_id=claim.claim_id,
                material=claim.material,
                citation_ids=claim.citation_ids,
                decision="SUPPORT",
                reason_code="citations_resolve",
            )
            for claim in run.claims
        )
    return Stage1OutputAudit(
        eval_id="eval:stage1:v1",
        case_id=gold.case_id,
        run_manifest_id=f"manifest:{gold.case_id}",
        run_manifest_hash="b" * 64,
        result_artifact_id=f"result:{gold.case_id}",
        result_artifact_hash="c" * 64,
        auditor_id="fixture-auditor",
        audited_at=_utc("2026-08-19T00:00:00Z"),
        adjudication_state=adjudication_state,
        claims=decisions,
    )


def test_happy_path_attribution_gates_pass():
    gold_cases = _gold_rows()
    runs = []
    audits = []
    for gold in gold_cases:
        claim = _claim(f"claim-{gold.case_id}")
        run = _run_output(gold, claims=(claim,))
        runs.append(run)
        audits.append(_audit(gold, run))
    metrics = compute_attribution_metrics(runs, audits, gold_cases)
    assert isinstance(metrics, AttributionMetrics)
    assert metrics.citation_correctness.value == pytest.approx(1.0)
    assert metrics.citation_correctness.gate_passed is True
    assert metrics.unsupported_primary_claims.numerator == 0
    assert metrics.unsupported_material_claims.value == pytest.approx(0.0)
    assert metrics.abstain_producing_sufficient.numerator == 0
    assert metrics.no_material_producing_sufficient.numerator == 0
    assert metrics.no_material_without_sanity.numerator == 0
    assert metrics.false_sufficient.numerator == 0
    assert metrics.refusal_correctness.value == pytest.approx(1.0)


def test_unsupported_primary_claim_fails_hard_gate():
    gold_cases = _gold_rows()
    gold = gold_cases[0]
    run = _run_output(gold, claims=(_claim("c1"),))
    audit = _audit(
        gold,
        run,
        decisions=(
            AuditClaimDecision(
                claim_id="c1", material=True, citation_ids=("fixture-ev-001",),
                decision="UNSUPPORTED", reason_code="no_causal_support",
            ),
        ),
    )
    metrics = compute_attribution_metrics([run], [audit], [gold])
    assert metrics.unsupported_primary_claims.numerator == 1
    assert metrics.citation_correctness.value < 1.0
    assert metrics.citation_correctness.gate_passed is False


def test_abstain_producing_sufficient_is_counted():
    gold_cases = _gold_rows()
    gold = next(g for g in gold_cases if g.oracle_status == "ABSTAIN")
    run = _run_output(gold, output_status="SUFFICIENT", claims=(_claim("c1"),))
    audit = _audit(gold, run)
    metrics = compute_attribution_metrics([run], [audit], [gold])
    assert metrics.abstain_producing_sufficient.numerator == 1
    assert metrics.status_confusion_matrix.get(("ABSTAIN", "SUFFICIENT")) == 1


def test_no_material_without_sanity_is_counted():
    gold = GoldenCase.model_validate(
        make_case(
            case_id="nm-1", ticker="AAPL", session_date="2026-01-06",
            cutoff="2026-01-06T21:00:00Z", question="Why did AAPL move?",
            oracle_status="PARTIAL", direction="mixed",
            cause_types=("MACRO_EVENT",), labels=("fixture-macro",),
            expected_attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
        )
    )
    run = _run_output(
        gold,
        output_status="PARTIAL",
        attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
        sanity_tasks=(),
    )
    audit = _audit(gold, run)
    metrics = compute_attribution_metrics([run], [audit], [gold])
    assert metrics.no_material_without_sanity.numerator == 1
    assert metrics.no_material_producing_sufficient.numerator == 0


def test_news_only_coverage_limited_case_is_non_scorable_for_news_and_model_failure():
    """News-body absence with no filing primary excludes news-attribution and
    model-failure denominators. It is never reported as a model failure."""
    gold = GoldenCase.model_validate(
        make_case(
            case_id="news-gap-1", ticker="AAPL", session_date="2025-05-12",
            cutoff="2025-05-12T20:00:00Z", question="Why did AAPL move this session?",
            oracle_status="PARTIAL", direction="positive",
            cause_types=("MACRO_EVENT",), labels=("fixture-macro",),
            expected_primary_evidence=(),
        )
    )
    run = _run_output(
        gold,
        output_status="SUFFICIENT",
        claims=(_claim("c1"),),
        coverage_limited=True,
    )
    audit = _audit(gold, run)
    metrics = compute_attribution_metrics([run], [audit], [gold])
    assert metrics.coverage_limited_count == 1
    assert metrics.false_sufficient.eligible_count == 0
    assert metrics.false_sufficient.non_scorable_count == 1
    assert metrics.false_sufficient.numerator == 0
    assert metrics.citation_correctness.eligible_count == 0
    assert metrics.citation_correctness.non_scorable_count == 1


def test_filing_backed_coverage_limited_case_stays_eligible_for_filing_metrics():
    """News-body absence must not exclude filing-backed model-failure or
    citation denominators when expected_primary_evidence is bound."""
    gold = GoldenCase.model_validate(
        make_case(
            case_id="file-gap-1", ticker="TSLA", session_date="2025-07-24",
            cutoff="2025-07-24T20:00:00Z", question="Why did TSLA fall?",
            oracle_status="PARTIAL", direction="negative",
            cause_types=("COMPANY_SPECIFIC_CATALYST",), labels=("fixture-label-0",),
            evidence_ids=("fixture-ev-001",),
            expected_primary_evidence=("fixture-ev-001",),
        )
    )
    run = _run_output(
        gold,
        output_status="SUFFICIENT",
        claims=(_claim("c1"),),
        coverage_limited=True,
    )
    audit = _audit(gold, run)
    metrics = compute_attribution_metrics([run], [audit], [gold])
    assert metrics.coverage_limited_count == 1
    assert metrics.false_sufficient.eligible_count == 1
    assert metrics.false_sufficient.non_scorable_count == 0
    assert metrics.false_sufficient.numerator == 1
    assert metrics.citation_correctness.eligible_count == 1
    assert metrics.citation_correctness.non_scorable_count == 0


def test_output_audit_roundtrip_and_validation(tmp_path):
    gold_cases = _gold_rows()
    gold = gold_cases[0]
    run = _run_output(gold, claims=(_claim("c1"),))
    audit = _audit(gold, run)
    path = tmp_path / "audit.jsonl"
    path.write_text(audit.model_dump_json() + "\n", encoding="utf-8")
    loaded = load_output_audit(path)
    assert loaded[0].schema_version == "v1_1_stage1_output_audit_v1"
    validated = validate_output_audit(loaded[0], run, gold, eval_id="eval:stage1:v1")
    assert validated is True


def test_output_audit_rejects_unaudited_material_claim(tmp_path):
    gold_cases = _gold_rows()
    gold = gold_cases[0]
    run = _run_output(gold, claims=(_claim("c1"), _claim("c2")))
    audit = _audit(gold, run, decisions=(
        AuditClaimDecision(
            claim_id="c1", material=True, citation_ids=("fixture-ev-001",),
            decision="SUPPORT", reason_code="citations_resolve",
        ),
    ))
    with pytest.raises(ValueError, match="material claim"):
        validate_output_audit(audit, run, gold, eval_id="eval:stage1:v1")


def test_output_audit_rejects_identity_mismatch(tmp_path):
    gold_cases = _gold_rows()
    gold = gold_cases[0]
    run = _run_output(gold, claims=(_claim("c1"),))
    audit = _audit(gold, run)
    with pytest.raises(ValueError, match="run_manifest"):
        validate_output_audit(audit, run, gold, eval_id="eval:stage1:v1", run_manifest_id="other")
