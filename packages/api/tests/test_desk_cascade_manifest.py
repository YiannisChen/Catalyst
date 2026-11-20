from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from catalyst_agents.attribution.context_pack import ContextBudget
from catalyst_agents.runtime.manifest import (
    ObservationPolicyConfig,
    RuntimeConfiguration,
)
from catalyst_app.api_dto import RunViewAnswerDTO
from catalyst_app.runtime.admission import AdmissionRequest
from catalyst_app.runtime.manifest_factory import build_default_manifest_factory
from catalyst_data.canonical.identity import DataRuntimeIdentity


def _runtime():
    return DataRuntimeIdentity(
        data_snapshot_id="s" * 64, corpus_manifest_id="m" * 64,
        fts_index_version="build:fts", dense_index_version="d" * 64,
        embedding_model_revision="emb:1", reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _configuration():
    return RuntimeConfiguration(
        observation_policy=ObservationPolicyConfig(
            material_target_return_pct=2, material_prior_return_pct=1.5,
            quiet_target_return_pct=.5, flat_reference_return_pct=.25,
            aligned_residual_pct=1, volume_elevated_ratio=1.5,
            volume_extreme_ratio=3, minimum_peer_count=2,
            require_sector_and_peer_for_broad_sector=True, scenario_policy_version="sp:v1",
        ),
        context_budget=ContextBudget(
            model_context_limit=128000, reserved_output_tokens=2000,
            reserved_system_instruction_tokens=1000, observation_tokens=300,
            coverage_summary_tokens=200, research_history_tokens=100, inventory_tokens=500,
            evidence_payload_tokens=60000, per_news_item_max_tokens=800,
            per_sec_chunk_max_tokens=1200, lead_only_tokens=1000,
            safety_margin_tokens=2000,
        ),
    )


def test_manifest_records_cascade_stage_identities_and_web_rows(monkeypatch, tmp_path):
    from catalyst_app.runtime import manifest_factory as factory_module
    from catalyst_data import web_evidence

    market = tmp_path / "market.sqlite3"  # RG-07: a desk_cascade_v1 admission needs the market store to exist
    market.write_bytes(b"")
    monkeypatch.setenv("CATALYST_MARKET_CONTEXT_DB", str(market))
    runtime = _runtime()
    deps = SimpleNamespace(data_runtime_identity=runtime, health={"status": "ready"})
    loader = SimpleNamespace(get_dependencies=lambda: deps)
    monkeypatch.setattr(factory_module, "production_runtime_configuration", _configuration)
    monkeypatch.setattr(factory_module, "production_prompt_identity",
                        lambda *_: (b"prompt", "prompt:v1", "prompt", "a" * 64, "b" * 64))
    monkeypatch.setattr(factory_module, "_resolve_code_revision", lambda: "rev")
    monkeypatch.setattr(web_evidence, "cached_discovery_v2_rows", lambda *_, **__: [
        {"scope_key": "company", "canonical_url": "https://example.com/a", "html_sha256": "a" * 64}
    ])
    monkeypatch.setattr(web_evidence, "cached_roundup_segments", lambda *_, **__: [])
    monkeypatch.setenv("CATALYST_EVIDENCE_WINDOW_POLICY", "desk_cascade_v1")
    monkeypatch.setenv("CATALYST_WEB_EVIDENCE", "discovery_v2")
    for key in ("CATALYST_PRIMARY_VERIFIER", "CATALYST_SKIP_CORRECTIVE", "CATALYST_WEB_ON_DEMAND"):
        monkeypatch.delenv(key, raising=False)
    request = AdmissionRequest(
        ticker="ACME", session_date="2026-01-15", query=None, provider="deepseek",
        model_id="deepseek-chat", base_url=None, credential_source_identifier="test",
    )
    manifest = build_default_manifest_factory(dependency_loader=loader)(request, "run:desk", "c" * 64)
    assert manifest.temporal_identity.evidence_window_start_at is None
    assert manifest.stage2_temporal_identity is not None
    assert manifest.stage2_temporal_identity.evidence_window_start_at is not None
    assert manifest.attribution_policy.quote_rules_version == "q1c_quote_rules_desk_cascade_v1"
    assert manifest.attribution_policy.stage1_verifier_version
    assert manifest.attribution_policy.stage2_verifier_version
    assert manifest.web_cache_rows_sha256

    from catalyst_agents.attribution.policy import AttributionPolicy
    from catalyst_agents.runtime.manifest import RunManifest

    historical_policy = AttributionPolicy.model_validate({
        **manifest.attribution_policy.model_dump(mode="json"),
        "web_evidence": "cache_code3_why_roundups",
        "web_on_demand": True,
        "investigator_mode": "agent",
        "investigator_version": "historical-agent-v1",
    })
    historical = manifest.model_copy(update={
        "attribution_policy": historical_policy,
        "run_timeout_seconds": 240,
    })
    restored = RunManifest.model_validate(historical.model_dump(mode="json"))
    assert restored.attribution_policy == historical_policy
    assert restored.model_dump(mode="json") == historical.model_dump(mode="json")


def test_answer_dto_accepts_optional_desk_snapshot_fields():
    dto = RunViewAnswerDTO(
        outcome="ABSTAINED", desk_context={"version": "desk_decomposition_v1", "best": None},
        basis=None, cascade_stage=2,
    )
    payload = dto.model_dump(mode="json")
    assert payload["desk_context"]["version"] == "desk_decomposition_v1"
    assert payload["cascade_stage"] == 2
    assert "basis" not in payload


def test_desk_graph_reuses_registered_sse_payload_contracts():
    import importlib.util
    import json
    import sys

    from catalyst_app.events import (
        AnalystDecisionPayload,
        AnalystDonePayload,
        AnswerReadyPayload,
        ChecksDonePayload,
        FactsReadyPayload,
        GatesDonePayload,
        RetrievalDonePayload,
        TierDecidedPayload,
        VerifierDonePayload,
    )

    test_graph_path = Path(__file__).resolve().parents[2] / "agents" / "tests" / "test_v1_graph.py"
    spec = importlib.util.spec_from_file_location("desk_graph_sse_contract_fixtures", test_graph_path)
    assert spec and spec.loader
    fixture_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fixture_module
    spec.loader.exec_module(fixture_module)
    kwargs, _fake_llm = fixture_module._desk_graph_kwargs([
        fixture_module._desk_extraction(publish=True),
    ])
    from catalyst_agents.graphs.graph import run_v1_graph

    emitted = []
    kwargs["on_step"] = lambda event_type, payload: emitted.append((event_type, payload))
    run_v1_graph(**kwargs)
    contract_path = Path(__file__).resolve().parents[3] / "contracts" / "events.schema.json"
    contracts = json.loads(contract_path.read_text(encoding="utf-8"))
    payload_models = {
        "facts.ready": FactsReadyPayload,
        "tier.decided": TierDecidedPayload,
        "retrieval.done": RetrievalDonePayload,
        "analyst.decision": AnalystDecisionPayload,
        "analyst.done": AnalystDonePayload,
        "checks.done": ChecksDonePayload,
        "gates.done": GatesDonePayload,
        "verifier.done": VerifierDonePayload,
        "answer.ready": AnswerReadyPayload,
    }
    for event_type, payload in emitted:
        model = payload_models.get(event_type)
        if model is None:
            continue
        assert event_type in contracts
        model.model_validate(payload)
