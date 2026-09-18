"""M7-3: append-only EvalManifest builder.

Covers the canonical eval_id/eligibility formulas, same-input determinism,
model/data/order sensitivity, empty planned run identity, pre-submit capture
ordering (one-to-one case bindings), and rejection of missing/duplicate/
reordered/extra bindings. Outcome append never changes eval_id.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.loader import canonical_bytes
from catalyst_eval.v1_1.manifest import (
    AgentPolicyIdentity,
    CaseResultRef,
    CodeProviderIdentity,
    DataRuntimeIdentityReference,
    EvalManifest,
    EvalOutcome,
    LatencyTokensCost,
    MetricContract,
    MetricDefinition,
    ModelByRole,
    PackageVersion,
    RetrievalPolicyIdentity,
    RunArtifactIdentity,
    RunManifestBinding,
)
from catalyst_eval.v1_1.manifest_builder import (
    EligibleExperimentInput,
    Stage1DatasetInput,
    build_eval_manifest,
)

from tests.v1_1_fixtures import make_case, make_stage1_cases


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _dataset(*, ids: tuple[str, ...] = ("v1f-001", "v1f-002")) -> Stage1DatasetInput:
    rows = {row["case_id"]: row for row in make_stage1_cases()}
    cases = tuple(GoldenCase.model_validate(rows[case_id]) for case_id in ids)
    return Stage1DatasetInput(
        dataset_id="v1_1_stage1_fixture",
        dataset_version="1.1.0",
        cases=cases,
    )


def _code_identity(**overrides) -> CodeProviderIdentity:
    base = dict(
        code_git_sha="3037ff8",
        package_versions=(PackageVersion(name="catalyst-eval", version="0.1.0"),),
        harness_revision="h:v1",
        provider_versions=(),
        models_by_role=(
            ModelByRole(
                role="analyst",
                model_id="deepseek-v4-flash",
                prompt_template_version="pt:v1",
                prompt_template_sha256="e" * 64,
            ),
        ),
        random_seed=7,
    )
    base.update(overrides)
    return CodeProviderIdentity(**base)


def _data_identity(**overrides) -> DataRuntimeIdentityReference:
    base = dict(
        data_runtime_identity_ref="runtime-id:7a004",
        data_runtime_identity_hash="d" * 64,
    )
    base.update(overrides)
    return DataRuntimeIdentityReference(**base)


def _retrieval_policy(**overrides) -> RetrievalPolicyIdentity:
    base = dict(
        arm_names=("fts5", "dense", "hybrid", "reranked"),
        arm_order=("fts5", "dense", "hybrid", "reranked"),
        top_k=8,
        candidate_pool_id="pool:stage1",
        dedup_policy_version="dedup:v1",
        independence_policy_version="ind:v1",
        reranker_policy_version="rr:v1",
        latency_bound_ms=5000,
    )
    base.update(overrides)
    return RetrievalPolicyIdentity(**base)


def _agent_policy(**overrides) -> AgentPolicyIdentity:
    return AgentPolicyIdentity(
        observation_policy_version="move_profile_v1",
        context_pack_policy_version="evidence_context_pack_v1",
        analyst_policy_version="bounded_competition_v1",
        writer_policy_version="writer_v1",
        token_budget=128_000,
        corrective_rounds=1,
        actions_per_batch=1,
        a1_policy_version="a1:v1",
        a2_policy_version="a2:v1",
        a3_policy_version="a3:v1",
        a4_policy_version="a4:v1",
        a5_policy_version="a5:v1",
        **overrides,
    )


def _metric_spec(**overrides) -> MetricContract:
    return MetricContract(
        metric_spec_version="ms:v1",
        definitions=(
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
        **overrides,
    )


def _experiment(**overrides) -> EligibleExperimentInput:
    base = dict(
        experiment_id="A3",
        eligibility_predicate="human-labelled-recoverable",
        ordered_eligible_ids=("v1f-008",),
        minimum_eligible_denominator=2,
    )
    base.update(overrides)
    return EligibleExperimentInput(**base)


def _build(**overrides):
    return build_eval_manifest(
        dataset=overrides.pop("dataset", _dataset()),
        stage=overrides.pop("stage", "stage1"),
        split=overrides.pop("split", "dev"),
        code_identity=overrides.pop("code_identity", _code_identity()),
        data_runtime_identity=overrides.pop("data_runtime_identity", _data_identity()),
        agent_policy=overrides.pop("agent_policy", _agent_policy()),
        retrieval_policy=overrides.pop("retrieval_policy", _retrieval_policy()),
        metric_spec=overrides.pop("metric_spec", _metric_spec()),
        eligible_experiments=overrides.pop("eligible_experiments", (_experiment(),)),
        **overrides,
    )


def _outcome(manifest: EvalManifest, **overrides) -> EvalOutcome:
    case_ids = list(manifest.evaluation_identity.ordered_case_ids)
    base: dict = dict(
        completed_at=_utc("2026-08-19T00:00:00Z"),
        observed_run_artifact_identity=RunArtifactIdentity(
            run_manifest_bindings=tuple(
                RunManifestBinding(
                    run_manifest_id=f"manifest:{case_id}",
                    run_manifest_hash="b" * 64,
                )
                for case_id in case_ids
            ),
        ),
        per_case_result_refs=tuple(
            CaseResultRef(
                case_id=case_id,
                run_manifest_id=f"manifest:{case_id}",
                run_manifest_hash="b" * 64,
                result_artifact_id=f"result:{case_id}",
            )
            for case_id in case_ids
        ),
        aggregate_metrics=(),
        latency_tokens_cost=LatencyTokensCost(
            total_latency_ms=100,
            total_tokens=100,
            total_cost=0.01,
        ),
        gate_results=(),
    )
    base.update(overrides)
    return EvalOutcome(**base)


def test_builder_writes_locked_identity_and_empty_planned_run_identity():
    manifest = _build()
    identity = manifest.evaluation_identity
    assert identity.dataset_id == "v1_1_stage1_fixture"
    assert identity.ordered_case_ids == ("v1f-001", "v1f-002")
    assert identity.stage == "stage1"
    assert identity.split == "dev"
    assert manifest.outcome is None
    assert manifest.run_artifact_identity == RunArtifactIdentity()
    assert identity.eval_id


def test_builder_computes_eligibility_hash_and_keeps_observed_denominator_absent():
    manifest = _build()
    experiment = manifest.eligible_experiments[0]
    assert experiment.experiment_id == "A3"
    assert experiment.observed_denominator is None
    expected = __import__(
        "hashlib"
    ).sha256(
        canonical_bytes(
            {
                "experiment_id": "A3",
                "eligibility_predicate_version": "human-labelled-recoverable",
                "ordered_eligible_ids": ["v1f-008"],
                "minimum_eligible_denominator": 2,
            }
        )
    ).hexdigest()
    assert experiment.eligibility_hash == expected


def test_builder_same_input_is_deterministic():
    assert _build().evaluation_identity.eval_id == _build().evaluation_identity.eval_id


def test_builder_eval_id_sensitive_to_model_policy():
    a = _build(code_identity=_code_identity(random_seed=7))
    b = _build(code_identity=_code_identity(random_seed=8))
    assert a.evaluation_identity.eval_id != b.evaluation_identity.eval_id


def test_builder_eval_id_sensitive_to_data_identity():
    a = _build(data_runtime_identity=_data_identity())
    b = _build(data_runtime_identity=_data_identity(data_runtime_identity_ref="runtime-id:other"))
    assert a.evaluation_identity.eval_id != b.evaluation_identity.eval_id


def test_builder_eval_id_sensitive_to_case_order():
    a = _build(dataset=_dataset(ids=("v1f-001", "v1f-002")))
    b = _build(dataset=_dataset(ids=("v1f-002", "v1f-001")))
    assert a.evaluation_identity.eval_id != b.evaluation_identity.eval_id


def test_builder_eval_id_sensitive_to_retrieval_policy_and_experiments():
    a = _build(retrieval_policy=_retrieval_policy(top_k=8))
    b = _build(retrieval_policy=_retrieval_policy(top_k=16))
    assert a.evaluation_identity.eval_id != b.evaluation_identity.eval_id
    c = _build(eligible_experiments=(_experiment(minimum_eligible_denominator=3),))
    assert a.evaluation_identity.eval_id != c.evaluation_identity.eval_id


def test_append_outcome_does_not_change_eval_id():
    manifest = _build()
    eval_id = manifest.evaluation_identity.eval_id
    completed = manifest.append_outcome(_outcome(manifest))
    assert completed.evaluation_identity.eval_id == eval_id
    assert completed.outcome is not None
    assert completed.evaluation_identity.ordered_case_ids == ("v1f-001", "v1f-002")


def test_append_outcome_requires_one_to_one_ordered_bindings():
    manifest = _build()
    # Missing binding.
    with pytest.raises(ValueError, match="one-to-one"):
        manifest.append_outcome(
            _outcome(manifest, per_case_result_refs=(_outcome(manifest).per_case_result_refs[0],))
        )
    # Reordered binding fails closed at outcome validation (Batch-B): the
    # per-case refs must bind one-to-one to the observed RunManifest id+hash.
    refs = list(_outcome(manifest).per_case_result_refs)
    with pytest.raises(ValueError, match="does not match ref"):
        _outcome(manifest, per_case_result_refs=(refs[1], refs[0]))
    # Extra binding.
    extra = CaseResultRef(
        case_id="v1f-003",
        run_manifest_id="manifest:v1f-003",
        run_manifest_hash="b" * 64,
        result_artifact_id="result:v1f-003",
    )
    with pytest.raises(ValueError, match="one-to-one"):
        manifest.append_outcome(
            _outcome(manifest, per_case_result_refs=(refs[0], refs[1], extra))
        )
    # Duplicate binding.
    with pytest.raises(ValueError, match="unique"):
        manifest.append_outcome(
            _outcome(manifest, per_case_result_refs=(refs[0], refs[0]))
        )


def test_outcome_refs_require_run_manifest_hash():
    with pytest.raises(ValidationError):
        CaseResultRef(
            case_id="v1f-001",
            run_manifest_id="manifest:v1f-001",
            result_artifact_id="result:v1f-001",
        )


def test_builder_rejects_empty_dataset():
    with pytest.raises(ValueError, match="at least one case"):
        _build(dataset=Stage1DatasetInput(
            dataset_id="x", dataset_version="1.0.0", cases=()
        ))
