"""V1.1 corrective research contract tests (M2-6, corrective).

Phase 4 TSD §16–19: MARKET_STRUCTURE always normalizes to
MARKET_STRUCTURE_UNSUPPORTED with recoverable=false; a production batch
contains exactly one action; empty batches are invalid; total_result_budget is
positive; lookback scope/count is consistent; batch identity/policy/deadline/
cancellation fields are part of the contract.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.retrieval.corrective import (
    MAX_ACTIONS_PER_BATCH,
    MAX_CORRECTIVE_ROUNDS,
    CorrectiveResearchAction,
    CorrectiveResearchBatch,
    GapReasonCode,
    MissingEvidence,
)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _missing(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "gap_id": "gap:1",
        "evidence_need": "COMPANY_PRIMARY",
        "time_scope": "PRIOR_SESSION",
        "expected_information": "issuer filing confirmation",
        "reason_code": "MISSING_PRIMARY_CONFIRMATION",
        "recoverable": True,
    }
    base.update(overrides)
    return base


def _action(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action_id": "action:1",
        "gap_id": "gap:1",
        "evidence_need": "COMPANY_PRIMARY",
        "time_scope": "PRIOR_SESSION",
        "lookback_sessions": None,
        "query_hints": ("8-K",),
        "research_fingerprint": "fp:1",
    }
    base.update(overrides)
    return base


def _batch(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "batch_id": "batch:1",
        "run_id": "run:1",
        "round": 1,
        "actions": (CorrectiveResearchAction(**_action()),),
        "shared_deadline": _utc("2026-01-06T21:00:00Z"),
        "internal_deadline": _utc("2026-01-06T21:00:00Z"),
        "total_result_budget": 20,
        "policy_version": "cp:v1",
        "cancellation_token_ref": None,
    }
    base.update(overrides)
    return base


def test_gap_reason_codes_are_exactly_the_nine_frozen_values() -> None:
    assert [member.value for member in GapReasonCode] == [
        "MISSING_PRIMARY_CONFIRMATION",
        "MISSING_INDEPENDENT_CORROBORATION",
        "MISSING_PRIOR_SESSION_CONTEXT",
        "MISSING_SECTOR_CONTEXT",
        "MISSING_MACRO_CONTEXT",
        "MISSING_FUNDAMENTAL_CONTEXT",
        "CONFLICT_REQUIRES_RESOLUTION",
        "MARKET_STRUCTURE_UNSUPPORTED",
        "LOCAL_COVERAGE_GAP",
    ]


def test_missing_evidence_is_strict_and_frozen() -> None:
    missing = MissingEvidence(**_missing())
    with pytest.raises(ValidationError):
        missing.gap_id = "other"  # frozen
    with pytest.raises(ValidationError):
        MissingEvidence(**_missing(), unknown_field=True)  # extra forbidden


def test_market_structure_always_normalizes_to_unsupported_non_recoverable() -> None:
    with pytest.raises(ValidationError):
        MissingEvidence(
            **_missing(
                evidence_need="MARKET_STRUCTURE",
                reason_code="MISSING_PRIMARY_CONFIRMATION",
                recoverable=False,
            )
        )
    with pytest.raises(ValidationError):
        MissingEvidence(
            **_missing(
                evidence_need="MARKET_STRUCTURE",
                reason_code="MARKET_STRUCTURE_UNSUPPORTED",
                recoverable=True,
            )
        )
    with pytest.raises(ValidationError):
        MissingEvidence(
            **_missing(
                evidence_need="COMPANY_NEWS",
                reason_code="MARKET_STRUCTURE_UNSUPPORTED",
                recoverable=False,
            )
        )
    missing = MissingEvidence(
        **_missing(
            evidence_need="MARKET_STRUCTURE",
            reason_code="MARKET_STRUCTURE_UNSUPPORTED",
            recoverable=False,
        )
    )
    assert missing.recoverable is False


def test_corrective_research_action_fields() -> None:
    action = CorrectiveResearchAction(**_action())
    assert action.action_id == "action:1"
    assert action.gap_id == "gap:1"
    with pytest.raises(ValidationError):
        CorrectiveResearchAction(**_action(), unknown_field=True)


def test_corrective_action_rejects_market_structure() -> None:
    with pytest.raises(ValidationError):
        CorrectiveResearchAction(
            **_action(
                evidence_need="MARKET_STRUCTURE",
                gap_id="gap:9",
            )
        )


def test_corrective_action_lookback_consistency() -> None:
    with pytest.raises(ValidationError):
        CorrectiveResearchAction(
            **_action(
                time_scope="PRIOR_SESSION",
                lookback_sessions=5,
            )
        )
    with pytest.raises(ValidationError):
        CorrectiveResearchAction(
            **_action(
                time_scope="LOOKBACK_SESSIONS",
                lookback_sessions=0,
            )
        )
    action = CorrectiveResearchAction(
        **_action(time_scope="LOOKBACK_SESSIONS", lookback_sessions=5)
    )
    assert action.lookback_sessions == 5


def test_corrective_batch_production_freeze_and_identity_fields() -> None:
    assert MAX_CORRECTIVE_ROUNDS == 1
    assert MAX_ACTIONS_PER_BATCH == 1
    batch = CorrectiveResearchBatch(**_batch())
    assert batch.batch_id == "batch:1"
    assert batch.run_id == "run:1"
    assert batch.round == 1
    assert batch.policy_version == "cp:v1"
    assert batch.cancellation_token_ref is None
    with pytest.raises(ValidationError):
        CorrectiveResearchBatch(
            **_batch(
                actions=(
                    CorrectiveResearchAction(**_action()),
                    CorrectiveResearchAction(**_action(action_id="action:2")),
                ),
                total_result_budget=40,
            )
        )


def test_empty_corrective_batch_is_invalid() -> None:
    with pytest.raises(ValidationError):
        CorrectiveResearchBatch(**_batch(actions=()))


def test_total_result_budget_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        CorrectiveResearchBatch(**_batch(total_result_budget=0))
    with pytest.raises(ValidationError):
        CorrectiveResearchBatch(**_batch(total_result_budget=-5))


def test_one_gap_id_maps_to_at_most_one_action() -> None:
    with pytest.raises(ValidationError):
        CorrectiveResearchBatch(
            **_batch(
                actions=(
                    CorrectiveResearchAction(**_action()),
                    CorrectiveResearchAction(**_action(action_id="action:2")),
                ),
                total_result_budget=40,
            )
        )
