"""Literal, independent fixtures for B4 Eval Foundation tests."""

from __future__ import annotations

from datetime import datetime, timezone

POOL_ARMS = (
    {"arm": "lexical", "version": "1.0.0", "top_k": 20},
    {"arm": "dense", "version": "1.0.0", "top_k": 20},
)
POOL_CHUNK_IDS = (
    "polygon:article-1:news_v2:body:0001",
    "sec:filing-1:filing_v2:item_1:0001",
)
POOL_ID = "b51ba542ff008a2de777155b8b6f965023729695bab573bc8c83281c286133da"


def min_benchmark_fields() -> dict:
    return {
        "case_id": "B001",
        "schema_version": "1.0.0",
        "dataset_version": "1.0.0",
        "split": "core_answerable",
        "parent_case_id": None,
        "ticker": "AAPL",
        "session_date": "2026-01-15",
        "cutoff_ts": datetime(2026, 1, 15, 21, tzinfo=timezone.utc),
        "observable_facts": {"close_return_pct": -3.5},
        "answerability": "answerable",
        "expected_abstention_reason_class": None,
        "acceptable_cause_labels": (),
        "evidence_judgments_by_chunk_id": {},
        "pool_manifest": None,
        "unjudged_handling": "chunks_outside_pool_explicitly_unjudged",
        "lineage": None,
        "annotator_notes": "",
    }
