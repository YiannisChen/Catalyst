"""Tests for trading_calendar — W1-B calendar coverage."""
import pytest
from catalyst_data.trading_calendar import (
    CALENDAR_YEARS, CalendarCoverageError, calendar_trading_days,
    latest_closed_trading_day_for_date,
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
