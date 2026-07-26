from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tests.benchmark_fixtures import min_benchmark_fields
from catalyst_eval.benchmark.case import BenchmarkCase
from catalyst_eval.benchmark.lineage import (
    ActionTimestamps,
    LineageRecord,
    validate_lineage,
)


@pytest.mark.parametrize(
    ("updates", "expected_error"),
    [
        ({"split": "adversarial", "parent_case_id": None}, "parent_case_id"),
        ({"split": "core_abstain", "answerability": "answerable"}, "answerability"),
        ({"split": "core_abstain", "answerability": "abstain", "expected_abstention_reason_class": None}, "expected_abstention"),
    ],
)
def test_benchmark_cross_field_contracts(updates, expected_error):
    fields = min_benchmark_fields() | updates
    with pytest.raises(ValidationError, match=expected_error):
        BenchmarkCase(**fields)


def test_cutoff_timestamp_must_be_utc_not_merely_aware():
    fields = min_benchmark_fields()
    fields["cutoff_ts"] = datetime(2026, 1, 15, 23, tzinfo=timezone(timedelta(hours=2)))
    with pytest.raises(ValidationError, match="UTC"):
        BenchmarkCase(**fields)


def test_lineage_rejects_annotation_before_session_date():
    record = LineageRecord(
        candidate_generation_source="manual",
        timestamps=ActionTimestamps(
            annotation_started=datetime(2026, 1, 14, 23, tzinfo=timezone.utc),
            annotation_completed=datetime(2026, 1, 15, 1, tzinfo=timezone.utc),
        ),
        adjudication_status="pending",
    )
    errors = validate_lineage(record, session_date="2026-01-15")
    assert tuple(error.code for error in errors) == ("annotation_predates_session",)
