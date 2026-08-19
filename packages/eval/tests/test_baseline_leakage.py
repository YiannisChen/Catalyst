"""M1-7: baseline leakage and secret scan tests."""

from __future__ import annotations

from catalyst_eval.baseline.leakage import scan_baseline_report


def _clean_report() -> dict:
    return {
        "schema_version": "baseline_v1",
        "identity": {
            "code_git_sha": "621375bc395e1dee644b335b2abe541a15d62fee",
            "integration_commit_sha": "549d5ffad0d995b7ce2767461109e19fb0ca5384",
            "package_versions": {"data_core": "0.1.0", "agents": "0.1.0",
                                 "app": "0.1.0", "eval": "0.1.0"},
            "snapshot_id": "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
            "app_default_db_marked_non_comparable": True,
        },
        "runs": [
            {
                "run_id": "run-1",
                "snapshot_id": "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
                "lancedb_table_name": "chunks__staging__b3761f4b943542a8",
            }
        ],
        "comparability": {
            "data_identity_comparable": False,
            "model_identity_comparable": False,
            "app_default_db_non_comparable": True,
        },
    }


def test_clean_report_passes():
    assert scan_baseline_report(_clean_report()) == []


def test_secret_key_name_fails():
    report = _clean_report()
    report["runs"][0]["api_key"] = "sk-test-1234567890"
    violations = scan_baseline_report(report)
    assert violations
    assert "api_key" in violations[0]
    assert "sk-test-1234567890" not in violations[0]


def test_provider_key_value_fails():
    report = _clean_report()
    report["runs"][0]["config"] = "model config with sk-live-abcdefgh123456 in it"
    violations = scan_baseline_report(report)
    assert violations
    assert "sk-live-abcdefgh123456" not in violations[0]


def test_golden_answer_key_in_run_row_fails():
    report = _clean_report()
    report["runs"][0]["oracle_answer"] = "AAPL rose because of strong earnings"
    violations = scan_baseline_report(report)
    assert violations
    assert "oracle_answer" in violations[0]


def test_golden_answer_marker_value_fails():
    report = _clean_report()
    report["runs"][0]["note"] = "golden marker THE_ANSWER_IS_42 embedded"
    violations = scan_baseline_report(
        report, golden_answer_markers=("THE_ANSWER_IS_42",)
    )
    assert violations
    assert "THE_ANSWER_IS_42" not in violations[0]


def test_violations_never_embed_secret_values():
    report = _clean_report()
    report["runs"][0]["credential"] = "supersecretvalue123"
    report["runs"][0]["api_key"] = "sk-test-zzzzzzzzzzzz"
    violations = scan_baseline_report(report)
    text = "\n".join(violations)
    assert "supersecretvalue123" not in text
    assert "sk-test-zzzzzzzzzzzz" not in text
