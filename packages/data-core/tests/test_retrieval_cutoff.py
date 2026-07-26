"""Tests for cutoff computation with session-close and early-close support."""
from __future__ import annotations


def test_cutoff_is_session_close():
    """Close-to-close cutoff uses official exchange close, including early-close."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    # Regular session: 16:00 ET = 21:00 UTC
    cutoff = compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                            mode="close_to_close")
    assert cutoff == "2026-01-15T21:00:00Z"

    # Early-close session: day after Thanksgiving 2025, 13:00 ET = 18:00 UTC
    cutoff_early = compute_cutoff(ticker="AAPL", session_date="2025-11-28",
                                  mode="close_to_close")
    assert cutoff_early == "2025-11-28T18:00:00Z"


def test_intraday_cutoff_is_explicit_utc():
    """Intraday uses explicit UTC as_of, not session close."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    cutoff = compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                            mode="intraday", as_of="2026-01-15T15:30:00Z")
    assert cutoff == "2026-01-15T15:30:00Z"


def test_cutoff_is_utc():
    """All cutoffs are UTC ISO-8601."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    for mode in ["close_to_close", "intraday"]:
        cutoff = compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                                mode=mode, as_of="2026-01-15T15:30:00Z")
        assert cutoff.endswith("Z")
        assert "T" in cutoff


def test_no_three_day_symmetric_window():
    """Cutoff is NOT ±3 days around session date."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    cutoff = compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                            mode="close_to_close")
    assert cutoff == "2026-01-15T21:00:00Z"
    assert "2026-01-12" not in cutoff[:10]


def test_weekend_rejected():
    """Weekend dates are rejected."""
    import pytest
    from catalyst_data.retrieval.cutoff import compute_cutoff

    # 2026-01-17 is a Saturday
    with pytest.raises(ValueError):
        compute_cutoff(ticker="AAPL", session_date="2026-01-17",
                        mode="close_to_close")


def test_holiday_rejected():
    """Full-holiday dates are rejected."""
    import pytest
    from catalyst_data.retrieval.cutoff import compute_cutoff

    # Christmas 2025-12-25
    with pytest.raises(ValueError):
        compute_cutoff(ticker="AAPL", session_date="2025-12-25",
                        mode="close_to_close")


# ── Item 3: Extended cutoff tests ────────────────────────────────────────────

def test_new_years_day_rejected():
    """2027-01-01 (New Year's Day) is rejected as non-trading day."""
    import pytest
    from catalyst_data.retrieval.cutoff import compute_cutoff

    with pytest.raises(ValueError, match="not_a_trading_session"):
        compute_cutoff(ticker="AAPL", session_date="2027-01-01",
                        mode="close_to_close")


def test_christmas_intraday_rejected():
    """2025-12-25 intraday should fail — it's a holiday."""
    import pytest
    from catalyst_data.retrieval.cutoff import compute_cutoff

    with pytest.raises(ValueError):
        compute_cutoff(ticker="AAPL", session_date="2025-12-25",
                        mode="intraday", as_of="2025-12-25T14:00:00Z")


def test_dst_spring_forward_close():
    """DST spring forward: 2026-03-12 (Thu) close is 21:00 UTC (16:00 ET)."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    # March 12, 2026 is a Thursday — regular close, DST active
    cutoff = compute_cutoff(ticker="AAPL", session_date="2026-03-12",
                            mode="close_to_close")
    assert cutoff == "2026-03-12T20:00:00Z"


def test_dst_fall_back_close():
    """DST fall back: 2025-11-05 (Wed) close is 21:00 UTC (16:00 ET)."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    cutoff = compute_cutoff(ticker="AAPL", session_date="2025-11-05",
                            mode="close_to_close")
    assert cutoff == "2025-11-05T21:00:00Z"


def test_early_close_day_after_thanksgiving_2025():
    """2025-11-28 (day after Thanksgiving) early close at 13:00 ET = 18:00 UTC."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    cutoff = compute_cutoff(ticker="AAPL", session_date="2025-11-28",
                            mode="close_to_close")
    assert cutoff == "2025-11-28T18:00:00Z"


def test_exchange_cutoff_policy_exists():
    """ExchangeCutoffPolicy class is importable from cutoff module."""
    from catalyst_data.retrieval.cutoff import ExchangeCutoffPolicy

    policy = ExchangeCutoffPolicy()
    result = policy.close_to_close("AAPL", "2026-01-15")
    assert result == "2026-01-15T21:00:00Z"


def test_intraday_fractional_second():
    """Fractional second as_of should parse correctly or be rejected."""
    import pytest
    from catalyst_data.retrieval.cutoff import compute_cutoff

    # ISO 8601 with fractional seconds is valid as long as it ends in Z
    cutoff = compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                            mode="intraday", as_of="2026-01-15T15:30:00.500Z")
    assert cutoff == "2026-01-15T15:30:00Z"


def test_intraday_after_close_rejected():
    """Intraday as_of after session close is rejected."""
    import pytest
    from catalyst_data.retrieval.cutoff import compute_cutoff

    with pytest.raises(ValueError, match="invalid_as_of"):
        compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                        mode="intraday", as_of="2026-01-15T22:00:00Z")


def test_cutoff_failures_expose_typed_codes():
    import pytest
    from catalyst_data.retrieval.cutoff import CutoffPolicyError, compute_cutoff

    with pytest.raises(CutoffPolicyError) as out_of_range:
        compute_cutoff("AAPL", "2028-01-03")
    assert out_of_range.value.code == "calendar_out_of_range"

    with pytest.raises(CutoffPolicyError) as holiday:
        compute_cutoff("AAPL", "2026-01-17")
    assert holiday.value.code == "not_a_trading_session"
