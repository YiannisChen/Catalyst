from __future__ import annotations


def test_hypothesis_has_all_fields():
    from catalyst_agents.attribution.hypothesis import HypothesisDraft

    h = HypothesisDraft(
        cause_label="earnings_guidance",
        direction="negative",
        transmission_mechanism="Reduced forward guidance lowered revenue expectations",
        supporting_evidence_ids=("poly:1:news_v2:body:0001",),
        counter_evidence_ids=(),
        missing_evidence=("Actual EPS figure not yet released",),
        change_condition="If actual EPS exceeds consensus, reassess",
        facts=("EPS guidance was lowered from $2.00 to $1.50",),
        calculations=(),
        inferences=("Market interpreted guidance cut as demand weakness signal",),
        unavailable_evidence=("Full earnings transcript",),
    )
    assert h.cause_label == "earnings_guidance"
    assert not hasattr(h, "prerequisite_gate_passed")


def test_hypothesis_rejects_model_self_reported_gate_fields():
    import pytest
    from pydantic import ValidationError
    from catalyst_agents.attribution.hypothesis import HypothesisDraft

    with pytest.raises(ValidationError):
        HypothesisDraft(
            cause_label="market",
            direction="negative",
            transmission_mechanism="Market beta",
            supporting_evidence_ids=("c1",),
            counter_evidence_ids=(),
            missing_evidence=(),
            change_condition="none",
            facts=(),
            calculations=(),
            inferences=(),
            unavailable_evidence=(),
            prerequisite_gate_passed=True,
        )


def test_hypothesis_draft_rejects_missing_contract_fields():
    import pytest
    from pydantic import ValidationError
    from catalyst_agents.attribution.hypothesis import HypothesisDraft

    with pytest.raises(ValidationError):
        HypothesisDraft(
            cause_label="earnings_guidance",
            direction="negative",
            transmission_mechanism="Guidance fell",
            change_condition="Reassess on updated guidance",
        )


def test_evidence_gate_requires_explicit_time_relevance_and_category():
    from catalyst_agents.attribution.gates import check_prerequisite

    base = {
        "chunk_id": "c1",
        "relevance": 0.9,
        "temporal_match": True,
        "available_at": "2026-01-15T18:00:00Z",
        "critic_category": "earnings",
    }
    context = {"cutoff": "2026-01-15T21:00:00Z"}

    assert check_prerequisite(cause="earnings_guidance", context=context, evidence=[base], edges={})[0]
    for missing in ("relevance", "temporal_match", "available_at"):
        invalid = dict(base)
        invalid.pop(missing)
        assert not check_prerequisite(cause="earnings_guidance", context=context, evidence=[invalid], edges={})[0]
    wrong_category = {**base, "critic_category": "macro"}
    assert not check_prerequisite(cause="earnings_guidance", context=context, evidence=[wrong_category], edges={})[0]


def test_peer_gate_accepts_effective_reviewed_manifest_edge():
    from attribution_fixtures import relationship_manifest_fixture
    from catalyst_agents.attribution.gates import check_prerequisite

    passed, _ = check_prerequisite(
        cause="peer_propagation",
        context={
            "ticker": "AAPL",
            "session_date": "2026-01-15",
            "cutoff": "2026-01-15T21:00:00Z",
            "peer_returns_by_ticker": {"MSFT": -3.0},
        },
        evidence=[{
            "chunk_id": "c2",
            "relevance": 0.8,
            "temporal_match": True,
            "available_at": "2026-01-15T19:00:00Z",
            "ticker_associations": ("MSFT",),
        }],
        edges=relationship_manifest_fixture(),
    )
    assert passed


def test_market_hypothesis_requires_benchmark_ohlcv():
    from catalyst_agents.attribution.gates import check_prerequisite

    passed, reason = check_prerequisite(cause="market", context={"benchmark_ohlcv_exists": False}, evidence=[], edges={})
    assert not passed
    assert "OHLCV" in reason


def test_peer_propagation_requires_edge():
    from catalyst_agents.attribution.gates import check_prerequisite

    passed, _ = check_prerequisite(
        cause="peer_propagation",
        context={"peer_context_available": True, "counterparty": "MSFT"},
        evidence=[{"chunk_id": "poly:peer1", "ticker_associations": ["MSFT"], "available_at": "2026-01-15T18:00:00Z"}],
        edges={"AAPL": {"supplier": ["MSFT"]}},
    )
    assert not passed


def test_edge_alone_insufficient():
    from catalyst_agents.attribution.gates import check_prerequisite

    passed, _ = check_prerequisite(
        cause="peer_propagation",
        context={"peer_context_available": True, "counterparty": "MSFT"},
        evidence=[],
        edges={"AAPL": {"peer": ["MSFT"]}},
    )
    assert not passed


def test_abstain_when_all_gates_fail():
    from catalyst_agents.attribution.gates import all_gates_failed

    results = [("market", False, "no OHLCV"), ("sector", False, "no ETF data"), ("earnings_guidance", False, "no evidence")]
    assert all_gates_failed(results)


def test_relationship_manifest_hash_and_effective_edges_validate():
    import json
    from pathlib import Path
    from catalyst_agents.attribution.relationships import effective_edges, relationship_manifest_hash, validate_relationship_manifest

    manifest = json.loads((Path(__file__).resolve().parents[1] / "catalyst_agents" / "attribution" / "manifests" / "relationships_core_v1.json").read_text())
    assert relationship_manifest_hash(manifest) == manifest["manifest_id"]
    assert validate_relationship_manifest(manifest) == manifest
    assert [edge["edge_id"] for edge in effective_edges(manifest, ticker="AAPL", session_date="2026-01-15")] == ["edge-aapl-msft-peer"]


def test_relationship_manifest_rejects_corruptions():
    import copy
    from attribution_fixtures import relationship_manifest_fixture
    from catalyst_agents.attribution.relationships import RelationshipManifestError, relationship_manifest_hash, validate_relationship_manifest
    import pytest

    base = relationship_manifest_fixture()
    corruptions = []
    dup = copy.deepcopy(base); dup["edges"].append(copy.deepcopy(dup["edges"][0])); dup["manifest_id"] = relationship_manifest_hash(dup); corruptions.append(dup)
    bad_type = copy.deepcopy(base); bad_type["edges"][0]["relationship_type"] = "unknown"; bad_type["manifest_id"] = relationship_manifest_hash(bad_type); corruptions.append(bad_type)
    bad_interval = copy.deepcopy(base); bad_interval["edges"][0]["effective_to"] = "2019-01-01"; bad_interval["manifest_id"] = relationship_manifest_hash(bad_interval); corruptions.append(bad_interval)
    future = copy.deepcopy(base); future["edges"][0]["reviewed_at"] = "2999-01-01T00:00:00Z"; future["manifest_id"] = relationship_manifest_hash(future); corruptions.append(future)
    bad_hash = copy.deepcopy(base); bad_hash["manifest_id"] = "0" * 64; corruptions.append(bad_hash)
    for manifest in corruptions:
        with pytest.raises(RelationshipManifestError):
            validate_relationship_manifest(manifest)
