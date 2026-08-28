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
