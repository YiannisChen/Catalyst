"""Tests for trading_calendar — W1-B calendar coverage."""
from datetime import time

import pytest
from catalyst_data.trading_calendar import (
    CALENDAR_YEARS,
    EARLY_CLOSE,
    EARLY_CLOSE_DATES,
    US_MARKET_HOLIDAYS,
    CalendarCoverageError,
    calendar_trading_days,
    is_trading_day,
    latest_closed_trading_day_for_date,
    session_close_utc,
)


class TestCalendarCoverage:
    def test_year_boundary_transition(self):
        days = calendar_trading_days("2024-12-30", "2025-01-03")
        assert "2024-12-30" in days  # Monday
        assert "2024-12-31" in days  # Tuesday
        assert "2025-01-01" not in days  # New Year's Day
        assert "2025-01-02" in days  # Thursday
        assert "2025-01-03" in days  # Friday
        assert len(days) == 4

    def test_weekend_excluded(self):
        # July 10 2026 = Friday, July 11 = Saturday, July 13 = Monday
        days = calendar_trading_days("2026-07-10", "2026-07-13")
        assert "2026-07-10" in days  # Friday
        assert "2026-07-11" not in days  # Saturday
        assert "2026-07-12" not in days  # Sunday
        assert "2026-07-13" in days  # Monday
        assert len(days) == 2

    def test_unsupported_year_fails_closed(self):
        with pytest.raises(CalendarCoverageError) as exc_info:
            calendar_trading_days("2023-12-01", "2023-12-31")
        assert "2023-12-01" in str(exc_info.value)

    def test_latest_closed_trading_day(self):
        from datetime import date
        # On a Monday, latest closed is previous Friday
        dt = date(2026, 7, 13)  # Monday
        result = latest_closed_trading_day_for_date(dt)
        # July 13 2026 is Monday = a trading day itself
        assert result.isoformat() == "2026-07-13"


class TestCalendarYearsConstant:
    def test_years_span_2024_to_2027(self):
        assert CALENDAR_YEARS[0] == 2024
        assert CALENDAR_YEARS[1] == 2027


class TestSessionOpenUtc:
    """Authoritative 09:30 America/New_York session open (M6 corrective)."""

    def test_regular_est_session_open(self):
        from catalyst_data.trading_calendar import session_open_utc

        # 2026-01-06 is EST (UTC-5): 09:30 ET == 14:30 UTC.
        assert session_open_utc("2026-01-06") == "2026-01-06T14:30:00Z"

    def test_regular_edt_session_open(self):
        from catalyst_data.trading_calendar import session_open_utc

        # 2026-07-15 is EDT (UTC-4): 09:30 ET == 13:30 UTC.
        assert session_open_utc("2026-07-15") == "2026-07-15T13:30:00Z"

    def test_early_close_session_open_keeps_0930_open(self):
        from catalyst_data.trading_calendar import session_close_utc, session_open_utc

        # 2026-11-27 early closes at 13:00 ET but still opens at 09:30 ET.
        assert session_open_utc("2026-11-27") == "2026-11-27T14:30:00Z"
        assert session_close_utc("2026-11-27") == "2026-11-27T18:00:00Z"

    def test_holiday_session_open_rejected(self):
        from catalyst_data.trading_calendar import session_open_utc

        with pytest.raises(ValueError, match="not_a_trading_session"):
            session_open_utc("2026-11-26")  # Thanksgiving

    def test_weekend_session_open_rejected(self):
        from catalyst_data.trading_calendar import session_open_utc

        with pytest.raises(ValueError, match="not_a_trading_session"):
            session_open_utc("2026-07-11")  # Saturday

    def test_open_is_never_derived_from_close_duration(self):
        """Open and close derive independently; an early close cannot shift open."""
        from catalyst_data.trading_calendar import session_close_utc, session_open_utc

        regular_open = session_open_utc("2026-07-15")
        early_open = session_open_utc("2026-11-27")
        # Both are regular 09:30 ET opens even though the close differs.
        assert regular_open == "2026-07-15T13:30:00Z"
        assert early_open == "2026-11-27T14:30:00Z"
        assert session_close_utc("2026-07-15") != session_close_utc("2026-11-27")


class TestClosedHolidayFile:
    def test_loaded_sets_equal_the_fixed_dates(self):
        assert US_MARKET_HOLIDAYS == frozenset({
            "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29",
            "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02",
            "2024-11-28", "2024-12-25",
            "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
            "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
            "2025-11-27", "2025-12-25",
            "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
            "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
            "2026-11-26", "2026-12-25",
            "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
            "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
            "2027-11-25", "2027-12-24",
        })
        assert EARLY_CLOSE_DATES == frozenset({
            "2024-07-03", "2024-11-29", "2024-12-24",
            "2025-07-03", "2025-11-28", "2025-12-24",
            "2026-07-02", "2026-11-27", "2026-12-24",
            "2027-07-02", "2027-11-26", "2027-12-23",
        })

    def test_observed_independence_day_2026_is_a_full_close(self):
        assert is_trading_day("2026-07-03") is False
        assert "2026-07-03" in US_MARKET_HOLIDAYS
        assert "2026-07-03" not in EARLY_CLOSE_DATES

    def test_july_2_2026_is_an_early_close_not_a_full_holiday(self):
        # 2026-07-02 is EDT: 13:00 America/New_York == 17:00 UTC.
        assert is_trading_day("2026-07-02") is True
        assert "2026-07-02" in EARLY_CLOSE_DATES
        assert "2026-07-02" not in US_MARKET_HOLIDAYS
        assert EARLY_CLOSE == time(13, 0)
        assert session_close_utc("2026-07-02") == "2026-07-02T17:00:00Z"

    def test_date_outside_coverage_raises(self):
        with pytest.raises(CalendarCoverageError) as exc_info:
            calendar_trading_days("2028-01-01", "2028-01-01")
        assert "2028-01-01" in str(exc_info.value)
