"""Phase A corrective: the M6 app/SSE adapter reads only persisted facts.

The adapter must seal one case outcome from the authoritative persisted M6
contract: the persisted terminal event, the RunManifest binding captured
pre-submit, and the versioned ``run_diagnostics`` artifact published in the
terminal transaction. It has no eval-side external authority callback: a fact
the runtime did not persist makes the corresponding metric non-scorable or
fails closed, and is never defaulted or substituted.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from catalyst_app.events import (
    AnswerCompletedPayload,
    AssuranceCompletedPayload,
    EvidenceAssessedPayload,
    RunAcceptedPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
    StageStartedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import (
    ArtifactPayload,
    EventRepository,
    payload_sha256,
)
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.m6_adapter import (
    DIAGNOSTICS_EVENT_ARTIFACT_TYPE,
    M6AppSseRunnerAdapter,
    Stage1AdapterError,
)

from tests.v1_1_fixtures import make_stage1_cases

RUN_ID = "run:fixture:1"
PREPARED_REF = "v1:corpus:prepared"
PREPARED_HASH = "a" * 64


def _utc_iso() -> str:
    return "2026-08-19T00:00:00Z"


class _FakeComposition:
    """Minimal admission seam that honours the production pre-submit hook."""

    def __init__(self, run_id: str, *, call_pre_submit: bool = True):
        self.admission = self._Admission(run_id, call_pre_submit=call_pre_submit)

    class _Admission:
        def __init__(self, run_id: str, *, call_pre_submit: bool):
            self.run_id = run_id
            self._call_pre_submit = call_pre_submit

        def admit(self, request, *, pre_submit=None):
            from catalyst_app.runtime.admission import AdmissionOutcome

            if pre_submit is not None and self._call_pre_submit:
                pre_submit(self.run_id)
            return AdmissionOutcome(kind="accepted", run_id=self.run_id)


def diagnostics_payload(
    *,
    run_id: str = RUN_ID,
    case_id: str = "v1f-001",
    identity_hash: str = PREPARED_HASH,
    identity_ref: str = PREPARED_REF,
    result_status: str = "SUFFICIENT",
    attribution_type: str = "EVIDENCE_BACKED_CAUSAL",
    refusal_reason: str | None = None,
    refusal_reason_available: bool = False,
    ranked: tuple[str, ...] = ("ev-001", "ev-002"),
    candidates: tuple[str, ...] = ("ev-001", "ev-002", "ev-003"),
    ticker_violations: tuple[str, ...] = (),
    cutoff_violations: tuple[str, ...] = (),
    degradation_reasons: tuple[str, ...] = (),
    arms: tuple[str, ...] = ("lexical", "dense", "fusion", "reranked"),
    observed: bool = True,
    rounds: tuple[dict, ...] = (),
    analyst_attempts: int = 1,
    writer_attempts: int = 1,
    cost_usd: float | None = 0.01,
    cost_method: str = "reported",
    tokens_in: int | None = 120,
    tokens_out: int | None = 80,
) -> dict:
    return {
        "schema_version": "v1.1_run_diagnostics_v1",
        "run_id": run_id,
        "data_runtime_identity_ref": identity_ref,
        "data_runtime_identity_hash": identity_hash,
        "recorded_at": _utc_iso(),
        "retrieval": {
            "corpus_manifest_id": "c" * 64,
            "index_manifest_id": "e" * 64,
            "data_runtime_identity_hash": identity_hash,
            "arms": [
                {"arm": name, "version": "1.0.0", "top_k": 8} for name in arms
            ],
            "ordered_candidate_evidence_ids": list(candidates),
            "ordered_final_ranked_evidence_ids": list(ranked),
            "per_task": [],
            "measured_latency_ms": 42,
            "degradation_reasons": list(degradation_reasons),
            "ticker_violations": list(ticker_violations),
            "cutoff_violations": list(cutoff_violations),
            "observed": observed,
        },
        "trajectory": {
            "corrective_rounds_executed": len(rounds),
            "initial_task_ids": ["task:company_primary"],
            "initial_task_fingerprints": ["fp:company_primary"],
            "rounds": list(rounds),
        },
        "provider": {
            "analyst_logical_calls": analyst_attempts,
            "analyst_provider_attempts": analyst_attempts,
            "writer_logical_calls": writer_attempts,
            "writer_provider_attempts": writer_attempts,
            "total_tokens_in": tokens_in,
            "total_tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "cost_method": cost_method,
        },
        "terminal": {
            "result_status": result_status,
            "attribution_type": attribution_type,
            "terminal_event_type": "run.completed",
            "status_ceiling": result_status,
            "refusal_reason": refusal_reason,
            "refusal_reason_available": refusal_reason_available,
        },
    }


def _seed_run(
    db_path: Path,
    *,
    run_id: str,
    case_id: str,
    diagnostics: dict | None = None,
    diagnostics_run_id: str | None = None,
    with_terminal_event: bool = True,
    with_context_pack: bool = True,
    lifecycle: str = "COMPLETED",
    result_status: str = "SUFFICIENT",
) -> dict:
    manifest_payload = {
        "run_id": run_id,
        "case_id": case_id,
        "data_runtime_identity_ref": PREPARED_REF,
        "data_runtime_identity_hash": PREPARED_HASH,
    }
    manifest_hash = payload_sha256(manifest_payload)
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at,"
            " updated_at, owner, task_token)"
            " VALUES (?, 'RUNNING', 'req', ?, ?, 1, ?, ?, 'eval', 't')",
            (run_id, f"manifest:{run_id}", manifest_hash, _utc_iso(), _utc_iso()),
        )
        conn.commit()
    repo = EventRepository(db_path=db_path)
    repo.append(
        run_id=run_id,
        event_type=RunEventType.RUN_ACCEPTED,
        stage="ADMISSION",
        payload=RunAcceptedPayload(
            request_identity_digest="r" * 64,
            ticker="AAPL",
            trade_date="2026-01-15",
            workflow_version="v1.1",
            model_provider_label="deepseek/deepseek-chat",
            stream_url=f"/api/live-runs/{run_id}/stream",
        ),
        artifact_payloads=[
            ArtifactPayload(
                artifact_id=f"manifest:{run_id}",
                artifact_type="run_manifest",
                payload=manifest_payload,
            )
        ],
    )
    if with_context_pack:
        repo.append(
            run_id=run_id,
            event_type=RunEventType.STAGE_STARTED,
            stage="CONTEXT_PACK_BUILD",
            payload=StageStartedPayload(stage="context_pack_build", round=1),
            artifact_payloads=[
                ArtifactPayload(
                    artifact_id=f"pack:{run_id}:1",
                    artifact_type="context_pack",
                    payload={"round": 1, "run_id": run_id},
                )
            ],
        )
    repo.append(
        run_id=run_id,
        event_type=RunEventType.EVIDENCE_ASSESSED,
        stage="EVIDENCE_ANALYST",
        payload=EvidenceAssessedPayload(
            accepted_evidence_ids=("ev-001",),
            decision="PROCEED",
            status_ceiling="SUFFICIENT",
        ),
    )
    repo.append(
        run_id=run_id,
        event_type=RunEventType.STAGE_STARTED,
        stage="CLAIM_VALIDATION",
        payload=StageStartedPayload(stage="claim_validation", round=1),
        artifact_payloads=[
            ArtifactPayload(
                artifact_id=f"claim:{run_id}:1",
                artifact_type="claim_detail",
                payload={
                    "claim_id": "claim:1",
                    "role": "PRIMARY",
                    "statement": "fixture",
                    "citation_evidence_ids": ["ev-001"],
                    "validation_status": "validated",
                },
            )
        ],
    )
    repo.append(
        run_id=run_id,
        event_type=RunEventType.ANSWER_COMPLETED,
        stage="STREAMING_ANSWER",
        payload=AnswerCompletedPayload(
            writer_stream_id="stream:1",
            final_text_hash="f" * 64,
            provisional=True,
        ),
        artifact_payloads=[
            ArtifactPayload(
                artifact_id=f"answer:{run_id}",
                artifact_type="answer",
                payload={"answer_id": f"answer:{run_id}", "text": "fixture"},
            )
        ],
    )
    if with_terminal_event:
        terminal_artifacts = [
            ArtifactPayload(
                artifact_id=f"attribution:{run_id}",
                artifact_type="attribution_result",
                payload={
                    "attribution_status": result_status,
                    "attribution_type": "EVIDENCE_BACKED_CAUSAL",
                },
            ),
            ArtifactPayload(
                artifact_id=f"assurance:{run_id}",
                artifact_type="assurance",
                payload={"valid": True, "final_result_status": "SUFFICIENT"},
            ),
        ]
        if diagnostics is not None:
            terminal_artifacts.append(
                ArtifactPayload(
                    artifact_id=f"diagnostics:{run_id}",
                    artifact_type=DIAGNOSTICS_EVENT_ARTIFACT_TYPE,
                    payload=diagnostics,
                )
            )
        repo.append_terminal(
            run_id=run_id,
            assurance_payload=AssuranceCompletedPayload(
                valid=True, final_result_status=result_status
            ),
            terminal_event_type=RunEventType.RUN_COMPLETED,
            terminal_payload=RunCompletedPayload(
                result_status=result_status,
                final_output_artifact_ref=f"answer:{run_id}",
                total_latency_ms=100,
                total_tokens=200,
                cost=0.01,
                runtime_identity_ref=PREPARED_REF,
            ),
            lifecycle_update=(
                RunLifecycleStatus.RUNNING,
                RunLifecycleStatus.COMPLETED,
            ),
            artifact_payloads=terminal_artifacts,
        )
    with open_rw(db_path) as conn:
        conn.execute(
            "UPDATE runs SET lifecycle_status = ? WHERE run_id = ?",
            (lifecycle, run_id),
        )
        conn.commit()
    return {"manifest_payload": manifest_payload}


def _init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.sqlite3"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()
    return db_path


def _case() -> GoldenCase:
    return GoldenCase.model_validate(make_stage1_cases()[0])


def _adapter(db_path: Path, case: GoldenCase, **kwargs) -> M6AppSseRunnerAdapter:
    return M6AppSseRunnerAdapter(
        composition=_FakeComposition(RUN_ID),
        db_path=db_path,
        prepared_identity_ref=PREPARED_REF,
        prepared_identity_hash=PREPARED_HASH,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# success path: authoritative persisted facts only
# ---------------------------------------------------------------------------

def test_m6_adapter_seals_outcome_from_persisted_diagnostics(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(
        db_path,
        run_id=RUN_ID,
        case_id=case.case_id,
        diagnostics=diagnostics_payload(case_id=case.case_id),
    )

    outcome = _adapter(db_path, case).run_case(case)

    assert outcome.case_id == case.case_id
    assert outcome.terminal_status == "COMPLETED"
    assert outcome.provider_calls == 2  # 1 analyst + 1 writer attempt
    assert outcome.cost_usd == pytest.approx(0.01)
    assert outcome.tokens == 200

    facts = outcome.run_facts
    assert facts["output_status"] == "SUFFICIENT"
    assert facts["refusal_reason"] is None
    assert facts["refusal_reason_available"] is False
    assert facts["claims"][0]["citation_ids"] == ["ev-001"]
    assert facts["claims"][0]["material"] is True
    # Sanity tasks are the runtime's own dispatched initial tasks.
    assert facts["sanity_tasks_completed"] == ["task:company_primary"]
    # Ranking, violations, latency and degraded all come from diagnostics.
    assert facts["retrieval"]["ranked_evidence_ids"] == ["ev-001", "ev-002"]
    assert facts["retrieval"]["candidate_evidence_ids"] == [
        "ev-001", "ev-002", "ev-003",
    ]
    assert facts["retrieval"]["reranker_contributed"] is True
    assert facts["retrieval"]["latency_ms"] == 42
    assert facts["retrieval"]["observed"] is True
    # No observed corrective round and no runtime stop judgement.
    assert facts["trajectory"]["corrective_triggered"] is False
    assert "stop_correct" not in facts["trajectory"]
    assert "corrected" not in facts["trajectory"]


def test_m6_adapter_derives_the_observed_pool_identity(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(
        db_path,
        run_id=RUN_ID,
        case_id=case.case_id,
        diagnostics=diagnostics_payload(case_id=case.case_id),
    )
    outcome = _adapter(db_path, case).run_case(case)

    from catalyst_eval.benchmark.pool_manifest import PoolManifest

    pool = PoolManifest.model_validate(outcome.run_facts["retrieval"]["pool"])
    assert pool.case_id == case.case_id
    assert pool.chunk_inventory == ("ev-001", "ev-002", "ev-003")
    assert [arm.arm for arm in pool.arms] == ["lexical", "dense", "hybrid", "reranked"]
    assert pool.corpus_manifest_id == "c" * 64
    assert pool.index_manifest_id == "e" * 64
    # The pool identity is bound to the persisted diagnostics artifact hash.
    assert pool.source_artifact_id == outcome.run_facts["diagnostics_artifact_hash"]


def test_m6_adapter_binds_the_claim_plan_to_the_persisted_claim_set(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(
        db_path,
        run_id=RUN_ID,
        case_id=case.case_id,
        diagnostics=diagnostics_payload(case_id=case.case_id),
    )
    outcome = _adapter(db_path, case).run_case(case)
    assert outcome.claim_plan_ref["artifact_id"] == f"claimplan:{RUN_ID}"
    assert outcome.claim_plan_ref["artifact_hash"] not in {
        outcome.run_manifest_hash,
        outcome.result_artifact_hash,
    }


def test_m6_adapter_records_observed_corrective_round_facts(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(
        db_path,
        run_id=RUN_ID,
        case_id=case.case_id,
        diagnostics=diagnostics_payload(
            case_id=case.case_id,
            rounds=(
                {
                    "round": 2,
                    "gap_ids": [f"gap:{RUN_ID}:1:abcd1234"],
                    "gap_reason_codes": ["MISSING_SECTOR_CONTEXT"],
                    "action_ids": ["action:1"],
                    "evidence_needs": ["SECTOR_NEWS"],
                    "time_scopes": ["LATEST_SESSION"],
                    "research_fingerprints": ["fp:1"],
                    "evidence_delta": {
                        "round": 2,
                        "added_evidence_ids": ["ev-009"],
                        "removed_evidence_ids": [],
                    },
                },
            ),
        ),
    )
    facts = _adapter(db_path, case).run_case(case).run_facts
    assert facts["trajectory"]["corrective_triggered"] is True
    assert facts["trajectory"]["gap_reason_codes"] == ["MISSING_SECTOR_CONTEXT"]
    assert facts["trajectory"]["corrective_actions"] == ["action:1"]
    assert facts["trajectory"]["evidence_delta_ids"] == ["ev-009"]


# ---------------------------------------------------------------------------
# fail-closed paths
# ---------------------------------------------------------------------------

def test_m6_adapter_rejects_missing_diagnostics_artifact(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(db_path, run_id=RUN_ID, case_id=case.case_id, diagnostics=None)
    with pytest.raises(Stage1AdapterError, match="run_diagnostics"):
        _adapter(db_path, case).run_case(case)


def test_m6_adapter_rejects_unknown_diagnostics_schema(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    payload = diagnostics_payload(case_id=case.case_id)
    payload["schema_version"] = "v1.1_run_diagnostics_v0"
    _seed_run(db_path, run_id=RUN_ID, case_id=case.case_id, diagnostics=payload)
    with pytest.raises(Stage1AdapterError, match="schema"):
        _adapter(db_path, case).run_case(case)


def test_m6_adapter_rejects_coercible_persisted_diagnostics_scalars(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    payload = diagnostics_payload(case_id=case.case_id)
    payload["retrieval"]["observed"] = "false"
    _seed_run(db_path, run_id=RUN_ID, case_id=case.case_id, diagnostics=payload)
    with pytest.raises(Stage1AdapterError, match="run_diagnostics"):
        _adapter(db_path, case).run_case(case)


def test_m6_adapter_rejects_incomplete_persisted_diagnostics_blocks(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    payload = diagnostics_payload(case_id=case.case_id)
    payload["retrieval"].pop("ordered_candidate_evidence_ids")
    _seed_run(db_path, run_id=RUN_ID, case_id=case.case_id, diagnostics=payload)
    with pytest.raises(Stage1AdapterError, match="run_diagnostics"):
        _adapter(db_path, case).run_case(case)


def test_m6_adapter_rejects_diagnostics_bound_to_another_run(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    payload = diagnostics_payload(case_id=case.case_id, run_id="run:someone:else")
    _seed_run(db_path, run_id=RUN_ID, case_id=case.case_id, diagnostics=payload)
    with pytest.raises(Stage1AdapterError, match="bound to a different run"):
        _adapter(db_path, case).run_case(case)


def test_m6_adapter_rejects_diagnostics_identity_mismatch(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    payload = diagnostics_payload(case_id=case.case_id, identity_hash="b" * 64)
    _seed_run(db_path, run_id=RUN_ID, case_id=case.case_id, diagnostics=payload)
    with pytest.raises(Stage1AdapterError, match="identity hash"):
        _adapter(db_path, case).run_case(case)


def test_m6_adapter_rejects_diagnostics_status_disagreement(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    payload = diagnostics_payload(case_id=case.case_id, result_status="ABSTAIN")
    _seed_run(db_path, run_id=RUN_ID, case_id=case.case_id, diagnostics=payload)
    with pytest.raises(Stage1AdapterError, match="disagrees"):
        _adapter(db_path, case).run_case(case)


def test_m6_adapter_rejects_missing_terminal_event(tmp_path):
    """A lifecycle row alone is not a terminal event contract."""
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(
        db_path,
        run_id=RUN_ID,
        case_id=case.case_id,
        diagnostics=None,
        with_terminal_event=False,
    )
    adapter = _adapter(db_path, case, case_timeout_seconds=0.05)
    with pytest.raises(Stage1AdapterError, match="terminal"):
        adapter.run_case(case)


def test_m6_adapter_rejects_missing_context_pack_without_substitution(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(
        db_path,
        run_id=RUN_ID,
        case_id=case.case_id,
        diagnostics=diagnostics_payload(case_id=case.case_id),
        with_context_pack=False,
    )
    with pytest.raises(Stage1AdapterError, match="context_pack"):
        _adapter(db_path, case).run_case(case)


def test_m6_adapter_fails_closed_when_manifest_artifact_missing(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    run_id = RUN_ID
    manifest_payload = {"run_id": run_id, "case_id": case.case_id}
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at,"
            " updated_at, owner, task_token)"
            " VALUES (?, 'RUNNING', 'req', ?, ?, 1, ?, ?, 'eval', 't')",
            (run_id, f"manifest:{run_id}", payload_sha256(manifest_payload),
             _utc_iso(), _utc_iso()),
        )
        conn.commit()
    adapter = _adapter(db_path, case, case_timeout_seconds=0.05)
    with pytest.raises(Stage1AdapterError):
        adapter.run_case(case)


def test_m6_adapter_binds_remaining_budget_before_dispatch(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(
        db_path,
        run_id=RUN_ID,
        case_id=case.case_id,
        diagnostics=diagnostics_payload(case_id=case.case_id),
    )
    adapter = _adapter(db_path, case)
    adapter.set_remaining_budget(provider_calls=0, cost_usd=1.0)
    with pytest.raises(Stage1AdapterError, match="remaining budget"):
        adapter.run_case(case)


def test_m6_adapter_rejects_unknown_remaining_cost_before_dispatch(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(
        db_path,
        run_id=RUN_ID,
        case_id=case.case_id,
        diagnostics=diagnostics_payload(case_id=case.case_id),
    )
    adapter = _adapter(db_path, case)
    adapter.set_remaining_budget(provider_calls=1, cost_usd=None)
    with pytest.raises(Stage1AdapterError, match="cost.*unknown|unknown.*cost"):
        adapter.run_case(case)


def test_m6_adapter_records_refusal_availability_when_present(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(
        db_path,
        run_id=RUN_ID,
        case_id=case.case_id,
        result_status="ABSTAIN",
        diagnostics=diagnostics_payload(
            case_id=case.case_id,
            result_status="ABSTAIN",
            refusal_reason="typed_status_reason",
            refusal_reason_available=True,
        ),
    )
    facts = _adapter(db_path, case).run_case(case).run_facts
    assert facts["refusal_reason"] == "typed_status_reason"
    assert facts["refusal_reason_available"] is True


def test_observed_pool_manifest_rejects_missing_recorded_at():
    retrieval = {
        "arms": [{"arm": "lexical", "top_k": 8}],
        "ordered_candidate_evidence_ids": [],
        "corpus_manifest_id": "c" * 64,
        "index_manifest_id": "e" * 64,
    }
    with pytest.raises(Stage1AdapterError, match="created_at|recorded_at|timestamp"):
        M6AppSseRunnerAdapter._observed_pool_manifest(
            case_id="v1f-001",
            retrieval=retrieval,
            diagnostics_artifact_hash="d" * 64,
            recorded_at=None,
        )


def test_pre_submit_rejects_manifest_object_hash_before_dispatch(tmp_path):
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_run(db_path, run_id=RUN_ID, case_id=case.case_id)
    adapter = M6AppSseRunnerAdapter(
        composition=_FakeComposition(RUN_ID),
        db_path=db_path,
        prepared_identity_ref=PREPARED_REF,
        prepared_identity_hash="b" * 64,
    )
    with pytest.raises(Stage1AdapterError, match="object hash|prepared identity"):
        adapter.run_case(case)


# ---------------------------------------------------------------------------
# supervisor blocker B: FAILED-case accounting durability
# ---------------------------------------------------------------------------

RUN_PROVIDER_ACCOUNTING_EVENT_ARTIFACT_TYPE = "run_provider_accounting"


def provider_accounting_payload(
    *,
    run_id: str = RUN_ID,
    analyst_attempts: int = 1,
    writer_attempts: int = 0,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = 0.01,
    cost_method: str = "upper_bound_charged",
) -> dict:
    return {
        "schema_version": "v1.1_run_provider_accounting_v1",
        "run_id": run_id,
        "provider": {
            "analyst_logical_calls": analyst_attempts,
            "analyst_provider_attempts": analyst_attempts,
            "writer_logical_calls": writer_attempts,
            "writer_provider_attempts": writer_attempts,
            "total_tokens_in": tokens_in,
            "total_tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "cost_method": cost_method,
        },
    }


def _seed_failed_run(
    db_path: Path,
    *,
    run_id: str,
    case_id: str,
    provider_accounting: dict | None = None,
    failure_code: str = "PROVIDER_BUDGET_EXCEEDED",
) -> dict:
    manifest_payload = {
        "run_id": run_id,
        "case_id": case_id,
        "data_runtime_identity_ref": PREPARED_REF,
        "data_runtime_identity_hash": PREPARED_HASH,
    }
    manifest_hash = payload_sha256(manifest_payload)
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at,"
            " updated_at, owner, task_token)"
            " VALUES (?, 'RUNNING', 'req', ?, ?, 1, ?, ?, 'eval', 't')",
            (run_id, f"manifest:{run_id}", manifest_hash, _utc_iso(), _utc_iso()),
        )
        conn.commit()
    repo = EventRepository(db_path=db_path)
    repo.append(
        run_id=run_id,
        event_type=RunEventType.RUN_ACCEPTED,
        stage="ADMISSION",
        payload=RunAcceptedPayload(
            request_identity_digest="r" * 64,
            ticker="AAPL",
            trade_date="2026-01-15",
            workflow_version="v1.1",
            model_provider_label="deepseek/deepseek-chat",
            stream_url=f"/api/live-runs/{run_id}/stream",
        ),
        artifact_payloads=[
            ArtifactPayload(
                artifact_id=f"manifest:{run_id}",
                artifact_type="run_manifest",
                payload=manifest_payload,
            )
        ],
    )
    terminal_artifacts = []
    if provider_accounting is not None:
        terminal_artifacts.append(
            ArtifactPayload(
                artifact_id=f"provider-accounting:{run_id}",
                artifact_type=RUN_PROVIDER_ACCOUNTING_EVENT_ARTIFACT_TYPE,
                payload=provider_accounting,
            )
        )
    repo.append_terminal(
        run_id=run_id,
        assurance_payload=AssuranceCompletedPayload(
            valid=False, violations=("executor_failure",)
        ),
        terminal_event_type=RunEventType.RUN_FAILED,
        terminal_payload=RunFailedPayload(
            failure_code=failure_code,
            stage="EXECUTION",
            retryable=True,
            safe_message="provider budget exhausted mid-case",
        ),
        lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.FAILED),
        artifact_payloads=terminal_artifacts,
    )
    with open_rw(db_path) as conn:
        conn.execute(
            "UPDATE runs SET lifecycle_status = ? WHERE run_id = ?",
            ("FAILED", run_id),
        )
        conn.commit()
    return {"manifest_payload": manifest_payload}


def test_m6_adapter_records_real_failed_case_accounting(tmp_path):
    """A FAILED case keeps the calls/cost the run actually consumed."""
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_failed_run(
        db_path,
        run_id=RUN_ID,
        case_id=case.case_id,
        provider_accounting=provider_accounting_payload(
            run_id=RUN_ID, analyst_attempts=1, cost_usd=0.02
        ),
    )
    outcome = _adapter(db_path, case).run_case(case)
    assert outcome.terminal_status == "FAILED"
    assert outcome.provider_calls == 1
    assert outcome.cost_usd == pytest.approx(0.02)
    assert outcome.run_facts["provider_accounting"]["provider_calls"] == 1
    assert outcome.run_facts["provider_accounting"]["cost_usd"] == pytest.approx(0.02)


def test_m6_adapter_fails_closed_when_a_failed_case_has_no_accounting(tmp_path):
    """A FAILED case with no persisted accounting is not reported as zero."""
    db_path = _init_db(tmp_path)
    case = _case()
    _seed_failed_run(db_path, run_id=RUN_ID, case_id=case.case_id)
    with pytest.raises(Stage1AdapterError, match="accounting"):
        _adapter(db_path, case).run_case(case)
