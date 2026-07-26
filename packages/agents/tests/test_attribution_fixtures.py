from __future__ import annotations

import pytest

from attribution_fixtures import (
    EXPECTED_20_SESSIONS,
    EXPECTED_VALID_12_SESSION_IDS,
    StubModelClient,
    VALID_12_VOLUMES,
    VALID_ASSURANCE_RECORD,
    fixture_evidence,
    mock_provider_with_ohlcv,
    volume_window_provider,
)


def test_literal_volume_fixture_values():
    assert len(EXPECTED_20_SESSIONS) == 20
    assert len(VALID_12_VOLUMES) == 12
    assert EXPECTED_VALID_12_SESSION_IDS[-1] == "2026-01-14"


def test_context_provider_returns_immutable_copies():
    provider = mock_provider_with_ohlcv()
    inputs = provider.load_context_inputs(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    with pytest.raises(TypeError):
        inputs.prior_volumes_by_session["2026-01-14"] = 1.0
    second = provider.load_context_inputs(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    assert second.prior_volumes_by_session["2026-01-14"] == 1190.0


def test_unknown_context_key_fails():
    provider = mock_provider_with_ohlcv()
    with pytest.raises(KeyError):
        provider.load_context_inputs(ticker="MSFT", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")


def test_volume_window_contains_out_of_window_landmine():
    provider = volume_window_provider(
        expected_prior_sessions=EXPECTED_20_SESSIONS,
        valid_prior_volumes=VALID_12_VOLUMES,
        older_volume=9_999_999,
        target_volume=1200.0,
    )
    inputs = provider.load_context_inputs(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    assert inputs.prior_volumes_by_session["2025-12-15"] == 9_999_999
    assert "2025-12-15" not in inputs.expected_prior_sessions


def test_fixture_retrieved_evidence_has_manifest_identity():
    evidence = fixture_evidence()
    assert evidence[0].corpus_manifest_id == "corpus-fixture-v1"
    assert evidence[0].index_manifest_id == "index-fixture-v1"
    assert evidence[0].available_at <= "2026-01-15T21:00:00Z"


def test_stub_model_client_counts_calls_deterministically():
    stub = StubModelClient(["one", "two"])
    assert stub.invoke("p1").content == "one"
    assert stub.invoke("p2").content == "two"
    assert stub.invoke("p3").content == "two"
    assert len(stub.calls) == 3


def test_expected_assurance_literal_not_production_generated():
    assert isinstance(VALID_ASSURANCE_RECORD, dict)
    assert [check["check_name"] for check in VALID_ASSURANCE_RECORD["checks"]] == [
        "cutoff",
        "citation_resolution",
        "judge_visibility",
        "prerequisite_gates",
        "legal_path",
        "trace_completeness",
        "identities",
        "budget_retry_repair",
        "degraded_state",
        "structured_context_support",
    ]
