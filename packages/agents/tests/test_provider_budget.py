"""Shared provider budget guard contract (M7 Phase A).

The guard is the only thing standing between the approved Stage-1 ceilings and
an unbounded provider bill, so its behaviour is pinned here in its owning
package: reservation happens *before* dispatch, per-case and cumulative
ceilings are both enforced, cost is only ever bounded by an identity-bound
price plus a bounded token envelope, and unreconciled accounting fails closed.
"""
from __future__ import annotations

import threading

import pytest

from catalyst_agents.runtime.provider_budget import (
    ModelPrice,
    ProviderBudgetError,
    ProviderBudgetExceeded,
    ProviderBudgetGuard,
    ProviderPriceUnavailable,
    RoleTokenLimit,
)

ROLE = "evidence_analyst"
PROVIDER = "deepseek"
MODEL = "deepseek-chat"


def _price(
    *, input_per_million: float = 1.0, output_per_million: float = 2.0
) -> ModelPrice:
    return ModelPrice(
        provider=PROVIDER,
        model_id=MODEL,
        input_usd_per_million_tokens=input_per_million,
        output_usd_per_million_tokens=output_per_million,
    )


def _guard(
    *,
    max_calls: int = 10,
    max_cost: float | None = 1.0,
    limit: RoleTokenLimit | None = RoleTokenLimit(1000, 500),
) -> ProviderBudgetGuard:
    prices = {} if max_cost is None else {(PROVIDER, MODEL): _price()}
    limits = {} if limit is None else {ROLE: limit}
    return ProviderBudgetGuard(
        max_provider_calls=max_calls, max_cost_usd=max_cost, prices=prices,
        role_token_limits=limits,
    )


def _reserve(guard: ProviderBudgetGuard):
    return guard.reserve(role=ROLE, provider=PROVIDER, model_id=MODEL)


# ---------------------------------------------------------------------------
# reservation and settlement
# ---------------------------------------------------------------------------

def test_reserve_consumes_a_call_before_dispatch():
    guard = _guard()
    guard.begin_case(provider_calls=5, cost_usd=1.0)
    assert guard.used_provider_calls == 0
    reservation = _reserve(guard)
    # The call is consumed by the reservation alone: a crash after dispatch
    # still counts it against the budget.
    assert guard.used_provider_calls == 1
    assert reservation.attempt == 1
    assert reservation.reserved_cost_usd == pytest.approx(
        (1000 * 1.0 + 500 * 2.0) / 1_000_000.0
    )


def test_settle_with_reported_usage_charges_the_actual_cost():
    guard = _guard()
    guard.begin_case(provider_calls=5, cost_usd=1.0)
    reservation = _reserve(guard)
    guard.settle(reservation, input_tokens=100, output_tokens=50)
    snapshot = guard.case_snapshot()
    assert snapshot["case_used_cost_usd"] == pytest.approx(
        (100 * 1.0 + 50 * 2.0) / 1_000_000.0
    )
    assert snapshot["cost_method"] == "reported"
    guard.reconcile_case(
        provider_calls=1, cost_usd=snapshot["case_used_cost_usd"]
    )


def test_settle_without_reported_usage_charges_the_bound_and_labels_it():
    guard = _guard()
    guard.begin_case(provider_calls=5, cost_usd=1.0)
    reservation = _reserve(guard)
    guard.settle(reservation)  # no usage reported
    snapshot = guard.case_snapshot()
    assert snapshot["case_used_cost_usd"] == reservation.reserved_cost_usd
    assert snapshot["cost_method"] == "upper_bound_charged"


def test_reported_usage_above_the_reserved_bound_fails_closed():
    guard = _guard(limit=RoleTokenLimit(10, 10))
    guard.begin_case(provider_calls=5, cost_usd=1.0)
    reservation = _reserve(guard)
    with pytest.raises(ProviderBudgetExceeded, match="upper\\s+bound|bound"):
        guard.settle(reservation, input_tokens=10_000, output_tokens=10_000)


