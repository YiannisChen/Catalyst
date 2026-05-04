from __future__ import annotations

from catalyst_eval.harness.frozen_eval import build_case_distribution, calibrate_thresholds


def test_build_case_distribution_counts_expected_status_and_refusals():
    rows = [
        {"expected_status": "SUFFICIENT", "should_refuse": False},
        {"expected_status": "SUFFICIENT", "should_refuse": False},
        {"expected_status": "PARTIAL", "should_refuse": False},
        {"expected_status": "INSUFFICIENT", "should_refuse": True},
    ]

    distribution = build_case_distribution(rows)

    assert distribution == {"sufficient": 2, "partial": 1, "should_refuse": 1}


def test_calibrate_thresholds_prefers_strictest_top_scoring_candidate():
    observations = [
        {"expected_status": "SUFFICIENT", "evidence_count": 4, "magnitude_coverage": 0.72},
        {"expected_status": "SUFFICIENT", "evidence_count": 4, "magnitude_coverage": 0.68},
        {"expected_status": "PARTIAL", "evidence_count": 2, "magnitude_coverage": 0.58},
        {"expected_status": "INSUFFICIENT", "evidence_count": 0, "magnitude_coverage": 0.0},
    ]

    result = calibrate_thresholds(
        observations,
        current={"K_sufficient": 4, "K_partial": 2, "M_threshold": 0.6},
    )

    assert result["chosen"] == {"K_sufficient": 4, "K_partial": 2, "M_threshold": 0.6}
    assert result["chosen_accuracy"] == 1.0
    assert result["rationale"].startswith("Selected the strictest")
