"""Atomic UTC-day caller quotas and model-cost reservations."""
from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from catalyst_data.ingestion.tavily_ledger import (
    DEFAULT_DAILY_TAVILY_CALL_LIMIT,
    daily_limit_from_environment,
    public_enabled_from_environment,
)

_MICROS_PER_USD = 1_000_000
DEFAULT_DAILY_MODEL_BUDGET_USD = 2.00
DEFAULT_ANONYMOUS_DAILY_LIMIT = 5
DEFAULT_INVITE_DAILY_LIMIT = 50
# Own-key (BYOK) runs from anonymous callers are counted apart from the free demo (2026-11-18).
DEFAULT_ANONYMOUS_BYOK_DAILY_LIMIT = 20
_SERVICE_FAILURES_REFUNDING_CALLER = frozenset(
    {"SYSTEM_ERROR", "PROCESS_RECOVERY", "SUBMISSION_FAILURE", "PROVIDER_UNAVAILABLE", "DATA_UNAVAILABLE"}
)


def _utc_now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("budget time must be timezone-aware")
    return result.astimezone(timezone.utc)


def _micros(value: float) -> int:
    if isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
        raise ValueError("budget cost must be finite and non-negative")
    try:
        amount = Decimal(str(value)) * _MICROS_PER_USD
    except InvalidOperation as exc:
        raise ValueError("budget cost must be finite and non-negative") from exc
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class BudgetAdmission:
    tier: str
    caller_key: str | None
    max_cost_usd: float
    global_budget: bool = True
    max_tokens: int | None = None