def test_reported_cost_overrun_is_persisted_before_fail_closed():
    """An over-bound provider bill is real consumption, not zero accounting."""
    guard = _guard(limit=RoleTokenLimit(10, 10))
    guard.begin_case(provider_calls=5, cost_usd=1.0)
    reservation = _reserve(guard)
    actual_cost = (10_000 * 1.0 + 10_000 * 2.0) / 1_000_000.0

    with pytest.raises(ProviderBudgetExceeded):
        guard.settle(reservation, input_tokens=10_000, output_tokens=10_000)

    snapshot = guard.case_snapshot()
    assert snapshot["case_used_cost_usd"] == pytest.approx(actual_cost)
    assert snapshot["case_outstanding_cost_usd"] == pytest.approx(0.0)
    assert snapshot["cost_method"] == "reported"


# ---------------------------------------------------------------------------
# ceilings
# ---------------------------------------------------------------------------

def test_cumulative_call_ceiling_blocks_the_next_reservation():
    guard = _guard(max_calls=2, max_cost=None)
    guard.begin_case(provider_calls=5, cost_usd=None)
    _reserve(guard)
    _reserve(guard)
    with pytest.raises(ProviderBudgetExceeded, match="provider-call ceiling"):
        _reserve(guard)
    assert guard.used_provider_calls == 2


def test_per_case_call_budget_blocks_before_the_cumulative_ceiling():
    guard = _guard(max_calls=10, max_cost=None)
    guard.begin_case(provider_calls=1, cost_usd=None)
    _reserve(guard)
    with pytest.raises(ProviderBudgetExceeded, match="per-case provider-call"):
        _reserve(guard)
    # A new case re-binds the per-case budget without resetting the total.
    guard.begin_case(provider_calls=1, cost_usd=None)
    _reserve(guard)
    assert guard.used_provider_calls == 2


def test_cost_ceiling_blocks_before_dispatch_when_the_bound_would_exceed():
    guard = _guard(max_calls=10, max_cost=0.001)
    guard.begin_case(provider_calls=10, cost_usd=0.001)
    # Each bound is (1000 + 2*500)/1e6 = 0.002 > 0.001, so nothing dispatches.
    with pytest.raises(ProviderBudgetExceeded, match="cost ceiling"):
        _reserve(guard)
    assert guard.used_provider_calls == 0


def test_case_cost_budget_blocks_before_dispatch():
    guard = _guard(max_calls=10, max_cost=1.0)
    guard.begin_case(provider_calls=10, cost_usd=0.001)
    with pytest.raises(ProviderBudgetExceeded, match="per-case cost budget"):
        _reserve(guard)
    assert guard.used_provider_calls == 0


def test_zero_case_budget_never_authorizes_a_dispatch():
    guard = _guard(max_calls=10, max_cost=1.0)
    guard.begin_case(provider_calls=0, cost_usd=1.0)
    with pytest.raises(ProviderBudgetExceeded):
        _reserve(guard)
    assert guard.used_provider_calls == 0


# ---------------------------------------------------------------------------
# identity-bound cost inputs (fail closed, never guess)
# ---------------------------------------------------------------------------

def test_unpriced_model_fails_closed_instead_of_guessing():
    guard = ProviderBudgetGuard(
        max_provider_calls=4,
        max_cost_usd=1.0,
        prices={},
        role_token_limits={ROLE: RoleTokenLimit(10, 10)},
    )
    guard.begin_case(provider_calls=4, cost_usd=1.0)
    with pytest.raises(ProviderPriceUnavailable, match="price"):
        _reserve(guard)
    assert guard.used_provider_calls == 0


def test_missing_role_token_limit_fails_closed():
    guard = _guard(limit=None)
    guard.begin_case(provider_calls=4, cost_usd=1.0)
    with pytest.raises(ProviderPriceUnavailable, match="token limit"):
        _reserve(guard)
    assert guard.used_provider_calls == 0


