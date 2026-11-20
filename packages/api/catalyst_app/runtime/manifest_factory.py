"""Production run identity: prompt identity, policies, temporal identity and the RunManifest factory.

Moved verbatim from composition.py (P3); composition.py re-exports these names.
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from catalyst_agents.attribution.policy import resolve_runtime_attribution_policy
from catalyst_agents.attribution.tiers import default_observation_policy
from catalyst_agents.nodes.writer import (
    WRITER_STANDARD_PROMPT_CONTRACT as _PRODUCTION_WRITER_TEMPLATE,
)
from catalyst_agents.runtime.manifest import (
    INITIAL_RUN_TIMEOUT_SECONDS,
    MAX_ACTIONS_PER_BATCH_PRODUCTION,
    MAX_CORRECTIVE_ROUNDS_PRODUCTION,
    ObservationPolicyConfig,
    RunManifest,
    RuntimeConfiguration,
    investigator_run_timeout,
)
from catalyst_agents.runtime.provider_capability import CAPABILITY_REVISION
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval.lexical_display import (
    CANDIDATE_DEPTH_CAP,
    DISPLAY_TOP_K,
    LEXICAL_DISPLAY_POLICY_HASH,
    MAX_CHUNKS_PER_DOCUMENT,
    RETRIEVAL_PROFILE,
    V1_QUERY_POLICY_VERSION,
)
from catalyst_data.storage.runtime_paths import runtime_store_path

from catalyst_app.persistence.events import payload_sha256
from catalyst_app.runtime.admission import AdmissionRequest, ManifestFactory


class RuntimeUnavailableError(RuntimeError):
    """The production runtime identity/config is unavailable; admission fails
    closed before a run becomes visible."""


_ANALYST_PROMPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "agents" / "catalyst_agents" / "prompts" / "evidence_analyst.md"
)


def market_store_path() -> Path:
    """The market context DB the desk cascade reads (research z, desk context): the configured path, else the
    shared default as ``graph.py``. RG-07: a missing file makes the runtime unavailable, instead of failing
    every second-level abstention at the earnings node."""
    return runtime_store_path("market_db")


@lru_cache(maxsize=2)
def production_prompt_identity(evidence_window_policy: str = "pre_close_v1") -> tuple[bytes, str, str, str, str]:
    """Return (template_bytes, template_version, analyst_prompt, analyst_hash,
    writer_hash) derived from the repo-owned prompt file and writer template."""
    prompt_path = _ANALYST_PROMPT_PATH
    if evidence_window_policy in ("event_time_v1", "event_time_v2"):
        prompt_path = _ANALYST_PROMPT_PATH.with_name("evidence_analyst_event_time.md")
    template_bytes = prompt_path.read_bytes()
    analyst_prompt = template_bytes.decode("utf-8")
    analyst_hash = hashlib.sha256(template_bytes).hexdigest()
    template_version = f"{prompt_path.name}@sha256:{analyst_hash[:12]}"
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


PRODUCTION_POLICY_VERSION = "policy:v1"


PRODUCTION_PACKING_POLICY_VERSION = "evidence_body_v1"


PRODUCTION_HYPOTHESIS_SCHEMA_VERSION = "hypothesis:v2"


PRODUCTION_CLAIM_SCHEMA_VERSION = "claim:v1"


def production_observation_policy() -> ObservationPolicyConfig:
    """V1.1 production observation policy (Frozen §6.1; M2 contract)."""
    return default_observation_policy()


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


def build_temporal_identity(session_date: str) -> TemporalIdentity:
    """Derive the run TemporalIdentity from the authoritative data-core US
    trading calendar (Final Migration TSD §14; data-core §7)."""
    from catalyst_data.canonical.temporal import (
        build_temporal_identity as build_identity,
    )

    try:
        return build_identity(session_date)
    except ValueError as exc:
        raise RuntimeUnavailableError(str(exc)) from exc


def build_default_manifest_factory(
    *, dependency_loader: Any, tokenizer_policy: str | None = None
) -> ManifestFactory:
    """Production RunManifest factory over authoritative request + runtime
    identity. Missing identity/config fails closed (RuntimeUnavailableError)
    before the run becomes visible."""
    if tokenizer_policy is None:
        from catalyst_data.corpus.tokenizer import (
            TOKENIZER_MODEL_ID,
            TOKENIZER_REVISION,
        )

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
        attribution_policy = resolve_runtime_attribution_policy(
            env=(
                {**os.environ, "CATALYST_PRIMARY_VERIFIER": "1", "CATALYST_SKIP_CORRECTIVE": "1"}
                if request.web_search_lead_id else None
            ),
            force_full=request.force_full,
        )
        if attribution_policy.evidence_window_policy == "desk_cascade_v1" and not market_store_path().is_file():
            raise RuntimeUnavailableError("market_store_missing: admission fails closed")
        template_bytes, template_version, _, analyst_hash, writer_hash = (
            production_prompt_identity(attribution_policy.evidence_window_policy)
        )
        stage2_temporal = None
        if attribution_policy.evidence_window_policy in ("event_time_v1", "event_time_v2", "event_time_p1"):
            from catalyst_data.canonical.temporal import (
                with_event_time_publication_window,
            )
            temporal = with_event_time_publication_window(temporal)
        elif attribution_policy.evidence_window_policy == "desk_cascade_v1" and attribution_policy.web_evidence == "discovery_v2":
            from catalyst_data.canonical.temporal import (
                with_event_time_publication_window,
            )
            # v2 rule "window": the stage-2 window opens at the prior close (D-1 close .. D+3).
            stage2_temporal = with_event_time_publication_window(
                temporal,
                start_sessions_back=(
                    1 if attribution_policy.v2_rules and "window" in attribution_policy.v2_rules.split(",") else 4
                ),
            )
        web_cache_rows_sha256 = None
        if attribution_policy.web_evidence != "off":
            web_temporal = stage2_temporal or temporal
            from catalyst_data.canonical.temporal import utc_iso_z
            from catalyst_data.web_evidence import (
                cached_discovery_v2_rows,
                cached_roundup_segments,
            )

            rows = cached_discovery_v2_rows(request.ticker, web_temporal.session_date)
            row_identity = [("discovery_v2", row["scope_key"], row["canonical_url"], row.get("html_sha256"))
                            for row in rows]
            start_at, end_at = web_temporal.publication_window()
            segments = cached_roundup_segments(
                request.ticker, web_temporal.session_date,
                start_at=utc_iso_z(start_at), end_at=utc_iso_z(end_at))
            row_identity.extend(("roundup:" + row["roundup_kind"],
                                 f'{row["canonical_url"]}#segment-{row["segment_index"]}',
                                 row["page_sha256"]) for row in segments)
            web_cache_rows_sha256 = hashlib.sha256(
                json.dumps(row_identity, ensure_ascii=True, separators=(",", ":")).encode()
            ).hexdigest()
        from catalyst_data.read.sec_units import (
            filing_units_disabled,
        )

        return RunManifest(
            run_id=run_id,
            request_hash=request_hash,
            temporal_identity=temporal,
            stage2_temporal_identity=stage2_temporal,
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
            # Live investigator searches fetch pages; the investigator runs get a longer deadline.
            run_timeout_seconds=investigator_run_timeout(attribution_policy) or INITIAL_RUN_TIMEOUT_SECONDS,
            provider_capability_revision=CAPABILITY_REVISION,
            runtime_configuration=production_runtime_configuration(),
            parent_run_id=request.parent_run_id,
            web_search_lead_id=request.web_search_lead_id,
            # Frozen-A3 §2.1 / WP-B B5: the production path *is* the measured
            # path. The six retrieval limits are filled from the frozen
            # data-core constants, never from string literals.
            retrieval_profile=RETRIEVAL_PROFILE,
            query_policy_version=V1_QUERY_POLICY_VERSION,
            candidate_depth=CANDIDATE_DEPTH_CAP,
            display_top_k=DISPLAY_TOP_K,
            max_chunks_per_document=MAX_CHUNKS_PER_DOCUMENT,
            retrieval_policy_hash=LEXICAL_DISPLAY_POLICY_HASH,
            attribution_policy=attribution_policy,
            web_cache_rows_sha256=web_cache_rows_sha256,
            filing_units="waived" if filing_units_disabled() else None,
        )

    return factory


def _context_pack_schema_version() -> str:
    from catalyst_agents.attribution.context_pack_builder import (
        CONTEXT_PACK_SCHEMA_VERSION,
    )

    return CONTEXT_PACK_SCHEMA_VERSION


def _resolve_code_revision() -> str:
    """Bind the run to the repository HEAD revision (data-core authority)."""
    from catalyst_data.retrieval.git_revision import resolve_git_revision

    repo_root = Path(__file__).resolve().parents[3]
    return resolve_git_revision(repo_root=repo_root, require_clean=False)
