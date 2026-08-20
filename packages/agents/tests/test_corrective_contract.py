"""V1.1 corrective research contract tests (M2-6).

MissingEvidence/CorrectiveResearchAction/CorrectiveResearchBatch with the
production freeze max_corrective_rounds=1, max_actions_per_batch=1 and the
MARKET_STRUCTURE_UNSUPPORTED non-recoverable gap rule (Frozen §6.4).
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


def test_market_structure_unsupported_is_non_recoverable_gap() -> None:
    with pytest.raises(ValidationError):
        MissingEvidence(
            **_missing(
                evidence_need="MARKET_STRUCTURE",
                reason_code="MARKET_STRUCTURE_UNSUPPORTED",
                recoverable=True,
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


def test_corrective_batch_production_freeze() -> None:
    assert MAX_CORRECTIVE_ROUNDS == 1
    assert MAX_ACTIONS_PER_BATCH == 1
    batch = CorrectiveResearchBatch(
        batch_id="batch:1",
        actions=(CorrectiveResearchAction(**_action()),),
        shared_deadline=_utc("2026-01-06T21:00:00Z"),
        total_result_budget=20,
    )
    assert batch.batch_id == "batch:1"
    with pytest.raises(ValidationError):
        CorrectiveResearchBatch(
            batch_id="batch:1",
            actions=(
                CorrectiveResearchAction(**_action()),
                CorrectiveResearchAction(**_action(action_id="action:2")),
            ),
            shared_deadline=_utc("2026-01-06T21:00:00Z"),
            total_result_budget=40,
        )


def test_one_gap_id_maps_to_at_most_one_action() -> None:
    with pytest.raises(ValidationError):
        CorrectiveResearchBatch(
            batch_id="batch:1",
            actions=(
                CorrectiveResearchAction(**_action()),
                CorrectiveResearchAction(**_action(action_id="action:2")),
            ),
            shared_deadline=_utc("2026-01-06T21:00:00Z"),
            total_result_budget=40,
        )