def test_call_only_ceiling_needs_no_price_or_token_limit():
    guard = _guard(max_cost=None, limit=None)
    guard.begin_case(provider_calls=2, cost_usd=None)
    reservation = _reserve(guard)
    assert reservation.reserved_cost_usd is None
    guard.settle(reservation, input_tokens=10, output_tokens=10)
    assert guard.case_snapshot()["cost_method"] == "unavailable"


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------

def test_reconcile_rejects_a_call_count_mismatch():
    guard = _guard()
    guard.begin_case(provider_calls=5, cost_usd=1.0)
    guard.settle(_reserve(guard), input_tokens=1, output_tokens=1)
    with pytest.raises(ProviderBudgetError, match="call accounting mismatch"):
        guard.reconcile_case(provider_calls=2, cost_usd=0.0)


def test_reconcile_rejects_a_cost_mismatch():
    guard = _guard()
    guard.begin_case(provider_calls=5, cost_usd=1.0)
    guard.settle(_reserve(guard), input_tokens=100, output_tokens=100)
    with pytest.raises(ProviderBudgetError, match="cost accounting mismatch"):
        guard.reconcile_case(provider_calls=1, cost_usd=0.5)


def test_reconcile_rejects_unknown_cost_when_cost_ceiling_is_active():
    guard = _guard()
    guard.begin_case(provider_calls=5, cost_usd=1.0)
    guard.settle(_reserve(guard), input_tokens=100, output_tokens=100)
    with pytest.raises(ProviderBudgetError, match="unknown|unavailable"):
        guard.reconcile_case(provider_calls=1, cost_usd=None)


def test_reconcile_accepts_exact_accounting():
    guard = _guard()
    guard.begin_case(provider_calls=5, cost_usd=1.0)
    guard.settle(_reserve(guard), input_tokens=100, output_tokens=100)
    guard.settle(_reserve(guard), input_tokens=10, output_tokens=10)
    snapshot = guard.case_snapshot()
    guard.reconcile_case(
        provider_calls=2, cost_usd=snapshot["case_used_cost_usd"]
    )


# ---------------------------------------------------------------------------
# shared across workers
# ---------------------------------------------------------------------------

def test_guard_is_shared_and_exact_across_threads():
    """Concurrent reservations can never exceed the approved total."""
    guard = _guard(max_calls=5, max_cost=None)
    guard.begin_case(provider_calls=5, cost_usd=None)
    granted: list[int] = []
    refused: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            reservation = _reserve(guard)
        except ProviderBudgetExceeded:
            with lock:
                refused.append("refused")
            return
        with lock:
            granted.append(reservation.attempt)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(granted) == 5
    assert sorted(granted) == [1, 2, 3, 4, 5]
    assert len(refused) == 11
    assert guard.used_provider_calls == 5


def test_snapshot_reports_remaining_budget():
    guard = _guard(max_calls=4, max_cost=1.0)
    guard.begin_case(provider_calls=4, cost_usd=1.0)
    guard.settle(_reserve(guard), input_tokens=100, output_tokens=100)
    snapshot = guard.snapshot()
    assert snapshot["remaining_provider_calls"] == 3
    assert snapshot["used_provider_calls"] == 1
    assert snapshot["remaining_cost_usd"] == pytest.approx(
        1.0 - snapshot["used_cost_usd"]
    )


# ---------------------------------------------------------------------------
# atomic cost reservation (supervisor blocker A)
# ---------------------------------------------------------------------------

def _dollar_bound_guard(*, max_cost: float | None, limits: Mapping[str, RoleTokenLimit]):
    """Guard whose pre-call bound is exactly $1.00 per attempt.

    price = 1 USD per million input tokens, limit = 1_000_000 input tokens and
    zero output tokens, so ``bound_usd`` is exactly 1.0.
    """
    return ProviderBudgetGuard(
        max_provider_calls=10,
        max_cost_usd=max_cost,
        prices={(PROVIDER, MODEL): _price(input_per_million=1.0, output_per_million=0.0)},
        role_token_limits=dict(limits),
    )


def _one_dollar_limit() -> RoleTokenLimit:
    return RoleTokenLimit(1_000_000, 0)


