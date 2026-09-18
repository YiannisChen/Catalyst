"""Shared provider budget guard at the provider invocation boundary (M7 Phase A).

The Stage-1 operator approves a total provider-call ceiling and (optionally) a
total USD ceiling for one run. A ceiling that is only checked between cases
cannot bound the work inside a case, so this guard is consulted at the exact
point where a provider call is about to be dispatched: every Analyst/Writer
provider attempt reserves budget first and fails closed when the remaining
budget cannot cover the next attempt.

Cost is only ever a *bound*, never a guess:

* the pre-call upper bound is ``max_input_tokens * input_price +
  max_output_tokens * output_price`` using an identity-bound price registered
  for the exact ``(provider, model_id)`` pair;
* if a USD ceiling is set and no price is registered for that identity, the
  guard fails closed rather than inventing a price or silently ignoring cost;
* after the call the reservation is settled with the genuinely reported token
  usage when the role reports it, and with the conservative bound otherwise
  (never under-charging).

Cost reservation is atomic with the call reservation: ``reserve`` holds both
the attempt and the pre-call cost bound inside one critical section, so two
concurrent reservations can never both pass a ceiling they jointly exceed. The
outstanding bound counts as committed budget in every ceiling check and in
``snapshot`` until the attempt is settled. ``settle`` replaces the bound with
the genuinely reported cost, keeps the bound when usage is unreported or only
partially reported, and may run exactly once per reservation.

The guard is thread-safe: the Analyst and Writer can be dispatched from
different workers, and ``provider_attempts`` must stay exact.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Mapping


class ProviderBudgetError(RuntimeError):
    """Base class for typed provider-budget failures."""


class ProviderBudgetExceeded(ProviderBudgetError):
    """The approved total budget cannot cover the next provider attempt."""


class ProviderPriceUnavailable(ProviderBudgetError):
    """No identity-bound price is registered, so cost cannot be bounded."""


@dataclass(frozen=True)
class ModelPrice:
    """Identity-bound price for one (provider, model_id) pair.

    Prices are USD per one million tokens, matching how providers publish them;
    the caller is responsible for registering only prices it can defend.
    """

    provider: str
    model_id: str
    input_usd_per_million_tokens: float
    output_usd_per_million_tokens: float

    def __post_init__(self) -> None:
        if self.input_usd_per_million_tokens < 0:
            raise ValueError("input price must be non-negative")
        if self.output_usd_per_million_tokens < 0:
            raise ValueError("output price must be non-negative")

    def bound_usd(self, *, max_input_tokens: int, max_output_tokens: int) -> float:
        if max_input_tokens < 0 or max_output_tokens < 0:
            raise ValueError("token bounds must be non-negative")
        return (
            max_input_tokens * self.input_usd_per_million_tokens
            + max_output_tokens * self.output_usd_per_million_tokens
        ) / 1_000_000.0

    def actual_usd(self, *, input_tokens: int, output_tokens: int) -> float:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token usage must be non-negative")
        return (
            input_tokens * self.input_usd_per_million_tokens
            + output_tokens * self.output_usd_per_million_tokens
        ) / 1_000_000.0


@dataclass(frozen=True)
class RoleTokenLimit:
    """Bounded token envelope for one role (pre-call upper bound inputs)."""

    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if self.max_input_tokens < 0:
            raise ValueError("max_input_tokens must be non-negative")
        if self.max_output_tokens < 0:
            raise ValueError("max_output_tokens must be non-negative")


@dataclass(frozen=True)
class ProviderReservation:
    """One reserved provider attempt.

    ``attempt`` is the guard-unique 1-based attempt number and doubles as the
    settlement handle: ``settle`` accepts each reservation exactly once.
    """

    role: str
    provider: str
    model_id: str
    attempt: int
    reserved_cost_usd: float | None
    price: ModelPrice | None


class ProviderBudgetGuard:
    """Shared, thread-safe provider-call/cost guard for one run."""

    def __init__(
        self,
        *,
        max_provider_calls: int,
        max_cost_usd: float | None = None,
        prices: Mapping[tuple[str, str], ModelPrice] | None = None,
        role_token_limits: Mapping[str, RoleTokenLimit] | None = None,
    ) -> None:
        if max_provider_calls < 0:
            raise ValueError("max_provider_calls must be non-negative")
        if max_cost_usd is not None and max_cost_usd < 0:
            raise ValueError("max_cost_usd must be non-negative")
        self.max_provider_calls = int(max_provider_calls)
        self.max_cost_usd = None if max_cost_usd is None else float(max_cost_usd)
        self._prices = dict(prices or {})
        self._role_limits = dict(role_token_limits or {})
        self._lock = threading.Lock()
        self._used_calls = 0
        self._used_cost = 0.0
        self._unreported_cost = False
        # Pre-call bounds held by reservations that have not settled yet. They
        # are committed budget: every ceiling check and snapshot includes them.
        self._outstanding_cost = 0.0
        self._outstanding_reservations = 0
        self._settled_attempts: set[int] = set()
        # Per-case view: bound by ``begin_case`` before each case dispatch.
        self._case_max_calls: int | None = None
        self._case_max_cost: float | None = None
        self._case_used_calls = 0
        self._case_used_cost = 0.0
        self._case_outstanding_cost = 0.0
        self._case_unreported_cost = False
        self._case_role_accounting: dict[str, dict[str, int]] = {}

    # -- registration ------------------------------------------------------

    def register_price(self, price: ModelPrice) -> None:
        with self._lock:
            self._prices[(price.provider, price.model_id)] = price

    def register_role_limit(self, role: str, limit: RoleTokenLimit) -> None:
        with self._lock:
            self._role_limits[role] = limit

# -- per-case binding --------------------------------------------------

    def begin_case(
        self, *, provider_calls: int, cost_usd: float | None = None
    ) -> None:
        """Bind the remaining total budget available to the next case.

        Called before each case dispatch. The per-case limits are enforced in
        addition to the cumulative limits, so a case can never exceed the
        budget the operator still has available at dispatch time.
        """
        if provider_calls < 0:
            raise ValueError("per-case provider-call budget must be non-negative")
        if cost_usd is not None and cost_usd < 0:
            raise ValueError("per-case cost budget must be non-negative")
        with self._lock:
            self._case_max_calls = int(provider_calls)
            self._case_max_cost = None if cost_usd is None else float(cost_usd)
            self._case_used_calls = 0
            self._case_used_cost = 0.0
            self._case_outstanding_cost = 0.0
            self._case_unreported_cost = False
            self._case_role_accounting = {}

    def record_logical_call(self, *, role: str) -> None:
        """Record one logical role call after its first attempt is reserved."""
        with self._lock:
            accounting = self._case_role_accounting.setdefault(
                role, {"logical_calls": 0, "provider_attempts": 0}
            )
            accounting["logical_calls"] += 1

    def case_snapshot(self) -> dict[str, object]:
        """Accounting for the current case (the run diagnostics view).

        ``case_outstanding_cost_usd`` is the pre-call bound still held by an
        unsettled reservation. It is committed budget but not yet a charged
        cost; ``case_committed_cost_usd`` is used + outstanding.
        """
        with self._lock:
            return {
                "case_max_provider_calls": self._case_max_calls,
                "case_max_cost_usd": self._case_max_cost,
                "case_used_provider_calls": self._case_used_calls,
                "case_used_cost_usd": self._case_used_cost,
                "case_outstanding_cost_usd": self._case_outstanding_cost,
                "case_committed_cost_usd": (
                    self._case_used_cost + self._case_outstanding_cost
                ),
                "cost_method": (
                    "unavailable"
                    if self.max_cost_usd is None
                    else (
                        "upper_bound_charged"
                        if self._case_unreported_cost
                        else "reported"
                    )
                ),
                "case_role_accounting": {
                    role: dict(values)
                    for role, values in self._case_role_accounting.items()
                },
            }

    def reconcile_case(
        self, *, provider_calls: int, cost_usd: float | None
    ) -> None:
        """Reconcile the diagnostics-reported case accounting with the guard.

        Raises when the two disagree, so a persistence or accounting gap can
        never pass silently. An unsettled reservation is a reconciliation
        failure: the case ended holding budget for an attempt whose settlement
        was never recorded.
        """
        with self._lock:
            if self._case_outstanding_cost > 0.0:
                raise ProviderBudgetError(
                    "case ended with "
                    f"{self._case_outstanding_cost:.9f} USD of unsettled "
                    "reservation bound; every dispatched provider attempt "
                    "must be settled"
                )
            if provider_calls != self._case_used_calls:
                raise ProviderBudgetError(
                    "provider-call accounting mismatch: diagnostics reported "
                    f"{provider_calls} but the guard recorded "
                    f"{self._case_used_calls}"
                )
            if self.max_cost_usd is not None:
                if cost_usd is None:
                    raise ProviderBudgetError(
                        "cost accounting is unknown/unavailable while a cost "
                        "ceiling is active"
                    )
                reported = float(cost_usd)
                if abs(reported - self._case_used_cost) > 1e-9:
                    raise ProviderBudgetError(
                        "cost accounting mismatch: diagnostics reported "
                        f"{reported:.9f} but the guard recorded "
                        f"{self._case_used_cost:.9f}"
                    )

    # -- reservation -------------------------------------------------------

    def reserve(self, *, role: str, provider: str, model_id: str) -> ProviderReservation:
        """Atomically reserve one provider attempt (call + cost bound) or fail closed.

        The call count, both ceiling checks, and the outstanding-bound update
        all happen inside one critical section, so two concurrent reservations
        can never both pass a ceiling their joint bound exceeds.
        """
        with self._lock:
            price = self._prices.get((provider, model_id))
            if self._used_calls >= self.max_provider_calls:
                raise ProviderBudgetExceeded(
                    "provider-call ceiling reached before dispatch: "
                    f"used={self._used_calls}/{self.max_provider_calls} "
                    f"role={role!r}"
                )
            if (
                self._case_max_calls is not None
                and self._case_used_calls >= self._case_max_calls
            ):
                raise ProviderBudgetExceeded(
                    "per-case provider-call budget reached before dispatch: "
                    f"case_used={self._case_used_calls}/{self._case_max_calls} "
                    f"role={role!r}"
                )
            reserved_cost: float | None = None
            if self.max_cost_usd is not None:
                if price is None:
                    raise ProviderPriceUnavailable(
                        "no identity-bound price registered for "
                        f"provider={provider!r} model_id={model_id!r}; refusing "
                        "to dispatch a cost-bounded provider call without a "
                        "defensible pre-call upper bound"
                    )
                limit = self._role_limits.get(role)
                if limit is None:
                    raise ProviderPriceUnavailable(
                        f"no bounded token limit registered for role {role!r}; "
                        "the pre-call cost upper bound is not computable"
                    )
                reserved_cost = price.bound_usd(
                    max_input_tokens=limit.max_input_tokens,
                    max_output_tokens=limit.max_output_tokens,
                )
                committed = self._used_cost + self._outstanding_cost
                if committed + reserved_cost > self.max_cost_usd:
                    raise ProviderBudgetExceeded(
                        "cost ceiling reached before dispatch: "
                        f"used={self._used_cost:.6f}+"
                        f"outstanding={self._outstanding_cost:.6f}+"
                        f"bound={reserved_cost:.6f} > "
                        f"max={self.max_cost_usd:.6f} role={role!r}"
                    )
                if self._case_max_cost is not None:
                    case_committed = (
                        self._case_used_cost + self._case_outstanding_cost
                    )
                    if case_committed + reserved_cost > self._case_max_cost:
                        raise ProviderBudgetExceeded(
                            "per-case cost budget reached before dispatch: "
                            f"case_used={self._case_used_cost:.6f}+"
                            f"case_outstanding={self._case_outstanding_cost:.6f}+"
                            f"bound={reserved_cost:.6f} > "
                            f"max={self._case_max_cost:.6f} role={role!r}"
                        )
            attempt = self._used_calls + 1
            self._used_calls = attempt
            self._case_used_calls += 1
            accounting = self._case_role_accounting.setdefault(
                role, {"logical_calls": 0, "provider_attempts": 0}
            )
            accounting["provider_attempts"] += 1
            if reserved_cost is not None:
                self._outstanding_cost += reserved_cost
                self._case_outstanding_cost += reserved_cost
                self._outstanding_reservations += 1
            return ProviderReservation(
                role=role,
                provider=provider,
                model_id=model_id,
                attempt=attempt,
                reserved_cost_usd=reserved_cost,
                price=price,
            )

    def settle(
        self,
        reservation: ProviderReservation,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Reconcile one reserved attempt with the genuinely reported usage.

        The reservation's pre-call bound is released and replaced by the
        reported cost when a complete token usage is reported; otherwise the
        conservative bound is charged (never under-charging). Each reservation
        settles exactly once; a second settlement fails closed.
        """
        with self._lock:
            if reservation.attempt in self._settled_attempts:
                raise ProviderBudgetError(
                    "reservation already settled: attempt "
                    f"{reservation.attempt} role={reservation.role!r}"
                )
            if reservation.reserved_cost_usd is None:
                # Call-only ceiling: nothing was reserved for cost, so there is
                # no cost to reconcile.
                self._settled_attempts.add(reservation.attempt)
                return
            price = reservation.price
            assert price is not None  # reserved_cost_usd implies a price
            fully_reported = input_tokens is not None and output_tokens is not None
            if fully_reported:
                actual = price.actual_usd(
                    input_tokens=input_tokens, output_tokens=output_tokens
                )
            else:
                actual = reservation.reserved_cost_usd

            self._outstanding_cost -= reservation.reserved_cost_usd
            self._case_outstanding_cost -= reservation.reserved_cost_usd
            self._outstanding_reservations -= 1
            self._settled_attempts.add(reservation.attempt)
            self._used_cost += actual
            self._case_used_cost += actual
            if not fully_reported:
                self._unreported_cost = True
                self._case_unreported_cost = True
                return
            if actual > reservation.reserved_cost_usd + 1e-12:
                raise ProviderBudgetExceeded(
                    "actual provider cost exceeded the reserved pre-call upper "
                    f"bound: actual={actual:.6f} bound="
                    f"{reservation.reserved_cost_usd:.6f} role={reservation.role!r}"
                )

    # -- observed state ----------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            committed_cost = self._used_cost + self._outstanding_cost
            return {
                "max_provider_calls": self.max_provider_calls,
                "max_cost_usd": self.max_cost_usd,
                "used_provider_calls": self._used_calls,
                "used_cost_usd": self._used_cost,
                "outstanding_cost_usd": self._outstanding_cost,
                "outstanding_reservations": self._outstanding_reservations,
                "committed_cost_usd": committed_cost,
                "remaining_provider_calls": self.max_provider_calls - self._used_calls,
                "remaining_cost_usd": (
                    None
                    if self.max_cost_usd is None
                    else self.max_cost_usd - committed_cost
                ),
                "cost_method": "unavailable" if self.max_cost_usd is None else (
                    "upper_bound_charged" if self._unreported_cost else "reported"
                ),
            }

    @property
    def used_provider_calls(self) -> int:
        with self._lock:
            return self._used_calls

    @property
    def used_cost_usd(self) -> float:
        with self._lock:
            return self._used_cost


__all__ = [
    "ModelPrice",
    "ProviderBudgetError",
    "ProviderBudgetExceeded",
    "ProviderBudgetGuard",
    "ProviderPriceUnavailable",
    "ProviderReservation",
    "RoleTokenLimit",
]
