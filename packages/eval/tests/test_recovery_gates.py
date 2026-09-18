"""M8-C/M8-D contract tests: causal-recovery gates without M7 reinterpretation."""
from __future__ import annotations

import pytest

from catalyst_eval.v1_1.recovery_gates import (
    NOT_EXERCISED,
    RECOVERY_FTS_GATES,
    RECOVERY_GATE_VERSION,
    RECOVERY_HYBRID_GATES,
    RecoveryCaseResult,
    evaluate_recovery_gates,
    recovery_report_section,
)
from catalyst_eval.v1_1.retrieval_metrics import STAGE1_RETRIEVAL_GATES


def _results(**overrides):
    base = [
        RecoveryCaseResult("c01", "SUFFICIENT", "SUFFICIENT", 3, 3, 0),
        RecoveryCaseResult("c04", "ABSTAIN", "ABSTAIN", 0, 0, 0),
    ]
    for case_id, gold, observed in overrides.get("extra", []):
        base.append(RecoveryCaseResult(case_id, gold, observed, 1, 1, 0))
    return base


def test_locked_thresholds_are_unchanged():
    assert RECOVERY_FTS_GATES == {
        "answerable_non_abstain": (6, 11),
        "c01_non_abstain": True,
        "c04_abstain": True,
        "citation_correctness": 1.0,
        "unsupported_primary_claims": 0,
        "false_sufficient_max": 1,
    }
    assert RECOVERY_HYBRID_GATES == {
        "sufficient_non_abstain": (7, 9),
        "status_match_min": (8, 12),
        "c04_abstain": True,
        "citation_correctness": 1.0,
        "unsupported_primary_claims": 0,
        "false_sufficient_max": 1,
    }
    assert RECOVERY_GATE_VERSION == "m8_v1"
    assert STAGE1_RETRIEVAL_GATES == {
        "recall_at_8": 0.75,
        "primary_source_hit": 0.80,
        "duplicate_adjusted_precision": 0.60,
        "no_ticker_or_cutoff_violations": 0.0,
    }


def test_empty_denominators_are_not_exercised_and_fail_the_seal():
    cases = [RecoveryCaseResult("c04", "ABSTAIN", "ABSTAIN")]
    report = evaluate_recovery_gates(mode="fts", case_results=cases)
    assert report.gates["answerable_non_abstain"] == NOT_EXERCISED
    assert report.gates["citation_correctness"] == NOT_EXERCISED
    assert report.gates["unsupported_primary_claims"] == NOT_EXERCISED
    assert report.gates["c01_non_abstain"] == NOT_EXERCISED
    assert report.sealed is False


def test_hybrid_gate_counts_and_seal():
    cases = [
        RecoveryCaseResult(f"c{i:02d}", "SUFFICIENT", "SUFFICIENT", 1, 1, 0)
        for i in range(1, 9)
    ]
    cases.append(RecoveryCaseResult("c09", "SUFFICIENT", "PARTIAL", 1, 1, 0))
    cases.append(RecoveryCaseResult("c10", "PARTIAL", "PARTIAL", 1, 1, 0))
    cases.append(RecoveryCaseResult("c11", "PARTIAL", "ABSTAIN", 0, 0, 0))
    cases.append(RecoveryCaseResult("c04", "ABSTAIN", "ABSTAIN", 0, 0, 0))
    report = evaluate_recovery_gates(mode="hybrid", case_results=cases)
    assert report.counts["gold_sufficient"] == 9
    assert report.counts["sufficient_non_abstain"] == 9
    assert report.counts["status_match"] == 10
    assert report.gates["sufficient_non_abstain"] == "PASS"
    assert report.gates["status_match_min"] == "PASS"
    assert report.gates["c04_abstain"] == "PASS"
    assert report.sealed is True

    regressed = [*cases[:-1], RecoveryCaseResult("c04", "ABSTAIN", "SUFFICIENT")]
    assert evaluate_recovery_gates(mode="hybrid", case_results=regressed).sealed is False


def test_unsupported_and_false_sufficient_fail_the_seal():
    cases = _results() + [
        RecoveryCaseResult("c02", "PARTIAL", "SUFFICIENT", 2, 1, 1),
    ]
    report = evaluate_recovery_gates(mode="fts", case_results=cases)
    assert report.gates["unsupported_primary_claims"] == "FAIL"
    assert report.gates["citation_correctness"] == "FAIL"
    assert report.gates["false_sufficient_max"] == "PASS"
    section = recovery_report_section(
        mode="fts",
        case_results=[
            {"case_id": "c01", "gold_status": "SUFFICIENT",
             "observed_status": "SUFFICIENT", "citation_total": 1,
             "citation_correct": 1, "unsupported_primary_claims": 0},
            {"case_id": "c04", "gold_status": "ABSTAIN",
             "observed_status": "ABSTAIN"},
        ],
    )
    assert section["recovery_gate_version"] == "m8_v1"
    assert section["sealed"] is False


def test_report_section_never_changes_m7_retrieval_gates():
    import catalyst_eval.v1_1.report as report_module

    source = report_module.build_report_payload.__code__.co_names
    assert "recovery_report_section" in source
    assert "STAGE1_RETRIEVAL_GATES" not in source
