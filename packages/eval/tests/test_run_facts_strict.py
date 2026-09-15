"""Adversarial tests for the persisted Stage-1 run-facts contract."""
from __future__ import annotations

import datetime
from copy import deepcopy
import hashlib
import json

import pytest

from catalyst_eval.benchmark.pool_manifest import PoolArm, PoolManifest
from catalyst_eval.v1_1.run_facts import validate_run_facts


def _valid_pool(case_id: str) -> dict:
    hash_input = {
        "schema_version": "1.0.0",
        "case_id": case_id,
        "arms": [{"arm": "lexical", "version": "1.0.0", "top_k": 8}],
        "chunk_inventory": ["evidence-1"],
        "corpus_manifest_id": "1" * 64,
        "index_manifest_id": None,
        "source_artifact_id": "2" * 64,
    }
    pool_id = hashlib.sha256(
        json.dumps(hash_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    pool = PoolManifest(
        schema_version="1.0.0",
        pool_id=pool_id,
        case_id=case_id,
        arms=(PoolArm(arm="lexical", version="1.0.0", top_k=8),),
        chunk_inventory=("evidence-1",),
        corpus_manifest_id="1" * 64,
        index_manifest_id=None,
        source_artifact_id="2" * 64,
        created_at=datetime.datetime(2026, 9, 15, tzinfo=datetime.timezone.utc),
    )
    return pool.model_dump(mode="json")


def _facts() -> dict:
    case_id = "case-001"
    return {
        "schema_version": "v1_1_stage1_run_facts_v1",
        "case_id": case_id,
        "output_status": "SUFFICIENT",
        "attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "refusal_reason": None,
        "refusal_reason_available": False,
        "claims": [
            {
                "claim_id": "claim-1",
                "material": True,
                "role": "PRIMARY",
                "citation_ids": ["evidence-1"],
                "statement": "supported",
            }
        ],
        "sanity_tasks_completed": ["task-1"],
        "latency_ms": 12,
        "tokens": 30,
        "cost_usd": 0.25,
        "model_limited": False,
        "trajectory": {
            "corrective_triggered": False,
            "rounds_executed": 0,
            "gap_reason_codes": [],
            "corrective_actions": [],
            "research_fingerprints": [],
            "evidence_delta_ids": [],
            "produced_structure": True,
        },
        "retrieval": {
            "observed": True,
            "pool": _valid_pool(case_id),
            "ranked_evidence_ids": ["evidence-1"],
            "candidate_evidence_ids": ["evidence-1"],
            "reranker_contributed": False,
            "latency_ms": 4,
            "degraded": False,
            "ticker_violations": [],
            "cutoff_violations": [],
        },
        "provider_accounting": {
            "provider_calls": 2,
            "tokens_in": 10,
            "tokens_out": 20,
            "cost_usd": 0.25,
            "cost_method": "reported",
        },
        "provider_calls": 2,
        "run_id": "run-1",
    }


def test_valid_run_facts_are_recursively_accepted():
    assert validate_run_facts(_facts(), expected_case_id="case-001") is not None


@pytest.mark.parametrize(
    ("path", "value", "match"),
    (
        (("refusal_reason_available",), "false", "strict bool"),
        (("model_limited",), 1, "strict bool"),
        (("trajectory", "corrective_triggered"), "false", "strict bool"),
        (("trajectory", "rounds_executed"), True, "non-negative int"),
        (("retrieval", "observed"), "false", "strict bool"),
        (("retrieval", "latency_ms"), True, "non-negative int"),
        (("provider_accounting", "cost_usd"), "0.25", "finite non-negative"),
        (("provider_accounting", "tokens_in"), True, "non-negative int"),
        (("sanity_tasks_completed",), [1], "non-empty string"),
        (("attribution_type",), None, "attribution_type"),
    ),
)
def test_malformed_strict_scalars_fail_closed(path, value, match):
    facts = _facts()
    target = facts
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match=match):
        validate_run_facts(facts, expected_case_id="case-001")


@pytest.mark.parametrize(
    ("path", "value", "match"),
    (
        (("provider_accounting", "provider_calls"), 1, "provider_calls"),
        (("tokens",), 29, "tokens"),
        (("cost_usd",), 0.24, "cost_usd"),
        (("claims",), [
            {"claim_id": "claim-1", "material": True, "role": "PRIMARY", "citation_ids": []},
            {"claim_id": "claim-1", "material": False, "role": "CONTEXT", "citation_ids": []},
        ], "duplicate claim_id"),
        (("claims",), [
            {"claim_id": "claim-1", "material": True, "role": "PRIMARY", "citation_ids": [""]},
        ], "citation"),
        (("claims",), [
            {"claim_id": "claim-1", "material": True, "role": "PRIMARY", "citation_ids": ["evidence-1", "evidence-1"]},
        ], "citation"),
    ),
)
def test_persisted_cross_field_and_identity_mismatches_fail_closed(path, value, match):
    facts = _facts()
    target = facts
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match=match):
        validate_run_facts(facts, expected_case_id="case-001")


