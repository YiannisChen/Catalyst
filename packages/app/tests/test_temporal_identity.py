"""Authoritative TemporalIdentity production derivation (M6 corrective).

``build_temporal_identity`` must derive the session open from the data-core
09:30 America/New_York authority, never from the close minus a fixed
duration, and must reject holidays/non-sessions fail-closed.
"""
from __future__ import annotations

import pytest

from catalyst_app.runtime.composition import RuntimeUnavailableError, build_temporal_identity


def _iso(value) -> str:
    return value.isoformat()


def test_regular_est_session(tmp_path) -> None:
    identity = build_temporal_identity("2026-01-06")
    assert identity.session_date == "2026-01-06"
    assert _iso(identity.session_open_at) == "2026-01-06T14:30:00+00:00"
    assert _iso(identity.session_close_at) == "2026-01-06T21:00:00+00:00"
    assert _iso(identity.cutoff_at) == "2026-01-06T21:00:00+00:00"


def test_regular_edt_session(tmp_path) -> None:
    identity = build_temporal_identity("2026-07-15")
    assert _iso(identity.session_open_at) == "2026-07-15T13:30:00+00:00"
    assert _iso(identity.session_close_at) == "2026-07-15T20:00:00+00:00"


def test_early_close_session_keeps_0930_open(tmp_path) -> None:
    identity = build_temporal_identity("2026-11-27")
    assert _iso(identity.session_open_at) == "2026-11-27T14:30:00+00:00"
    assert _iso(identity.session_close_at) == "2026-11-27T18:00:00+00:00"


def test_holiday_rejected(tmp_path) -> None:
    with pytest.raises(RuntimeUnavailableError, match="not_a_trading_session"):
        build_temporal_identity("2026-11-26")  # Thanksgiving


def test_missing_session_date_rejected(tmp_path) -> None:
    with pytest.raises(RuntimeUnavailableError, match="session_date is required"):
        build_temporal_identity("")
