import pytest

from catalyst_data.retry import get_retry_policy


def test_polygon_retry_policy_matches_plan():
    policy = get_retry_policy("polygon")

    assert policy.rate_limit.base_seconds == 60.0
    assert policy.rate_limit.max_seconds == 300.0
    assert policy.rate_limit.max_retries == 3
    assert policy.server_error.base_seconds == 5.0
    assert policy.server_error.max_seconds == 60.0
    assert policy.server_error.max_retries == 5
    assert policy.timeout.max_retries == 1


def test_fmp_retry_policy_matches_plan():
    policy = get_retry_policy("fmp")

    assert policy.server_error.base_seconds == 5.0
    assert policy.server_error.max_retries == 3
    assert policy.timeout.base_seconds == 5.0
    assert policy.timeout.max_retries == 3


def test_fred_retry_policy_matches_plan():
    policy = get_retry_policy("fred")

    assert policy.server_error.base_seconds == 10.0
    assert policy.server_error.max_retries == 3


def test_unknown_provider_raises_clear_error():
    with pytest.raises(ValueError, match="Unknown provider"):
        get_retry_policy("unknown")
