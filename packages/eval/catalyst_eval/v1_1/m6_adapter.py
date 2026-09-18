"""M6 app/SSE runner adapter for Stage-1 execute (M7-8, Phase A corrective).

Eval-owned adapter that drives the live M6 app/SSE boundary through an
injected ``RuntimeComposition`` (AdmissionController + RunClaimer + SQLite
authority).

Authoritative-facts-only contract (M7 Phase A):

* the adapter reads only what the M6 runtime persisted: ``runs`` (lifecycle +
  pre-submit RunManifest binding), ``run_events`` (ordered persisted envelope),
  and ``run_artifacts`` (sealed payloads).
* the terminal outcome comes from the persisted terminal event, which must
  agree with the persisted lifecycle and be immediately preceded by
  ``assurance.completed``.
* retrieval, trajectory, provider-accounting, and terminal-status facts come
  from the agents-owned ``run_diagnostics`` artifact published in the terminal
  transaction. There is no eval-side external authority callback: if the
  runtime did not persist a fact, the corresponding metric is non-scorable
  rather than defaulted.
* the retrieved pool identity passed to the retrieval metrics is *derived from
  observed facts* (candidate inventory + runtime identity + the arm top_k the
  runtime served) and is bound to the persisted diagnostics artifact hash. It
  is not the frozen M3 four-arm judgment pool, and it is never synthesised.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from catalyst_app.events import RunEventType, _EVENT_PAYLOAD_TYPES
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import payload_sha256

from catalyst_eval.v1_1.loader import canonical_bytes
from catalyst_eval.v1_1.runner import CaseRunOutcome

DIAGNOSTICS_EVENT_ARTIFACT_TYPE = "run_diagnostics"
DIAGNOSTICS_SCHEMA_VERSION = "v1.1_run_diagnostics_v1"
# Durable consumed-provider accounting published by the app run adapter for
# every run that dispatched provider work (including FAILED/TIMEOUT/CANCELLED
# runs that never reach the completed diagnostics publication).
PROVIDER_ACCOUNTING_EVENT_ARTIFACT_TYPE = "run_provider_accounting"
PROVIDER_ACCOUNTING_SCHEMA_VERSION = "v1.1_run_provider_accounting_v1"
RUN_FACTS_SCHEMA_VERSION = "v1_1_stage1_run_facts_v1"

# Persisted reranker status that proves the reranker contributed.
_RERANKED_STAGE = "reranked"

# Retrieval stage name -> benchmark arm name.
_ARM_ALIASES = {"lexical": "lexical", "dense": "dense", "fusion": "hybrid", "reranked": "reranked"}
# Benchmark-declared arm version for the observed pool identity.
_DECLARED_ARM_VERSION = "1.0.0"

_TERMINAL_EVENT_STATUS = {
    RunEventType.RUN_COMPLETED: "COMPLETED",
    RunEventType.RUN_FAILED: "FAILED",
    RunEventType.RUN_CANCELLED: "CANCELLED",
}


class Stage1AdapterError(RuntimeError):
    pass


def _sha256_canonical(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def default_case_request_builder(case: Any, **kwargs) -> Any:
    """Build an M6 AdmissionRequest from a case (lazy app import)."""
    from catalyst_app.runtime.admission import AdmissionRequest

    return AdmissionRequest(
        ticker=case.ticker,
        session_date=case.session_date,
        query=case.question,
        provider=kwargs.get("provider", "deepseek"),
        model_id=kwargs.get("model_id", "deepseek-chat"),
        base_url=kwargs.get("base_url"),
        credential_source_identifier=kwargs.get("credential_source", "server_env"),
        workflow_version="v1.1",
        config_version="v1.1",
    )


class M6AppSseRunnerAdapter:
    """Drive one case through the M6 app/SSE boundary and seal its artifacts."""

    def __init__(
        self,
        *,
        composition: Any,
        db_path: str | Path,
        case_request_builder: Callable[..., Any] | None = None,
        provider: str = "deepseek",
        model_id: str = "deepseek-chat",
        base_url: str | None = None,
        credential_source: str = "server_env",
        poll_interval_seconds: float = 0.05,
        case_timeout_seconds: float = 30.0,
        prepared_identity_ref: str | None = None,
        prepared_identity_hash: str | None = None,
        credential_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.composition = composition
        self.db_path = Path(db_path)
        self.case_request_builder = case_request_builder or default_case_request_builder
        self.provider = provider
        self.model_id = model_id
        self.base_url = base_url
        self.credential_source = credential_source
        self.poll_interval_seconds = poll_interval_seconds
        self.case_timeout_seconds = case_timeout_seconds
        self.prepared_identity_ref = prepared_identity_ref
        self.prepared_identity_hash = prepared_identity_hash
        # The volatile operator credential is resolved by this callback and
        # registered into the composition's credential store through the
        # pre-submit hook. It is never stored on the adapter, an artifact, an
        # event, a log line, or an exception.
        self._credential_provider = credential_provider
        self._captured_manifest_binding: dict[str, tuple[str, str]] = {}
        self._remaining_provider_calls: int | None = None
        self._remaining_cost_usd: float | None = None

    # -- operator budget binding -------------------------------------------

    def set_remaining_budget(
        self, *, provider_calls: int | None, cost_usd: float | None
    ) -> None:
        """Bind the remaining total budget the next case may not exceed.

        The caller re-binds this before every dispatch so a case can never
        start when the remaining budget cannot cover the next provider work.
        """
        self._remaining_provider_calls = provider_calls
        self._remaining_cost_usd = cost_usd

    def _check_remaining_budget(self, case_id: str) -> None:
        if self._remaining_provider_calls is None and self._remaining_cost_usd is None:
            return
        calls = self._remaining_provider_calls or 0
        if self._remaining_cost_usd is None:
            raise Stage1AdapterError(
                f"remaining cost is unknown before dispatch of {case_id!r}; refusing dispatch"
            )
        cost = self._remaining_cost_usd
        if calls <= 0 or cost <= 0:
            raise Stage1AdapterError(
                f"remaining budget exhausted before dispatch of {case_id!r}: "
                f"provider_calls={calls} cost_usd={cost}"
            )

    # -- public ------------------------------------------------------------

    def run_case(self, case: Any) -> CaseRunOutcome:
        """Admit one run, wait for the persisted terminal event, seal the outcome."""
        self._check_remaining_budget(case.case_id)
        request = self.case_request_builder(
            case,
            provider=self.provider,
            model_id=self.model_id,
            base_url=self.base_url,
            credential_source=self.credential_source,
        )
        admitted = self.composition.admission.admit(
            request, pre_submit=self._pre_submit
        )
        if admitted.kind != "accepted" or not admitted.run_id:
            raise Stage1AdapterError(
                f"admission for {case.case_id!r} was not accepted: "
                f"kind={admitted.kind} failure_code={admitted.failure_code}"
            )
        run_id = admitted.run_id
        self._await_terminal_event(run_id)
        return self._load_outcome(run_id, case)

    # -- pre-submit RunManifest capture --------------------------------------

    def _pre_submit(self, run_id: str) -> None:
        """Capture the manifest binding, then register the volatile credential.

        Runs after the ACCEPTED transaction commits and before the executor
        task is submitted, so the credential is visible to the worker and the
        RunManifest binding is captured before any provider dispatch.
        """
        self._capture_manifest_binding(run_id)
        if self._credential_provider is not None:
            api_key = self._credential_provider()
            if not api_key:
                raise Stage1AdapterError(
                    "provider credential is missing; refusing dispatch"
                )
            store = getattr(self.composition, "credential_store", None)
            if store is None:
                raise Stage1AdapterError(
                    "provider credential store is unavailable; refusing dispatch"
                )
            store.register(run_id, api_key=api_key)

    def _capture_manifest_binding(self, run_id: str) -> None:
        """Capture the authoritative RunManifest id/hash before dispatch."""
        with open_rw(self.db_path) as conn:
            row = conn.execute(
                "SELECT run_manifest_id, manifest_hash FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            manifest_row = conn.execute(
                "SELECT payload_json FROM run_artifacts WHERE run_id = ? "
                "AND artifact_type = 'run_manifest' ORDER BY event_seq ASC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            raise Stage1AdapterError(
                f"pre-submit capture failed: run {run_id!r} has no runs row"
            )
        manifest_id = str(row["run_manifest_id"])
        manifest_hash = str(row["manifest_hash"])
        if not manifest_id or not manifest_hash:
            raise Stage1AdapterError(
                f"pre-submit capture failed: run {run_id!r} has no RunManifest binding"
            )
        if manifest_row is None:
            raise Stage1AdapterError(
                f"pre-submit capture failed: run {run_id!r} has no RunManifest artifact"
            )
        try:
            manifest_payload = json.loads(manifest_row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise Stage1AdapterError(
                f"pre-submit capture failed: run {run_id!r} has invalid RunManifest"
            ) from exc
        if self.prepared_identity_ref is not None and str(
            manifest_payload.get("data_runtime_identity_ref")
        ) != self.prepared_identity_ref:
            raise Stage1AdapterError(
                f"run {run_id!r} data runtime identity ref does not match the prepared identity"
            )
        if self.prepared_identity_hash is not None and str(
            manifest_payload.get("data_runtime_identity_hash")
        ) != self.prepared_identity_hash:
            raise Stage1AdapterError(
                f"run {run_id!r} data runtime identity object hash does not match the prepared identity"
            )
        self._captured_manifest_binding[run_id] = (manifest_id, manifest_hash)

    # -- terminal wait (persisted event, never a bare lifecycle poll) --------

    def _await_terminal_event(self, run_id: str) -> None:
        deadline = time.monotonic() + self.case_timeout_seconds
        terminal_types = {event_type.value for event_type in _TERMINAL_EVENT_STATUS}
        while True:
            with open_rw(self.db_path) as conn:
                row = conn.execute(
                    "SELECT event_type FROM run_events WHERE run_id = ?"
                    " ORDER BY seq DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
            if row is not None and str(row["event_type"]) in terminal_types:
                return
            if time.monotonic() >= deadline:
                raise Stage1AdapterError(
                    f"run {run_id!r} did not persist a terminal event within "
                    f"{self.case_timeout_seconds}s"
                )
            time.sleep(self.poll_interval_seconds)

    # -- artifact loading --------------------------------------------------

    def _load_outcome(self, run_id: str, case: Any) -> CaseRunOutcome:
        case_id = case.case_id
        with open_rw(self.db_path) as conn:
            run_row = conn.execute(
                "SELECT lifecycle_status, run_manifest_id, manifest_hash"
                " FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise Stage1AdapterError(f"run {run_id!r} has no runs row")
            event_rows = conn.execute(
                "SELECT seq, event_type, payload_json FROM run_events"
                " WHERE run_id = ? ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
            artifact_rows = conn.execute(
                "SELECT artifact_id, artifact_type, event_seq, payload_hash,"
                " payload_json FROM run_artifacts WHERE run_id = ?"
                " ORDER BY event_seq ASC, artifact_id ASC",
                (run_id,),
            ).fetchall()

        lifecycle_status = str(run_row["lifecycle_status"])
        terminal_type, terminal_payload, terminal_seq, events = (
            self._validate_terminal_event(run_id, lifecycle_status, event_rows)
        )
        artifacts = self._index_artifacts(run_id, artifact_rows, events)

        manifest_records = artifacts.get("run_manifest") or []
        if not manifest_records:
            raise Stage1AdapterError(
                f"run {run_id!r} has no persisted RunManifest artifact"
            )
        manifest_record = manifest_records[0]
        manifest_payload = _parse_payload(manifest_record, run_id)
        self._verify_manifest_binding(
            run_id, run_row, manifest_record, manifest_payload
        )

        if terminal_type is not RunEventType.RUN_COMPLETED:
            return self._load_failed_outcome(
                run_id=run_id,
                case_id=case_id,
                manifest_record=manifest_record,
                manifest_payload=manifest_payload,
                terminal_type=terminal_type,
                terminal_payload=terminal_payload,
                artifacts=artifacts,
                events=events,
            )

        self._require_assurance_before_terminal(run_id, events, terminal_seq)
        attribution_record = self._require_artifact(
            artifacts, run_id, "attribution_result", terminal_seq
        )
        assurance_record = self._require_artifact(
            artifacts, run_id, "assurance", terminal_seq
        )
        context_pack_record = self._require_latest_artifact(
            artifacts, run_id, "context_pack"
        )
        diagnostics_record = self._require_artifact(
            artifacts, run_id, DIAGNOSTICS_EVENT_ARTIFACT_TYPE, terminal_seq
        )
        attribution_payload = _parse_payload(attribution_record, run_id)
        assurance_payload = _parse_payload(assurance_record, run_id)
        context_pack_payload = _parse_payload(context_pack_record, run_id)
        diagnostics = self._load_diagnostics(run_id, diagnostics_record, terminal_payload)
        self._verify_diagnostics_identity(
            run_id, diagnostics, attribution_payload, terminal_payload
        )

        claim_records = artifacts.get("claim_detail") or []
        claim_plan_ref = self._claim_plan_ref(run_id, events, claim_records)
        claims = self._build_claims(run_id, claim_records)

        run_facts = self._build_run_facts(
            run_id=run_id,
            case_id=case_id,
            diagnostics=diagnostics,
            diagnostics_artifact_hash=str(diagnostics_record["payload_hash"]),
            claims=claims,
            terminal_payload=terminal_payload,
        )

        accounting = diagnostics["provider"]
        provider_calls = (
            accounting["analyst_provider_attempts"]
            + accounting["writer_provider_attempts"]
        )
        from catalyst_eval.v1_1.run_facts import validate_run_facts

        validate_run_facts(
            run_facts,
            expected_case_id=case_id,
            row_provider_calls=provider_calls,
        )
        return CaseRunOutcome(
            case_id=case_id,
            run_manifest_id=str(manifest_record["artifact_id"]),
            run_manifest_hash=_sha256_canonical(manifest_payload),
            result_artifact_id=str(attribution_record["artifact_id"]),
            result_artifact_hash=_sha256_canonical(attribution_payload),
            terminal_status=_TERMINAL_EVENT_STATUS[terminal_type],
            run_manifest_payload=manifest_payload,
            result_artifact_payload=attribution_payload,
            context_pack_ref={
                "artifact_id": str(context_pack_record["artifact_id"]),
                "artifact_hash": _sha256_canonical(context_pack_payload),
            },
            claim_plan_ref=claim_plan_ref,
            assurance_ref={
                "artifact_id": str(assurance_record["artifact_id"]),
                "artifact_hash": _sha256_canonical(assurance_payload),
            },
            provider_calls=provider_calls,
            cost_usd=accounting.get("cost_usd"),
            latency_ms=int(terminal_payload.total_latency_ms),
            tokens=_total_tokens(accounting),
            run_facts=run_facts,
        )

    # -- validation helpers -------------------------------------------------

    def _validate_terminal_event(
        self, run_id: str, lifecycle_status: str, event_rows: Any
    ) -> tuple[RunEventType, Any, int, list[dict[str, Any]]]:
        if not event_rows:
            raise Stage1AdapterError(
                f"run {run_id!r} has no persisted run_events; the terminal "
                "event contract cannot be verified from a lifecycle row alone"
            )
        events: list[dict[str, Any]] = []
        seen_seqs: set[int] = set()
        for row in event_rows:
            seq = int(row["seq"])
            if seq in seen_seqs:
                raise Stage1AdapterError(
                    f"run {run_id!r} has a duplicate persisted event seq {seq}"
                )
            seen_seqs.add(seq)
            event_type = RunEventType(str(row["event_type"]))
            payload = _parse_event_payload(row["payload_json"], run_id, event_type)
            events.append({"seq": seq, "event_type": event_type, "payload": payload})
        terminal = events[-1]
        terminal_type: RunEventType = terminal["event_type"]
        if terminal_type not in _TERMINAL_EVENT_STATUS:
            raise Stage1AdapterError(
                f"run {run_id!r} persisted no terminal event (last event is "
                f"{terminal_type.value!r}); a lifecycle poll is not a terminal "
                "outcome"
            )
        expected_status = _TERMINAL_EVENT_STATUS[terminal_type]
        if lifecycle_status != expected_status:
            raise Stage1AdapterError(
                f"run {run_id!r} terminal event {terminal_type.value!r} "
                f"disagrees with the persisted lifecycle {lifecycle_status!r} "
                f"(expected {expected_status!r})"
            )
        return terminal_type, terminal["payload"], terminal["seq"], events

    @staticmethod
    def _index_artifacts(
        run_id: str, artifact_rows: Any, events: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        event_seqs = {event["seq"] for event in events}
        artifacts: dict[str, list[dict[str, Any]]] = {}
        for row in artifact_rows:
            event_seq = int(row["event_seq"])
            if event_seq not in event_seqs:
                raise Stage1AdapterError(
                    f"run {run_id!r} artifact {row['artifact_id']!r} is bound to "
                    f"unknown event seq {event_seq}"
                )
            artifacts.setdefault(str(row["artifact_type"]), []).append(
                {
                    "artifact_id": str(row["artifact_id"]),
                    "artifact_type": str(row["artifact_type"]),
                    "event_seq": event_seq,
                    "payload_hash": str(row["payload_hash"]),
                    "payload_json": row["payload_json"],
                }
            )
        return artifacts

    def _verify_manifest_binding(
        self,
        run_id: str,
        run_row: Any,
        manifest_record: Mapping[str, Any],
        manifest_payload: Mapping[str, Any],
    ) -> None:
        captured = self._captured_manifest_binding.get(run_id)
        if captured is None:
            raise Stage1AdapterError(
                f"run {run_id!r} was not admitted through this adapter; the "
                "pre-submit RunManifest binding is missing"
            )
        captured_id, captured_hash = captured
        if str(manifest_record["artifact_id"]) != captured_id:
            raise Stage1AdapterError(
                f"run {run_id!r} persisted RunManifest artifact id does not "
                "match the pre-submit binding"
            )
        if str(run_row["run_manifest_id"]) != captured_id:
            raise Stage1AdapterError(
                f"run {run_id!r} runs.run_manifest_id does not match the "
                "pre-submit RunManifest binding"
            )
        if payload_sha256(dict(manifest_payload)) != captured_hash:
            raise Stage1AdapterError(
                f"run {run_id!r} persisted RunManifest hash does not match the "
                "pre-submit binding"
            )
        if self.prepared_identity_ref is not None:
            if str(manifest_payload.get("data_runtime_identity_ref")) != (
                self.prepared_identity_ref
            ):
                raise Stage1AdapterError(
                    f"run {run_id!r} data runtime identity ref does not match "
                    "the prepared identity"
                )
        if self.prepared_identity_hash is not None:
            if str(manifest_payload.get("data_runtime_identity_hash")) != (
                self.prepared_identity_hash
            ):
                raise Stage1AdapterError(
                    f"run {run_id!r} data runtime identity hash does not match "
                    "the prepared identity"
                )

    @staticmethod
    def _require_assurance_before_terminal(
        run_id: str, events: list[dict[str, Any]], terminal_seq: int
    ) -> None:
        prior = [e for e in events if e["seq"] < terminal_seq]
        if not prior or prior[-1]["event_type"] is not RunEventType.ASSURANCE_COMPLETED:
            raise Stage1AdapterError(
                f"run {run_id!r} terminal event is not immediately preceded by "
                "the persisted assurance.completed event"
            )

    @staticmethod
    def _require_artifact(
        artifacts: Mapping[str, list[dict[str, Any]]],
        run_id: str,
        artifact_type: str,
        event_seq: int,
    ) -> Mapping[str, Any]:
        records = [
            record
            for record in artifacts.get(artifact_type) or []
            if record["event_seq"] == event_seq
        ]
        if not records:
            raise Stage1AdapterError(
                f"run {run_id!r} has no persisted {artifact_type!r} artifact "
                f"bound to terminal event seq {event_seq}"
            )
        return records[0]

    @staticmethod
    def _require_latest_artifact(
        artifacts: Mapping[str, list[dict[str, Any]]],
        run_id: str,
        artifact_type: str,
    ) -> Mapping[str, Any]:
        records = artifacts.get(artifact_type) or []
        if not records:
            raise Stage1AdapterError(
                f"run {run_id!r} has no persisted {artifact_type!r} artifact"
            )
        return records[-1]

    def _load_diagnostics(
        self,
        run_id: str,
        diagnostics_record: Mapping[str, Any],
        terminal_payload: Any,
    ) -> dict[str, Any]:
        """Read + hash-verify the persisted diagnostics artifact."""
        payload = _parse_payload(diagnostics_record, run_id)
        declared_hash = str(diagnostics_record["payload_hash"])
        if payload_sha256(payload) != declared_hash:
            raise Stage1AdapterError(
                f"run {run_id!r} persisted run_diagnostics payload does not "
                "match its recorded payload hash"
            )
        try:
            from catalyst_agents.observability.diagnostics import (
                CorrectiveRoundObservation,
                EvidenceDeltaObservation,
                ProviderAccountingDiagnostics,
                RetrievalArmObservation,
                RetrievalDiagnostics,
                RetrievalTaskObservation,
                RunDiagnostics,
                TerminalDiagnostics,
                TrajectoryDiagnostics,
            )

            _require_complete_model_payload(payload, RunDiagnostics, "run_diagnostics")
            _require_complete_model_payload(
                payload["retrieval"], RetrievalDiagnostics, "run_diagnostics.retrieval"
            )
            _require_complete_model_payload(
                payload["trajectory"], TrajectoryDiagnostics, "run_diagnostics.trajectory"
            )
            _require_complete_model_payload(
                payload["provider"],
                ProviderAccountingDiagnostics,
                "run_diagnostics.provider",
            )
            _require_complete_model_payload(
                payload["terminal"], TerminalDiagnostics, "run_diagnostics.terminal"
            )
            for index, arm in enumerate(payload["retrieval"]["arms"]):
                _require_complete_model_payload(
                    arm,
                    RetrievalArmObservation,
                    f"run_diagnostics.retrieval.arms[{index}]",
                )
            for index, task in enumerate(payload["retrieval"]["per_task"]):
                _require_complete_model_payload(
                    task,
                    RetrievalTaskObservation,
                    f"run_diagnostics.retrieval.per_task[{index}]",
                )
            for index, round_payload in enumerate(payload["trajectory"]["rounds"]):
                _require_complete_model_payload(
                    round_payload,
                    CorrectiveRoundObservation,
                    f"run_diagnostics.trajectory.rounds[{index}]",
                )
                delta = round_payload["evidence_delta"]
                if delta is not None:
                    _require_complete_model_payload(
                        delta,
                        EvidenceDeltaObservation,
                        f"run_diagnostics.trajectory.rounds[{index}].evidence_delta",
                    )
            validated = RunDiagnostics.model_validate_json(
                diagnostics_record["payload_json"], strict=True
            )
        except Exception as exc:
            raise Stage1AdapterError(
                f"run {run_id!r} persisted run_diagnostics violates its strict "
                f"complete contract: {exc}"
            ) from exc
        payload = validated.model_dump(mode="json")
        if payload["schema_version"] != DIAGNOSTICS_SCHEMA_VERSION:
            raise Stage1AdapterError(
                f"run {run_id!r} run_diagnostics schema must be "
                f"{DIAGNOSTICS_SCHEMA_VERSION}, got "
                f"{payload.get('schema_version')!r}"
            )
        if payload["run_id"] != run_id:
            raise Stage1AdapterError(
                f"run {run_id!r} run_diagnostics is bound to a different run"
            )
        recorded_terminal = payload["terminal"]
        if recorded_terminal["result_status"] != terminal_payload.result_status:
            raise Stage1AdapterError(
                f"run {run_id!r} run_diagnostics status "
                f"{recorded_terminal.get('result_status')!r} disagrees with the "
                f"terminal event {terminal_payload.result_status!r}"
            )
        return payload

    def _verify_diagnostics_identity(
        self,
        run_id: str,
        diagnostics: Mapping[str, Any],
        attribution_payload: Mapping[str, Any],
        terminal_payload: Any,
    ) -> None:
        retrieval = diagnostics["retrieval"]
        if self.prepared_identity_hash is not None:
            if str(retrieval.get("data_runtime_identity_hash")) != (
                self.prepared_identity_hash
            ):
                raise Stage1AdapterError(
                    f"run {run_id!r} diagnostics runtime identity hash does not "
                    "match the prepared identity"
                )
        declared_type = diagnostics["terminal"].get("attribution_type")
        if declared_type != attribution_payload.get("attribution_type"):
            raise Stage1AdapterError(
                f"run {run_id!r} diagnostics attribution_type disagrees with "
                "the persisted attribution result"
            )
        if diagnostics["provider"].get("cost_usd") is None and (
            terminal_payload.cost is not None
        ):
            raise Stage1AdapterError(
                f"run {run_id!r} terminal event reports a cost the diagnostics "
                "did not record"
            )

    def _load_failed_outcome(
        self,
        *,
        run_id: str,
        case_id: str,
        manifest_record: Mapping[str, Any],
        manifest_payload: Mapping[str, Any],
        terminal_type: RunEventType,
        terminal_payload: Any,
        artifacts: Mapping[str, list[dict[str, Any]]],
        events: list[dict[str, Any]],
    ) -> CaseRunOutcome:
        """Seal a FAILED/CANCELLED outcome with the provider work it consumed.

        A terminal non-COMPLETED run has no result artifacts to hash, but the
        provider attempts it already dispatched are real consumption. The
        consumed accounting is read from the durable provider-accounting
        artifact the runtime published for this run; when none was persisted
        the outcome fails closed rather than reporting a zero-cost failure.
        """
        provider_calls, cost_usd, cost_method, run_facts = (
            self._consumed_provider_accounting(
                run_id=run_id,
                case_id=case_id,
                artifacts=artifacts,
                terminal_type=terminal_type,
                terminal_payload=terminal_payload,
            )
        )
        return CaseRunOutcome(
            case_id=case_id,
            run_manifest_id=str(manifest_record["artifact_id"]),
            run_manifest_hash=_sha256_canonical(manifest_payload),
            result_artifact_id="",
            result_artifact_hash="",
            terminal_status=_TERMINAL_EVENT_STATUS[terminal_type],
            run_manifest_payload=manifest_payload,
            result_artifact_payload=None,
            provider_calls=provider_calls,
            cost_usd=cost_usd,
            run_facts=run_facts,
        )

    def _consumed_provider_accounting(
        self,
        *,
        run_id: str,
        case_id: str,
        artifacts: Mapping[str, list[dict[str, Any]]],
        terminal_type: RunEventType,
        terminal_payload: Any,
    ) -> tuple[int, float | None, str, dict[str, Any]]:
        records = artifacts.get(PROVIDER_ACCOUNTING_EVENT_ARTIFACT_TYPE) or []
        if not records:
            raise Stage1AdapterError(
                f"run {run_id!r} ended "
                f"{_TERMINAL_EVENT_STATUS[terminal_type]} without a persisted "
                f"{PROVIDER_ACCOUNTING_EVENT_ARTIFACT_TYPE!r} artifact; the "
                "consumed provider calls/cost cannot be recovered and must not "
                "be reported as zero"
            )
        record = records[-1]
        payload = _parse_payload(record, run_id)
        if payload.get("schema_version") != PROVIDER_ACCOUNTING_SCHEMA_VERSION:
            raise Stage1AdapterError(
                f"run {run_id!r} provider accounting schema must be "
                f"{PROVIDER_ACCOUNTING_SCHEMA_VERSION}, got "
                f"{payload.get('schema_version')!r}"
            )
        if str(payload.get("run_id")) != run_id:
            raise Stage1AdapterError(
                f"run {run_id!r} provider accounting is bound to a different run"
            )
        if payload_sha256(payload) != str(record["payload_hash"]):
            raise Stage1AdapterError(
                f"run {run_id!r} provider accounting payload does not match "
                "its recorded payload hash"
            )
        provider = payload.get("provider")
        if not isinstance(provider, Mapping):
            raise Stage1AdapterError(
                f"run {run_id!r} provider accounting is missing the provider block"
            )
        provider_calls = int(provider.get("analyst_provider_attempts") or 0) + int(
            provider.get("writer_provider_attempts") or 0
        )
        cost_usd = provider.get("cost_usd")
        cost_method = str(provider.get("cost_method") or "unavailable")
        failure_code = getattr(terminal_payload, "failure_code", None)
        run_facts = {
            "schema_version": RUN_FACTS_SCHEMA_VERSION,
            "case_id": case_id,
            "terminal_status": _TERMINAL_EVENT_STATUS[terminal_type],
            "terminal_failure_code": failure_code,
            "latency_ms": None,
            "tokens": _total_tokens(provider),
            "cost_usd": cost_usd,
            "provider_calls": provider_calls,
            "provider_accounting": {
                "provider_calls": provider_calls,
                "cost_usd": cost_usd,
                "cost_method": cost_method,
                "tokens_in": provider.get("total_tokens_in"),
                "tokens_out": provider.get("total_tokens_out"),
            },
        }
        return provider_calls, cost_usd, cost_method, run_facts

    def _build_run_facts(
        self,
        *,
        run_id: str,
        case_id: str,
        diagnostics: Mapping[str, Any],
        diagnostics_artifact_hash: str,
        claims: list[dict[str, Any]],
        terminal_payload: Any,
    ) -> dict[str, Any]:
        retrieval = diagnostics["retrieval"]
        trajectory = diagnostics["trajectory"]
        provider = diagnostics["provider"]
        terminal = diagnostics["terminal"]

        rounds = tuple(trajectory.get("rounds") or ())
        gap_reason_codes: list[str] = []
        corrective_actions: list[str] = []
        research_fingerprints: list[str] = []
        added_evidence: list[str] = []
        for round_record in rounds:
            for code in round_record.get("gap_reason_codes") or ():
                if code not in gap_reason_codes:
                    gap_reason_codes.append(code)
            for action_id in round_record.get("action_ids") or ():
                if action_id not in corrective_actions:
                    corrective_actions.append(action_id)
            for fingerprint in round_record.get("research_fingerprints") or ():
                if fingerprint not in research_fingerprints:
                    research_fingerprints.append(fingerprint)
            delta = round_record.get("evidence_delta") or {}
            for evidence_id in delta.get("added_evidence_ids") or ():
                if evidence_id not in added_evidence:
                    added_evidence.append(evidence_id)

        # Sanity tasks are the initial research tasks the runtime actually
        # dispatched (the runtime's own observation, not a fabricated list).
        sanity_tasks = tuple(trajectory.get("initial_task_ids") or ())

        return {
            "schema_version": RUN_FACTS_SCHEMA_VERSION,
            "case_id": case_id,
            "output_status": terminal["result_status"],
            "latency_ms": terminal_payload.total_latency_ms,
            "tokens": _total_tokens(provider),
            "cost_usd": provider.get("cost_usd"),
            "attribution_type": terminal.get("attribution_type"),
            "refusal_reason": terminal.get("refusal_reason"),
            "refusal_reason_available": terminal["refusal_reason_available"],
            "status_ceiling": terminal.get("status_ceiling"),
            "claims": claims,
            "sanity_tasks_completed": list(sanity_tasks),
            # Contract-proven, not defaulted: a run excluded by a model or
            # runtime limitation terminalizes FAILED, so a COMPLETED row can
            # never be a model-limited exclusion. Coverage limitation is a
            # benchmark property (stratification), never a runtime fact.
            "model_limited": False,
            "trajectory": {
                "corrective_triggered": trajectory["corrective_rounds_executed"] > 0,
                "rounds_executed": trajectory["corrective_rounds_executed"],
                "gap_reason_codes": gap_reason_codes,
                "corrective_actions": corrective_actions,
                "research_fingerprints": research_fingerprints,
                "evidence_delta_ids": added_evidence,
                # The runtime observes structure creation as its own non-
                # abstaining terminal status; it never reports a stop judgement.
                "produced_structure": terminal["result_status"] != "ABSTAIN",
            },
            "retrieval": {
                "observed": retrieval["observed"],
                "pool": self._observed_pool_manifest(
                    case_id=case_id,
                    retrieval=retrieval,
                    diagnostics_artifact_hash=diagnostics_artifact_hash,
                    recorded_at=diagnostics.get("recorded_at"),
                ),
                "ranked_evidence_ids": list(
                    retrieval.get("ordered_final_ranked_evidence_ids") or ()
                ),
                "candidate_evidence_ids": list(
                    retrieval.get("ordered_candidate_evidence_ids") or ()
                ),
                "reranker_contributed": _reranker_contributed(retrieval),
                "latency_ms": retrieval.get("measured_latency_ms"),
                "degraded": bool(retrieval.get("degradation_reasons")),
                "ticker_violations": list(retrieval.get("ticker_violations") or ()),
                "cutoff_violations": list(retrieval.get("cutoff_violations") or ()),
            },
            "provider_accounting": {
                "provider_calls": provider["analyst_provider_attempts"]
                + provider["writer_provider_attempts"],
                "cost_usd": provider.get("cost_usd"),
                "cost_method": provider.get("cost_method"),
                "tokens_in": provider.get("total_tokens_in"),
                "tokens_out": provider.get("total_tokens_out"),
            },
            "diagnostics_artifact_hash": diagnostics_artifact_hash,
            "run_id": run_id,
        }

    @staticmethod
    def _observed_pool_manifest(
        *,
        case_id: str,
        retrieval: Mapping[str, Any],
        diagnostics_artifact_hash: str,
        recorded_at: Any,
    ) -> dict[str, Any]:
        """Build the observed retrieval-pool identity from persisted facts.

        Arms come from the retrieval stages the runtime actually served (with
        the benchmark-declared arm version), the inventory is the sorted unique
        observed candidate evidence set, and the manifest identity binds the
        run's runtime identity plus the persisted diagnostics artifact hash.
        """
        from catalyst_eval.benchmark.pool_manifest import PoolArm, PoolManifest

        arm_order = {"lexical": 0, "dense": 1, "hybrid": 2, "reranked": 3}
        observed_arms: dict[str, int] = {}
        for arm in retrieval.get("arms") or ():
            name = _ARM_ALIASES.get(str(arm.get("arm")))
            if name is None:
                continue
            top_k = int(arm.get("top_k") or 0)
            if top_k > 0:
                observed_arms[name] = max(observed_arms.get(name, 0), top_k)
        if not observed_arms:
            raise Stage1AdapterError(
                f"retrieval diagnostics for {case_id!r} record no served "
                "retrieval arm; the observed pool identity cannot be built"
            )
        arms = tuple(
            PoolArm(arm=name, version=_DECLARED_ARM_VERSION, top_k=observed_arms[name])
            for name in sorted(observed_arms, key=lambda n: arm_order[n])
        )
        candidates = retrieval.get("ordered_candidate_evidence_ids") or ()
        created_at = recorded_at
        if created_at is None:
            raise Stage1AdapterError(
                f"retrieval diagnostics for {case_id!r} have no recorded_at; "
                "observed pool created_at cannot be inferred"
            )
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise Stage1AdapterError(
                    f"retrieval diagnostics for {case_id!r} have invalid recorded_at"
                ) from exc
        if not isinstance(created_at, datetime):
            raise Stage1AdapterError(
                f"retrieval diagnostics for {case_id!r} have invalid recorded_at"
            )
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        index_manifest_id = retrieval.get("index_manifest_id")
        return PoolManifest(
            schema_version="1.0.0",
            pool_id=_pool_id(
                schema_version="1.0.0",
                case_id=case_id,
                arms=arms,
                chunk_inventory=tuple(sorted(set(candidates))),
                corpus_manifest_id=str(retrieval["corpus_manifest_id"]),
                index_manifest_id=index_manifest_id,
                source_artifact_id=diagnostics_artifact_hash,
            ),
            case_id=case_id,
            arms=arms,
            chunk_inventory=tuple(sorted(set(candidates))),
            corpus_manifest_id=str(retrieval["corpus_manifest_id"]),
            index_manifest_id=index_manifest_id,
            source_artifact_id=diagnostics_artifact_hash,
            created_at=created_at,
        ).model_dump(mode="json")

    def _claim_plan_ref(
        self,
        run_id: str,
        events: list[dict[str, Any]],
        claim_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        validated = [
            e
            for e in events
            if e["event_type"] is RunEventType.STAGE_STARTED
            and str(getattr(e["payload"], "stage", "")).lower() == "claim_validation"
        ]
        if not validated:
            raise Stage1AdapterError(
                f"run {run_id!r} persisted no claim-validation stage event; the "
                "validated claim-plan identity cannot be sealed"
            )
        ordered = [
            {
                "artifact_id": str(record["artifact_id"]),
                "artifact_hash": _sha256_canonical(_parse_payload(record, run_id)),
            }
            for record in claim_records
        ]
        return {
            "artifact_id": f"claimplan:{run_id}",
            "artifact_hash": hashlib.sha256(
                canonical_bytes({"run_id": run_id, "claims": ordered})
            ).hexdigest(),
        }

    def _build_claims(
        self, run_id: str, claim_records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        for record in claim_records:
            payload = _parse_payload(record, run_id)
            claim_id = str(payload.get("claim_id") or "")
            role = str(payload.get("role") or "")
            if not claim_id or not role:
                raise Stage1AdapterError(
                    f"run {run_id!r} claim artifact {record['artifact_id']!r} "
                    "is missing claim_id/role; a claim fact is never invented"
                )
            claims.append(
                {
                    "claim_id": claim_id,
                    "material": role in {"PRIMARY", "SECONDARY"},
                    "citation_ids": list(payload.get("citation_evidence_ids") or ()),
                    "role": role,
                    "statement": payload.get("statement") or "",
                }
            )
        return claims


def _reranker_contributed(retrieval: Mapping[str, Any]) -> bool:
    """The reranker contributed iff the runtime served a reranked stage."""
    for arm in retrieval.get("arms") or ():
        if _ARM_ALIASES.get(str(arm.get("arm"))) == _RERANKED_STAGE:
            return True
    return False


def _total_tokens(provider: Mapping[str, Any]) -> int | None:
    tokens_in = provider.get("total_tokens_in")
    tokens_out = provider.get("total_tokens_out")
    if tokens_in is None or tokens_out is None:
        return None
    return tokens_in + tokens_out


def _require_complete_model_payload(
    payload: Any, model_type: Any, label: str
) -> None:
    """Require persisted JSON to name every typed contract field exactly."""
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be an object")
    expected = set(model_type.model_fields)
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{label} fields mismatch: missing={missing} extra={extra}")


def _pool_id(
    *,
    schema_version: str,
    case_id: str,
    arms: Any,
    chunk_inventory: tuple[str, ...],
    corpus_manifest_id: str,
    index_manifest_id: str | None,
    source_artifact_id: str,
) -> str:
    payload = {
        "schema_version": schema_version,
        "case_id": case_id,
        "arms": [
            {"arm": arm.arm, "version": arm.version, "top_k": arm.top_k}
            for arm in arms
        ],
        "chunk_inventory": list(chunk_inventory),
        "corpus_manifest_id": corpus_manifest_id,
        "index_manifest_id": index_manifest_id,
        "source_artifact_id": source_artifact_id,
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _parse_event_payload(payload_json: str, run_id: str, event_type: RunEventType) -> Any:
    payload = _parse_payload_json(payload_json, run_id)
    model = _EVENT_PAYLOAD_TYPES.get(event_type)
    if model is None:
        raise Stage1AdapterError(
            f"run {run_id!r} persisted unknown event type {event_type.value!r}"
        )
    try:
        return model.model_validate(payload)
    except Exception as exc:  # pragma: no cover - pydantic error path
        raise Stage1AdapterError(
            f"run {run_id!r} event {event_type.value!r} payload violates the "
            f"sealed envelope contract: {exc}"
        ) from exc


def _parse_payload(record: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    return _parse_payload_json(record["payload_json"], run_id)


def _parse_payload_json(payload_json: str, run_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise Stage1AdapterError(
            f"run {run_id!r} persisted payload is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise Stage1AdapterError(
            f"run {run_id!r} persisted payload must be a JSON object"
        )
    return payload


__all__ = [
    "DIAGNOSTICS_EVENT_ARTIFACT_TYPE",
    "DIAGNOSTICS_SCHEMA_VERSION",
    "M6AppSseRunnerAdapter",
    "Stage1AdapterError",
    "default_case_request_builder",
]
