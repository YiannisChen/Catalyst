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

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable
from uuid import uuid4

from catalyst_agents.attribution.provider import ContextProvider
from catalyst_agents.graph import run_v1_graph
from catalyst_agents.runtime.context import SQLiteContextProvider
from catalyst_agents.runtime.manifest import (
    INITIAL_RUN_TIMEOUT_SECONDS,
    MAX_ACTIONS_PER_BATCH_PRODUCTION,
    MAX_CORRECTIVE_ROUNDS_PRODUCTION,
    ObservationPolicyConfig,
    RunManifest,
    RuntimeConfiguration,
)
from catalyst_agents.runtime.control import (
    RunCancelledError,
    RunDeadlineExceededError,
    raise_if_control_expired,
)
from catalyst_agents.runtime.pack_persistence import (
    PersistedPackPair,
    PersistedPackRefs,
)
from catalyst_agents.runtime.provider_capability import CAPABILITY_REVISION
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity

from catalyst_app.events import (
    AssuranceCompletedPayload,
    EvidenceAssessedPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
    StageStartedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.llm_factory import build_v1_llm
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import (
    ArtifactPayload,
    EventRepository,
    IllegalLifecycleTransitionError,
    TerminalRunError,
    payload_sha256,
)
from catalyst_app.runtime.admission import (
    AdmissionController,
    AdmissionRequest,
    ManifestFactory,
)
from catalyst_app.runtime.cancel import (
    CancellationTokenRegistry,
    CancellationController,
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
from catalyst_app.runtime.stream_bridge import StreamBridge
from catalyst_app.runtime_credential_store import RuntimeCredentialStore

TIMEOUT_FAILURE_CODE = "TIMEOUT"


class RuntimeUnavailableError(RuntimeError):
    """The production runtime identity/config is unavailable; admission fails
    closed before a run becomes visible."""


# ---------------------------------------------------------------------------
# Production prompt/template identity (repo-owned, never fixture constants)
# ---------------------------------------------------------------------------

_ANALYST_PROMPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "agents" / "catalyst_agents" / "prompts" / "evidence_analyst.md"
)

# The Writer system instruction is code-owned by agents
# (``catalyst_agents.nodes.writer.WRITER_STANDARD_INSTRUCTION``); the manifest
# identity hashes the canonical instruction text so it never drifts from the
# V1.1 writer contract and is never duplicated here.
from catalyst_agents.nodes.writer import WRITER_STANDARD_INSTRUCTION as _PRODUCTION_WRITER_TEMPLATE


@lru_cache(maxsize=1)
def production_prompt_identity() -> tuple[bytes, str, str, str, str]:
    """Return (template_bytes, template_version, analyst_prompt, analyst_hash,
    writer_hash) derived from the repo-owned prompt file and writer template."""
    template_bytes = _ANALYST_PROMPT_PATH.read_bytes()
    analyst_prompt = template_bytes.decode("utf-8")
    analyst_hash = hashlib.sha256(template_bytes).hexdigest()
    template_version = f"evidence_analyst.md@sha256:{analyst_hash[:12]}"
    writer_hash = hashlib.sha256(
        canonical_prompt_identity(_PRODUCTION_WRITER_TEMPLATE)
    ).hexdigest()
    return (
        template_bytes,
        template_version,
        analyst_prompt,
        analyst_hash,
        writer_hash,
    )


def canonical_prompt_identity(template: str) -> bytes:
    return json.dumps(
        {"role": "system", "template": template},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Production policy defaults (V1.1 repo contract; Final TSD §15/§14)
# ---------------------------------------------------------------------------

PRODUCTION_POLICY_VERSION = "policy:v1"
PRODUCTION_PACKING_POLICY_VERSION = "packing:v1"
PRODUCTION_HYPOTHESIS_SCHEMA_VERSION = "hypothesis:v1"
PRODUCTION_CLAIM_SCHEMA_VERSION = "claim:v1"


def production_observation_policy() -> ObservationPolicyConfig:
    """V1.1 production observation policy (Frozen §6.1; M2 contract)."""
    return ObservationPolicyConfig(
        material_target_return_pct=2.0,
        material_prior_return_pct=1.5,
        quiet_target_return_pct=0.5,
        flat_reference_return_pct=0.25,
        aligned_residual_pct=1.0,
        volume_elevated_ratio=1.5,
        volume_extreme_ratio=3.0,
        minimum_peer_count=3,
        require_sector_and_peer_for_broad_sector=True,
        scenario_policy_version="sp:v1",
    )


def production_runtime_configuration() -> RuntimeConfiguration:
    from catalyst_agents.attribution.context_pack import ContextBudget

    return RuntimeConfiguration(
        observation_policy=production_observation_policy(),
        context_budget=ContextBudget(
            model_context_limit=128_000,
            reserved_output_tokens=2_000,
            reserved_system_instruction_tokens=1_000,
            observation_tokens=300,
            coverage_summary_tokens=200,
            research_history_tokens=100,
            inventory_tokens=500,
            evidence_payload_tokens=60_000,
            per_news_item_max_tokens=800,
            per_sec_chunk_max_tokens=1_200,
            lead_only_tokens=1_000,
            safety_margin_tokens=2_000,
        ),
        research_stage_timeout_seconds=30,
        max_initial_research_concurrency=2,
    )


# ---------------------------------------------------------------------------
# Temporal identity (data-core calendar authority)
# ---------------------------------------------------------------------------

def build_temporal_identity(session_date: str) -> TemporalIdentity:
    """Derive the run TemporalIdentity from the authoritative data-core US
    trading calendar (Final Migration TSD §14; data-core §7)."""
    from catalyst_data.trading_calendar import (
        latest_closed_trading_day_for_date,
        session_close_utc,
        session_open_utc,
    )

    if not session_date:
        raise RuntimeUnavailableError("session_date is required for temporal identity")
    try:
        open_iso = session_open_utc(session_date)
        close_iso = session_close_utc(session_date)
    except ValueError as exc:
        raise RuntimeUnavailableError(str(exc)) from exc
    open_at = datetime.fromisoformat(open_iso.replace("Z", "+00:00"))
    close_at = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
    prior_day = latest_closed_trading_day_for_date(
        datetime.fromisoformat(session_date).date() - timedelta(days=1)
    )
    prior_close_iso = session_close_utc(prior_day.isoformat())
    prior_close_at = datetime.fromisoformat(prior_close_iso.replace("Z", "+00:00"))
    return TemporalIdentity(
        session_date=session_date,
        market_timezone="America/New_York",
        session_open_at=open_at,
        session_close_at=close_at,
        information_window_start_at=prior_close_at,
        cutoff_at=close_at,
    )


# ---------------------------------------------------------------------------
# RunManifest production factory
# ---------------------------------------------------------------------------

def build_default_manifest_factory(
    *, dependency_loader: Any, tokenizer_policy: str | None = None
) -> ManifestFactory:
    """Production RunManifest factory over authoritative request + runtime
    identity. Missing identity/config fails closed (RuntimeUnavailableError)
    before the run becomes visible."""
    if tokenizer_policy is None:
        from catalyst_data.corpus.tokenizer import TOKENIZER_MODEL_ID, TOKENIZER_REVISION

        tokenizer_policy = f"{TOKENIZER_MODEL_ID}@{TOKENIZER_REVISION}"

    def factory(request: AdmissionRequest, run_id: str, request_hash: str) -> RunManifest:
        deps = dependency_loader.get_dependencies()
        data_runtime_identity = getattr(deps, "data_runtime_identity", None)
        health = getattr(deps, "health", {}) or {}
        if data_runtime_identity is None or health.get("status") != "ready":
            raise RuntimeUnavailableError(
                "production runtime identity is not ready; admission fails closed"
            )
        temporal = build_temporal_identity(request.session_date)
        template_bytes, template_version, _, analyst_hash, writer_hash = (
            production_prompt_identity()
        )
        return RunManifest(
            run_id=run_id,
            request_hash=request_hash,
            temporal_identity=temporal,
            data_runtime_identity_ref=data_runtime_identity.corpus_manifest_id,
            data_runtime_identity_hash=payload_sha256(
                data_runtime_identity.model_dump(mode="json")
            ),
            code_revision=_resolve_code_revision(),
            workflow_version=request.workflow_version,
            policy_version=PRODUCTION_POLICY_VERSION,
            analyst_model_id=request.model_id,
            analyst_prompt_hash=analyst_hash,
            writer_model_id=request.model_id,
            writer_prompt_hash=writer_hash,
            context_pack_schema_version=_context_pack_schema_version(),
            packing_policy_version=PRODUCTION_PACKING_POLICY_VERSION,
            context_token_budget=production_runtime_configuration().context_budget.model_context_limit,
            tokenizer_policy=tokenizer_policy,
            hypothesis_schema_version=PRODUCTION_HYPOTHESIS_SCHEMA_VERSION,
            claim_schema_version=PRODUCTION_CLAIM_SCHEMA_VERSION,
            max_corrective_rounds=MAX_CORRECTIVE_ROUNDS_PRODUCTION,
            max_actions_per_batch=MAX_ACTIONS_PER_BATCH_PRODUCTION,
            run_timeout_seconds=INITIAL_RUN_TIMEOUT_SECONDS,
            provider_capability_revision=CAPABILITY_REVISION,
            runtime_configuration=production_runtime_configuration(),
        )

    return factory


def _context_pack_schema_version() -> str:
    from catalyst_agents.attribution.context_pack_builder import CONTEXT_PACK_SCHEMA_VERSION

    return CONTEXT_PACK_SCHEMA_VERSION


def _resolve_code_revision() -> str:
    """Bind the run to the repository HEAD revision (data-core authority)."""
    from catalyst_data.retrieval.git_revision import resolve_git_revision

    repo_root = Path(__file__).resolve().parents[3]
    return resolve_git_revision(repo_root=repo_root, require_clean=False)


# ---------------------------------------------------------------------------
# Graph runtime boundary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunBoundary:
    """Per-run external boundary: request provider identity + volatile key."""

    provider: str
    base_url: str | None
    model_id: str
    api_key: str | None


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
        if payload_sha256(data_runtime_identity.model_dump(mode="json")) != manifest.data_runtime_identity_hash:
            raise RuntimeUnavailableError(
                "data runtime identity hash does not match the persisted RunManifest"
            )
        retriever = getattr(deps, "retriever", None)
        if retriever is None:
            raise RuntimeUnavailableError("no production retrieval runtime is bound")
        analyst_llm = build_v1_llm(
            model_id=manifest.analyst_model_id,
            provider=boundary.provider,
            api_key=boundary.api_key,
            base_url=boundary.base_url,
        )
        writer_llm = build_v1_llm(
            model_id=manifest.writer_model_id,
            provider=boundary.provider,
            api_key=boundary.api_key,
            base_url=boundary.base_url,
        )
        template_bytes, template_version, analyst_prompt, _, _ = (
            production_prompt_identity()
        )
        return ResolvedGraphRuntime(
            observation_provider=SQLiteContextProvider(deps.sqlite_db_path),
            retriever=retriever,
            analyst_llm=analyst_llm,
            writer_llm=writer_llm,
            capability_registry=_production_capability_registry(deps),
            data_runtime_identity=data_runtime_identity,
            corrective_policy=None,
            prompt_template=analyst_prompt,
            query_builder=None,
            structured_provider=None,
        )


def _production_capability_registry(deps: Any) -> Any:
    """Corrective backend registry derived from the real runtime capabilities.

    Every corrective EvidenceNeed is served by the retrieval stack (sqlite +
    lancedb + embedding + retriever); backend health is computed from the
    loader's runtime health rather than declared HEALTHY unconditionally.
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
        and _component_status("lancedb") == "ready"
        and _component_status("embedding") == "ready"
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

class RunArtifactsPackPersistence:
    """Production ``PackPersistence`` over the app-owned run_artifacts rows."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        events: EventRepository,
        run_id: str,
    ) -> None:
        self.db_path = Path(db_path)
        self.events = events
        self.run_id = run_id

    def persist_pack_and_render(
        self,
        *,
        run_id: str,
        pack: Any | None,
        pack_sha256: str,
        rendered_messages: tuple[Any, ...],
        rendered_messages_sha256: str,
        prompt_template_version: str,
        prompt_template_sha256: str,
    ) -> PersistedPackRefs:
        from catalyst_agents.attribution.context_pack_builder import (
            RenderMessage,
            canonical_context_pack_json,
        )
        import hashlib

        # Canonical verification before commit (M6 corrective): the declared
        # hashes must match the exact serialized pack/render contract and the
        # prompt-template identity must match the pack; run_id must match the
        # store's run_id. Mismatches are rejected before anything is visible.
        if run_id != self.run_id:
            raise ValueError(
                f"persist run_id {run_id!r} does not match store run_id "
                f"{self.run_id!r}"
            )
        if pack is not None:
            if pack.run_id != self.run_id:
                raise ValueError(
                    f"pack run_id {pack.run_id!r} does not match store run_id "
                    f"{self.run_id!r}"
                )
            expected_pack_sha256 = hashlib.sha256(
                canonical_context_pack_json(
                    pack.model_dump(mode="json", exclude={"context_pack_sha256"})
                )
            ).hexdigest()
            if pack_sha256 != expected_pack_sha256:
                raise ValueError(
                    "declared pack_sha256 does not match the canonical pack contract"
                )
            if pack.context_pack_sha256 != expected_pack_sha256:
                raise ValueError(
                    "pack.context_pack_sha256 does not match its canonical "
                    "serialization"
                )
            if prompt_template_version != pack.prompt_template_version:
                raise ValueError(
                    "prompt-template version does not match the pack identity"
                )
            if prompt_template_sha256 != pack.prompt_template_sha256:
                raise ValueError(
                    "prompt-template hash does not match the pack identity"
                )
        expected_render_sha256 = hashlib.sha256(
            canonical_context_pack_json(
                [
                    RenderMessage.model_validate(message).model_dump(mode="json")
                    if not isinstance(message, RenderMessage)
                    else message.model_dump(mode="json")
                    for message in rendered_messages
                ]
            )
        ).hexdigest()
        if rendered_messages_sha256 != expected_render_sha256:
            raise ValueError(
                "rendered_messages_sha256 does not match the exact normalized "
                "rendered messages"
            )

        # Persist round 1 and round 2 using the actual pack round so the
        # artifact identity is stable and never derived from event_seq.
        round_no = pack.round if pack is not None else 1
        pack_artifact_id = f"pack:{run_id}:{round_no}"
        render_artifact_id = f"render:{run_id}:{round_no}"
        pack_payload = (
            pack.model_dump(mode="json") if pack is not None else {"round": round_no}
        )
        messages_payload = [
            {"role": message.role, "content": message.content}
            for message in rendered_messages
        ]
        seq = self.events.append(
            run_id=run_id,
            event_type=RunEventType.STAGE_STARTED,
            stage="CONTEXT_PACK_BUILD",
            payload=StageStartedPayload(
                stage="context_pack_build", round=round_no
            ),
            artifact_payloads=[
                ArtifactPayload(
                    artifact_id=pack_artifact_id,
                    artifact_type="context_pack",
                    payload=pack_payload,
                ),
                ArtifactPayload(
                    artifact_id=render_artifact_id,
                    artifact_type="rendered_messages",
                    payload={
                        "schema_version": "v1",
                        "messages": messages_payload,
                    },
                ),
            ],
        )
        del seq
        return PersistedPackRefs(
            run_id=run_id,
            pack_artifact_id=pack_artifact_id,
            rendered_messages_artifact_id=render_artifact_id,
            pack_sha256=pack_sha256,
            rendered_messages_sha256=rendered_messages_sha256,
            prompt_template_version=prompt_template_version,
            prompt_template_sha256=prompt_template_sha256,
        )

    def load_pair(self, *, run_id: str) -> PersistedPackPair | None:
        from catalyst_agents.attribution.context_pack import EvidenceAnalystContextPack
        from catalyst_agents.attribution.context_pack_builder import RenderMessage

        with open_rw(self.db_path) as conn:
            pack_row = conn.execute(
                "SELECT artifact_id, payload_hash, payload_json FROM run_artifacts"
                " WHERE run_id = ? AND artifact_type = 'context_pack'"
                " ORDER BY event_seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            render_row = conn.execute(
                "SELECT artifact_id, payload_hash, payload_json FROM run_artifacts"
                " WHERE run_id = ? AND artifact_type = 'rendered_messages'"
                " ORDER BY event_seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if pack_row is None or render_row is None:
            return None
        pack_payload = json.loads(pack_row["payload_json"])
        render_payload = json.loads(render_row["payload_json"])
        pack = EvidenceAnalystContextPack.model_validate(pack_payload)
        messages = tuple(
            RenderMessage(role=item["role"], content=item["content"])
            for item in render_payload.get("messages", ())
        )
        refs = PersistedPackRefs(
            run_id=run_id,
            pack_artifact_id=pack_row["artifact_id"],
            rendered_messages_artifact_id=render_row["artifact_id"],
            pack_sha256=pack.context_pack_sha256,
            rendered_messages_sha256=pack.rendered_messages_sha256,
            prompt_template_version=pack.prompt_template_version,
            prompt_template_sha256=pack.prompt_template_sha256,
        )
        return PersistedPackPair(
            run_id=run_id,
            pack=pack,
            rendered_messages=messages,
            refs=refs,
        )

# ---------------------------------------------------------------------------
# Production run adapter (invokes run_v1_graph)
# ---------------------------------------------------------------------------

class _AdapterRunControl:
    """App-owned concrete RunControl over the shared token + absolute deadline.

    Implements the agents-owned ``RunControl`` protocol without importing
    packages/app on the agents side.
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
    ) -> None:
        self.db_path = Path(db_path)
        self.events = events
        self.claimer = claimer
        self.tokens = tokens
        self.cancellation = cancellation
        self.credential_store = credential_store
        self.graph_resolver = graph_resolver
        self.last_deadline_epoch_ms: int | None = None
        self._started_monotonic: float | None = None

    def __call__(self, run_id: str, timeout_seconds: float) -> dict[str, Any]:
        if self._started_monotonic is None:
            self._started_monotonic = time.monotonic()
        manifest = self._load_manifest(run_id)
        if manifest is None:
            raise RuntimeError(f"run {run_id} has no persisted immutable RunManifest")
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
            return self._acknowledge_cancelled(run_id)
        if self._past_deadline(deadline_epoch_ms):
            return self._terminalize_timeout(run_id)
        self._append_stage_started(run_id, "research", round=1)
        bridge = StreamBridge(db_path=self.db_path, events=self.events, run_id=run_id)
        packs = RunArtifactsPackPersistence(
            db_path=self.db_path, events=self.events, run_id=run_id
        )
        try:
            boundary = self._load_boundary(run_id)
            resolved = self.graph_resolver.resolve(manifest, boundary)
            result = run_v1_graph(
                run_id=run_id,
                temporal_identity=manifest.temporal_identity,
                data_runtime_identity=resolved.data_runtime_identity,
                ticker=self._load_ticker(run_id),
                cutoff=manifest.temporal_identity.cutoff_at.isoformat(),
                requested_manifest_id=resolved.data_runtime_identity.corpus_manifest_id,
                observation_provider=resolved.observation_provider,
                retriever=resolved.retriever,
                policy_config=manifest.runtime_configuration.observation_policy,
                research_concurrency=manifest.runtime_configuration.max_initial_research_concurrency,
                research_stage_timeout_seconds=manifest.runtime_configuration.research_stage_timeout_seconds,
                persistence=packs,
                packing_policy_version=manifest.packing_policy_version,
                template_bytes=production_prompt_identity()[0],
                template_version=production_prompt_identity()[1],
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
            )
            # Cooperative boundary: observed before result-artifact
            # persistence and immediately before the terminal commit.
            raise_if_control_expired(control)
            self._persist_result_artifacts(run_id, result, bridge)
            raise_if_control_expired(control)
            return self._terminalize_completed(run_id, result, bridge)
        except RunCancelledError:
            return self._acknowledge_cancelled(run_id)
        except RunDeadlineExceededError:
            return self._terminalize_timeout(run_id)
        except BaseException as exc:
            if token.requested or self._is_cancel_requested(run_id):
                return self._acknowledge_cancelled(run_id)
            if self._past_deadline(deadline_epoch_ms):
                return self._terminalize_timeout(run_id)
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

    def _load_boundary(self, run_id: str) -> RunBoundary:
        with open_rw(self.db_path) as conn:
            row = conn.execute(
                "SELECT provider, base_url, run_manifest_id FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        manifest = self._load_manifest(run_id)
        if row is None or manifest is None:
            raise RuntimeError(f"run {run_id} boundary identity is missing")
        credential = self.credential_store.get(run_id)
        return RunBoundary(
            provider=row["provider"] or "unknown",
            base_url=row["base_url"],
            model_id=manifest.analyst_model_id,
            api_key=credential.api_key if credential is not None else None,
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

    def _elapsed_latency_ms(self) -> int:
        """Real monotonic elapsed latency since this adapter instance started.

        Falls back to 0 when the adapter was constructed without a start
        timestamp (direct legacy invocations).
        """
        if self._started_monotonic is None:
            return 0
        return max(0, int((time.monotonic() - self._started_monotonic) * 1000))

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
            ),
            lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.FAILED),
        )
        return {"run_id": run_id, "status": "FAILED", "failure_code": TIMEOUT_FAILURE_CODE}

    def _persist_result_artifacts(
        self, run_id: str, result: Any, bridge: StreamBridge
    ) -> None:
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
            ),
            artifact_payloads=[
                ArtifactPayload(
                    artifact_id=f"evidence:{run_id}:{item.evidence_id}",
                    artifact_type="evidence_detail",
                    payload=_evidence_detail_payload(item),
                )
                for item in all_items
            ],
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

    def _terminalize_completed(
        self, run_id: str, result: Any, bridge: StreamBridge
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
                    total_latency_ms=self._elapsed_latency_ms(),
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
                return self._acknowledge_cancelled(run_id)
            raise
        return {"run_id": run_id, "status": "COMPLETED"}


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
    raw = os.getenv("CATALYST_DB_PATH")
    if raw:
        return Path(raw)
    return Path(".local/live_runtime.db")


def build_runtime_composition(
    *,
    db_path: str | Path | None = None,
    dependency_loader: Any | None = None,
    credential_store: RuntimeCredentialStore | None = None,
    condition_registry: ConditionRegistry | None = None,
    graph_resolver: GraphRuntimeResolver | None = None,
    manifest_factory: ManifestFactory | None = None,
    run_adapter: RunAdapter | None = None,
    admission_slots: int = DEFAULT_ADMISSION_SLOTS,
    max_workers: int = DEFAULT_MAX_WORKERS,
    shutdown_grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
) -> RuntimeComposition:
    """Build the single app-owned runtime composition.

    External boundaries (dependency loader, credential store, graph resolver,
    manifest factory, run adapter) may be injected for deterministic tests;
    the production defaults are the repository-owned implementations.
    """
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

    def failure_handler(run_id: str, code: str) -> None:
        _default_failure_handler(events, claimer)(run_id, code)

    adapter = run_adapter or ProductionRunAdapter(
        db_path=path,
        events=events,
        claimer=claimer,
        tokens=tokens,
        cancellation=cancellation,
        credential_store=store,
        graph_resolver=resolver,
    )
    executor = RunExecutor(
        admission_slots=admission_slots,
        max_workers=max_workers,
        run_adapter=adapter,
        failure_handler=failure_handler,
        terminal_check=is_terminal,
        shutdown_grace_seconds=shutdown_grace_seconds,
    )
    admission = AdmissionController(
        db_path=path,
        executor=executor,
        events=events,
        manifest_factory=manifest,
        cancellation=cancellation,
        credential_store=store,
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


def _default_failure_handler(events: EventRepository, claimer: RunClaimer):
    """Terminalize a run as FAILED when the adapter crashes mid-run."""

    def handler(run_id: str, code: str) -> None:
        lifecycle = claimer.current_lifecycle(run_id)
        if lifecycle is None or lifecycle not in (
            RunLifecycleStatus.ACCEPTED,
            RunLifecycleStatus.RUNNING,
            RunLifecycleStatus.CANCEL_REQUESTED,
        ):
            return
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
            ),
            lifecycle_update=(lifecycle, RunLifecycleStatus.FAILED),
        )

    return handler


__all__ = [
    "GraphRuntimeResolver",
    "ProductionGraphRuntimeResolver",
    "ProductionRunAdapter",
    "ResolvedGraphRuntime",
    "RunArtifactsPackPersistence",
    "RunBoundary",
    "RuntimeComposition",
    "RuntimeUnavailableError",
    "TIMEOUT_FAILURE_CODE",
    "build_default_manifest_factory",
    "build_runtime_composition",
    "build_temporal_identity",
    "production_prompt_identity",
]
