"""Business operations for durable live runs and persisted run views."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_app.api_dto import (
    RunViewCostDTO,
    RunViewDTO,
    RunViewFailureDTO,
    RunViewModelDTO,
    RunViewRevisionDTO,
    RunViewRunDTO,
)
from catalyst_app.persistence.connect import open_readonly
from catalyst_app.product_tier import with_product_fields
from catalyst_app.runtime.admission import (
    AdmissionController,
    AdmissionOutcome,
    AdmissionRequest,
)
from catalyst_app.runtime.budget import BudgetAdmission
from catalyst_app.schemas import CreateRunRequest


class RunViewDataError(ValueError):
    """Persisted run data cannot be represented by the run-view contract."""


def utc_parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def create_live_run(
    *,
    controller: AdmissionController,
    request: CreateRunRequest,
    provider: str,
    model_id: str,
    base_url: str | None,
    credential_source: str,
    budget: BudgetAdmission,
    pre_submit: Callable[[str], None] | None,
    idempotency_key: str | None,
    visibility: str,
    public_cache_key: str | None,
) -> AdmissionOutcome:
    """Build and submit a durable run admission request."""
    admission_request = AdmissionRequest(
        ticker=request.ticker,
        session_date=request.trade_date,
        query=request.query,
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        credential_source_identifier=credential_source,
        workflow_version="v1.1",
        config_version=request.config,
        idempotency_key=idempotency_key,
        force_full=request.force_full,
        budget=budget,
        origin="http",
        visibility=visibility,
        public_cache_key=public_cache_key,
    )
    return controller.admit(admission_request, pre_submit=pre_submit)


def read_run_view(db_path: Path, run_id: str) -> RunViewDTO | None:
    """Build the UI projection solely from this run's persisted rows/artifacts."""
    with open_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT run_id, lifecycle_status, created_at, ticker, provider,"
            " failure_code, failure_message FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        terminal = conn.execute(
            "SELECT occurred_at FROM run_events WHERE run_id = ?"
            " AND event_type IN ('run.completed', 'run.failed', 'run.cancelled')"
            " ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        artifact_rows = conn.execute(
            "SELECT artifact_type, payload_json FROM run_artifacts"
            " WHERE run_id = ? AND artifact_type IN"
            " ('run_manifest', 'context_pack', 'decision_trace', 'answer',"
            " 'answer_snapshot', 'run_diagnostics', 'run_provider_accounting')"
            " ORDER BY event_seq DESC, artifact_id DESC",
            (run_id,),
        ).fetchall()

    artifacts: dict[str, dict[str, Any]] = {}
    for artifact in artifact_rows:
        artifact_type = artifact["artifact_type"]
        if artifact_type in artifacts:
            continue
        try:
            payload = json.loads(artifact["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            artifacts[artifact_type] = payload

    manifest = artifacts.get("run_manifest", {})
    temporal = manifest.get("temporal_identity")
    temporal = temporal if isinstance(temporal, dict) else {}
    policy_value = manifest.get("attribution_policy")
    policy = policy_value if isinstance(policy_value, dict) else None
    trace = artifacts.get("decision_trace")
    snapshot = artifacts.get("answer_snapshot")
    abstention_reason_availability = "not_applicable"
    if snapshot is not None and snapshot.get("outcome") == "ABSTAINED":
        from catalyst_agents.attribution.answer_snapshot import abstention_reason_text
        from catalyst_agents.observability.decision_trace import ABSTENTION_REASON_CODES

        recorded_reason = (trace or {}).get("abstention_reason")
        if (isinstance(recorded_reason, dict)
                and recorded_reason.get("reason_code") in ABSTENTION_REASON_CODES):
            abstention_reason_availability = "present"
            snapshot = dict(snapshot)
            abstention = dict(snapshot.get("abstention") or {})
            abstention.update(recorded_reason)
            abstention["reason_text"] = abstention_reason_text(recorded_reason["reason_code"])
            snapshot["abstention"] = abstention
        else:
            # Legacy trace fields are used only when they contain enough
            # evidence to classify the reason; old snapshot labels are not
            # treated as authoritative enum values.
            derived_reason = None
            if trace is not None:
                from catalyst_agents.attribution.answer_snapshot import (
                    abstention_reason_text,
                    derive_abstention_reason,
                )

                derived_reason = derive_abstention_reason(trace)
            snapshot = dict(snapshot)
            abstention = dict(snapshot.get("abstention") or {})
            if derived_reason is not None:
                abstention.update(derived_reason)
                abstention["reason_text"] = abstention_reason_text(derived_reason["reason_code"])
                abstention_reason_availability = "derived"
            else:
                for field in ("reason_code", "reason_text", "details"):
                    abstention.pop(field, None)
                abstention_reason_availability = "not_recorded"
            snapshot["abstention"] = abstention
    facts_value = snapshot.get("facts") if snapshot else None
    facts = facts_value if isinstance(facts_value, dict) else None

    created_at = utc_parse(row["created_at"])
    if created_at is None:
        raise RunViewDataError("run_created_at_invalid")
    finished_at = utc_parse(terminal["occurred_at"]) if terminal else None
    trace_duration = trace.get("total_ms") if trace else None
    if isinstance(trace_duration, int) and not isinstance(trace_duration, bool) and trace_duration >= 0:
        duration_ms = trace_duration
        duration_source = "decision_trace"
    elif finished_at is not None:
        duration_ms = max(0, int((finished_at - created_at).total_seconds() * 1000))
        duration_source = "terminal_event"
    else:
        duration_ms = None
        duration_source = "not_recorded"

    diagnostics = artifacts.get("run_diagnostics", {})
    accounting = artifacts.get("run_provider_accounting", {})
    provider_stats = diagnostics.get("provider")
    if not isinstance(provider_stats, dict):
        provider_stats = accounting.get("provider")
    provider_stats = provider_stats if isinstance(provider_stats, dict) else {}

    trace_calls = trace.get("model_calls") if trace else None
    if isinstance(trace_calls, dict):
        model_calls = {
            str(role): count
            for role, count in trace_calls.items()
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0
        }
    else:
        model_calls = {}
    for role, field in (
        ("analyst", "analyst_logical_calls"),
        ("verifier", "verifier_logical_calls"),
        ("writer", "writer_logical_calls"),
    ):
        value = provider_stats.get(field)
        if role not in model_calls and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            model_calls[role] = value
    attempt_fields = (
        "analyst_provider_attempts",
        "verifier_provider_attempts",
        "writer_provider_attempts",
    )
    if any(isinstance(provider_stats.get(field), int) for field in attempt_fields):
        provider_attempts = sum(
            provider_stats.get(field, 0)
            for field in attempt_fields
            if isinstance(provider_stats.get(field, 0), int)
            and not isinstance(provider_stats.get(field, 0), bool)
        )
    else:
        provider_attempts = accounting.get("provider_calls")
        if not isinstance(provider_attempts, int) or isinstance(provider_attempts, bool):
            provider_attempts = None
    cost_usd = provider_stats.get("cost_usd")
    cost_method = provider_stats.get("cost_method")
    if "cost_method" not in provider_stats and accounting:
        cost_usd = accounting.get("cost_usd")
        cost_method = accounting.get("cost_method")

    # RG-13: a run that has not COMPLETED does not expose its answer; an
    # answer pushed mid-run is provisional and the terminal event withdraws it.
    run_completed = row["lifecycle_status"] == "COMPLETED"
    if not run_completed:
        snapshot = None
    answer_payload = artifacts.get("answer")
    legacy_text = None
    if run_completed and snapshot is None and answer_payload is not None:
        text_value = answer_payload.get("text")
        legacy_text = text_value if isinstance(text_value, str) else None

    failure = None
    if row["lifecycle_status"] == "FAILED":
        failure = RunViewFailureDTO(
            code=row["failure_code"] or "FAILED",
            message=row["failure_message"],
        )

    return RunViewDTO(
        run=RunViewRunDTO(
            run_id=row["run_id"],
            ticker=row["ticker"],
            session_date=temporal.get("session_date"),
            lifecycle=row["lifecycle_status"],
            created_at=created_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            duration_source=duration_source,
            model=RunViewModelDTO(
                provider=row["provider"],
                analyst_model_id=manifest.get("analyst_model_id"),
            ),
            policy=policy,
            code_revision=RunViewRevisionDTO(
                sha=manifest.get("code_revision") if isinstance(manifest.get("code_revision"), str) else None,
                dirty=None,
            ),
            force_full=policy.get("force_full") if policy is not None else None,
        ),
        cost=RunViewCostDTO(
            model_calls=model_calls,
            provider_attempts=provider_attempts,
            cost_usd=cost_usd if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool) else None,
            cost_method=cost_method if isinstance(cost_method, str) else None,
        ),
        answer=with_product_fields(snapshot) if isinstance(snapshot, dict) else snapshot,
        legacy_text=legacy_text,
        facts=facts,
        trace=trace,
        failure=failure,
        availability={
            "answer": (
                "present" if snapshot is not None
                else "not_applicable" if not run_completed
                else "not_recorded"
            ),
            "abstention_reason": abstention_reason_availability,
            "trace": "present" if trace is not None else "not_recorded",
            "policy": "present" if policy is not None else "not_recorded",
            "cost": "present" if diagnostics or accounting else "not_recorded",
            "facts": "present" if facts is not None else "not_recorded",
        },
    )
