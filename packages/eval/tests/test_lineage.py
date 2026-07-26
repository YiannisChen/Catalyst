"""Tests for lineage validation."""
from __future__ import annotations

import datetime


def test_lineage_pending_no_second_pass():
    """Pending has no second-pass timestamps."""
    from catalyst_eval.benchmark.lineage import LineageRecord, ActionTimestamps, validate_lineage

    ts = ActionTimestamps(
        annotation_started=datetime.datetime(2026, 1, 20, 12, 0, 0, tzinfo=datetime.timezone.utc),
        annotation_completed=datetime.datetime(2026, 1, 20, 13, 0, 0, tzinfo=datetime.timezone.utc),
    )
    record = LineageRecord(
        candidate_generation_source="manual",
        timestamps=ts,
        adjudication_status="pending",
    )
    errors = validate_lineage(record)
    assert len(errors) == 0


def test_lineage_rejects_batch_stamps():
    """Equal timestamps produce batch_timestamp errors."""
    from catalyst_eval.benchmark.lineage import LineageRecord, ActionTimestamps, validate_lineage

    t = datetime.datetime(2026, 1, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)
    ts = ActionTimestamps(
        annotation_started=t,
        annotation_completed=t,
    )
    record = LineageRecord(
        candidate_generation_source="manual",
        timestamps=ts,
        adjudication_status="pending",
    )
    errors = validate_lineage(record)
    assert any(e["code"] == "batch_timestamp" for e in errors)


def test_lineage_validator_reports_non_utc_legacy_timestamp():
    """Validation remains typed for data loaded without Pydantic validation."""
    from catalyst_eval.benchmark.lineage import LineageRecord, ActionTimestamps, validate_lineage

    timestamps = ActionTimestamps.model_construct(
        annotation_started=datetime.datetime(2026, 1, 20, 12, 0, 0),
        annotation_completed=datetime.datetime(2026, 1, 20, 13, 0, 0),
        second_pass_started=None,
        second_pass_completed=None,
    )
    record = LineageRecord.model_construct(
        candidate_generation_source="manual",
        model_assisted_fields=(),
        human_confirmed_fields=(),
        timestamps=timestamps,
        adjudication_status="pending",
    )

    errors = validate_lineage(record)
    assert [error.code for error in errors] == [
        "non_utc_timestamp", "non_utc_timestamp",
    ]


def test_bump_version_metadata_only():
    """metadata_only bumps patch."""
    from catalyst_eval.benchmark.versioning import bump_version

    assert bump_version("1.0.0", "metadata_only") == "1.0.1"


def test_bump_version_case_added():
    """case_added bumps minor, resets patch."""
    from catalyst_eval.benchmark.versioning import bump_version

    assert bump_version("1.0.5", "case_added") == "1.1.0"


def test_bump_version_schema_modified():
    """schema_modified bumps major, resets minor and patch."""
    from catalyst_eval.benchmark.versioning import bump_version

    assert bump_version("1.2.3", "schema_modified") == "2.0.0"
