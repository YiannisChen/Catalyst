from __future__ import annotations

from attribution_fixtures import VALID_ASSURANCE_RECORD, mock_run_artifacts


def test_assurance_record_produced_per_run():
    from catalyst_agents.runtime.assurance.record import RunAssuranceRecord

    record = RunAssuranceRecord.model_validate(VALID_ASSURANCE_RECORD)
    assert [check.check_name for check in record.checks] == [
        "cutoff", "citation_resolution", "judge_visibility",
        "prerequisite_gates", "legal_path", "trace_completeness",
        "identities", "budget_retry_repair", "degraded_state",
        "structured_context_support",
    ]


def test_assurance_without_eval_import():
    # In a fresh interpreter: an in-process sys.modules check depends on which
    # test files were collected first.
    import os
    import subprocess
    import sys
    from pathlib import Path

    packages = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        str(packages / name) for name in ("agents", "data", "api"))}
    code = "import sys, catalyst_agents.runtime.assurance; print('catalyst_eval' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_assurance_deterministic():
    from catalyst_agents.runtime.assurance.checks import run_all_checks

    artifacts1 = mock_run_artifacts(cutoff="2026-01-15T21:00:00Z", citations=["c1", "c2"])
    artifacts2 = mock_run_artifacts(cutoff="2026-01-15T21:00:00Z", citations=["c1", "c2"])

    assert run_all_checks("run-001", artifacts1) == run_all_checks("run-001", artifacts2)


def test_source_support_flags_in_assurance():
    from catalyst_agents.runtime.assurance.checks import compute_source_flags

    flags = compute_source_flags([{"source_class": "analysis_opinion"}, {"source_class": "reported_news"}])
    assert flags["opinion_only_support"] is False
    assert flags["unknown_origin_support"] is False

    opinion_only = compute_source_flags([{"source_class": "analysis_opinion"}])
    assert opinion_only["opinion_only_support"] is True




def test_corruption_checks_fail_independently():
    from catalyst_agents.runtime.assurance.checks import run_all_checks

    cases = {
        "citation_resolution": mock_run_artifacts(citations=["missing"]),
        "judge_visibility": mock_run_artifacts(judge_visible_ids=[]),
        "cutoff": mock_run_artifacts(cutoff="2026-01-15T17:00:00Z"),
        "prerequisite_gates": mock_run_artifacts(gate_results=[("market", False, "no OHLCV")]),
        "legal_path": mock_run_artifacts(legal_path_ok=False),
        "identities": mock_run_artifacts(retrieval_corpus_manifest_id="other"),
        "budget_retry_repair": mock_run_artifacts(repair_count=2),
    }
    for expected, artifacts in cases.items():
        failed = [check.check_name for check in run_all_checks("run-001", artifacts) if check.status == "fail"]
        assert expected in failed


def test_assurance_rejects_missing_judge_visibility_artifact():
    from catalyst_agents.runtime.assurance.checks import run_all_checks

    artifacts = mock_run_artifacts(judge_visible_ids=None)
    failed = {check.check_name for check in run_all_checks("run-001", artifacts) if check.status == "fail"}
    assert "judge_visibility" in failed


def test_source_flags_use_valid_support_only():
    from catalyst_agents.runtime.assurance.checks import compute_source_flags

    flags = compute_source_flags([
        {"source_class": "analysis_opinion", "valid_support": True},
        {"source_class": "reported_news", "valid_support": False},
    ])
    assert flags["opinion_only_support"] is True


# ── AMEND-5: no-hypothesis ABSTAIN marks judge/prerequisite gates N/A ────────

