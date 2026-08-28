"""M7 Batch-B corrective: the live M6 app/SSE adapter is injectable and
fully fixture-testable without providers.

The adapter drives the M6 boundary (AdmissionController + RunClaimer +
SQLite authority) and seals every artifact hash from the serialized DB rows.
A fixture composition with a fake admit/claimer plus a seeded runtime DB
proves the full run_case contract offline.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from catalyst_app.events import (
    AssuranceCompletedPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
    StageStartedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import ArtifactPayload, EventRepository
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_eval.v1_1.loader import canonical_bytes
from catalyst_eval.v1_1.m6_adapter import M6AppSseRunnerAdapter, Stage1AdapterError

from tests.v1_1_fixtures import make_stage1_cases


def _utc_iso() -> str:
    return "2026-08-19T00:00:00Z"


def _sha(payload: dict) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


class _FakeComposition:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.admission = self._Admission(run_id)
        self.claimer = self._Claimer(run_id)

    class _Admission:
        def __init__(self, run_id: str):
            self.run_id = run_id

        def admit(self, request, *, pre_submit=None):
            from catalyst_app.runtime.admission import AdmissionOutcome

            return AdmissionOutcome(kind="accepted", run_id=self.run_id)

    class _Claimer:
        def __init__(self, run_id: str):
            self.run_id = run_id

        def current_lifecycle(self, run_id: str):
            return RunLifecycleStatus.COMPLETED


def _seed_completed_run(db_path: Path, run_id: str) -> dict:
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at,"
            " updated_at, owner, task_token)"
            " VALUES (?, 'RUNNING', 'req', ?, ?, 1, ?, ?, 'eval', 't')",
            (run_id, f"manifest:{run_id}", "b" * 64, _utc_iso(), _utc_iso()),
        )
        conn.commit()
    repo = EventRepository(db_path=db_path)
    manifest_payload = {"run_id": run_id, "case_id": "v1f-001"}
    result_payload = {
        "attribution_status": "SUFFICIENT",
        "attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "provider_calls": 2,
        "cost_usd": 0.01,
        "latency_ms": 100,
        "tokens": 200,
    }
    assurance_payload = {"valid": True, "checks": ["stream_complete"]}
    claim_payload = {
        "claim_id": "claim:v1f-001:1",
        "role": "PRIMARY",
        "statement": "fixture",
        "support_evidence_ids": ["fixture-ev-001"],
        "citation_evidence_ids": ["fixture-ev-001"],
        "validation_status": "validated",
    }
    repo.append(
        run_id=run_id,
        event_type=RunEventType.STAGE_STARTED,
        stage="CONTEXT_PACK_BUILD",
        payload=StageStartedPayload(stage="context_pack_build", round=1),
        artifact_payloads=[
            ArtifactPayload(
                artifact_id=f"manifest:{run_id}",
                artifact_type="run_manifest",
                payload=manifest_payload,
            ),
            ArtifactPayload(
                artifact_id=f"attribution:{run_id}",
                artifact_type="attribution_result",
                payload=result_payload,
            ),
            ArtifactPayload(
                artifact_id=f"assurance:{run_id}",
                artifact_type="assurance",
                payload=assurance_payload,
            ),
            ArtifactPayload(
                artifact_id=f"claim:{run_id}:1",
                artifact_type="claim_detail",
                payload=claim_payload,
            ),
        ],
    )
    repo.append_terminal(
        run_id=run_id,
        assurance_payload=AssuranceCompletedPayload(
            valid=True, final_result_status="SUFFICIENT"
        ),
        terminal_event_type=RunEventType.RUN_COMPLETED,
        terminal_payload=RunCompletedPayload(
            result_status="SUFFICIENT",
            final_output_artifact_ref=f"answer:stream:{run_id}",
            total_latency_ms=100,
            runtime_identity_ref="runtime-id:stage1",
        ),
        lifecycle_update=(
            RunLifecycleStatus.RUNNING,
            RunLifecycleStatus.COMPLETED,
        ),
    )
    return {
        "manifest_payload": manifest_payload,
        "result_payload": result_payload,
        "assurance_payload": assurance_payload,
        "claim_payload": claim_payload,
    }


def test_m6_adapter_run_case_seals_hashes_from_db_artifacts(tmp_path):
    from catalyst_eval.v1_1.case import GoldenCase

    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()
    run_id = "run:fixture:1"
    artifacts = _seed_completed_run(db_path, run_id)
    case = GoldenCase.model_validate(make_stage1_cases()[0])

    composition = _FakeComposition(run_id)
    adapter = M6AppSseRunnerAdapter(composition=composition, db_path=db_path)
    outcome = adapter.run_case(case)

    assert outcome.case_id == "v1f-001"
    assert outcome.run_manifest_id == run_id
    assert outcome.run_manifest_hash == _sha(artifacts["manifest_payload"])
    assert outcome.result_artifact_id == f"attribution:{run_id}"
    assert outcome.result_artifact_hash == _sha(artifacts["result_payload"])
    assert outcome.terminal_status == "COMPLETED"
    assert outcome.run_manifest_payload == artifacts["manifest_payload"]
    assert outcome.result_artifact_payload == artifacts["result_payload"]
    assert outcome.assurance_ref["artifact_hash"] == _sha(
        artifacts["assurance_payload"]
    )
    assert outcome.run_facts["claims"][0]["citation_ids"] == ["fixture-ev-001"]
    assert outcome.provider_calls == 2


def test_m6_adapter_fails_closed_when_manifest_artifact_missing(tmp_path):
    from catalyst_eval.v1_1.case import GoldenCase

    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()
    run_id = "run:fixture:missing"
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at,"
            " updated_at, owner, task_token)"
            " VALUES (?, 'RUNNING', 'req', ?, ?, 1, ?, ?, 'eval', 't')",
            (run_id, f"manifest:{run_id}", "b" * 64, _utc_iso(), _utc_iso()),
        )
        conn.commit()
    case = GoldenCase.model_validate(make_stage1_cases()[0])
    adapter = M6AppSseRunnerAdapter(
        composition=_FakeComposition(run_id), db_path=db_path
    )
    try:
        adapter.run_case(case)
    except Stage1AdapterError as exc:
        assert "RunManifest" in str(exc)
    else:
        raise AssertionError("missing RunManifest artifact must fail closed")
