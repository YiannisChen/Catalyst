"""Tests for BenchmarkCase schema — contract §6.1 fields and validation."""
from __future__ import annotations

import datetime


def test_benchmark_case_minimal_construction():
    """BenchmarkCase can be constructed with all required fields."""
    from catalyst_eval.benchmark.case import BenchmarkCase, CauseLabel

    case = BenchmarkCase(
        case_id="case-001",
        schema_version="1.0.0",
        dataset_version="1.0.0",
        split="core_answerable",
        parent_case_id=None,
        ticker="AAPL",
        session_date="2026-01-15",
        cutoff_ts=datetime.datetime(2026, 1, 15, 21, 0, 0, tzinfo=datetime.timezone.utc),
        observable_facts={"revenue": 94.8e9},
        answerability="answerable",
        expected_abstention_reason_class=None,
        acceptable_cause_labels=(
            CauseLabel(descriptor="strong earnings", direction="positive"),
        ),
        evidence_judgments_by_chunk_id={},
        pool_manifest=None,
        unjudged_handling="chunks_outside_pool_explicitly_unjudged",
        lineage=None,
        annotator_notes="",
    )
    assert case.case_id == "case-001"
    assert case.ticker == "AAPL"
    assert case.split == "core_answerable"
    assert case.answerability == "answerable"


def test_benchmark_case_extra_forbid():
    """BenchmarkCase rejects unknown fields."""
    import pydantic
    from catalyst_eval.benchmark.case import BenchmarkCase, CauseLabel

    with __import__('pytest').raises(pydantic.ValidationError):
        BenchmarkCase(
            case_id="case-001",
            schema_version="1.0.0",
            dataset_version="1.0.0",
            split="core_answerable",
            ticker="AAPL",
            session_date="2026-01-15",
            cutoff_ts=datetime.datetime(2026, 1, 15, 21, 0, 0, tzinfo=datetime.timezone.utc),
            observable_facts={},
            answerability="answerable",
            acceptable_cause_labels=(),
            evidence_judgments_by_chunk_id={},
            unjudged_handling="chunks_outside_pool_explicitly_unjudged",
            annotator_notes="",
            extra_unknown_field="should_fail",  # extra forbid
        )


def test_benchmark_case_frozen():
    """BenchmarkCase is frozen (immutable after construction)."""
    import pydantic
    from catalyst_eval.benchmark.case import BenchmarkCase, CauseLabel

    case = BenchmarkCase(
        case_id="case-001",
        schema_version="1.0.0",
        dataset_version="1.0.0",
        split="core_answerable",
        ticker="AAPL",
        session_date="2026-01-15",
        cutoff_ts=datetime.datetime(2026, 1, 15, 21, 0, 0, tzinfo=datetime.timezone.utc),
        observable_facts={},
        answerability="answerable",
        acceptable_cause_labels=(),
        evidence_judgments_by_chunk_id={},
        unjudged_handling="chunks_outside_pool_explicitly_unjudged",
        annotator_notes="",
    )
    with __import__('pytest').raises(pydantic.ValidationError):
        case.case_id = "modified"  # type: ignore[misc]


def test_cause_label_descriptor_stripped():
    """CauseLabel strips descriptor whitespace."""
    from catalyst_eval.benchmark.case import CauseLabel

    cl = CauseLabel(descriptor="  strong earnings  ", direction="positive")
    assert cl.descriptor == "strong earnings"


def test_evidence_judgment_grade_range():
    """EvidenceJudgment.grade is exactly Literal[0, 1, 2]."""
    import datetime
    import pydantic
    from catalyst_eval.benchmark.judgment import EvidenceJudgment

    j = EvidenceJudgment(
        chunk_id="poly:a:news_v2:body:0001",
        grade=2,
        rationale="Perfect match to the benchmark expectation.",
        annotator="human-1",
        judged_at=datetime.datetime(2026, 1, 20, 12, 0, 0, tzinfo=datetime.timezone.utc),
    )
    assert j.grade == 2

    with __import__('pytest').raises(pydantic.ValidationError):
        EvidenceJudgment(
            chunk_id="poly:a:news_v2:body:0001",
            grade=3,  # invalid
            rationale="Bad grade.",
            annotator="human-1",
            judged_at=datetime.datetime(2026, 1, 20, 12, 0, 0, tzinfo=datetime.timezone.utc),
        )


def test_evidence_judgment_rationale_non_empty():
    """EvidenceJudgment.rationale must be non-empty after strip."""
    import datetime
    import pydantic
    from catalyst_eval.benchmark.judgment import EvidenceJudgment

    with __import__('pytest').raises(pydantic.ValidationError):
        EvidenceJudgment(
            chunk_id="poly:a:news_v2:body:0001",
            grade=1,
            rationale="   ",  # whitespace only
            annotator="human-1",
            judged_at=datetime.datetime(2026, 1, 20, 12, 0, 0, tzinfo=datetime.timezone.utc),
        )
