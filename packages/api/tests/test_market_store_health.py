"""RG-07: the production resolver fails closed with market_store_missing when the market context DB is missing.

A desk_cascade_v1 run reads the market store (desk context, research_z); with the store missing every
stage-2 abstention would fail (SYSTEM_ERROR). Admission must instead return 503 runtime_unavailable via
the existing RuntimeUnavailableError path. An existing (even empty) store keeps readiness unchanged.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from catalyst_agents.attribution.policy import AttributionPolicy
from catalyst_agents.runtime.manifest import (
    RunManifest,
    RuntimeConfiguration,
    TemporalIdentity,
)
from catalyst_app.runtime.composition import (
    ProductionGraphRuntimeResolver,
    RuntimeUnavailableError,
)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-06T14:30:00Z"),
        session_close_at=_utc("2026-01-06T21:00:00Z"),
        information_window_start_at=_utc("2026-01-05T21:00:00Z"),
        cutoff_at=_utc("2026-01-06T21:00:00Z"),
    )


def _manifest(attribution_policy: AttributionPolicy) -> RunManifest:
    from catalyst_agents.attribution.context_pack import ContextBudget
    from catalyst_agents.attribution.tiers import default_observation_policy
    from catalyst_app.persistence.events import payload_sha256
    return RunManifest(
        run_id="run-1",
        request_hash="a" * 64,
        temporal_identity=_temporal(),
        data_runtime_identity_ref="runtime-id:test",
        data_runtime_identity_hash=payload_sha256(_ReadyLoader().get_dependencies().data_runtime_identity.model_dump(mode="json")),
        code_revision="m1-1-rg07-test",
        workflow_version="v1",
        policy_version="p1",
        analyst_model_id="model-default",
        analyst_prompt_hash="a" * 64,
        writer_model_id="model-default",
        writer_prompt_hash="b" * 64,
        context_pack_schema_version="v1",
        packing_policy_version="p1",
        context_token_budget=128_000,
        tokenizer_policy="test",
        hypothesis_schema_version="hypothesis:v2",
        claim_schema_version="claim:v1",
        provider_capability_revision="test",
        runtime_configuration=RuntimeConfiguration(
            observation_policy=default_observation_policy(),
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
        ),
        attribution_policy=attribution_policy,
    )


class _ReadyLoader:
    def get_dependencies(self, *, force_reload: bool = False):
        from catalyst_agents.runtime.dependencies import RuntimeDependencies
        from catalyst_data.canonical.identity import DataRuntimeIdentity

        del force_reload
        return RuntimeDependencies(
            sqlite_db_path=Path("/tmp/rg07-runtime.db"),
            health={"status": "ready", "errors": []},
            retriever=object(),
            data_runtime_identity=DataRuntimeIdentity(
                data_snapshot_id="snap",
                corpus_manifest_id="m" * 64,
                fts_index_version="fts",
                query_policy_version="qp",
            ),
        )


from catalyst_app.runtime.composition import RunBoundary


def _resolve(monkeypatch: pytest.MonkeyPatch, market_db: Path) -> None:
    monkeypatch.setenv("CATALYST_MARKET_CONTEXT_DB", str(market_db))
    resolver = ProductionGraphRuntimeResolver(dependency_loader=_ReadyLoader(), credential_store=None)
    manifest = _manifest(
        AttributionPolicy(verifier_enabled=False, corrective_enabled=False, quote_rules_version="q1",
                          evidence_window_policy="desk_cascade_v1")
    )
    boundary = RunBoundary(provider="openai", base_url=None, model_id="model-default", api_key="test-key-not-a-secret")
    resolver.resolve(manifest, boundary=boundary)


def test_missing_market_store_fails_closed_with_market_store_missing(tmp_path, monkeypatch):
    missing = tmp_path / "nope.sqlite3"
    with pytest.raises(RuntimeUnavailableError, match="market_store_missing"):
        _resolve(monkeypatch, missing)
    assert not missing.exists()


def test_existing_empty_market_store_keeps_readiness(tmp_path, monkeypatch):
    store = tmp_path / "market.sqlite3"
    store.write_bytes(b"")
    _resolve(monkeypatch, store)


def test_historical_discovery_v2_with_non_desk_window_fails_closed():
    policy = AttributionPolicy(
        verifier_enabled=False,
        corrective_enabled=False,
        quote_rules_version="q1",
        evidence_window_policy="event_time_v1",
        web_evidence="discovery_v2",
    )
    resolver = ProductionGraphRuntimeResolver(dependency_loader=_ReadyLoader(), credential_store=None)
    manifest = _manifest(policy)
    boundary = RunBoundary(
        provider="openai", base_url=None, model_id="model-default", api_key="test-key-not-a-secret"
    )
    with pytest.raises(RuntimeUnavailableError, match="only desk_cascade_v1 can execute"):
        resolver.resolve(manifest, boundary=boundary)


def test_market_store_path_helper_uses_repo_anchored_runtime_default(monkeypatch, tmp_path):
    from catalyst_app.runtime.manifest_factory import market_store_path
    from catalyst_data.storage.runtime_paths import runtime_store_path

    monkeypatch.delenv("CATALYST_MARKET_CONTEXT_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert market_store_path() == runtime_store_path("market_db")
    assert market_store_path().is_absolute()
    monkeypatch.setenv("CATALYST_MARKET_CONTEXT_DB", "/x/y.sqlite3")
    assert market_store_path() == Path("/x/y.sqlite3")


def test_daily_and_api_core_store_defaults_agree(monkeypatch, tmp_path):
    from catalyst_agents.graphs import graph
    from catalyst_app.routers import data_jobs
    from catalyst_app.runtime.manifest_factory import market_store_path
    from catalyst_data.ingestion.ops.daily_config import resolve_path_values
    from catalyst_data.ingestion.ops.ready_snapshot import (
        default_path as ready_snapshot_default_path,
    )
    from catalyst_data.storage.runtime_paths import repository_root, runtime_store_path

    repo = repository_root()
    runtime_root = tmp_path / "shared-runtime-root"
    for key in (
        "CATALYST_MARKET_CONTEXT_DB", "CATALYST_NEWS_BODIES_DB",
        "CATALYST_UPDATE_JOBS_DB", "CATALYST_HEADLINE_ARCHIVE_DB",
        "CATALYST_READY_SNAPSHOT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CATALYST_RUNTIME_DATA_DIR", str(runtime_root))
    monkeypatch.chdir(tmp_path)
    values = resolve_path_values(
        repo,
        {"CATALYST_PEER_MAP": str(repo / "data/manifests/peer_map_v1.json"),
         "CATALYST_RUNTIME_DATA_DIR": str(runtime_root)},
    )

    assert values["market_db"] == market_store_path()
    assert values["bodies_db"] == data_jobs._bodies_path()
    assert values["jobs_db"] == data_jobs._jobs_path()
    assert values["archive_db"] == runtime_store_path("archive_db")
    assert ready_snapshot_default_path() == values["jobs_db"].with_name("ready_range.json")
    assert graph.runtime_store_path("market_db") == values["market_db"]
    assert values["market_db"] == runtime_root / "market_context.sqlite3"
    assert not runtime_root.exists()


def test_admission_fails_closed_when_the_market_store_is_missing(tmp_path, monkeypatch):
    """RG-07: the manifest factory (admission) refuses a desk_cascade_v1 run without the market store, so the user
    gets 503 runtime_unavailable instead of an ACCEPTED run that fails at the earnings node."""
    from types import SimpleNamespace

    from catalyst_app.runtime import manifest_factory as factory_module
    monkeypatch.setenv("CATALYST_MARKET_CONTEXT_DB", str(tmp_path / "missing.sqlite3"))
    monkeypatch.setattr(factory_module, "resolve_runtime_attribution_policy", lambda **_: AttributionPolicy(
        verifier_enabled=False, corrective_enabled=False, quote_rules_version="q1", evidence_window_policy="desk_cascade_v1"))
    deps = SimpleNamespace(data_runtime_identity=object(), health={"status": "ready"})
    factory = factory_module.build_default_manifest_factory(
        dependency_loader=SimpleNamespace(get_dependencies=lambda: deps), tokenizer_policy="test")
    request = SimpleNamespace(session_date="2026-01-06", force_full=False, web_search_lead_id=None)
    with pytest.raises(RuntimeUnavailableError, match="market_store_missing"):
        factory(request, "run-1", "a" * 64)
