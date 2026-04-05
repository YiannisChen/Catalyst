from __future__ import annotations

import pytest

from scripts.smoke_test import resolve_sources


def test_resolve_sources_returns_available_when_not_requested():
    available = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals"]

    assert resolve_sources(None, available) == available


def test_resolve_sources_filters_requested_sources_in_order():
    available = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals"]

    resolved = resolve_sources("fmp_fundamentals,polygon_news", available)

    assert resolved == ["fmp_fundamentals", "polygon_news"]


def test_resolve_sources_rejects_unknown_source():
    available = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals"]

    with pytest.raises(ValueError, match="Unknown logical source"):
        resolve_sources("not_a_source", available)


def test_resolve_sources_rejects_unavailable_source():
    available = ["polygon_news", "polygon_ohlcv"]

    with pytest.raises(ValueError, match="not available"):
        resolve_sources("fmp_fundamentals", available)
