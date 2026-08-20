"""V1.1 ResearchTask contract tests (M2-4).

EvidenceNeed/TimeScope and the frozen EvidenceNeed retrieval strategy mapping
(Frozen §6.2). MARKET_STRUCTURE has no V1.1 backend and must never produce a
retrieval action.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.retrieval.task import (
    EvidenceNeed,
    ResearchTask,
    RetrievalStrategy,
    TimeScope,
    retrieval_strategy_for,
)


def _task(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": "task-1",
        "evidence_need": "COMPANY_NEWS",
        "time_scope": "SESSION_INFORMATION_WINDOW",
        "lookback_sessions": None,
        "query_hints": ("AAPL guidance",),
        "retrieval_policy_id": "qp:v1",
    }
    base.update(overrides)
    return base


def test_evidence_need_enum_is_exactly_the_seven_frozen_values() -> None:
    assert [member.value for member in EvidenceNeed] == [
        "COMPANY_PRIMARY",
        "COMPANY_NEWS",
        "SECTOR_NEWS",
        "MACRO_EVENT",
        "MACRO_SERIES",
        "FUNDAMENTALS",
        "MARKET_STRUCTURE",
    ]


def test_time_scope_enum_is_exactly_the_three_frozen_values() -> None:
    assert [member.value for member in TimeScope] == [
        "SESSION_INFORMATION_WINDOW",
        "PRIOR_SESSION",
        "LOOKBACK_SESSIONS",
    ]


def test_research_task_is_strict_and_frozen() -> None:
    task = ResearchTask(**_task())
    assert task.evidence_need is EvidenceNeed.COMPANY_NEWS
    assert task.time_scope is TimeScope.SESSION_INFORMATION_WINDOW
    assert task.lookback_sessions is None
    with pytest.raises(ValidationError):
        task.task_id = "other"  # frozen
    with pytest.raises(ValidationError):
        ResearchTask(**_task(), unknown_field=True)  # extra forbidden


def test_research_task_rejects_unknown_evidence_need_and_time_scope() -> None:
    with pytest.raises(ValidationError):
        ResearchTask(**_task(evidence_need="NEWS"))
    with pytest.raises(ValidationError):
        ResearchTask(**_task(time_scope="YESTERDAY"))


def test_lookback_sessions_requires_lookback_time_scope() -> None:
    with pytest.raises(ValidationError):
        ResearchTask(
            **_task(
                time_scope="SESSION_INFORMATION_WINDOW",
                lookback_sessions=5,
            )
        )
    task = ResearchTask(
        **_task(time_scope="LOOKBACK_SESSIONS", lookback_sessions=5)
    )
    assert task.lookback_sessions == 5


def test_market_structure_strategy_is_no_backend_capability_gap() -> None:
    assert (
        retrieval_strategy_for(EvidenceNeed.MARKET_STRUCTURE)
        is RetrievalStrategy.NO_BACKEND
    )


def test_frozen_evidence_need_strategy_mapping() -> None:
    assert retrieval_strategy_for(EvidenceNeed.COMPANY_PRIMARY) is RetrievalStrategy.HYBRID_TEXT
    assert retrieval_strategy_for(EvidenceNeed.COMPANY_NEWS) is RetrievalStrategy.HYBRID_TEXT
    assert retrieval_strategy_for(EvidenceNeed.SECTOR_NEWS) is RetrievalStrategy.HYBRID_TEXT
    assert retrieval_strategy_for(EvidenceNeed.MACRO_EVENT) is RetrievalStrategy.HYBRID_TEXT
    assert retrieval_strategy_for(EvidenceNeed.MACRO_SERIES) is RetrievalStrategy.DETERMINISTIC_STRUCTURED
    assert retrieval_strategy_for(EvidenceNeed.FUNDAMENTALS) is RetrievalStrategy.DETERMINISTIC_STRUCTURED