def test_report_provider_call_mismatch_is_rejected():
    facts = _facts()
    facts.pop("provider_calls")
    with pytest.raises(ValueError, match="provider_calls"):
        validate_run_facts(
            facts, expected_case_id="case-001", row_provider_calls=1
        )


def test_observed_false_cannot_carry_measured_results_or_pool():
    facts = _facts()
    facts["retrieval"]["observed"] = False
    with pytest.raises(ValueError, match="observed=false"):
        validate_run_facts(facts, expected_case_id="case-001")


@pytest.mark.parametrize(
    "field",
    (
        "ranked_evidence_ids",
        "candidate_evidence_ids",
        "ticker_violations",
        "cutoff_violations",
    ),
)
def test_retrieval_identity_lists_reject_duplicates(field):
    facts = _facts()
    facts["retrieval"][field] = ["evidence-1", "evidence-1"]
    with pytest.raises(ValueError, match="duplicate"):
        validate_run_facts(facts, expected_case_id="case-001")


def test_ranked_evidence_must_come_from_the_observed_candidate_pool():
    facts = _facts()
    facts["retrieval"]["ranked_evidence_ids"] = ["fabricated-evidence"]
    with pytest.raises(ValueError, match="ranked_evidence_ids"):
        validate_run_facts(facts, expected_case_id="case-001")


def test_candidate_evidence_must_match_the_pool_inventory():
    facts = _facts()
    facts["retrieval"]["candidate_evidence_ids"] = ["different-candidate"]
    with pytest.raises(ValueError, match="candidate_evidence_ids"):
        validate_run_facts(facts, expected_case_id="case-001")


def test_pool_case_identity_must_match_the_run_case():
    facts = _facts()
    facts["retrieval"]["pool"] = _valid_pool("different-case")
    with pytest.raises(ValueError, match="pool.case_id"):
        validate_run_facts(facts, expected_case_id="case-001")


def test_observed_false_cannot_claim_degraded_retrieval():
    facts = _facts()
    facts["retrieval"] = {
        "observed": False,
        "pool": None,
        "ranked_evidence_ids": [],
        "candidate_evidence_ids": [],
        "reranker_contributed": False,
        "latency_ms": None,
        "degraded": True,
        "ticker_violations": [],
        "cutoff_violations": [],
    }
    with pytest.raises(ValueError, match="observed=false"):
        validate_run_facts(facts, expected_case_id="case-001")


def test_top_level_tokens_are_unknown_when_provider_sum_is_incomplete():
    facts = _facts()
    facts["provider_accounting"]["tokens_out"] = None
    facts["tokens"] = None
    assert validate_run_facts(facts, expected_case_id="case-001") is not None

    malformed = deepcopy(facts)
    malformed["tokens"] = 0
    with pytest.raises(ValueError, match="tokens"):
        validate_run_facts(malformed, expected_case_id="case-001")
