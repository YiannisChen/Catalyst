"""V1.1 EvalManifest contract tests (M2-10).

Eval-owned experiment identity (eval TSD §10 / Frozen §7.3). It references
DataRuntimeIdentity and RunManifest by typed ID/hash references and never
reproduces their full authoritative fields.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_eval.v1_1.manifest import (
    EligibleExperiment,
    EvalManifest,
    RunManifestBinding,
)


def _binding(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_manifest_id": "manifest:run:1",
        "run_manifest_hash": "b" * 64,
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


def _manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "eval_id": "eval:stage1:v1",
        "schema_version": "v1",
        "dataset_id": "catalyst-stage1",
        "dataset_version": "v1",
        "stage": "stage1",
        "split": "dev",
        "ordered_case_ids": ("stage1-001", "stage1-002"),
        "case_list_sha256": "a" * 64,
        "code_revision": "17693fa",
        "provider": "anthropic",
        "model_id": "claude-x",
        "data_runtime_identity_ref": "runtime-id:7a004",
        "data_runtime_identity_hash": "d" * 64,
        "run_manifest_bindings": (RunManifestBinding(**_binding()),),
        "retrieval_policy_version": "qp:v1",
        "a1_policy_version": "a1:v1",
        "a2_policy_version": "a2:v1",
        "a3_policy_version": "a3:v1",
        "a4_policy_version": "a4:v1",
        "a5_policy_version": "a5:v1",
        "metric_contract": {
            "retrieval": {"numerator": "primary-source-hit", "denominator": "cases-with-primary"},
            "attribution": {"numerator": "citation-correct", "denominator": "material-claims"},
        },
        "eligible_experiments": (EligibleExperiment(**_experiment()),),
        "outcome": None,
    }
    base.update(overrides)
    return base


def test_eval_manifest_fields() -> None:
    manifest = EvalManifest(**_manifest())
    assert set(EvalManifest.model_fields) == {
        "eval_id",
        "schema_version",
        "dataset_id",
        "dataset_version",
        "stage",
        "split",
        "ordered_case_ids",
        "case_list_sha256",
        "code_revision",
        "provider",
        "model_id",
        "data_runtime_identity_ref",
        "data_runtime_identity_hash",
        "run_manifest_bindings",
        "retrieval_policy_version",
        "a1_policy_version",
        "a2_policy_version",
        "a3_policy_version",
        "a4_policy_version",
        "a5_policy_version",
        "metric_contract",
        "eligible_experiments",
        "outcome",
    }
    assert manifest.ordered_case_ids == ("stage1-001", "stage1-002")


def test_eval_manifest_hashes_are_hex_strings() -> None:
    with pytest.raises(ValidationError):
        EvalManifest(**_manifest(case_list_sha256="not-hex"))
    with pytest.raises(ValidationError):
        EvalManifest(**_manifest(data_runtime_identity_hash="x" * 63))


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
    binding = manifest.run_manifest_bindings[0]
    assert binding.run_manifest_id == "manifest:run:1"
    assert binding.run_manifest_hash == "b" * 64
    # No full RunManifest fields are reproduced.
    assert "run_id" not in RunManifestBinding.model_fields
    assert "analyst_prompt_hash" not in RunManifestBinding.model_fields


def test_eval_manifest_identity_fields_are_immutable() -> None:
    manifest = EvalManifest(**_manifest())
    with pytest.raises(ValidationError):
        manifest.eval_id = "other"  # frozen
    with pytest.raises(ValidationError):
        EvalManifest(**_manifest(), unknown_field=True)  # extra forbidden


def test_eligible_experiment_contract() -> None:
    experiment = EligibleExperiment(**_experiment())
    assert experiment.minimum_eligible_denominator == 8
    assert experiment.observed_denominator is None
    experiment_with_outcome = EligibleExperiment(
        **_experiment(observed_denominator=10)
    )
    assert experiment_with_outcome.observed_denominator == 10
    with pytest.raises(ValidationError):
        EligibleExperiment(**_experiment(minimum_eligible_denominator=0))


def test_outcome_is_appended_after_execution() -> None:
    assert EvalManifest(**_manifest()).outcome is None
    with_outcome = EvalManifest(
        **_manifest(outcome={"status": "COMPLETED", "runs": 12})
    )
    assert with_outcome.outcome == {"status": "COMPLETED", "runs": 12}
