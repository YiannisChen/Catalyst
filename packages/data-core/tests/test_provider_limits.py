from catalyst_data.provider_limits import FMP, FRED, POLYGON


def test_polygon_limits_match_plan():
    assert POLYGON == {"rate_per_min": 5, "burst": 5, "concurrency": 1}


def test_fmp_limits_match_plan():
    assert FMP == {"rate_per_day": 250, "concurrency": 2}


def test_fred_limits_match_plan():
    assert FRED == {"rate_per_min": 120, "concurrency": 3}
