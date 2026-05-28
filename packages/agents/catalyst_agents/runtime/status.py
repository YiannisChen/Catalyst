from __future__ import annotations

from typing import Any, Mapping


_STATUS_MAP = {
    "SUFFICIENT": "SUCCEEDED",
    "PARTIAL": "PARTIAL",
    "INSUFFICIENT": "INSUFFICIENT",
    "SYSTEM_ERROR": "FAILED_SYSTEM",
    "FAILED_REQUEST": "FAILED_REQUEST",
    "QUEUED": "QUEUED",
    "RUNNING": "RUNNING",
}

_REQUEST_FAILURE_REASONS = {
    "unsupported_ticker",
    "date_out_of_range",
    "missing_trading_day_context",
    "invalid_query_type",
    "empty_query",
    "query_too_long",
    "invalid_trade_date",
}

_NON_RETRYABLE_INSUFFICIENT = {"NON_MATERIAL_MOVE"}
_RETRYABLE_PARTIAL = {"INCONCLUSIVE"}


def _status_name(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def normalize_run_status(status: Any, *, sub_reason: str | None = None) -> dict[str, str | None]:
    status_name = _status_name(status)
    top_level = _STATUS_MAP.get(status_name or "", status_name or "UNKNOWN")
    if top_level == "FAILED_SYSTEM" and sub_reason is None:
        sub_reason = "system_error"
    return {"status": top_level, "sub_reason": sub_reason}


def is_retryable(status: Any, *, sub_reason: str | None = None) -> bool:
    top_level = normalize_run_status(status, sub_reason=sub_reason)["status"]
    if top_level in {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED_REQUEST"}:
        return False
    if top_level == "FAILED_SYSTEM":
        return True
    if top_level == "PARTIAL":
        return sub_reason in _RETRYABLE_PARTIAL
    if top_level == "INSUFFICIENT":
        return sub_reason not in _NON_RETRYABLE_INSUFFICIENT
    return False


def build_failure_payload(
    *,
    status: Any,
    sub_reason: str | None,
    message: str | None,
    source: str,
    node: str | None = None,
) -> dict[str, Any]:
    normalized = normalize_run_status(status, sub_reason=sub_reason)
    return {
        "status": normalized["status"],
        "sub_reason": normalized["sub_reason"],
        "message": message,
        "source": source,
        "node": node,
        "retryable": is_retryable(normalized["status"], sub_reason=normalized["sub_reason"]),
    }


def _row_get(row: Mapping[str, Any] | None, key: str) -> Any:
    if row is None:
        return None
    if hasattr(row, "keys") and key not in row.keys():
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def failure_payload_from_rows(
    *,
    run_row: Mapping[str, Any] | None,
    trace_event_row: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    event_error_type = _row_get(trace_event_row, "error_type")
    if event_error_type:
        return build_failure_payload(
            status="FAILED_SYSTEM",
            sub_reason=str(event_error_type),
            message=_row_get(trace_event_row, "error_message"),
            source="trace_events",
            node=_row_get(trace_event_row, "node"),
        )

    run_error_type = _row_get(run_row, "error_type")
    run_status = _row_get(run_row, "status")
    if run_error_type in _REQUEST_FAILURE_REASONS or run_status == "FAILED_REQUEST":
        return build_failure_payload(
            status="FAILED_REQUEST",
            sub_reason=str(run_error_type) if run_error_type else None,
            message=_row_get(run_row, "error_message"),
            source="agent_runs",
        )
    if run_error_type or run_status == "SYSTEM_ERROR":
        return build_failure_payload(
            status="SYSTEM_ERROR",
            sub_reason=str(run_error_type) if run_error_type else "system_error",
            message=_row_get(run_row, "error_message"),
            source="agent_runs",
        )
    return None
