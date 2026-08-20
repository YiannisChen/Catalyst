"""V1.1 EvidenceState contract tests (M2-4).

Immutable cumulative union keyed by evidence_id/fact_id (Final Migration
TSD §6.1/§6.4).
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.evidence_state import EvidenceState, EvidenceStateItem


def _item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "evidence_id": "corpus:chunk:0001",
        "first_seen_round": 1,
        "research_task_ids": ("task-1",),
        "state": "accepted",
        "canonical_asset_id": "issuer:AAPL:news:0001",
        "content_version_id": "content:v1:0001",
        "chunk_id": "corpus:chunk:0001",
        "fact_id": None,
        "content_state": "FULL_TEXT",
        "source_class": "reported_news",
        "independence_group": None,
        "dedup_cluster_id": None,
    }
    base.update(overrides)
    return base


def test_evidence_state_item_is_strict_and_frozen() -> None:
    item = EvidenceStateItem(**_item())
    assert item.evidence_id == item.chunk_id
    with pytest.raises(ValidationError):
        item.evidence_id = "other"  # frozen
    with pytest.raises(ValidationError):
        EvidenceStateItem(**_item(), unknown_field=True)  # extra forbidden


def test_evidence_state_item_requires_exactly_one_of_chunk_or_fact() -> None:
    with pytest.raises(ValidationError):
        EvidenceStateItem(**_item(fact_id="fact:1"))
    with pytest.raises(ValidationError):
        EvidenceStateItem(
            **_item(
                evidence_id="fact:1",
                chunk_id=None,
                fact_id=None,
            )
        )


def test_same_chunk_merges_contributions_and_keeps_all_task_ids() -> None:
    state = EvidenceState()
    state = state.upsert(EvidenceStateItem(**_item(research_task_ids=("task-1",))))
    state = state.upsert(
        EvidenceStateItem(
            **_item(
                research_task_ids=("task-2",),
                state="lead",
            )
        )
    )
    assert len(state.items) == 1
    assert set(state.items[0].research_task_ids) == {"task-1", "task-2"}


def test_same_chunk_across_rounds_keeps_first_seen_round() -> None:
    state = EvidenceState()
    state = state.upsert(EvidenceStateItem(**_item(first_seen_round=1)))
    state = state.upsert(EvidenceStateItem(**_item(first_seen_round=2)))
    assert state.items[0].first_seen_round == 1


def test_multiple_chunks_from_one_article_stay_separate() -> None:
    state = EvidenceState()
    state = state.upsert(EvidenceStateItem(**_item()))
    state = state.upsert(
        EvidenceStateItem(
            **_item(
                evidence_id="corpus:chunk:0002",
                chunk_id="corpus:chunk:0002",
            )
        )
    )
    assert len(state.items) == 2


def test_syndicated_assets_keep_common_group_and_separate_ids() -> None:
    state = EvidenceState()
    state = state.upsert(
        EvidenceStateItem(
            **_item(
                independence_group="syndication:g1",
                dedup_cluster_id="cluster:g1",
            )
        )
    )
    state = state.upsert(
        EvidenceStateItem(
            **_item(
                evidence_id="corpus:chunk:0002",
                chunk_id="corpus:chunk:0002",
                independence_group="syndication:g1",
                dedup_cluster_id="cluster:g1",
            )
        )
    )
    assert len(state.items) == 2
    assert state.items[0].independence_group == "syndication:g1"
    assert state.items[1].independence_group == "syndication:g1"
    assert state.items[0].evidence_id != state.items[1].evidence_id


def test_conflicting_immutable_metadata_fails_integrity_validation() -> None:
    state = EvidenceState()
    state = state.upsert(EvidenceStateItem(**_item()))
    with pytest.raises(ValueError):
        state.upsert(
            EvidenceStateItem(
                **_item(
                    canonical_asset_id="issuer:MSFT:news:0002",
                )
            )
        )
    with pytest.raises(ValueError):
        state.upsert(
            EvidenceStateItem(
                **_item(
                    source_class="issuer_disclosure",
                )
            )
        )


def test_evidence_state_is_immutable() -> None:
    state = EvidenceState()
    with pytest.raises(ValidationError):
        state.items = (EvidenceStateItem(**_item()),)  # frozen
