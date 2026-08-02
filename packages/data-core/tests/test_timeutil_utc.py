"""UTC second-resolution Z normalizer tests."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from catalyst_data.timeutil import TimestampNormalizationError, normalize_utc_second_z


def test_z_input():
    assert normalize_utc_second_z("2025-08-15T12:30:45Z") == "2025-08-15T12:30:45Z"


def test_plus_08_conversion():
    assert (
        normalize_utc_second_z("2025-08-15T20:00:00+08:00") == "2025-08-15T12:00:00Z"
    )


def test_minus_04_crosses_utc_day_boundary():
    assert (
        normalize_utc_second_z("2025-08-15T22:00:00-04:00") == "2025-08-16T02:00:00Z"
    )


def test_fractional_seconds_stripped():
    assert (
        normalize_utc_second_z("2025-08-15T12:30:45.123456Z") == "2025-08-15T12:30:45Z"
    )


def test_date_only():
    assert normalize_utc_second_z("2025-08-15") == "2025-08-15T00:00:00Z"


def test_naive_datetime_treated_as_utc():
    dt = datetime(2025, 8, 15, 12, 0, 0)
    assert normalize_utc_second_z(dt) == "2025-08-15T12:00:00Z"


def test_aware_datetime_converted():
    dt = datetime(2025, 8, 15, 20, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert normalize_utc_second_z(dt) == "2025-08-15T12:00:00Z"


def test_malformed_and_empty():
    with pytest.raises(TimestampNormalizationError):
        normalize_utc_second_z("")
    with pytest.raises(TimestampNormalizationError):
        normalize_utc_second_z(None)
    with pytest.raises(TimestampNormalizationError):
        normalize_utc_second_z("not-a-date")
