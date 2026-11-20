"""App-owned V1.1 runtime composition (M6 corrective).

One app-owned composition wires every runtime piece together: db_path, one
EventRepository (with the SSE after-commit notifier), one RunClaimer, one
CancellationTokenRegistry/CancellationController, one bounded RunExecutor,
one AdmissionController, the production RunManifest factory, the production
run adapter that invokes the repository-owned ``run_v1_graph``, the
EventRepository-backed PackPersistence, the per-run StreamBridge Writer sink,
and the RuntimeDependencyLoader/credential-store access.

Final Migration TSD §11/§15/§17/§18; Frozen §6.1/§8-9. There is exactly one
graph (``run_v1_graph``), one event taxonomy (``catalyst_app.events``), one
admission/executor, and one SQLite authority. Missing runtime identity or
configuration fails closed before visible successful admission.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from catalyst_agents.attribution.provider import ContextProvider, RetrievedEvidence
from catalyst_agents.runtime.context import SQLiteContextProvider
from catalyst_agents.runtime.manifest import RunManifest
from catalyst_data.canonical.identity import DataRuntimeIdentity

from catalyst_app.db_paths import runtime_db_path
from catalyst_app.events import (
    AssuranceCompletedPayload,
    RunEventType,
    RunFailedPayload,
    run_has_answer_ready,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.llm_factory import build_v1_llm
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import (
    ArtifactPayload,
    EventRepository,
    payload_sha256,
)
from catalyst_app.public_text import validate_safe_public_text
from catalyst_app.runtime.admission import AdmissionController, ManifestFactory
from catalyst_app.runtime.cancel import (
    CancellationController,
    CancellationTokenRegistry,
)
from catalyst_app.runtime.claim import RunClaimer
from catalyst_app.runtime.executor import (
    DEFAULT_ADMISSION_SLOTS,
    DEFAULT_MAX_WORKERS,
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    RunAdapter,
    RunExecutor,
)
from catalyst_app.runtime.sse import ConditionRegistry
from catalyst_app.runtime_credential_store import RuntimeCredentialStore


def _failure_code_for_exception(code: str, exc: BaseException | None) -> str:
    from catalyst_agents.runtime.provider_budget import (
        ProviderBudgetExceeded,
        ProviderPriceUnavailable,
        ProviderTokenLimitUnavailable,
    )
    from catalyst_data.storage.connect import SQLiteReadTimeoutError
    from catalyst_data.storage.errors import StoreReadError

    if provider_account_error_status(exc) is not None:
        return "PROVIDER_UNAVAILABLE"

    if isinstance(exc, ProviderBudgetExceeded):
        return "BUDGET_EXCEEDED"
    if isinstance(exc, ProviderPriceUnavailable):
        return "MODEL_PRICE_UNAVAILABLE"
    if isinstance(exc, ProviderTokenLimitUnavailable):
        return "MODEL_TOKEN_LIMIT_UNAVAILABLE"
    if isinstance(exc, (StoreReadError, SQLiteReadTimeoutError)):
        return "DATA_UNAVAILABLE"
    return code


def provider_account_error_status(exc: BaseException | None) -> int | None:
    """Return provider HTTP account error status without depending on SDK types."""
    if exc is None:
        return None
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    return status if status in {401, 402, 403} else None

TIMEOUT_FAILURE_CODE = "TIMEOUT"

# Durable provider-accounting artifact type/schema. Published by the run
# adapter after any provider work so a FAILED/CANCELLED/TIMEOUT run keeps the
# calls/cost it actually consumed; the eval-side adapter reads it instead of
# defaulting a failed case to zero.
PROVIDER_ACCOUNTING_ARTIFACT_TYPE = "run_provider_accounting"
PROVIDER_ACCOUNTING_SCHEMA_VERSION = "v1.1_run_provider_accounting_v1"
STORE_FAILURE_DIAGNOSTICS_SCHEMA_VERSION = "v1.1_run_failure_diagnostics_v1"

logger = logging.getLogger(__name__)
_store_read_failure_lock = threading.Lock()
_store_read_failure_count = 0
_provider_account_error_lock = threading.Lock()
_provider_account_error_count = 0


def provider_account_error_count() -> int:
    with _provider_account_error_lock:
        return _provider_account_error_count


def record_provider_account_error(run_id: str, exc: BaseException) -> None:
    status = provider_account_error_status(exc)
    if status is None:
        return
    global _provider_account_error_count
    with _provider_account_error_lock:
        _provider_account_error_count += 1
    logger.error("provider_account_error", extra={
        "event_name": "provider_account_error", "run_id": run_id,
        "provider": str(getattr(exc, "provider", None) or "unknown")[:100],
        "model": str(getattr(exc, "model", None) or "unknown")[:160],
        "role": str(getattr(exc, "role", None) or "unknown")[:80],
        "status_code": status,
    })


def store_read_failure_count() -> int:
    """Process-local count exposed by the health endpoint for monitoring."""
    with _store_read_failure_lock:
        return _store_read_failure_count


def _store_read_failure_details(exc: BaseException) -> dict[str, str]:
    from catalyst_data.storage.connect import SQLiteReadTimeoutError
    from catalyst_data.storage.errors import StoreReadError

    if not isinstance(exc, (StoreReadError, SQLiteReadTimeoutError)):
        raise TypeError("store failure diagnostics require a recognized store exception")
    message = getattr(exc, "reason", None) or str(exc)
    message = " ".join(str(message).split())
    message = re.sub(
        r"(?i)\b(api[_ -]?key|token|secret|password|authorization)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        message,
    )[:200]
    return {
        "store_path": str(getattr(exc, "store_path", None) or "unknown"),
        "operation": str(getattr(exc, "operation", None) or "sqlite read")[:120],
        "exception_class": type(exc).__name__,
        "exception_message": message,
    }


def record_store_read_failure(run_id: str, exc: BaseException) -> None:
    """Emit the stable alert log and increment the health-visible counter."""
    global _store_read_failure_count
    from catalyst_data.storage.connect import SQLiteReadTimeoutError
    from catalyst_data.storage.errors import StoreReadError

    if not isinstance(exc, (StoreReadError, SQLiteReadTimeoutError)):
        return
    details = _store_read_failure_details(exc)
    with _store_read_failure_lock:
        _store_read_failure_count += 1
    logger.error(
        "data_store_read_failed",
        extra={
            "event_name": "data_store_read_failed",
            "run_id": run_id,
            **details,
        },
    )


# ---------------------------------------------------------------------------
# Production prompt/template identity (repo-owned, never fixture constants)
# ---------------------------------------------------------------------------


# The Writer system instruction is code-owned by agents
# (``catalyst_agents.nodes.writer.WRITER_STANDARD_PROMPT_CONTRACT``: the
# standard instruction plus the WP-C C3b marker contract); the manifest
# identity hashes the canonical contract text so it never drifts from the
# V1.1 writer contract and is never duplicated here.

from catalyst_app.runtime.artifact_persistence import (  # noqa: F401  (moved; re-exported)
    RunArtifactsPackPersistence,
)
from catalyst_app.runtime.manifest_factory import (  # noqa: F401  (moved; re-exported)
    _ANALYST_PROMPT_PATH,
    PRODUCTION_CLAIM_SCHEMA_VERSION,
    PRODUCTION_HYPOTHESIS_SCHEMA_VERSION,
    PRODUCTION_PACKING_POLICY_VERSION,
    PRODUCTION_POLICY_VERSION,
    RuntimeUnavailableError,
    _context_pack_schema_version,
    _resolve_code_revision,
    build_default_manifest_factory,
    build_temporal_identity,
    canonical_prompt_identity,
    production_observation_policy,
    production_prompt_identity,
    production_runtime_configuration,
)

# ---------------------------------------------------------------------------
# Production policy defaults (V1.1 repo contract; Final TSD §15/§14)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Temporal identity (data-core calendar authority)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RunManifest production factory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Graph runtime boundary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunBoundary:
    """Per-run external boundary: request provider identity + volatile key.

    ``provider_timeout_seconds`` is the remaining time to the persisted
    absolute run deadline at boundary resolution; it bounds provider request
    timeouts and is never larger than the run's remaining budget.
    """

    provider: str
    base_url: str | None
    model_id: str
    api_key: str | None
    provider_timeout_seconds: float | None = None
    tavily_context: Any | None = None


@dataclass(frozen=True)
class ResolvedGraphRuntime:
    """Deterministic external-boundary graph dependencies."""

    observation_provider: ContextProvider
    retriever: Any
    analyst_llm: Any
    writer_llm: Any
    capability_registry: Any
    data_runtime_identity: DataRuntimeIdentity
    corrective_policy: Any | None = None
    prompt_template: str | None = None
    query_builder: Callable[[Any, str], str] | None = None
    structured_provider: Any | None = None
    stage2_retriever: Any | None = None
    desk_context_fn: Callable[..., Any] | None = None
    investigator_deps: Any | None = None


class GraphRuntimeResolver:
    def resolve(self, manifest: RunManifest, boundary: RunBoundary) -> ResolvedGraphRuntime:
        raise NotImplementedError


class ProductionGraphRuntimeResolver(GraphRuntimeResolver):
    """Production resolver: real loader, real observation provider, real
    LLM clients (BYOK credential only, never persisted)."""

    def __init__(
        self,
        *,
        dependency_loader: Any,
        credential_store: RuntimeCredentialStore | None = None,
    ) -> None:
        self.dependency_loader = dependency_loader
        self.credential_store = credential_store

    def resolve(self, manifest: RunManifest, boundary: RunBoundary) -> ResolvedGraphRuntime:
        deps = self.dependency_loader.get_dependencies()
        data_runtime_identity = getattr(deps, "data_runtime_identity", None)
        health = getattr(deps, "health", {}) or {}
        if data_runtime_identity is None or health.get("status") != "ready":
            raise RuntimeUnavailableError(
                "production runtime identity is not ready; graph execution fails closed"
            )
        if getattr(manifest.attribution_policy, "evidence_window_policy", "") == "desk_cascade_v1":
            from catalyst_app.runtime.manifest_factory import market_store_path
            if not market_store_path().is_file():
                raise RuntimeUnavailableError("market_store_missing: graph execution fails closed")
        if payload_sha256(data_runtime_identity.model_dump(mode="json")) != manifest.data_runtime_identity_hash:
            raise RuntimeUnavailableError(
                "data runtime identity hash does not match the persisted RunManifest"
            )
        retriever = getattr(deps, "retriever", None)
        if retriever is None:
            raise RuntimeUnavailableError("no production retrieval runtime is bound")
        web_policy = getattr(manifest.attribution_policy, "web_evidence", "off")
        if web_policy not in ("off", "discovery_v2"):
            raise RuntimeUnavailableError("unsupported historical web evidence policy")
        evidence_window_policy = getattr(manifest.attribution_policy, "evidence_window_policy", "")
        if evidence_window_policy != "desk_cascade_v1":
            raise RuntimeUnavailableError(
                "unsupported historical top-level evidence window policy; "
                "only desk_cascade_v1 can execute"
            )
        if getattr(manifest.attribution_policy, "web_on_demand", False):
            raise RuntimeUnavailableError("historical web on-demand policy is unsupported")
        stage2_retriever = None
        if web_policy == "discovery_v2":
            from catalyst_agents.runtime.web_evidence import WebEvidenceRetriever
            stage2_retriever = WebEvidenceRetriever(retriever)
        analyst_llm = build_v1_llm(
            model_id=manifest.analyst_model_id,
            provider=boundary.provider,
            api_key=boundary.api_key,
            base_url=boundary.base_url,
            timeout_seconds=boundary.provider_timeout_seconds,
        )
        template_bytes, template_version, analyst_prompt, _, _ = (
            production_prompt_identity(
                getattr(manifest.attribution_policy, "evidence_window_policy", "pre_close_v1")
            )
        )
        desk_context_fn = None
        if getattr(manifest.attribution_policy, "evidence_window_policy", "") == "desk_cascade_v1":
            from catalyst_agents.attribution.desk_context import decompose, peers_for
            from catalyst_agents.runtime.peer_map import load_peer_map_from_env
            market_db = Path(os.environ.get("CATALYST_MARKET_CONTEXT_DB", str(deps.sqlite_db_path)))
            peer_map = load_peer_map_from_env()

            def desk_context_fn(ticker: str, session_date: str, sector_ticker: str | None,
                                commodity_ticker: str | None):
                return decompose(market_db, ticker, session_date, sector_ticker=sector_ticker,
                                 commodity_ticker=commodity_ticker, peers=peers_for(ticker, peer_map))

        investigator_deps = None
        if getattr(manifest.attribution_policy, "investigator_mode", None) is not None:
            raise ValueError("unknown investigator mode")
        return ResolvedGraphRuntime(
            observation_provider=SQLiteContextProvider(deps.sqlite_db_path),
            retriever=retriever,
            analyst_llm=analyst_llm,
            # Q1c: the answer is code-rendered; no Writer client is built.
            writer_llm=None,
            capability_registry=_production_capability_registry(deps),
            data_runtime_identity=data_runtime_identity,
            corrective_policy=None,
            prompt_template=analyst_prompt,
            query_builder=None,
            structured_provider=None,
            stage2_retriever=stage2_retriever,
            desk_context_fn=desk_context_fn,
            investigator_deps=investigator_deps,
        )


def _production_capability_registry(deps: Any) -> Any:
    """Corrective backend registry derived from the real runtime capabilities.

    V1.1 serves one path: sqlite plus the bound lexical retriever (Frozen-A3
    §2.1). The dense components are ``not_served`` and do not gate recovery, so
    the registry is HEALTHY iff the runtime status, the retrieval component and
    sqlite are ready and a retriever is bound -- computed from the loader's
    runtime health rather than declared HEALTHY unconditionally.
    """
    from catalyst_agents.retrieval.corrective import (
        BackendCapability,
        BackendHealth,
        CorrectiveCapabilityRegistry,
    )
    from catalyst_agents.retrieval.task import EvidenceNeed

    health = getattr(deps, "health", None) or {}
    def _component_status(name: str) -> str:
        component = health.get(name)
        if isinstance(component, dict):
            return str(component.get("status", ""))
        return str(component or "")

    ready = (
        health.get("status") == "ready"
        and _component_status("retrieval") == "ready"
        and _component_status("sqlite") == "ready"
        and getattr(deps, "retriever", None) is not None
    )
    backend_health = (
        BackendHealth.HEALTHY if ready else BackendHealth.UNAVAILABLE
    )
    capabilities = {}
    for name in (
        "COMPANY_PRIMARY", "COMPANY_NEWS", "SECTOR_NEWS", "MACRO_EVENT",
        "MACRO_SERIES", "FUNDAMENTALS",
    ):
        capabilities[EvidenceNeed(name)] = BackendCapability(
            backend=f"backend:{name.lower()}", health=backend_health
        )
    return CorrectiveCapabilityRegistry(capabilities)


# ---------------------------------------------------------------------------
# EventRepository-backed PackPersistence (app-owned run_artifacts envelope)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Production run adapter (invokes run_v1_graph)
# ---------------------------------------------------------------------------

class _AdapterRunControl:
    """App-owned concrete RunControl over the shared token + absolute deadline.

    Implements the agents-owned ``RunControl`` protocol without importing
    packages/api on the agents side.
    """

    def __init__(self, *, token: Any, deadline_epoch_ms: int | None) -> None:
        self._token = token
        self._deadline_epoch_ms = deadline_epoch_ms

    def should_cancel(self) -> bool:
        return bool(self._token.requested)

    def deadline_epoch_ms(self) -> int | None:
        return self._deadline_epoch_ms

    def cancellation_reason(self) -> str | None:
        return "user_cancelled"


class _WebLeadRetriever:
    """Retriever seam for a source-check child run: every task sees one page."""

    def __init__(self, lead: dict[str, Any]):
        self.lead = lead

    def retrieve(self, query: str, *, ticker: str, cutoff: str, requested_manifest_id: str, temporal_identity: Any,
                 top_k: int, candidate_depth: int, source_classes: Any, temporal_centre_date: str | None = None,
                 temporal_center_date: str | None = None) -> tuple[RetrievedEvidence, ...]:
        del query, top_k, candidate_depth, source_classes, temporal_centre_date, temporal_center_date
        import hashlib
        from datetime import datetime, timezone

        published = datetime.fromisoformat(self.lead["published_at"].replace("Z", "+00:00"))
        if not temporal_identity.information_window_start_at < published <= temporal_identity.cutoff_at:
            return ()
        lead_id = self.lead["lead_id"]
        body = self.lead["extracted_text"]
        digest = hashlib.sha256(body.encode()).hexdigest()
        return (RetrievedEvidence(
            chunk_id=f"websearch:{lead_id}", document_id=f"websearch:{lead_id}", content_text=body,
            available_at=published.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            source_class="reported_news", ticker_associations=(ticker,), dedup_cluster_id=None,
            cluster_first_available_at=published.isoformat(), representative_document_id=f"websearch:{lead_id}",
            is_novel=True, lexical_raw_score=1.0, lexical_rank=1, corpus_manifest_id=requested_manifest_id,
            index_manifest_id=None, mode_requested="web_search", mode_served="web_search", is_degraded=False,
            fallback_reason=None, canonical_asset_id=f"websearch:{lead_id}",
            canonical_content_version_id=f"websearch-version:{lead_id}", corpus_document_id=f"websearch:{lead_id}",
            chunk_ordinal=0, asset_type="NEWS", content_hash=digest, material_capability="MATERIAL_CAPABLE",
            serving_status="served", temporal_precision="page_timestamp", canonical_url=self.lead["url"],
            publisher=self.lead.get("publisher") or None, evidence_role="INDEPENDENT_REPORT",
            parse_quality="complete", independence_status="UNKNOWN", provider="tavily",
            content_state="FULL_TEXT", eligible_at=published, temporal_identity=temporal_identity,
        ),)


def _enum_value(value: Any) -> Any:
    """Coerce an enum to its string value; pass plain strings through."""
    return getattr(value, "value", value)


def _evidence_detail_payload(item: Any) -> dict[str, Any]:
    return {
        "evidence_id": item.evidence_id,
        "canonical_asset_id": item.canonical_asset_id,
        "content_version_id": item.canonical_content_version_id,
        "chunk_id": item.chunk_id,
        "fact_id": item.fact_id,
        "excerpt": item.excerpt_text or "",
        "source_class": _enum_value(item.source_class),
        "content_state": _enum_value(item.content_state),
        "eligible_at": item.eligible_at.isoformat(),
        "ticker_scope": [],
        "provider": item.provider,
        "publisher": item.publisher,
        "dedup_cluster_id": None,
    }


from catalyst_app.runtime.production_run_adapter import ProductionRunAdapter

# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

@dataclass
class RuntimeComposition:
    db_path: Path
    events: EventRepository
    claimer: RunClaimer
    tokens: CancellationTokenRegistry
    cancellation: CancellationController
    executor: RunExecutor
    admission: AdmissionController
    credential_store: RuntimeCredentialStore
    dependency_loader: Any
    graph_resolver: GraphRuntimeResolver
    manifest_factory: ManifestFactory
    run_adapter: RunAdapter
    condition_registry: ConditionRegistry | None = None


def _default_db_path() -> Path:
    return runtime_db_path()


def build_runtime_composition(
    *,
    db_path: str | Path | None = None,
    dependency_loader: Any | None = None,
    credential_store: RuntimeCredentialStore | None = None,
    condition_registry: ConditionRegistry | None = None,
    graph_resolver: GraphRuntimeResolver | None = None,
    manifest_factory: ManifestFactory | None = None,
    run_adapter: RunAdapter | None = None,
    provider_budget: Any | None = None,
    admission_slots: int = DEFAULT_ADMISSION_SLOTS,
    max_workers: int = DEFAULT_MAX_WORKERS,
    shutdown_grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    experiment_override: Any | None = None,
) -> RuntimeComposition:
    """Build the single app-owned runtime composition.

    External boundaries (dependency loader, credential store, graph resolver,
    manifest factory, run adapter) may be injected for deterministic tests;
    the production defaults are the repository-owned implementations.

    ``experiment_override`` is eval-owned (M7-7): production composition
    rejects any non-None override so eval seams can never leak into the
    production runtime.
    """
    if experiment_override is not None:
        raise ValueError(
            "production composition rejects eval experiment overrides; "
            "eval-only policy seams are recorded in the eval manifest, never "
            "in the production run manifest"
        )
    from catalyst_app.dependencies import (
        get_credential_store,
        get_runtime_dependency_loader,
    )

    path = Path(db_path) if db_path is not None else _default_db_path()
    loader = dependency_loader or get_runtime_dependency_loader()
    store = credential_store or get_credential_store()
    resolver = graph_resolver or ProductionGraphRuntimeResolver(
        dependency_loader=loader, credential_store=store
    )
    manifest = manifest_factory or build_default_manifest_factory(
        dependency_loader=loader
    )

    notifier = condition_registry.notify_from_thread if condition_registry else None
    events = EventRepository(db_path=path, notifier=notifier)
    claimer = RunClaimer(db_path=path, events=events)
    tokens = CancellationTokenRegistry()
    cancellation = CancellationController(
        db_path=path, events=events, claimer=claimer, tokens=tokens
    )
    from catalyst_app.persistence.schema import init_runtime_db

    with open_rw(path) as conn:
        init_runtime_db(conn)

    def is_terminal(run_id: str) -> bool:
        lifecycle = claimer.current_lifecycle(run_id)
        return lifecycle is not None and lifecycle in {
            RunLifecycleStatus.COMPLETED,
            RunLifecycleStatus.FAILED,
            RunLifecycleStatus.CANCELLED,
        }

    terminal_failure_handler = _default_failure_handler(events, claimer)

    def failure_handler(
        run_id: str, code: str, exc: BaseException | None = None
    ) -> None:
        code = _failure_code_for_exception(code, exc)
        terminal_failure_handler(run_id, code, exc)

    adapter = run_adapter or ProductionRunAdapter(
        db_path=path,
        events=events,
        claimer=claimer,
        tokens=tokens,
        cancellation=cancellation,
        credential_store=store,
        graph_resolver=resolver,
        provider_budget=provider_budget,
    )
    executor = RunExecutor(
        admission_slots=admission_slots,
        max_workers=max_workers,
        run_adapter=adapter,
        failure_handler=failure_handler,
        terminal_check=is_terminal,
        shutdown_grace_seconds=shutdown_grace_seconds,
    )
    from catalyst_app.runtime.budget import BudgetLedger

    admission = AdmissionController(
        db_path=path,
        executor=executor,
        events=events,
        manifest_factory=manifest,
        cancellation=cancellation,
        credential_store=store,
        budget_ledger=BudgetLedger.from_environment(),
    )
    return RuntimeComposition(
        db_path=path,
        events=events,
        claimer=claimer,
        tokens=tokens,
        cancellation=cancellation,
        executor=executor,
        admission=admission,
        credential_store=store,
        dependency_loader=loader,
        graph_resolver=resolver,
        manifest_factory=manifest,
        run_adapter=adapter,
        condition_registry=condition_registry,
    )


def _sanitized_failure_message(exc: BaseException | None) -> str | None:
    """Public diagnostic text for a crashed run: ``ExceptionType: first line``.

    Fail-closed sanitization: the candidate must pass
    ``validate_safe_public_text`` (bounded length, no secret/raw-provider
    patterns). When it does not, the exception type alone is persisted as the
    typed code; when even that is unusable the generic executor code is used.
    No key, header, or raw provider body can reach the durable public field.
    """
    if exc is None:
        return None
    from catalyst_data.storage.connect import SQLiteReadTimeoutError
    from catalyst_data.storage.errors import StoreReadError

    if isinstance(exc, (StoreReadError, SQLiteReadTimeoutError)):
        return "数据暂时不可用"
    if provider_account_error_status(exc) is not None:
        return "服务暂时不可用"
    type_name = type(exc).__name__ or "Exception"
    first_line = " ".join((str(exc).splitlines() or [""])[0].split())
    candidates = []
    if first_line:
        candidates.append(f"{type_name}: {first_line[:240]}")
    candidates.append(type_name)
    candidates.append("EXECUTOR_FAILURE")
    for candidate in candidates:
        try:
            validate_safe_public_text(candidate)
        except ValueError:
            continue
        return candidate
    return None


def _default_failure_handler(events: EventRepository, claimer: RunClaimer):
    """Terminalize a run as FAILED when the adapter crashes mid-run."""

    def handler(
        run_id: str, code: str, exc: BaseException | None = None
    ) -> None:
        from catalyst_data.storage.connect import SQLiteReadTimeoutError
        from catalyst_data.storage.errors import StoreReadError

        if provider_account_error_status(exc) is not None:
            record_provider_account_error(run_id, exc)
        if isinstance(exc, (StoreReadError, SQLiteReadTimeoutError)):
            record_store_read_failure(run_id, exc)
            details = _store_read_failure_details(exc)
            diagnostics = {
                "schema_version": STORE_FAILURE_DIAGNOSTICS_SCHEMA_VERSION,
                "run_id": run_id,
                **details,
            }
            artifact_payloads = [
                ArtifactPayload(
                    artifact_id=f"failure-diagnostics:{run_id}",
                    artifact_type="run_failure_diagnostics",
                    payload=diagnostics,
                    schema_version=STORE_FAILURE_DIAGNOSTICS_SCHEMA_VERSION,
                )
            ]
        else:
            artifact_payloads = []
        lifecycle = claimer.current_lifecycle(run_id)
        if lifecycle is None or lifecycle not in (
            RunLifecycleStatus.ACCEPTED,
            RunLifecycleStatus.RUNNING,
            RunLifecycleStatus.CANCEL_REQUESTED,
        ):
            return
        # RG-13: the pushed answer is provisional and invalidated by this
        # terminal event iff an answer.ready event exists in the run's log.
        provisional_output_invalidated = run_has_answer_ready(events.db_path, run_id)
        events.append_terminal(
            run_id=run_id,
            assurance_payload=AssuranceCompletedPayload(
                valid=False, violations=("executor_failure",)
            ),
            terminal_event_type=RunEventType.RUN_FAILED,
            terminal_payload=RunFailedPayload(
                failure_code=code,
                stage="EXECUTION",
                retryable=True,
                safe_message=_sanitized_failure_message(exc),
                provisional_output_invalidated=provisional_output_invalidated,
            ),
            artifact_payloads=artifact_payloads,
            lifecycle_update=(lifecycle, RunLifecycleStatus.FAILED),
        )

    return handler


__all__ = [
    "GraphRuntimeResolver",
    "ProductionGraphRuntimeResolver",
    "ProductionRunAdapter",
    "ResolvedGraphRuntime",
    "RunArtifactsPackPersistence",
    "PROVIDER_ACCOUNTING_ARTIFACT_TYPE",
    "PROVIDER_ACCOUNTING_SCHEMA_VERSION",
    "RunBoundary",
    "RuntimeComposition",
    "RuntimeUnavailableError",
    "TIMEOUT_FAILURE_CODE",
    "build_default_manifest_factory",
    "build_runtime_composition",
    "build_temporal_identity",
    "production_prompt_identity",
]