def test_two_concurrent_reservations_split_a_tight_total_ceiling():
    """Two $1.00 attempts against a $1.50 ceiling: exactly one may dispatch.

    The reservation must both check *and* hold the pre-call cost bound inside
    one critical section; a check that only compares committed cost lets both
    threads pass because neither has settled yet.
    """
    guard = _dollar_bound_guard(
        max_cost=1.50, limits={ROLE: _one_dollar_limit(), "writer": _one_dollar_limit()}
    )
    guard.begin_case(provider_calls=10, cost_usd=1.50)
    barrier = threading.Barrier(2)
    granted: list[str] = []
    refused: list[str] = []
    lock = threading.Lock()

    def worker(role: str) -> None:
        barrier.wait()
        try:
            guard.reserve(role=role, provider=PROVIDER, model_id=MODEL)
        except ProviderBudgetExceeded:
            with lock:
                refused.append(role)
            return
        with lock:
            granted.append(role)

    threads = [
        threading.Thread(target=worker, args=(role,)) for role in (ROLE, "writer")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(granted) == 1
    assert len(refused) == 1
    assert guard.used_provider_calls == 1


def test_two_concurrent_reservations_split_a_tight_per_case_ceiling():
    """The per-case cost ceiling is enforced with the same atomicity."""
    guard = _dollar_bound_guard(
        max_cost=10.0, limits={ROLE: _one_dollar_limit(), "writer": _one_dollar_limit()}
    )
    guard.begin_case(provider_calls=10, cost_usd=1.50)
    barrier = threading.Barrier(2)
    granted: list[str] = []
    lock = threading.Lock()

    def worker(role: str) -> None:
        barrier.wait()
        try:
            guard.reserve(role=role, provider=PROVIDER, model_id=MODEL)
        except ProviderBudgetExceeded:
            return
        with lock:
            granted.append(role)

    threads = [
        threading.Thread(target=worker, args=(role,)) for role in (ROLE, "writer")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(granted) == 1


def test_snapshot_counts_outstanding_reservations_as_committed_budget():
    """An unsettled reservation must appear in the remaining-cost snapshot."""
    guard = _dollar_bound_guard(max_cost=5.0, limits={ROLE: _one_dollar_limit()})
    guard.begin_case(provider_calls=5, cost_usd=5.0)
    reservation = _reserve(guard)
    snapshot = guard.snapshot()
    assert snapshot["used_cost_usd"] == pytest.approx(0.0)
    assert snapshot["outstanding_cost_usd"] == pytest.approx(1.0)
    assert snapshot["remaining_cost_usd"] == pytest.approx(4.0)
    assert snapshot["outstanding_reservations"] == 1
    guard.settle(reservation, input_tokens=0, output_tokens=0)
    assert guard.snapshot()["outstanding_cost_usd"] == pytest.approx(0.0)


def test_double_settlement_fails_closed():
    """A reservation may be settled exactly once."""
    guard = _dollar_bound_guard(max_cost=5.0, limits={ROLE: _one_dollar_limit()})
    guard.begin_case(provider_calls=5, cost_usd=5.0)
    reservation = _reserve(guard)
    guard.settle(reservation, input_tokens=0, output_tokens=0)
    with pytest.raises(ProviderBudgetError, match="already settled|double"):
        guard.settle(reservation, input_tokens=0, output_tokens=0)
    # The failed second settlement must not have charged the budget twice.
    assert guard.used_cost_usd == pytest.approx(0.0)
    assert guard.snapshot()["outstanding_cost_usd"] == pytest.approx(0.0)


def test_settle_charges_the_bound_when_usage_is_partially_reported():
    """Half-reported usage is not usable evidence; charge the bound."""
    guard = _dollar_bound_guard(max_cost=5.0, limits={ROLE: _one_dollar_limit()})
    guard.begin_case(provider_calls=5, cost_usd=5.0)
    guard.settle(_reserve(guard), input_tokens=10)
    assert guard.used_cost_usd == pytest.approx(1.0)
    assert guard.case_snapshot()["cost_method"] == "upper_bound_charged"


# ---------------------------------------------------------------------------
# invocation-boundary accounting (supervisor blocker A)
# ---------------------------------------------------------------------------

def test_non_retryable_provider_exception_still_consumes_call_and_bound():
    """A dispatched attempt that raises a non-retryable error is still billed."""
    from catalyst_agents.runtime.provider_capability import invoke_with_bounded_retry

    guard = _dollar_bound_guard(max_cost=5.0, limits={ROLE: _one_dollar_limit()})
    guard.begin_case(provider_calls=5, cost_usd=5.0)

    class NonRetryable(Exception):
        pass

    def boom():
        raise NonRetryable("provider exploded after dispatch")

    with pytest.raises(NonRetryable):
        invoke_with_bounded_retry(
            boom,
            role=ROLE,
            semantic_input_hash="h" * 64,
            budget=guard,
            provider=PROVIDER,
            model_id=MODEL,
        )
    snapshot = guard.case_snapshot()
    assert snapshot["case_used_provider_calls"] == 1
    assert snapshot["case_used_cost_usd"] == pytest.approx(1.0)
    assert snapshot["case_outstanding_cost_usd"] == pytest.approx(0.0)
    guard.reconcile_case(provider_calls=1, cost_usd=1.0)


def test_retryable_provider_exception_charges_each_attempt_separately():
    """Each retry attempt reserves and settles its own budget."""
    from catalyst_agents.runtime.provider_capability import (
        ModelTransportFailure,
        invoke_with_bounded_retry,
    )

    guard = _dollar_bound_guard(max_cost=5.0, limits={ROLE: _one_dollar_limit()})
    guard.begin_case(provider_calls=5, cost_usd=5.0)
    calls: list[int] = []

    def flaky():
        calls.append(1)
        raise ModelTransportFailure("transport down")

    with pytest.raises(Exception):
        invoke_with_bounded_retry(
            flaky,
            role=ROLE,
            semantic_input_hash="h" * 64,
            budget=guard,
            provider=PROVIDER,
            model_id=MODEL,
        )
    assert len(calls) == 2  # one logical call, one bounded retry
    snapshot = guard.case_snapshot()
    assert snapshot["case_used_provider_calls"] == 2
    assert snapshot["case_used_cost_usd"] == pytest.approx(2.0)
    assert snapshot["case_outstanding_cost_usd"] == pytest.approx(0.0)


def test_failed_retry_accounting_separates_logical_calls_from_attempts():
    """Two failed Analyst attempts are one logical call, not two logical calls."""
    from catalyst_agents.runtime.provider_capability import (
        ModelTransportFailure,
        TechnicalRetryExhausted,
        invoke_with_bounded_retry,
    )

    guard = _dollar_bound_guard(max_cost=5.0, limits={ROLE: _one_dollar_limit()})
    guard.begin_case(provider_calls=5, cost_usd=5.0)

    def failing():
        raise ModelTransportFailure("provider unavailable")

    with pytest.raises(TechnicalRetryExhausted):
        invoke_with_bounded_retry(
            failing,
            role=ROLE,
            semantic_input_hash="h" * 64,
            budget=guard,
            provider=PROVIDER,
            model_id=MODEL,
        )

    assert guard.case_snapshot()["case_role_accounting"] == {
        ROLE: {"logical_calls": 1, "provider_attempts": 2}
    }


def test_reservation_failure_never_charges_an_undispatched_attempt():
    """A refused reservation dispatches nothing and consumes nothing."""
    guard = _dollar_bound_guard(max_cost=0.50, limits={ROLE: _one_dollar_limit()})
    guard.begin_case(provider_calls=5, cost_usd=0.50)
    with pytest.raises(ProviderBudgetExceeded):
        _reserve(guard)
    snapshot = guard.case_snapshot()
    assert snapshot["case_used_provider_calls"] == 0
    assert snapshot["case_used_cost_usd"] == pytest.approx(0.0)
    assert snapshot["case_outstanding_cost_usd"] == pytest.approx(0.0)
