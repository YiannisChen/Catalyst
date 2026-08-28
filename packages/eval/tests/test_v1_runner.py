"""M7-8: Stage-1 runner over the M6 app/SSE boundary.

run_stage1 captures the pre-submit RunManifest binding, waits for a terminal
event, verifies artifact hashes, and appends exactly one outcome only after
the complete ordered case set exists. Ordered observed bindings match
ordered case_ids one-to-one; resume reuses only identity-valid terminal
ledger rows and never reruns or bills twice.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalyst_eval.v1_1.execution_ledger import ExecutionLedger, LedgerRow
from catalyst_eval.v1_1.manifest import EvalManifest, LatencyTokensCost, RunArtifactIdentity
from catalyst_eval.v1_1.runner import (
    CaseRunOutcome,
    Stage1Report,
    run_stage1,
)

def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


@pytest.fixture()
def builder_manifest() -> EvalManifest:
    from tests.v1_1_fixtures import build_fixture_eval_manifest

    return build_fixture_eval_manifest()


def _outcome(case_id: str) -> CaseRunOutcome:
    return CaseRunOutcome(
        case_id=case_id,
        run_manifest_id=f"manifest:{case_id}",
        run_manifest_hash="b" * 64,
        result_artifact_id=f"result:{case_id}",
        result_artifact_hash="c" * 64,
        terminal_status="COMPLETED",
        provider_calls=2,
        cost_usd=0.01,
        latency_ms=100,
        tokens=200,
    )


def _fake_adapter(manifest: EvalManifest):
    outcomes = {
        case_id: _outcome(case_id)
        for case_id in manifest.evaluation_identity.ordered_case_ids
    }

    def adapter(case_id: str):
        return outcomes[case_id]

    return adapter


def test_runner_appends_outcome_after_complete_ordered_set(builder_manifest):
    manifest = builder_manifest
    cases = {case_id: None for case_id in manifest.evaluation_identity.ordered_case_ids}
    report = run_stage1(
        manifest, cases, runner_adapter=_fake_adapter(manifest), metrics_fn=lambda m, o: ((), {})
    )
    assert isinstance(report, Stage1Report)
    assert report.manifest.outcome is not None
    refs = report.manifest.outcome.per_case_result_refs
    assert [ref.case_id for ref in refs] == list(
        manifest.evaluation_identity.ordered_case_ids
    )
    # Exactly one outcome; identity unchanged.
    with pytest.raises(ValueError, match="once"):
        report.manifest.append_outcome(report.manifest.outcome)


def test_runner_verifies_artifact_hashes(builder_manifest):
    from catalyst_eval.v1_1.runner import RunArtifactHashError

    manifest = builder_manifest
    cases = {case_id: None for case_id in manifest.evaluation_identity.ordered_case_ids}

    def bad_adapter(case_id: str):
        outcome = _outcome(case_id)
        return CaseRunOutcome(
            **{**outcome.__dict__, "result_artifact_hash": "z" * 64}
        )

    with pytest.raises(RunArtifactHashError):
        run_stage1(manifest, cases, runner_adapter=bad_adapter, metrics_fn=lambda m, o: ((), {}))


def test_runner_rejects_missing_case(builder_manifest):
    manifest = builder_manifest
    cases = {case_id: None for case_id in manifest.evaluation_identity.ordered_case_ids}
    del cases[manifest.evaluation_identity.ordered_case_ids[0]]
    with pytest.raises(ValueError, match="one-to-one"):
        run_stage1(manifest, cases, runner_adapter=_fake_adapter(manifest), metrics_fn=lambda m, o: ((), {}))


def test_ledger_roundtrip_and_checksum(tmp_path):
    rows = [
        LedgerRow(
            eval_id="eval:stage1:v1", case_id="v1f-001",
            run_manifest_id="manifest:v1f-001", run_manifest_hash="b" * 64,
            result_artifact_id="result:v1f-001", result_artifact_hash="c" * 64,
            terminal_status="COMPLETED", attempts=1, provider_calls=2,
            cost_usd=0.01, checksum="x",
        ),
    ]
    ledger = ExecutionLedger(rows=tuple(rows))
    path = tmp_path / "ledger.jsonl"
    ledger.write(path)
    loaded = ExecutionLedger.load(path)
    assert loaded.rows[0].case_id == "v1f-001"
    assert loaded.rows[0].identity_valid is True


def test_ledger_rejects_incompatible_eval_id(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = ExecutionLedger(
        rows=(
            LedgerRow(
                eval_id="eval:other", case_id="v1f-001",
                run_manifest_id="m", run_manifest_hash="b" * 64,
                result_artifact_id="r", result_artifact_hash="c" * 64,
                terminal_status="COMPLETED", attempts=1, provider_calls=1,
                cost_usd=0.01, checksum="x",
            ),
        )
    )
    ledger.write(path)
    with pytest.raises(ValueError, match="eval_id"):
        ExecutionLedger.load(path, expected_eval_id="eval:stage1:v1")


def test_ledger_never_reuses_nonterminal_or_corrupt_rows(tmp_path):
    rows = [
        LedgerRow(
            eval_id="eval:stage1:v1", case_id="v1f-001",
            run_manifest_id="m", run_manifest_hash="b" * 64,
            result_artifact_id="r", result_artifact_hash="c" * 64,
            terminal_status="RUNNING", attempts=1, provider_calls=1,
            cost_usd=0.01, checksum="x",
        ),
    ]
    ledger = ExecutionLedger(rows=tuple(rows))
    reusable = ledger.reusable_terminal_rows("eval:stage1:v1")
    assert reusable == ()
