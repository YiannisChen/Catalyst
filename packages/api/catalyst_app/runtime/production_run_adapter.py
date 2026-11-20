"""Production run adapter moved out of composition."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from catalyst_agents.graphs.graph import run_v1_graph
from catalyst_agents.runtime.control import (
    RunCancelledError,
    RunDeadlineExceededError,
    raise_if_control_expired,
)
from catalyst_agents.runtime.manifest import RunManifest
from catalyst_data.canonical.temporal import utc_iso_z

from catalyst_app.events import (
    AnalystDecisionPayload,
    AnalystDonePayload,
    AnswerReadyPayload,
    AssuranceCompletedPayload,
    ChecksDonePayload,
    EvidenceAssessedPayload,
    FactsReadyPayload,
    GatesDonePayload,
    InvestigatorDonePayload,
    RetrievalDonePayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
    StageStartedPayload,
    TierDecidedPayload,
    VerifierDonePayload,
    run_has_answer_ready,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import (
    ArtifactPayload,
    EventRepository,
    IllegalLifecycleTransitionError,
    TerminalRunError,
)
from catalyst_app.runtime.artifact_persistence import RunArtifactsPackPersistence
from catalyst_app.runtime.cancel import (
    CancellationController,
    CancellationTokenRegistry,
)
from catalyst_app.runtime.claim import RunClaimer
from catalyst_app.runtime.composition import (
    PROVIDER_ACCOUNTING_ARTIFACT_TYPE,
    PROVIDER_ACCOUNTING_SCHEMA_VERSION,
    TIMEOUT_FAILURE_CODE,
    GraphRuntimeResolver,
    RunBoundary,
    _AdapterRunControl,
    _evidence_detail_payload,
    _WebLeadRetriever,
    logger,
)
from catalyst_app.runtime.manifest_factory import production_prompt_identity
from catalyst_app.runtime.stream_bridge import StreamBridge
from catalyst_app.runtime_credential_store import RuntimeCredentialStore


class ProductionRunAdapter:
    """One app-owned run adapter per run: claim -> resolve -> run_v1_graph ->
    persist artifacts -> atomic terminal commit -> release only after commit."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        events: EventRepository,
        claimer: RunClaimer,
        tokens: CancellationTokenRegistry,
        cancellation: CancellationController,
        credential_store: RuntimeCredentialStore,
        graph_resolver: GraphRuntimeResolver,
        provider_budget: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.events = events
        self.claimer = claimer
        self.tokens = tokens
        self.cancellation = cancellation
        self.credential_store = credential_store
        self.graph_resolver = graph_resolver
        self.provider_budget = provider_budget
        self._budget_local = threading.local()
        self._accounting_persisted: set[str] = set()
        self.last_deadline_epoch_ms: int | None = None

    def __call__(self, run_id: str, timeout_seconds: float) -> dict[str, Any]:
        # Per-run local start: the adapter is shared process-wide and must
        # never publish one adapter-wide elapsed time across sequential runs.
        started_monotonic = time.monotonic()
        self._budget_local.guard = None
        manifest = self._load_manifest(run_id)
        if manifest is None:
            raise RuntimeError(f"run {run_id} has no persisted immutable RunManifest")
        self._budget_local.guard = self._provider_budget_for_run(run_id)
        if not self.claimer.claim_run(run_id, owner="executor", task_token=uuid4().hex):
            return {"run_id": run_id, "status": "NOT_CLAIMED"}
        # The one absolute deadline was derived at admission (persisted
        # deadline_at / created_at + run_timeout_seconds); execution reuses it
        # so queue delay reduces the remaining budget and never resets it.
        deadline_epoch_ms = self._absolute_deadline_epoch_ms(
            run_id, manifest, timeout_seconds
        )
        self.last_deadline_epoch_ms = deadline_epoch_ms
        token = self.tokens.token(run_id)
        control = _AdapterRunControl(
            token=token, deadline_epoch_ms=deadline_epoch_ms
        )
        # Fail fast before any stage event or provider dispatch when
        # cancellation/deadline already won while the run was queued.
        if token.requested or self._is_cancel_requested(run_id):
            self._persist_provider_accounting(run_id)
            return self._acknowledge_cancelled(run_id)
        if self._past_deadline(deadline_epoch_ms):
            self._persist_provider_accounting(run_id)
            return self._terminalize_timeout(run_id)
        self._append_stage_started(run_id, "research", round=1)
        bridge = StreamBridge(db_path=self.db_path, events=self.events, run_id=run_id)
        packs = RunArtifactsPackPersistence(
            db_path=self.db_path, events=self.events, run_id=run_id
        )
        try:
            boundary = self._load_boundary(
                run_id, deadline_epoch_ms=deadline_epoch_ms
            )
            resolved = self.graph_resolver.resolve(manifest, boundary)

            progress_payloads = {
                RunEventType.FACTS_READY: FactsReadyPayload,
                RunEventType.TIER_DECIDED: TierDecidedPayload,
                RunEventType.RETRIEVAL_DONE: RetrievalDonePayload,
                RunEventType.ANALYST_DONE: AnalystDonePayload,
                RunEventType.ANALYST_DECISION: AnalystDecisionPayload,
                RunEventType.CHECKS_DONE: ChecksDonePayload,
                RunEventType.GATES_DONE: GatesDonePayload,
                RunEventType.VERIFIER_DONE: VerifierDonePayload,
                RunEventType.ANSWER_READY: AnswerReadyPayload,
                RunEventType.INVESTIGATOR_DONE: InvestigatorDonePayload,
            }

            def persist_progress(event_name: str, payload: dict[str, Any]) -> None:
                # Progress events are for display only: a bad payload is logged and dropped,
                # never allowed to fail the attribution run.
                try:
                    event_type = RunEventType(event_name)
                    payload_model = progress_payloads[event_type].model_validate(payload)
                    self.events.append(
                        run_id=run_id,
                        event_type=event_type,
                        stage=event_type.value.split(".", 1)[0].upper(),
                        payload=payload_model,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("progress event %s dropped for run %s", event_name, run_id)

            run_retriever = resolved.retriever
            web_lead = None
            run_attribution_policy = manifest.attribution_policy
            if manifest.web_search_lead_id:
                import json

                from catalyst_data.ingestion.collectors.web_search import get_pre_lead
                from catalyst_data.retrieval.query_policy import UNIVERSE_SPEC_PATH

                web_lead = get_pre_lead(manifest.web_search_lead_id)
                if (
                    web_lead is None
                    or web_lead["ticker"] != self._load_ticker(run_id)
                    or web_lead["session_date"] != manifest.temporal_identity.session_date
                ):
                    raise RuntimeError("selected web lead is unavailable for this run")
                run_retriever = _WebLeadRetriever(web_lead)
                if run_attribution_policy is not None:
                    run_attribution_policy = run_attribution_policy.model_copy(
                        update={"corrective_enabled": False}
                    )
                universe = json.loads(Path(UNIVERSE_SPEC_PATH).read_text())
                web_company_name = str(
                    universe["companies"].get(web_lead["ticker"], {}).get("legal_name") or web_lead["ticker"]
                )
            else:
                web_company_name = None

            result = run_v1_graph(
                provider_budget=self._current_provider_budget(),
                provider_identity=boundary.provider,
                analyst_model_identity=manifest.analyst_model_id,
                writer_model_identity=manifest.writer_model_id,
                run_id=run_id,
                temporal_identity=manifest.temporal_identity,
                data_runtime_identity=resolved.data_runtime_identity,
                ticker=self._load_ticker(run_id),
                cutoff=utc_iso_z(manifest.temporal_identity.cutoff_at),
                requested_manifest_id=resolved.data_runtime_identity.corpus_manifest_id,
                observation_provider=resolved.observation_provider,
                retriever=run_retriever,
                stage2_retriever=resolved.stage2_retriever,
                stage2_temporal_identity=manifest.stage2_temporal_identity,
                desk_context_fn=resolved.desk_context_fn,
                investigator_deps=getattr(resolved, "investigator_deps", None),
                policy_config=manifest.runtime_configuration.observation_policy,
                research_concurrency=manifest.runtime_configuration.max_initial_research_concurrency,
                research_stage_timeout_seconds=manifest.runtime_configuration.research_stage_timeout_seconds,
                persistence=packs,
                packing_policy_version=manifest.packing_policy_version,
                template_bytes=production_prompt_identity(
                    getattr(manifest.attribution_policy, "evidence_window_policy", "pre_close_v1")
                )[0],
                template_version=production_prompt_identity(
                    getattr(manifest.attribution_policy, "evidence_window_policy", "pre_close_v1")
                )[1],
                analyst_llm=resolved.analyst_llm,
                writer_llm=resolved.writer_llm,
                capability_registry=resolved.capability_registry,
                corrective_policy=resolved.corrective_policy,
                prompt_template=resolved.prompt_template,
                query_builder=resolved.query_builder,
                structured_provider=resolved.structured_provider,
                run_deadline_epoch_ms=deadline_epoch_ms,
                normalization_policy_version="assessment-normalizer-v1",
                writer_sink=bridge,
                control=control,
                attribution_policy=run_attribution_policy,
                force_full=bool(
                    getattr(manifest.attribution_policy, "force_full", False)
                ),
                on_step=persist_progress,
                web_source_title=web_lead["title"] if web_lead else None,
                web_source_company_name=web_company_name,
            )
            # Cooperative boundary: observed before result-artifact
            # persistence and immediately before the terminal commit.
            raise_if_control_expired(control)
            self._persist_result_artifacts(run_id, result, bridge)
            raise_if_control_expired(control)
            diagnostics = self._build_run_diagnostics(
                run_id=run_id,
                manifest=manifest,
                resolved=resolved,
                result=result,
                provider_budget=self._current_provider_budget(),
            )
            return self._terminalize_completed(
                run_id,
                result,
                bridge,
                started_monotonic=started_monotonic,
                diagnostics=diagnostics,
            )
        except RunCancelledError:
            self._persist_provider_accounting(run_id)
            return self._acknowledge_cancelled(run_id)
        except RunDeadlineExceededError:
            self._persist_provider_accounting(run_id)
            return self._terminalize_timeout(run_id)
        except BaseException as exc:
            if token.requested or self._is_cancel_requested(run_id):
                self._persist_provider_accounting(run_id)
                return self._acknowledge_cancelled(run_id)
            if self._past_deadline(deadline_epoch_ms):
                self._persist_provider_accounting(run_id)
                return self._terminalize_timeout(run_id)
            self._persist_provider_accounting(run_id)
            raise
        finally:
            self.credential_store.remove(run_id)

    # -- internals ---------------------------------------------------------

    def _load_manifest(self, run_id: str) -> RunManifest | None:
        with open_rw(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload_json FROM run_artifacts"
                " WHERE run_id = ? AND artifact_type = 'run_manifest'"
                " ORDER BY event_seq ASC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return RunManifest.model_validate_json(row["payload_json"])

    def _load_boundary(
        self, run_id: str, *, deadline_epoch_ms: int | None = None
    ) -> RunBoundary:
        with open_rw(self.db_path) as conn:
            row = conn.execute(
                "SELECT provider, base_url, run_manifest_id, access_tier, visibility "
                "FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        manifest = self._load_manifest(run_id)
        if row is None or manifest is None:
            raise RuntimeError(f"run {run_id} boundary identity is missing")
        credential = self.credential_store.get(run_id)
        provider_timeout_seconds: float | None = None
        if deadline_epoch_ms is not None:
            remaining_ms = max(0, deadline_epoch_ms - int(time.time() * 1000))
            provider_timeout_seconds = remaining_ms / 1000.0
        from catalyst_data.ingestion.tavily_ledger import TavilyCallContext

        keys = set(row.keys()) if hasattr(row, "keys") else set()
        tier = row["access_tier"] if "access_tier" in keys else "local"
        return RunBoundary(
            provider=row["provider"] or "unknown",
            base_url=row["base_url"],
            model_id=manifest.analyst_model_id,
            api_key=credential.api_key if credential is not None else None,
            provider_timeout_seconds=provider_timeout_seconds,
            tavily_context=TavilyCallContext.from_environment(
                db_path=self.db_path, tier=tier, run_id=run_id
            ),
        )

    def _load_ticker(self, run_id: str) -> str:
        with open_rw(self.db_path) as conn:
            row = conn.execute(
                "SELECT ticker FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None or not row["ticker"]:
            raise RuntimeError(f"run {run_id} ticker identity is missing")
        return row["ticker"]

    def _append_stage_started(self, run_id: str, stage: str, *, round: int) -> None:
        self.events.append(
            run_id=run_id,
            event_type=RunEventType.STAGE_STARTED,
            stage=stage.upper(),
            payload=StageStartedPayload(stage=stage, round=round),
        )

    def _is_cancel_requested(self, run_id: str) -> bool:
        lifecycle = self.claimer.current_lifecycle(run_id)
        return lifecycle is RunLifecycleStatus.CANCEL_REQUESTED

    def _past_deadline(self, deadline_epoch_ms: int) -> bool:
        return int(time.time() * 1000) >= deadline_epoch_ms

    def _elapsed_latency_ms(self, started_monotonic: float) -> int:
        """Real monotonic elapsed latency for one run since its local start.

        Floors at zero so genuinely sub-millisecond runs report 0.
        """
        return max(0, int((time.monotonic() - started_monotonic) * 1000))

    def _absolute_deadline_epoch_ms(
        self, run_id: str, manifest: Any, timeout_seconds: float
    ) -> int:
        """Absolute epoch-ms deadline: persisted deadline_at first, then the
        deterministic created_at + run_timeout_seconds formula, then the
        submitted remaining budget for legacy rows."""
        try:
            with open_rw(self.db_path) as conn:
                row = conn.execute(
                    "SELECT deadline_at, created_at FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            if row is not None and row["deadline_at"]:
                deadline = datetime.fromisoformat(
                    row["deadline_at"].replace("Z", "+00:00")
                )
                return int(deadline.timestamp() * 1000)
            if row is not None and row["created_at"]:
                created = datetime.fromisoformat(
                    row["created_at"].replace("Z", "+00:00")
                )
                deadline = created + timedelta(
                    seconds=manifest.run_timeout_seconds
                )
                return int(deadline.timestamp() * 1000)
        except (TypeError, ValueError):
            pass
        # Legacy rows without a parseable creation time: the submitted
        # remaining budget is the only bound available.
        return int((time.time() + timeout_seconds) * 1000)

    def _acknowledge_cancelled(self, run_id: str) -> dict[str, Any]:
        # Never claim CANCELLED unless the durable terminalization succeeded;
        # a durable failure propagates to the executor failure handler so the
        # row terminalizes FAILED instead.
        self.cancellation.acknowledge_cancellation(run_id)
        return {"run_id": run_id, "status": "CANCELLED"}

    def _terminalize_timeout(self, run_id: str) -> dict[str, Any]:
        # RG-13: the pushed answer is provisional and invalidated by this
        # terminal event iff an answer.ready event exists in the run's log.
        provisional_output_invalidated = run_has_answer_ready(self.db_path, run_id)
        self.claimer.terminalize(
            run_id=run_id,
            assurance_payload=AssuranceCompletedPayload(
                valid=False, violations=("run_timeout",)
            ),
            terminal_event_type=RunEventType.RUN_FAILED,
            terminal_payload=RunFailedPayload(
                failure_code=TIMEOUT_FAILURE_CODE,
                stage="EXECUTION",
                retryable=False,
                safe_message="Run exceeded its absolute deadline.",
                provisional_output_invalidated=provisional_output_invalidated,
            ),
            lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.FAILED),
        )
        return {"run_id": run_id, "status": "FAILED", "failure_code": TIMEOUT_FAILURE_CODE}

    def _persist_result_artifacts(
        self, run_id: str, result: Any, bridge: StreamBridge
    ) -> None:
        if getattr(result, "tier_only", False):
            self.events.append(
                run_id=run_id,
                event_type=RunEventType.STAGE_STARTED,
                stage="TIER_ONLY",
                payload=StageStartedPayload(stage="tier_only", round=1),
                artifact_payloads=[
                    ArtifactPayload(
                        artifact_id=f"answer_snapshot:{run_id}",
                        artifact_type="answer_snapshot",
                        payload=result.answer_snapshot,
                    )
                ],
            )
            return
        assessment = result.assessment
        all_items = tuple(result.evidence_state.evidence_items) + tuple(
            result.evidence_state.structured_facts
        )
        accepted_ids = tuple(
            d.evidence_id
            for d in assessment.normalized_evidence_decisions
            if d.disposition.value == "SUPPORT"
        )
        rejected_ids = tuple(
            d.evidence_id
            for d in assessment.normalized_evidence_decisions
            if d.disposition.value != "SUPPORT"
        )
        gap_ids = tuple(gap.gap_id for gap in assessment.validated_missing_evidence)
        self.events.append(
            run_id=run_id,
            event_type=RunEventType.EVIDENCE_ASSESSED,
            stage="EVIDENCE_ANALYST",
            payload=EvidenceAssessedPayload(
                accepted_evidence_ids=accepted_ids,
                lead_only_evidence_ids=(),
                rejected_evidence_ids=rejected_ids,
                reason_codes=tuple(
                    d.reason_code for d in assessment.normalized_evidence_decisions
                ),
                gap_ids=gap_ids,
                status_ceiling=assessment.status_ceiling.value,
                decision=assessment.research_decision.value,
                normalization_diagnostics=tuple(
                    getattr(assessment, "normalization_diagnostics", ())
                ),
            ),
            artifact_payloads=[
                ArtifactPayload(
                    artifact_id=f"evidence:{run_id}:{item.evidence_id}",
                    artifact_type="evidence_detail",
                    payload=_evidence_detail_payload(item),
                )
                for item in all_items
            ] + (
                # What each stage decided and why (agents observability/decision_trace.py).
                [ArtifactPayload(artifact_id=f"decision_trace:{run_id}",
                                 artifact_type="decision_trace",
                                 payload=result.decision_trace)]
                if getattr(result, "decision_trace", None) else []
            ) + (
                [ArtifactPayload(artifact_id=f"answer_snapshot:{run_id}",
                                 artifact_type="answer_snapshot",
                                 payload=result.answer_snapshot)]
                if getattr(result, "answer_snapshot", None) else []
            ),
        )
        claim_artifacts = [
            ArtifactPayload(
                artifact_id=f"claim:{run_id}:{claim.claim_id}",
                artifact_type="claim_detail",
                payload={
                    "claim_id": claim.claim_id,
                    "role": claim.role.value,
                    "statement": claim.statement,
                    "mechanism": claim.mechanism,
                    "support_evidence_ids": list(claim.support_evidence_ids),
                    "counter_evidence_ids": list(claim.counter_evidence_ids),
                    "limitations": list(claim.limitations),
                    "citation_evidence_ids": list(claim.citation_evidence_ids),
                    "validation_status": "validated",
                    "validation_codes": [],
                },
            )
            for claim in result.validated_claim_plan.claims
        ]
        if claim_artifacts:
            self.events.append(
                run_id=run_id,
                event_type=RunEventType.STAGE_STARTED,
                stage="CLAIM_VALIDATION",
                payload=StageStartedPayload(
                    stage="claim_validation", round=1 + result.corrective_rounds
                ),
                artifact_payloads=claim_artifacts,
            )

    def _current_provider_budget(self) -> Any | None:
        return getattr(self._budget_local, "guard", self.provider_budget)

    def _provider_budget_for_run(self, run_id: str) -> Any:
        from catalyst_agents.runtime.provider_budget import (
            ModelPrice,
            ProviderBudgetGuard,
            ProviderPriceUnavailable,
        )

        with open_rw(self.db_path) as conn:
            row = conn.execute(
                "SELECT r.access_tier, b.max_run_cost_micros, b.max_run_tokens "
                "FROM runs r LEFT JOIN run_budget_reservations b ON b.run_id=r.run_id "
                "WHERE r.run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("run access tier unavailable")
        access_tier = row["access_tier"]
        max_tokens = row["max_run_tokens"]
        token_bounded = max_tokens is not None
        if token_bounded:
            max_cost = None
        elif access_tier in {"anonymous", "invite"}:
            if row["max_run_cost_micros"] is None:
                raise RuntimeError("budget reservation unavailable")
            max_cost = int(row["max_run_cost_micros"]) / 1_000_000
        elif access_tier in {"admin", "local"}:
            template = self.provider_budget
            configured_cap = (
                int(row["max_run_cost_micros"]) / 1_000_000
                if access_tier == "admin" and row["max_run_cost_micros"] is not None
                else getattr(template, "max_cost_usd", None)
            )
            if configured_cap is None:
                configured_cap = os.environ.get("CATALYST_MAX_RUN_MODEL_COST_USD", 0.05)
            try:
                max_cost = float(configured_cap)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("invalid non-public provider budget configuration") from exc
            if not math.isfinite(max_cost) or max_cost <= 0:
                raise RuntimeError("invalid non-public provider budget configuration")
        else:
            raise RuntimeError("run access tier unavailable")
        template = self.provider_budget
        max_calls = int(getattr(template, "max_provider_calls", 2**31 - 1))
        from catalyst_app.runtime.model_pricing import (
            PricingUnavailable,
            load_pricing_config,
        )

        if access_tier in {"anonymous", "invite"} and not token_bounded:
            # Public runs use the one checked-in pricing source. The inline
            # JSON variable remains an admin/local compatibility surface only;
            # a public run never dispatches without this file's prices and
            # role token bounds.
            try:
                public_pricing = load_pricing_config()
            except PricingUnavailable as exc:
                raise ProviderPriceUnavailable("MODEL_PRICE_UNAVAILABLE") from exc
            prices = dict(public_pricing.prices)
            role_limits = dict(public_pricing.role_token_limits)
        else:
            # Admin runs start from the same checked-in prices and role token
            # bounds; without them every cost-capped run failed before its
            # first model call. A configured template and the inline JSON
            # variable still override per model.
            try:
                base_pricing = load_pricing_config()
                prices = dict(base_pricing.prices)
                role_limits = dict(base_pricing.role_token_limits)
            except PricingUnavailable:
                prices, role_limits = {}, {}
            prices.update(getattr(template, "_prices", {}) or {})
            role_limits.update(getattr(template, "_role_limits", {}) or {})
            raw_prices = os.environ.get("CATALYST_MODEL_PRICES_JSON")
            if raw_prices:
                try:
                    records = json.loads(raw_prices)
                    if not isinstance(records, list):
                        raise ValueError("must be an array")
                    configured_pairs: set[tuple[str, str]] = set()
                    for item in records:
                        if not isinstance(item, dict):
                            raise ValueError("each model price must be an object")
                        input_price = item["input_usd_per_million_tokens"]
                        output_price = item["output_usd_per_million_tokens"]
                        if (
                            isinstance(input_price, bool)
                            or isinstance(output_price, bool)
                            or not math.isfinite(float(input_price))
                            or not math.isfinite(float(output_price))
                        ):
                            raise ValueError("model prices must be finite numbers")
                        price = ModelPrice(
                            provider=str(item["provider"]),
                            model_id=str(item["model_id"]),
                            input_usd_per_million_tokens=float(input_price),
                            output_usd_per_million_tokens=float(output_price),
                        )
                        identity = (price.provider, price.model_id)
                        if identity in configured_pairs:
                            raise ValueError("model price identity is duplicated")
                        configured_pairs.add(identity)
                        prices[identity] = price
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError("CATALYST_MODEL_PRICES_JSON is invalid") from exc
        return ProviderBudgetGuard(
            max_provider_calls=max_calls,
            max_cost_usd=max_cost,
            max_tokens=int(max_tokens) if token_bounded else None,
            prices=prices,
            role_token_limits=role_limits,
        )

    def _build_run_diagnostics(
        self,
        *,
        run_id: str,
        manifest: Any,
        resolved: Any,
        result: Any,
        provider_budget: Any | None = None,
    ) -> Any:
        """Assemble the observed diagnostics artifact for one completed run.

        Retrieval facts come from the graph's own retrieval execution; provider
        accounting comes from the shared budget guard when one is bound and
        from the graph's recorded logical/attempt counts otherwise. Nothing is
        invented: an unobserved fact stays ``None``/``unavailable``.
        """
        from catalyst_agents.observability import (
            ProviderAccountingDiagnostics,
            build_run_diagnostics,
            unavailable_retrieval_diagnostics,
        )

        identity = resolved.data_runtime_identity
        retrieval = getattr(result, "retrieval_diagnostics", None)
        if retrieval is None:
            retrieval = unavailable_retrieval_diagnostics(identity)

        cost_usd: float | None = None
        cost_method: str = "unavailable"
        if provider_budget is not None:
            # The per-case view is the run's own accounting; the cumulative
            # guard view spans every case in the process.
            snapshot = provider_budget.case_snapshot()
            cost_usd = snapshot.get("case_used_cost_usd")
            cost_method = str(snapshot.get("cost_method") or "unavailable")
            if cost_usd is None or cost_method == "unavailable":
                cost_usd, cost_method = None, "unavailable"
        provider = ProviderAccountingDiagnostics(
            analyst_logical_calls=int(getattr(result, "analyst_logical_calls", 0)),
            analyst_provider_attempts=int(
                getattr(result, "analyst_provider_attempts", 0)
            ),
            writer_logical_calls=int(getattr(result, "writer_logical_calls", 0)),
            writer_provider_attempts=int(
                getattr(result, "writer_provider_attempts", 0)
            ),
            verifier_logical_calls=int(getattr(result, "verifier_logical_calls", 0)),
            verifier_provider_attempts=int(
                getattr(result, "verifier_provider_attempts", 0)
            ),
            total_tokens_in=getattr(result, "reported_input_tokens", None),
            total_tokens_out=getattr(result, "reported_output_tokens", None),
            cost_usd=cost_usd,
            cost_method=cost_method,
        )
        return build_run_diagnostics(
            result=result,
            run_id=run_id,
            retrieval=retrieval,
            provider=provider,
            data_runtime_identity_ref=manifest.data_runtime_identity_ref,
            data_runtime_identity=identity,
        )

    def _persist_provider_accounting(self, run_id: str) -> None:
        """Persist the run's consumed provider accounting as a durable artifact.

        FAILED/CANCELLED/TIMEOUT runs never reach the completed diagnostics
        publication, but the provider attempts they already dispatched are
        real consumption. The guard's per-case view is the authoritative
        record of what was reserved and settled, so it is written to the run's
        own runtime database before the terminal event. Idempotent per run and
        a no-op when no budget guard is bound or nothing was consumed.
        """
        provider_budget = self._current_provider_budget()
        if provider_budget is None:
            return
        if run_id in self._accounting_persisted:
            return
        snapshot = provider_budget.case_snapshot()
        token_snapshot = {
            "max_tokens": snapshot.get("max_tokens"),
            "used_tokens": snapshot.get("used_tokens"),
            "outstanding_tokens": snapshot.get("outstanding_tokens"),
            "remaining_tokens": snapshot.get("remaining_tokens"),
            "token_method": snapshot.get("token_method"),
        }
        role_accounting = snapshot.get("case_role_accounting") or {}

        def role_value(role: str, field: str) -> int:
            values = role_accounting.get(role) or {}
            return int(values.get(field) or 0)

        analyst_logical_calls = role_value("evidence_analyst", "logical_calls")
        analyst_provider_attempts = role_value(
            "evidence_analyst", "provider_attempts"
        )
        writer_logical_calls = role_value("streaming_writer", "logical_calls")
        writer_provider_attempts = role_value(
            "streaming_writer", "provider_attempts"
        )
        payload = {
            "schema_version": PROVIDER_ACCOUNTING_SCHEMA_VERSION,
            "run_id": run_id,
            "provider": {
                "analyst_logical_calls": analyst_logical_calls,
                "analyst_provider_attempts": analyst_provider_attempts,
                "writer_logical_calls": writer_logical_calls,
                "writer_provider_attempts": writer_provider_attempts,
                "verifier_logical_calls": role_value("primary_verifier", "logical_calls"),
                "verifier_provider_attempts": role_value(
                    "primary_verifier", "provider_attempts"
                ),
                "reported_reason_logical_calls": role_value("reported_reason", "logical_calls"),
                "reported_reason_provider_attempts": role_value("reported_reason", "provider_attempts"),
                "total_tokens_in": None,
                "total_tokens_out": None,
                "cost_usd": snapshot.get("case_used_cost_usd"),
                "cost_method": str(snapshot.get("cost_method") or "unavailable"),
                **token_snapshot,
                "roles": {
                    role: dict(values)
                    for role, values in role_accounting.items()
                },
            },
            "provider_calls": int(snapshot.get("case_used_provider_calls") or 0),
            "cost_usd": snapshot.get("case_used_cost_usd"),
            "cost_method": str(snapshot.get("cost_method") or "unavailable"),
            "outstanding_cost_usd": snapshot.get("case_outstanding_cost_usd"),
            **token_snapshot,
        }
        try:
            self.events.append(
                run_id=run_id,
                event_type=RunEventType.STAGE_STARTED,
                stage="PROVIDER_ACCOUNTING",
                payload=StageStartedPayload(stage="provider_accounting", round=1),
                artifact_payloads=[
                    ArtifactPayload(
                        artifact_id=f"provider-accounting:{run_id}",
                        artifact_type=PROVIDER_ACCOUNTING_ARTIFACT_TYPE,
                        payload=payload,
                    )
                ],
            )
        except TerminalRunError:
            # The run already terminalized (the completed path publishes its
            # accounting inside diagnostics); the durable record exists.
            self._accounting_persisted.add(run_id)
            return
        self._accounting_persisted.add(run_id)

    def _terminalize_completed(
        self,
        run_id: str,
        result: Any,
        bridge: StreamBridge,
        *,
        started_monotonic: float,
        diagnostics: Any | None = None,
    ) -> dict[str, Any]:
        final_status = result.validated_claim_plan.status.value
        final_type = result.validated_claim_plan.attribution_type.value
        envelope = bridge.assured_envelope() or {}
        terminal_artifacts = [
            ArtifactPayload(
                artifact_id=f"attribution:{run_id}",
                artifact_type="attribution_result",
                payload={
                    "attribution_status": final_status,
                    "attribution_type": final_type,
                },
            ),
            ArtifactPayload(
                artifact_id=f"assurance:{run_id}",
                artifact_type="assurance",
                payload={
                    "valid": True,
                    "checks": [check.check_name for check in result.assurance_checks],
                    "final_result_status": final_status,
                    "attribution_type": final_type,
                    "completed_at": envelope.get("completed_at"),
                },
            ),
        ]
        if diagnostics is not None:
            # Published in the same terminal transaction as assurance +
            # run.completed, so the diagnostics are bound to the terminal
            # sequence and to the run's runtime identity.
            terminal_artifacts.append(
                ArtifactPayload(
                    artifact_id=f"diagnostics:{run_id}",
                    artifact_type="run_diagnostics",
                    payload=diagnostics.model_dump(mode="json"),
                    schema_version="v1",
                )
            )
        try:
            self.claimer.terminalize(
                run_id=run_id,
                assurance_payload=AssuranceCompletedPayload(
                    valid=True, final_result_status=final_status
                ),
                terminal_event_type=RunEventType.RUN_COMPLETED,
                terminal_payload=RunCompletedPayload(
                    result_status=final_status,
                    final_output_artifact_ref=f"answer:stream:{run_id}",
                    total_latency_ms=self._elapsed_latency_ms(started_monotonic),
                    total_tokens=None,
                    runtime_identity_ref=self._load_manifest(run_id).data_runtime_identity_ref,
                ),
                required_artifact_payloads=terminal_artifacts,
                lifecycle_update=(
                    RunLifecycleStatus.RUNNING,
                    RunLifecycleStatus.COMPLETED,
                ),
            )
        except (IllegalLifecycleTransitionError, TerminalRunError):
            # Only the exact conditional lifecycle race is handled here:
            # cancellation or a prior terminal (timeout) won the conditional
            # update. Any other SQLite/artifact/validation/hash error is
            # re-raised so the executor failure handler terminalizes FAILED.
            lifecycle = self.claimer.current_lifecycle(run_id)
            if lifecycle in (
                RunLifecycleStatus.CANCEL_REQUESTED,
                RunLifecycleStatus.CANCELLED,
            ):
                # The completion transaction lost to cancellation. Preserve
                # the provider work already consumed before acknowledging the
                # terminal cancellation; otherwise a real charge disappears
                # behind a CANCELLED status.
                self._persist_provider_accounting(run_id)
                return self._acknowledge_cancelled(run_id)
            raise
        return {"run_id": run_id, "status": "COMPLETED"}
