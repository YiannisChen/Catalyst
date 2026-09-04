"""M7-6: Stage-1 attribution metrics + sealed human output audit.

Citation correctness, causal support, and unsupported-material decisions come
only from the sealed human output-audit artifact; LLM judges remain
diagnostics. DATA-02: coverage-limited cases are non-scorable for
news-attribution efficacy metrics only. News-body absence is never
misattributed to the model. Model-failure safety gates are never suppressed by
coverage limitation or by empty expected_primary_evidence:

- false_sufficient denominator/eligible_count = all 12 Stage-1 cases.
- abstain_producing_sufficient denominator/eligible_count = ABSTAIN oracle
  cases only.
- no_material_producing_sufficient denominator = cases whose
  expected_attribution_type is NO_MATERIAL_PUBLIC_CATALYST.
- no_material_without_sanity denominator = runs producing
  NO_MATERIAL_PUBLIC_CATALYST.

expected_primary_evidence controls primary-source retrieval/citation
eligibility only; bound primary-source (including White House/CMS NEWS
evidence) stays eligible for citation metrics when the news body is
coverage-limited. coverage_limited and model_limited reporting stay separate.
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
    model_limited: bool = False,
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
        model_limited=model_limited,
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


def _run_all(
    gold_cases: list[GoldenCase],
    *,
    output_status: dict[str, str] | None = None,
    coverage_limited: set[str] | None = None,
    model_limited: set[str] | None = None,
):
    """Run every gold case through a default-support run with per-case toggles."""
    limited = coverage_limited or set()
    model = model_limited or set()
    runs = []
    audits = []
    for gold in gold_cases:
        run = _run_output(
            gold,
            claims=(_claim(f"claim-{gold.case_id}"),),
            output_status=(output_status or {}).get(gold.case_id),
            coverage_limited=gold.case_id in limited,
            model_limited=gold.case_id in model,
        )
        runs.append(run)
        audits.append(_audit(gold, run))
    return compute_attribution_metrics(runs, audits, gold_cases)


def test_news_gap_coverage_limited_case_is_non_scorable_for_news_attribution_only():
    """News-body absence with no bound primary-source is non-scorable for
    news-attribution/citation metrics only; false SUFFICIENT still counts."""
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
    # false SUFFICIENT is a model-failure safety gate: all cases are eligible.
    assert metrics.false_sufficient.eligible_count == 1
    assert metrics.false_sufficient.non_scorable_count == 0
    assert metrics.false_sufficient.numerator == 1
    # News-attribution efficacy (citation) stays non-scorable on the gap.
    assert metrics.citation_correctness.eligible_count == 0
    assert metrics.citation_correctness.non_scorable_count == 1


def test_bound_primary_source_coverage_limited_case_counts_model_failure_and_citation():
    """Coverage limitation must not exclude bound primary-source (filing or
    White House/CMS NEWS evidence) from model-failure or citation metrics."""
    gold = GoldenCase.model_validate(
        make_case(
            case_id="bound-gap-1", ticker="TSLA", session_date="2025-07-24",
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


def test_c04_regression_abstain_coverage_limited_empty_primary_sufficient_fails_safety_gates():
    """c04 regression: ABSTAIN + coverage_limited + empty expected_primary,
    model output SUFFICIENT, must fail false_sufficient AND
    abstain_producing_sufficient."""
    gold = GoldenCase.model_validate(
        make_case(
            case_id="v1f-c04-reg", ticker="JPM", session_date="2025-02-20",
            cutoff="2025-02-20T21:00:00Z", question="Why did JPM sit out this session?",
            oracle_status="ABSTAIN", direction="unknown",
            cause_types=(),
            expected_refusal_reason=REFUSAL,
            expected_attribution_type=None,
        )
    )
    run = _run_output(
        gold,
        output_status="SUFFICIENT",
        claims=(),
        coverage_limited=True,
    )
    audit = _audit(gold, run)
    metrics = compute_attribution_metrics([run], [audit], [gold])
    assert metrics.coverage_limited_count == 1
    # The case is registered by BOTH safety gates: a false SUFFICIENT and an
    # ABSTAIN producing SUFFICIENT. The false_sufficient aggregate gate uses
    # the locked <=1-of-12 tolerance (one lone violation still passes the
    # count<=1 formula); the ABSTAIN hard gate has zero tolerance and fails.
    assert metrics.false_sufficient.numerator == 1
    assert metrics.false_sufficient.eligible_count == 1
    assert metrics.false_sufficient.non_scorable_count == 0
    assert metrics.false_sufficient.gate_passed is True
    assert metrics.abstain_producing_sufficient.numerator == 1
    assert metrics.abstain_producing_sufficient.eligible_count == 1
    assert metrics.abstain_producing_sufficient.non_scorable_count == 0
    assert metrics.abstain_producing_sufficient.gate_passed is False
    assert metrics.status_confusion_matrix.get(("ABSTAIN", "SUFFICIENT")) == 1


def test_false_sufficient_denominator_is_all_stage1_cases():
    """false SUFFICIENT uses the locked <=1-of-12 denominator: every one of
    the 12 Stage-1 cases is eligible regardless of coverage limitation."""
    gold_cases = _gold_rows()
    metrics = _run_all(
        gold_cases,
        output_status={"v1f-007": "SUFFICIENT"},
        coverage_limited={"v1f-007"},
    )
    assert metrics.coverage_limited_count == 1
    assert metrics.false_sufficient.numerator == 1
    assert metrics.false_sufficient.eligible_count == 12
    assert metrics.false_sufficient.non_scorable_count == 0
    assert metrics.false_sufficient.case_ids == ("v1f-007",)


def test_abstain_producing_sufficient_denominator_is_abstain_cases_only():
    """abstain_producing_sufficient counts ABSTAIN-oracle cases that output
    SUFFICIENT; other cases are non-scorable for that metric."""
    gold_cases = _gold_rows()
    metrics = _run_all(
        gold_cases,
        output_status={"v1f-011": "SUFFICIENT"},
    )
    assert metrics.abstain_producing_sufficient.numerator == 1
    assert metrics.abstain_producing_sufficient.eligible_count == 1
    assert metrics.abstain_producing_sufficient.non_scorable_count == 11
    assert metrics.abstain_producing_sufficient.gate_passed is False
    assert metrics.false_sufficient.numerator == 1
    assert metrics.false_sufficient.eligible_count == 12


def test_no_material_producing_sufficient_denominator_is_expected_no_material_cases():
    """no_material_producing_sufficient denominator = cases whose
    expected_attribution_type is NO_MATERIAL_PUBLIC_CATALYST."""
    nm1 = GoldenCase.model_validate(
        make_case(
            case_id="nm-1", ticker="AAPL", session_date="2026-01-06",
            cutoff="2026-01-06T21:00:00Z", question="Why did AAPL move?",
            oracle_status="PARTIAL", direction="mixed",
            cause_types=("MACRO_EVENT",), labels=("fixture-macro",),
            expected_attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
        )
    )
    nm2 = GoldenCase.model_validate(
        make_case(
            case_id="nm-2", ticker="AAPL", session_date="2026-01-07",
            cutoff="2026-01-07T21:00:00Z", question="Why did AAPL move again?",
            oracle_status="PARTIAL", direction="mixed",
            cause_types=("MACRO_EVENT",), labels=("fixture-macro",),
            expected_attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
        )
    )
    eb = GoldenCase.model_validate(
        make_case(
            case_id="eb-1", ticker="MSFT", session_date="2026-01-08",
            cutoff="2026-01-08T21:00:00Z", question="Why did MSFT move?",
            oracle_status="PARTIAL", direction="positive",
            cause_types=("COMPANY_SPECIFIC_CATALYST",), labels=("fixture-msft",),
            evidence_ids=("fixture-ev-004",),
            expected_primary_evidence=("fixture-ev-004",),
        )
    )
    gold_cases = [nm1, nm2, eb]
    runs = [
        _run_output(nm1, output_status="SUFFICIENT"),
        _run_output(nm2, output_status="PARTIAL", attribution_type="NO_MATERIAL_PUBLIC_CATALYST", sanity_tasks=("sanity:ok",)),
        _run_output(eb, output_status="SUFFICIENT"),
    ]
    audits = [_audit(g, r) for g, r in zip(gold_cases, runs)]
    metrics = compute_attribution_metrics(runs, audits, gold_cases)
    assert metrics.no_material_producing_sufficient.numerator == 1
    assert metrics.no_material_producing_sufficient.eligible_count == 2
    assert metrics.no_material_producing_sufficient.non_scorable_count == 1
    assert metrics.false_sufficient.numerator == 2
    assert metrics.false_sufficient.eligible_count == 3


def test_no_material_without_sanity_denominator_is_no_material_runs():
    """no_material_without_sanity counts runs that produced
    NO_MATERIAL_PUBLIC_CATALYST without completed sanity tasks."""
    nm_a = GoldenCase.model_validate(
        make_case(
            case_id="nm-a", ticker="AAPL", session_date="2026-01-06",
            cutoff="2026-01-06T21:00:00Z", question="Why did AAPL move?",
            oracle_status="PARTIAL", direction="mixed",
            cause_types=("MACRO_EVENT",), labels=("fixture-macro",),
            expected_attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
        )
    )
    nm_b = GoldenCase.model_validate(
        make_case(
            case_id="nm-b", ticker="AAPL", session_date="2026-01-07",
            cutoff="2026-01-07T21:00:00Z", question="Why did AAPL move again?",
            oracle_status="PARTIAL", direction="mixed",
            cause_types=("MACRO_EVENT",), labels=("fixture-macro",),
            expected_attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
        )
    )
    eb = GoldenCase.model_validate(
        make_case(
            case_id="eb-x", ticker="MSFT", session_date="2026-01-08",
            cutoff="2026-01-08T21:00:00Z", question="Why did MSFT move?",
            oracle_status="PARTIAL", direction="positive",
            cause_types=("COMPANY_SPECIFIC_CATALYST",), labels=("fixture-msft",),
            evidence_ids=("fixture-ev-004",),
            expected_primary_evidence=("fixture-ev-004",),
        )
    )
    gold_cases = [nm_a, nm_b, eb]
    runs = [
        _run_output(nm_a, output_status="PARTIAL", attribution_type="NO_MATERIAL_PUBLIC_CATALYST", sanity_tasks=()),
        _run_output(nm_b, output_status="PARTIAL", attribution_type="NO_MATERIAL_PUBLIC_CATALYST", sanity_tasks=("sanity:ok",)),
        _run_output(eb, output_status="PARTIAL"),
    ]
    audits = [_audit(g, r) for g, r in zip(gold_cases, runs)]
    metrics = compute_attribution_metrics(runs, audits, gold_cases)
    assert metrics.no_material_without_sanity.numerator == 1
    assert metrics.no_material_without_sanity.eligible_count == 2
    assert metrics.no_material_without_sanity.non_scorable_count == 1
    assert metrics.no_material_without_sanity.gate_passed is False


def test_model_limited_count_is_zero_without_explicit_model_limitation():
    """model_limited_count is not an alias for model-eligible cases: a full
    12-case run with no explicit model/runtime limitation must count 0."""
    gold_cases = _gold_rows()
    metrics = _run_all(gold_cases)
    assert metrics.model_limited_count == 0
    assert metrics.false_sufficient.eligible_count == 12


def test_model_limited_count_counts_explicit_model_limited_runs():
    """A run explicitly flagged model_limited is counted once; the counter is
    independent from coverage_limited_count."""
    gold_cases = _gold_rows()
    metrics = _run_all(
        gold_cases,
        coverage_limited={"v1f-003"},
        model_limited={"v1f-007"},
    )
    assert metrics.coverage_limited_count == 1
    assert metrics.model_limited_count == 1


def test_empty_no_material_checks_record_not_exercised():
    """Zero-eligible conditional NO_MATERIAL checks must publish exercised=false
    instead of implying the zero-violation formula was empirically validated."""
    gold_cases = _gold_rows()
    assert not any(
        g.expected_attribution_type == "NO_MATERIAL_PUBLIC_CATALYST"
        for g in gold_cases
    ), "fixture regression: baseline must not contain a NO_MATERIAL stratum"
    metrics = _run_all(gold_cases)
    assert metrics.no_material_producing_sufficient.eligible_count == 0
    assert metrics.no_material_producing_sufficient.numerator == 0
    assert metrics.no_material_producing_sufficient.gate_passed is True
    assert metrics.no_material_producing_sufficient.exercised is False
    assert metrics.no_material_without_sanity.eligible_count == 0
    assert metrics.no_material_without_sanity.numerator == 0
    assert metrics.no_material_without_sanity.gate_passed is True
    assert metrics.no_material_without_sanity.exercised is False
    as_dict = metrics.as_dict()
    assert as_dict["no_material_producing_sufficient"]["exercised"] is False
    assert as_dict["no_material_without_sanity"]["exercised"] is False


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
