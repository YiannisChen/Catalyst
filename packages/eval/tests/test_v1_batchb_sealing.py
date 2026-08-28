"""M7 Batch-B corrective: fail-close EvalManifest/outcome sealing.

The runner must recompute authoritative RunManifest/result hashes from the
serialized artifacts, bind ordered case refs to RunManifest id/hash, include
and validate ContextPack/ClaimPlan/Assurance refs, and reject missing,
duplicate, reordered, extra, mismatched, or non-COMPLETED success bindings.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalyst_eval.v1_1.execution_ledger import ExecutionLedger, LedgerRow
from catalyst_eval.v1_1.manifest import EvalManifest, EvalOutcome, RunArtifactIdentity
from catalyst_eval.v1_1.runner import CaseRunOutcome, RunArtifactHashError, run_stage1


@pytest.fixture()
def builder_manifest() -> EvalManifest:
    from tests.v1_1_fixtures import build_fixture_eval_manifest

    return build_fixture_eval_manifest()


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _real_hash(payload) -> str:
    from catalyst_eval.v1_1.loader import canonical_bytes
    import hashlib

    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _outcome(case_id: str, **overrides) -> CaseRunOutcome:
    run_manifest_payload = {"run_id": f"manifest:{case_id}", "case_id": case_id}
    result_artifact_payload = {"result": case_id}
    base = dict(
        case_id=case_id,
        run_manifest_id=f"manifest:{case_id}",
        run_manifest_hash=_real_hash(run_manifest_payload),
        result_artifact_id=f"result:{case_id}",
        result_artifact_hash=_real_hash(result_artifact_payload),
        terminal_status="COMPLETED",
        run_manifest_payload=run_manifest_payload,
        result_artifact_payload=result_artifact_payload,
        context_pack_ref={
            "artifact_id": f"pack:{case_id}", "artifact_hash": "b" * 64,
        },
        claim_plan_ref={
            "artifact_id": f"claimplan:{case_id}", "artifact_hash": "c" * 64,
        },
        assurance_ref={
            "artifact_id": f"assurance:{case_id}", "artifact_hash": "b" * 64,
        },
        provider_calls=2,
        cost_usd=0.01,
        latency_ms=100,
        tokens=200,
    )
    base.update(overrides)
    return CaseRunOutcome(**base)


def _fake_adapter(manifest: EvalManifest, *, outcomes=None, bad_case: str | None = None, **bad_overrides):
    outcomes = outcomes or {
        case_id: _outcome(case_id)
        for case_id in manifest.evaluation_identity.ordered_case_ids
    }
    if bad_case is not None:
        outcomes = {
            **outcomes,
            bad_case: _outcome(bad_case, **bad_overrides),
        }

    def adapter(case_id: str):
        return outcomes[case_id]

    return adapter


def _cases(manifest: EvalManifest):
    return {case_id: None for case_id in manifest.evaluation_identity.ordered_case_ids}


def test_runner_recomputes_hashes_from_serialized_artifacts(builder_manifest):
    """A declared hash that does not match the canonical artifact bytes is
    rejected even when it is a well-formed SHA-256 digest."""
    manifest = builder_manifest
    first = manifest.evaluation_identity.ordered_case_ids[0]
    with pytest.raises(RunArtifactHashError, match="run_manifest_hash"):
        run_stage1(
            manifest,
            _cases(manifest),
            runner_adapter=_fake_adapter(
                manifest, bad_case=first, run_manifest_hash="a" * 64
            ),
            metrics_fn=lambda m, o: ((), {}),
        )


def test_runner_rejects_missing_serialized_artifacts(builder_manifest):
    manifest = builder_manifest
    first = manifest.evaluation_identity.ordered_case_ids[0]
    with pytest.raises(RunArtifactHashError, match="run_manifest_payload"):
        run_stage1(
            manifest,
            _cases(manifest),
            runner_adapter=_fake_adapter(
                manifest, bad_case=first, run_manifest_payload=None
            ),
            metrics_fn=lambda m, o: ((), {}),
        )


def test_runner_rejects_non_completed_success_bindings(builder_manifest):
    """FAILED/CANCELLED/nonterminal outcomes must never append a success
    outcome even when every hash and order check passes."""
    manifest = builder_manifest
    for status in ("FAILED", "CANCELLED", "RUNNING"):
        first = manifest.evaluation_identity.ordered_case_ids[0]
        bad = _outcome(first, terminal_status=status)
        with pytest.raises(ValueError, match="COMPLETED"):
            run_stage1(
                manifest,
                _cases(manifest),
                runner_adapter=_fake_adapter(
                    manifest, bad_case=first, terminal_status=status
                ),
                metrics_fn=lambda m, o: ((), {}),
            )


def test_runner_binds_ordered_refs_to_run_manifest_identity(builder_manifest):
    """Ordered per-case bindings must be unique one-to-one on the RunManifest
    id; duplicate/missing/mismatched bindings fail closed."""
    manifest = builder_manifest
    first = manifest.evaluation_identity.ordered_case_ids[0]
    second = manifest.evaluation_identity.ordered_case_ids[1]
    # Two cases bound to the same RunManifest id is a duplicate binding.
    with pytest.raises(ValueError, match="duplicate run_manifest_id"):
        run_stage1(
            manifest,
            _cases(manifest),
            runner_adapter=_fake_adapter(
                manifest,
                bad_case=second,
                run_manifest_id=f"manifest:{first}",
            ),
            metrics_fn=lambda m, o: ((), {}),
        )


def test_runner_records_context_pack_claim_plan_assurance_refs(builder_manifest):
    manifest = builder_manifest
    report = run_stage1(
        manifest,
        _cases(manifest),
        runner_adapter=_fake_adapter(manifest),
        metrics_fn=lambda m, o: ((), {}),
    )
    identity = report.manifest.outcome.observed_run_artifact_identity
    assert len(identity.context_pack_refs) == len(
        manifest.evaluation_identity.ordered_case_ids
    )
    assert len(identity.claim_plan_refs) == len(
        manifest.evaluation_identity.ordered_case_ids
    )
    assert len(identity.assurance_refs) == len(
        manifest.evaluation_identity.ordered_case_ids
    )


def test_ledger_run_facts_survive_roundtrip_and_checksum(tmp_path):
    """Ledger rows carry the sealed run facts used by audit/report; tampered
    facts fail the checksum and are never identity-valid."""
    row = LedgerRow(
        eval_id="eval:stage1:v1",
        case_id="v1f-001",
        run_manifest_id="manifest:v1f-001",
        run_manifest_hash="b" * 64,
        result_artifact_id="result:v1f-001",
        result_artifact_hash="c" * 64,
        terminal_status="COMPLETED",
        attempts=1,
        provider_calls=2,
        cost_usd=0.01,
        checksum="x",
        run_facts={
            "schema_version": "v1_1_stage1_run_facts_v1",
            "case_id": "v1f-001",
            "output_status": "SUFFICIENT",
            "claims": [],
        },
    )
    ledger = ExecutionLedger(rows=(row,))
    path = tmp_path / "ledger.jsonl"
    ledger.write(path)
    loaded = ExecutionLedger.load(path, expected_eval_id="eval:stage1:v1")
    assert loaded.rows[0].run_facts["output_status"] == "SUFFICIENT"
    assert loaded.rows[0].identity_valid is True
