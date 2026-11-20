from __future__ import annotations

import pytest
from catalyst_agents.runtime.provider_budget import (
    ModelPrice,
    ProviderBudgetExceeded,
    ProviderBudgetGuard,
    ProviderTokenLimitUnavailable,
    RoleTokenLimit,
)

ROLE = "writer"


def _guard(*, limit: RoleTokenLimit | None = RoleTokenLimit(80, 20), price: bool = False):
    return ProviderBudgetGuard(
        max_provider_calls=10,
        max_tokens=100,
        prices=(
            {("p", "m"): ModelPrice("p", "m", 2.0, 4.0)} if price else {}
        ),
        role_token_limits={} if limit is None else {ROLE: limit},
    )


def test_token_reservation_is_atomic_and_released_on_settlement():
    guard = _guard()
    first = guard.reserve(role=ROLE, provider="p", model_id="unknown")
    assert guard.snapshot()["outstanding_tokens"] == 100
    with pytest.raises(ProviderBudgetExceeded, match="token ceiling"):
        guard.reserve(role=ROLE, provider="p", model_id="unknown")
    guard.settle(first, input_tokens=30, output_tokens=10)
    assert guard.snapshot()["used_tokens"] == 40
    assert guard.snapshot()["outstanding_tokens"] == 0
    assert guard.snapshot()["remaining_tokens"] == 60


def test_missing_role_limit_has_token_specific_error():
    guard = _guard(limit=None)
    with pytest.raises(ProviderTokenLimitUnavailable):
        guard.reserve(role=ROLE, provider="p", model_id="unknown")


def test_unreported_usage_is_charged_at_reserved_token_bound():
    guard = _guard(limit=RoleTokenLimit(40, 10))
    reservation = guard.reserve(role=ROLE, provider="p", model_id="unknown")
    guard.settle(reservation, input_tokens=40)
    snapshot = guard.case_snapshot()
    assert snapshot["used_tokens"] == 50
    assert snapshot["token_method"] == "upper_bound_charged"
    assert snapshot["cost_method"] == "unavailable"


def test_known_price_is_accounted_without_a_cost_ceiling():
    guard = _guard(limit=RoleTokenLimit(40, 10), price=True)
    reservation = guard.reserve(role=ROLE, provider="p", model_id="m")
    guard.settle(reservation, input_tokens=10, output_tokens=5)
    snapshot = guard.case_snapshot()
    assert snapshot["case_used_cost_usd"] == pytest.approx(0.00004)
    assert snapshot["cost_method"] == "reported"


def test_case_token_counters_restart_at_begin_case():
    guard = _guard()
    reservation = guard.reserve(role=ROLE, provider="p", model_id="unknown")
    guard.settle(reservation, input_tokens=30, output_tokens=10)
    assert guard.case_snapshot()["used_tokens"] == 40

    guard.begin_case(provider_calls=10)
    snapshot = guard.case_snapshot()
    assert snapshot["used_tokens"] == 0
    assert snapshot["outstanding_tokens"] == 0
    assert snapshot["remaining_tokens"] == 100