class BudgetLedger:
    """Evaluate and persist public quotas in the caller's admission transaction."""

    def __init__(
        self,
        *,
        daily_model_budget_usd: float = DEFAULT_DAILY_MODEL_BUDGET_USD,
        max_run_cost_usd: float = 0.05,
        anonymous_daily_limit: int = DEFAULT_ANONYMOUS_DAILY_LIMIT,
        invite_daily_limit: int = DEFAULT_INVITE_DAILY_LIMIT,
        daily_tavily_call_limit: int = DEFAULT_DAILY_TAVILY_CALL_LIMIT,
        public_tavily_enabled: bool = False,
        anonymous_byok_daily_limit: int = DEFAULT_ANONYMOUS_BYOK_DAILY_LIMIT,
    ) -> None:
        if (
            not math.isfinite(float(daily_model_budget_usd))
            or not math.isfinite(float(max_run_cost_usd))
            or daily_model_budget_usd <= 0
            or max_run_cost_usd <= 0
        ):
            raise ValueError("budget limits must be finite and positive")
        if anonymous_daily_limit < 0 or invite_daily_limit < 1:
            raise ValueError("anonymous caller limit must be non-negative and invite limit positive")
        if anonymous_byok_daily_limit < 1:
            raise ValueError("anonymous own-key limit must be positive")
        if daily_tavily_call_limit < 1:
            raise ValueError("tavily call limit must be positive")
        self.daily_model_budget_micros = _micros(daily_model_budget_usd)
        self.max_run_cost_usd = float(max_run_cost_usd)
        self.anonymous_daily_limit = int(anonymous_daily_limit)
        self.invite_daily_limit = int(invite_daily_limit)
        self.anonymous_byok_daily_limit = int(anonymous_byok_daily_limit)
        self.daily_tavily_call_limit = int(daily_tavily_call_limit)
        self.public_tavily_enabled = bool(public_tavily_enabled)

    @classmethod
    def from_environment(cls) -> "BudgetLedger":
        def number(name: str, default: float) -> float:
            raw = os.environ.get(name)
            try:
                return default if raw is None else float(raw)
            except ValueError as exc:
                raise RuntimeError(f"invalid numeric budget configuration: {name}") from exc

        def integer(name: str, default: int) -> int:
            raw = os.environ.get(name)
            try:
                return default if raw is None else int(raw)
            except ValueError as exc:
                raise RuntimeError(f"invalid integer budget configuration: {name}") from exc

        return cls(
            daily_model_budget_usd=number(
                "CATALYST_DAILY_MODEL_BUDGET_USD", DEFAULT_DAILY_MODEL_BUDGET_USD
            ),
            max_run_cost_usd=number("CATALYST_MAX_RUN_MODEL_COST_USD", 0.05),
            anonymous_daily_limit=integer(
                "CATALYST_ANONYMOUS_DAILY_RUN_LIMIT", DEFAULT_ANONYMOUS_DAILY_LIMIT
            ),
            invite_daily_limit=integer(
                "CATALYST_INVITE_DAILY_RUN_LIMIT", DEFAULT_INVITE_DAILY_LIMIT
            ),
            anonymous_byok_daily_limit=integer(
                "CATALYST_ANONYMOUS_BYOK_DAILY_RUN_LIMIT", DEFAULT_ANONYMOUS_BYOK_DAILY_LIMIT
            ),
            daily_tavily_call_limit=daily_limit_from_environment(),
            public_tavily_enabled=public_enabled_from_environment(),
        )

    def _caller_limit(self, request: BudgetAdmission) -> tuple[int, str]:
        """Daily limit and SQL filter for this caller: anonymous free-demo (server key) runs and anonymous
        own-key runs are counted apart; invite runs share one count."""
        if request.tier == "anonymous":
            if request.global_budget:
                return self.anonymous_daily_limit, " AND global_budget_charged=1"
            return self.anonymous_byok_daily_limit, " AND global_budget_charged=0"
        return self.invite_daily_limit, ""

    @staticmethod
    def _used_today(
        conn: sqlite3.Connection, utc_day: str, request: BudgetAdmission, scope: str
    ) -> int:
        used = conn.execute(
            "SELECT COUNT(*) FROM run_budget_reservations "
            "WHERE utc_day=? AND tier=? AND caller_key=? AND quota_charged=1" + scope,
            (utc_day, request.tier, request.caller_key),
        ).fetchone()[0]
        return int(used)

    def check(
        self,
        conn: sqlite3.Connection,
        request: BudgetAdmission,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Return a contract denial code, or None when reservation may proceed."""
        if request.tier not in {"anonymous", "invite", "admin"}:
            raise ValueError("unknown access tier")
        if (not math.isfinite(float(request.max_cost_usd)) or request.max_cost_usd < 0
                or (request.global_budget and request.max_cost_usd == 0)):
            raise ValueError("max_cost_usd must be finite and non-negative")
        if request.max_cost_usd > self.max_run_cost_usd + 1e-12:
            raise ValueError("max_cost_usd must be clamped before admission")
        if request.tier == "anonymous" and not request.caller_key:
            raise ValueError("anonymous quota requires an IP hash")
        if request.tier == "invite" and not request.caller_key:
            raise ValueError("invite quota requires a token id")

        current = _utc_now(now)
        utc_day = current.date().isoformat()
        self.settle_terminal_runs(conn)
        if request.tier != "admin":
            limit, scope = self._caller_limit(request)
            used = self._used_today(conn, utc_day, request, scope)
            if used >= limit:
                return "rate_limited_ip" if request.tier == "anonymous" else "rate_limited_token"

            global_budget = request.global_budget and request.tier != "admin"
            if global_budget:
                reserved = _micros(request.max_cost_usd)
                used_cost = conn.execute(
                    "SELECT COALESCE(SUM(COALESCE(settled_cost_micros, reserved_cost_micros)), 0) "
                    "FROM run_budget_reservations WHERE utc_day=? AND global_budget_charged=1",
                    (utc_day,),
                ).fetchone()[0]
                if int(used_cost) + reserved > self.daily_model_budget_micros:
                    return "daily_budget_exhausted"
        return None

    def reserve(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        request: BudgetAdmission,
        *,
        now: datetime | None = None,
    ) -> None:
        current = _utc_now(now)
        quota_charged = request.tier != "admin"
        global_charged = request.global_budget and request.tier != "admin"
        # Keep the reservation row for admin own-key runs too: the adapter
        # reads max_run_tokens from this row when it constructs the run guard.
        conn.execute(
            "INSERT INTO run_budget_reservations "
            "(run_id, utc_day, tier, caller_key, quota_charged, global_budget_charged, "
            "max_run_cost_micros, max_run_tokens, reserved_cost_micros, settled_cost_micros, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                run_id,
                current.date().isoformat(),
                request.tier,
                request.caller_key,
                int(quota_charged),
                int(global_charged),
                _micros(request.max_cost_usd),
                request.max_tokens,
                _micros(request.max_cost_usd) if global_charged else 0,
                current.isoformat(),
            ),
        )

    def remaining(
        self, conn: sqlite3.Connection, request: BudgetAdmission, *, now: datetime | None = None
    ) -> int:
        if request.tier == "admin":
            return -1
        current = _utc_now(now)
        limit, scope = self._caller_limit(request)
        used = self._used_today(conn, current.date().isoformat(), request, scope)
        return max(0, limit - used)

    def settle(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        *,
        actual_cost_usd: float | None,
        refund_caller: bool = False,
        allow_costed_caller_refund: bool = False,
    ) -> None:
        row = conn.execute(
            "SELECT reserved_cost_micros, settled_cost_micros "
            "FROM run_budget_reservations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None or row["settled_cost_micros"] is not None:
            return
        reserved = int(row["reserved_cost_micros"])
        settled = reserved if actual_cost_usd is None else _micros(actual_cost_usd)
        may_refund_caller = refund_caller and (
            (actual_cost_usd is not None and settled == 0) or allow_costed_caller_refund
        )
        conn.execute(
            "UPDATE run_budget_reservations SET settled_cost_micros=?, "
            "quota_charged=CASE WHEN ? THEN 0 ELSE quota_charged END WHERE run_id=?",
            (settled, int(may_refund_caller), run_id),
        )

    def settle_terminal_runs(self, conn: sqlite3.Connection) -> None:
        """Settle terminal reservations missed by a prior process, idempotently."""
        rows = conn.execute(
            "SELECT b.run_id, b.reserved_cost_micros, r.lifecycle_status, r.failure_code, "
            "(SELECT a.payload_json FROM run_artifacts a WHERE a.run_id=b.run_id "
            "AND a.artifact_type='run_provider_accounting' "
            "ORDER BY a.event_seq DESC LIMIT 1) AS payload_json "
            "FROM run_budget_reservations b JOIN runs r ON r.run_id=b.run_id "
            "WHERE b.settled_cost_micros IS NULL "
            "AND r.lifecycle_status IN ('COMPLETED','FAILED','CANCELLED')",
        ).fetchall()
        for row in rows:
            cost = None
            if row["payload_json"]:
                try:
                    import json

                    payload = json.loads(row["payload_json"])
                    provider = payload.get("provider") or {}
                    candidate = provider.get("cost_usd", payload.get("cost_usd"))
                    if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                        if math.isfinite(float(candidate)) and candidate >= 0:
                            cost = float(candidate)
                except (TypeError, ValueError):
                    cost = None
            self.settle(
                conn,
                row["run_id"],
                actual_cost_usd=cost,
                refund_caller=row["failure_code"] in _SERVICE_FAILURES_REFUNDING_CALLER,
                # Keep provider account failures on the exact DATA_UNAVAILABLE refund path.
                allow_costed_caller_refund=row["failure_code"] in {"DATA_UNAVAILABLE", "PROVIDER_UNAVAILABLE"},
            )
