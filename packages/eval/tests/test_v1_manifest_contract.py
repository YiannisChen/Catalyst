"""V1.1 EvalManifest contract tests (M2-10, corrective).

Eval-owned experiment identity (eval TSD §10 / Frozen §7.3). All authoritative
identity and metric contracts are typed groups; DataRuntimeIdentity and
RunManifest are referenced by ID/hash and never reproduced. Outcome is
append-once: identity is frozen before execution, outcome starts absent, one
append completes the manifest, a second append fails, and identity mutation
fails.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_eval.v1_1.manifest import (
    AgentPolicyIdentity,
    CodeProviderIdentity,
    DataRuntimeIdentityReference,
    EligibleExperiment,
    EvalManifest,
    EvalOutcome,
    EvaluationIdentity,
    GateResult,
    LatencyTokensCost,
    MetricAggregate,
    MetricContract,
    MetricDefinition,
    ModelByRole,
    PackageVersion,
    RetrievalPolicyIdentity,
    RunArtifactIdentity,
    RunManifestBinding,
)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _evaluation(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "eval_id": "eval:stage1:v1",
        "schema_version": "v1",
        "dataset_id": "catalyst-stage1",
        "dataset_version": "v1",
        "stage": "stage1",
        "split": "dev",
        "ordered_case_ids": ("stage1-001", "stage1-002"),
        "case_list_sha256": "a" * 64,
    }
    base.update(overrides)
    return base


def _code_identity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "code_git_sha": "17693fa",
        "package_versions": (
            PackageVersion(name="catalyst-eval", version="0.1.0"),
        ),
        "harness_revision": "h:v1",
        "provider_versions": (
            PackageVersion(name="anthropic", version="0.40.0"),
        ),
        "models_by_role": (
            ModelByRole(
                role="analyst",
                model_id="claude-x",
                prompt_template_version="pt:v1",
                prompt_template_sha256="e" * 64,
            ),
        ),
        "random_seed": 42,
    }
    base.update(overrides)
    return base


def _data_identity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "data_runtime_identity_ref": "runtime-id:7a004",
        "data_runtime_identity_hash": "d" * 64,
    }
    base.update(overrides)
    return base


def _run_artifact_identity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_manifest_bindings": (
            RunManifestBinding(
                run_manifest_id="manifest:run:1",
                run_manifest_hash="b" * 64,
            ),
        ),
        "context_pack_refs": (),
        "claim_plan_refs": (),
        "assurance_refs": (),
    }
    base.update(overrides)
    return base


def _retrieval_policy(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "arm_names": ("fts5", "dense", "hybrid", "reranked"),
        "arm_order": ("fts5", "dense", "hybrid", "reranked"),
        "top_k": 8,
        "candidate_pool_id": "pool:1",
        "dedup_policy_version": "dedup:v1",
        "independence_policy_version": "ind:v1",
        "reranker_policy_version": "rr:v1",
        "latency_bound_ms": 5000,
    }
    base.update(overrides)
    return base


def _agent_policy(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "observation_policy_version": "move_profile_v1",
        "context_pack_policy_version": "evidence_context_pack_v1",
        "analyst_policy_version": "bounded_competition_v1",
        "writer_policy_version": "writer_v1",
        "token_budget": 128_000,
        "corrective_rounds": 1,
        "actions_per_batch": 1,
        "a1_policy_version": "a1:v1",
        "a2_policy_version": "a2:v1",
        "a3_policy_version": "a3:v1",
        "a4_policy_version": "a4:v1",
        "a5_policy_version": "a5:v1",
    }
    base.update(overrides)
    return base


def _metric_contract(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "metric_spec_version": "ms:v1",
        "definitions": (
            MetricDefinition(
                metric_id="citation_correctness",
                numerator_rule="supported cited units",
                denominator_rule="resolved cited units",
                eligible_count=12,
                excluded_count=0,
                non_scorable_count=0,
                hard_gate=True,
            ),
        ),
    }
    base.update(overrides)
    return base


def _experiment(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "experiment_id": "A3",
        "eligibility_predicate": "human-labelled-recoverable",
        "ordered_eligible_ids": ("stage1-001", "stage1-002"),
        "eligibility_hash": "c" * 64,
        "minimum_eligible_denominator": 8,
        "observed_denominator": None,
    }
    base.update(overrides)
    return base


def _result_ref(case_id: str, run_id: str = "run:1") -> dict[str, Any]:
    return {
        "case_id": case_id,
        "run_manifest_id": f"manifest:{run_id}:{case_id}",
        "run_manifest_hash": "b" * 64,
        "result_artifact_id": f"result:{run_id}:{case_id}",
    }


def _outcome(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "completed_at": _utc("2026-01-08T09:00:00Z"),
        "observed_run_artifact_identity": {
            "run_manifest_bindings": (
                {
                    "run_manifest_id": "manifest:run:1:stage1-001",
                    "run_manifest_hash": "b" * 64,
                },
                {
                    "run_manifest_id": "manifest:run:1:stage1-002",
                    "run_manifest_hash": "b" * 64,
                },
            ),
            "context_pack_refs": (),
            "claim_plan_refs": (),
            "assurance_refs": (),
        },
        "per_case_result_refs": (
            _result_ref("stage1-001"),
            _result_ref("stage1-002"),
        ),
        "aggregate_metrics": (
            MetricAggregate(
                metric_id="citation_correctness",
                numerator=12,
                denominator=12,
                eligible_count=12,
                excluded_count=0,
                non_scorable_count=0,
                hard_gate_passed=True,
            ),
        ),
        "latency_tokens_cost": LatencyTokensCost(
            total_latency_ms=120_000,
            total_tokens=45_000,
            total_cost=0.42,
        ),
        "gate_results": (
            GateResult(gate_id="citation_correctness", passed=True),
        ),
    }
    base.update(overrides)
    return base


def _manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "evaluation_identity": EvaluationIdentity(**_evaluation()),
        "code_provider_identity": CodeProviderIdentity(**_code_identity()),
        "data_identity": DataRuntimeIdentityReference(**_data_identity()),
        "run_artifact_identity": RunArtifactIdentity(**_run_artifact_identity()),
        "retrieval_policy": RetrievalPolicyIdentity(**_retrieval_policy()),
        "agent_policy": AgentPolicyIdentity(**_agent_policy()),
        "metric_contract": MetricContract(**_metric_contract()),
        "eligible_experiments": (EligibleExperiment(**_experiment()),),
        "outcome": None,
    }
    base.update(overrides)
    return base


def test_eval_manifest_uses_typed_identity_groups() -> None:
    manifest = EvalManifest(**_manifest())
    assert set(EvalManifest.model_fields) == {
        "evaluation_identity",
        "code_provider_identity",
        "data_identity",
        "run_artifact_identity",
        "retrieval_policy",
        "agent_policy",
        "metric_contract",
        "eligible_experiments",
        "outcome",
    }
    assert manifest.evaluation_identity.ordered_case_ids == ("stage1-001", "stage1-002")
    assert manifest.code_provider_identity.random_seed == 42
    assert manifest.code_provider_identity.models_by_role[0].role == "analyst"
    assert manifest.retrieval_policy.arm_names == ("fts5", "dense", "hybrid", "reranked")
    assert manifest.agent_policy.a3_policy_version == "a3:v1"
    assert isinstance(manifest.metric_contract, MetricContract)
    assert manifest.outcome is None


def test_eval_manifest_hashes_are_hex_strings() -> None:
    with pytest.raises(ValidationError):
        EvalManifest(**_manifest(evaluation_identity=EvaluationIdentity(**_evaluation(case_list_sha256="not-hex"))))
    with pytest.raises(ValidationError):
        EvalManifest(**_manifest(data_identity=DataRuntimeIdentityReference(**_data_identity(data_runtime_identity_hash="x" * 63))))


def test_eval_manifest_does_not_reproduce_data_runtime_identity() -> None:
    fields = set(EvalManifest.model_fields)
    for duplicated in (
        "data_snapshot_id",
        "corpus_manifest_id",
        "fts_index_version",
        "dense_index_version",
        "embedding_model_revision",
        "reranker_revision",
        "query_policy_version",
    ):
        assert duplicated not in fields
    with pytest.raises(ValidationError):
        EvalManifest(**_manifest(data_snapshot_id="snapshot:7a004"))


def test_eval_manifest_references_run_manifests_by_id_and_hash() -> None:
    manifest = EvalManifest(**_manifest())
    binding = manifest.run_artifact_identity.run_manifest_bindings[0]
    assert binding.run_manifest_id == "manifest:run:1"
    assert binding.run_manifest_hash == "b" * 64
    assert "run_id" not in RunManifestBinding.model_fields
    assert "analyst_prompt_hash" not in RunManifestBinding.model_fields


def test_eval_manifest_identity_fields_are_immutable() -> None:
    manifest = EvalManifest(**_manifest())
    with pytest.raises(ValidationError):
        manifest.evaluation_identity = EvaluationIdentity(**_evaluation(eval_id="other"))
    with pytest.raises(ValidationError):
        EvalManifest(**_manifest(), unknown_field=True)  # extra forbidden


def test_eligible_experiment_contract() -> None:
    experiment = EligibleExperiment(**_experiment())
    assert experiment.minimum_eligible_denominator == 8
    assert experiment.observed_denominator is None
    with pytest.raises(ValidationError):
        EligibleExperiment(**_experiment(minimum_eligible_denominator=0))
    with pytest.raises(ValidationError):
        EligibleExperiment(**_experiment(observed_denominator=-1))


def test_metric_contract_counts_are_non_negative() -> None:
    with pytest.raises(ValidationError):
        MetricDefinition(
            metric_id="m",
            numerator_rule="n",
            denominator_rule="d",
            eligible_count=-1,
        )
    with pytest.raises(ValidationError):
        MetricAggregate(
            metric_id="m",
            numerator=1,
            denominator=0,
            eligible_count=0,
            excluded_count=0,
            non_scorable_count=0,
        )  # denominator zero rejected


def test_outcome_is_append_once() -> None:
    manifest = EvalManifest(**_manifest())
    assert manifest.outcome is None
    completed = manifest.append_outcome(EvalOutcome(**_outcome()))
    assert completed.outcome is not None
    with pytest.raises(ValueError):
        completed.append_outcome(EvalOutcome(**_outcome()))


def test_outcome_append_cannot_alter_predeclared_eligibility_identity() -> None:
    manifest = EvalManifest(**_manifest())
    completed = manifest.append_outcome(EvalOutcome(**_outcome()))
    # The predeclared eligibility identity is untouched by the outcome append.
    assert completed.eligible_experiments[0].ordered_eligible_ids == (
        "stage1-001",
        "stage1-002",
    )
    assert completed.eligible_experiments[0].minimum_eligible_denominator == 8
    # observed denominators live on the eligible experiment, not the outcome.
    assert completed.eligible_experiments[0].observed_denominator is None
    with pytest.raises(ValidationError):
        completed.evaluation_identity.ordered_case_ids = ()  # frozen identity


def test_eval_manifest_rejects_identity_replacement_and_raw_outcomes() -> None:
    manifest = EvalManifest(**_manifest())
    with pytest.raises((TypeError, ValueError, ValidationError)):
        manifest.model_copy(update={
            "evaluation_identity": EvaluationIdentity(**_evaluation(eval_id="other"))
        })
    with pytest.raises((TypeError, ValueError, ValidationError)):
        manifest.append_outcome(_outcome())  # type: ignore[arg-type]


def test_evaluation_and_retrieval_identity_are_nonempty_unique_and_ordered() -> None:
    with pytest.raises(ValidationError):
        EvaluationIdentity(**_evaluation(ordered_case_ids=()))
    with pytest.raises(ValidationError):
        EvaluationIdentity(**_evaluation(ordered_case_ids=("stage1-001", "stage1-001")))
    with pytest.raises(ValidationError):
        RetrievalPolicyIdentity(**_retrieval_policy(
            arm_names=("fts5", "fts5"), arm_order=("fts5", "fts5"),
        ))
