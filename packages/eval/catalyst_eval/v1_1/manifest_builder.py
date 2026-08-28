"""V1.1 EvalManifest builder — append-only pre-execution identity (M7-3).

Builds the locked pre-execution EvalManifest: one canonical ``eval_id`` over
the complete pre-execution identity payload, an empty planned
``run_artifact_identity``, eligibility hashes locked before outcomes exist,
and ``observed_denominator`` left absent. Data runtime identity is referenced
by ref/hash only; the M2 schema is reused (no second manifest schema).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.loader import canonical_bytes, case_list_sha256, dataset_content_sha256
from catalyst_eval.v1_1.manifest import (
    AgentPolicyIdentity,
    CodeProviderIdentity,
    DataRuntimeIdentityReference,
    EligibleExperiment,
    EvalManifest,
    EvaluationIdentity,
    MetricContract,
    RetrievalPolicyIdentity,
    RunArtifactIdentity,
)

EVAL_MANIFEST_SCHEMA_VERSION = "v1"


@dataclass(frozen=True)
class Stage1DatasetInput:
    """Dataset identity + ordered GoldenCase rows (never the data copy)."""

    dataset_id: str
    dataset_version: str
    cases: tuple[GoldenCase, ...]


@dataclass(frozen=True)
class EligibleExperimentInput:
    """Predeclared eligible subset; the eligibility hash is derived here."""

    experiment_id: str
    eligibility_predicate: str
    ordered_eligible_ids: tuple[str, ...]
    minimum_eligible_denominator: int = 1


def _eligibility_hash(experiment: EligibleExperimentInput) -> str:
    payload = {
        "experiment_id": experiment.experiment_id,
        "eligibility_predicate_version": experiment.eligibility_predicate,
        "ordered_eligible_ids": list(experiment.ordered_eligible_ids),
        "minimum_eligible_denominator": experiment.minimum_eligible_denominator,
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _compute_eval_id(
    *,
    identity_without_id: dict,
    code_identity: CodeProviderIdentity,
    data_runtime_identity: DataRuntimeIdentityReference,
    retrieval_policy: RetrievalPolicyIdentity,
    agent_policy: AgentPolicyIdentity,
    metric_spec: MetricContract,
    eligible_experiments: tuple[EligibleExperiment, ...],
) -> str:
    """Hash the complete pre-execution identity payload (M7 execution lock).

    Outcomes, timestamps, local paths, credentials, and observed run bindings
    are excluded.
    """
    payload = {
        "evaluation_identity": identity_without_id,
        "code_provider_identity": code_identity.model_dump(mode="json"),
        "data_identity": data_runtime_identity.model_dump(mode="json"),
        "retrieval_policy": retrieval_policy.model_dump(mode="json"),
        "agent_policy": agent_policy.model_dump(mode="json"),
        "metric_contract": metric_spec.model_dump(mode="json"),
        "eligible_experiments": [
            experiment.model_dump(
                mode="json", exclude={"observed_denominator"}
            )
            for experiment in eligible_experiments
        ],
        "run_artifact_identity": {},  # empty planned identity
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def build_eval_manifest(
    *,
    dataset: Stage1DatasetInput,
    stage: str,
    split: str,
    code_identity: CodeProviderIdentity,
    data_runtime_identity: DataRuntimeIdentityReference,
    agent_policy: AgentPolicyIdentity,
    retrieval_policy: RetrievalPolicyIdentity,
    metric_spec: MetricContract,
    eligible_experiments: Sequence[EligibleExperimentInput],
) -> EvalManifest:
    """Build the immutable pre-execution EvalManifest.

    ``observed_denominator`` is never written here; it belongs only to the
    outcome/report after eligible outcomes exist.
    """
    if not dataset.cases:
        raise ValueError("dataset must contain at least one case")
    case_ids = [case.case_id for case in dataset.cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("dataset case_ids must be unique")

    built_experiments = tuple(
        EligibleExperiment(
            experiment_id=experiment.experiment_id,
            eligibility_predicate=experiment.eligibility_predicate,
            ordered_eligible_ids=experiment.ordered_eligible_ids,
            eligibility_hash=_eligibility_hash(experiment),
            minimum_eligible_denominator=experiment.minimum_eligible_denominator,
            observed_denominator=None,
        )
        for experiment in eligible_experiments
    )

    identity_without_id = {
        "schema_version": EVAL_MANIFEST_SCHEMA_VERSION,
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.dataset_version,
        "stage": stage,
        "split": split,
        "ordered_case_ids": case_ids,
        "case_list_sha256": case_list_sha256(case_ids),
    }
    eval_id = _compute_eval_id(
        identity_without_id=identity_without_id,
        code_identity=code_identity,
        data_runtime_identity=data_runtime_identity,
        retrieval_policy=retrieval_policy,
        agent_policy=agent_policy,
        metric_spec=metric_spec,
        eligible_experiments=built_experiments,
    )
    return EvalManifest(
        evaluation_identity=EvaluationIdentity(
            eval_id=eval_id,
            **identity_without_id,
        ),
        code_provider_identity=code_identity,
        data_identity=data_runtime_identity,
        run_artifact_identity=RunArtifactIdentity(),
        retrieval_policy=retrieval_policy,
        agent_policy=agent_policy,
        metric_contract=metric_spec,
        eligible_experiments=built_experiments,
        outcome=None,
    )


__all__ = [
    "EligibleExperimentInput",
    "Stage1DatasetInput",
    "build_eval_manifest",
]
