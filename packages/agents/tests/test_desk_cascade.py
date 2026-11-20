from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalyst_agents.attribution.analyst import AnalystDecision
from catalyst_agents.attribution.extraction import (
    AboutTicker,
    EventTiming,
    EventType,
    Extraction,
    GateConfig,
    Session,
    cluster_events,
    select_and_gate,
)
from catalyst_agents.attribution.policy import resolve_attribution_policy
from catalyst_agents.nodes.desk_decision import build_analyst_decision, desk_decide
from catalyst_data.canonical.temporal import build_temporal_identity


def _item(eid: str, *, host: str = "one.example") -> dict:
    return {
        "evidence_id": eid, "content_state": "FULL_TEXT",
        "eligible_at": "2026-01-15T16:00:00Z", "publisher": "Example",
        "source_class": "reported_news", "canonical_url": f"https://{host}/story",
    }


def _record(eid: str, *, event_type=EventType.EARNINGS, explicit=True, host="one.example"):
    sentence = f"Shares of ACME fell after earnings cut {eid}."
    extraction = Extraction(
        about_ticker=AboutTicker.ISSUER, session=Session.THIS_SESSION,
        event_type=event_type, event_summary="earnings outlook cut",
        event_timing=EventTiming.INTRADAY, explicit_attribution=explicit,
        attribution_quote=sentence if explicit else "", event_quote=f"ACME cut outlook {eid}.",
        mechanism_quote="Investors focused on weaker guidance.",
    )
    return _item(eid, host=host), extraction


def test_synthetic_analyst_decision_is_schema_valid_for_published_secondary_and_abstain():
    primary_records = [_record("e1", host="one.example")]
    secondary_records = [_record("e2", event_type=EventType.ANALYST_ACTION, host="two.example")]
    primary = select_and_gate(cluster_events(primary_records), {"observation": {}}, GateConfig("desk"))
    secondary = select_and_gate(cluster_events(secondary_records), {"observation": {}}, GateConfig("desk"))
    decision = build_analyst_decision(
        primary, secondary.primary, [*primary_records, *secondary_records], included_evidence_ids=("e1", "e2")
    )
    assert isinstance(decision, AnalystDecision)
    assert [h.proposed_role.value for h in decision.candidate_hypotheses] == ["PRIMARY", "SECONDARY"]

    abstain = select_and_gate([], {"observation": {}}, GateConfig("desk"))
    empty = build_analyst_decision(abstain, None, primary_records, included_evidence_ids=("e1",))
    assert not empty.candidate_hypotheses
    assert empty.recommended_status.value == "ABSTAIN"


def test_policy_accepts_desk_cascade_and_restricts_web_modes():
    policy = resolve_attribution_policy({}, evidence_window_policy="desk_cascade_v1", web_evidence="discovery_v2")
    assert policy.quote_rules_version == "q1c_quote_rules_desk_cascade_v1"
    assert policy.stage1_verifier_version and policy.stage2_verifier_version
    assert policy.web_evidence == "discovery_v2"
    try:
        resolve_attribution_policy({}, evidence_window_policy="desk_cascade_v1", web_evidence="roundups_only")
    except ValueError:
        pass
    else:
        raise AssertionError("desk cascade accepted an unsupported web mode")

    production = resolve_attribution_policy({})
    assert production.evidence_window_policy == "desk_cascade_v1" and production.web_evidence == "discovery_v2"
    default = resolve_attribution_policy({}, evidence_window_policy="pre_close_v1")
    dumped = default.model_dump(mode="json")
    assert "stage1_verifier_version" not in dumped and "desk_context_version" not in dumped


def test_runtime_policy_is_cascade_only_and_internal_quote_policies_remain_available():
    import pytest
    from catalyst_agents.attribution.policy import resolve_runtime_attribution_policy

    assert resolve_runtime_attribution_policy({}).evidence_window_policy == "desk_cascade_v1"
    for legacy in ("pre_close_v1", "event_time_v1", "event_time_v2", "event_time_p1"):
        with pytest.raises(ValueError, match="only desk_cascade_v1"):
            resolve_runtime_attribution_policy({"CATALYST_EVIDENCE_WINDOW_POLICY": legacy})
    # Both stage-level verifier prompts remain available to the cascade.
    from types import SimpleNamespace

    from catalyst_agents.attribution.verifier import build_prompt

    context = SimpleNamespace(
        observation=SimpleNamespace(target_return=1.0, ticker="X"),
        temporal_identity=build_temporal_identity("2026-01-15"),
    )
    assert "Judge the session" not in build_prompt(
        "primary", context, evidence_window_policy="pre_close_v1"
    )
    assert "Judge the session" in build_prompt(
        "primary", context, evidence_window_policy="event_time_p1"
    )


class _FakeExtractor:
    def __init__(self, payload):
        self.payload = payload
        self.options = None
        self.messages = None

    def bind(self, **kwargs):
        self.options = kwargs
        return self

    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(content=json.dumps(self.payload))


def test_desk_decide_matches_frozen_gate_for_same_tiny_pack():
    from catalyst_agents.attribution.extraction import (
        GateConfig,
        check_direction,
        cluster_events,
        parse_extraction,
        select_and_gate,
        validate_quotes,
    )

    item = {
        "evidence_id": "e1", "content_state": "FULL_TEXT", "eligible_at": "2026-04-08T19:00:00Z",
        "publisher": "Example News", "source_class": "reported_news", "canonical_url": "https://example.com/story",
        "excerpt_text": (
            "Shares of ACME fell 4% after ACME cut its full year outlook sharply. "
            "ACME cut its full year outlook sharply after reporting quarterly results."
        ),
    }
    payload = {
        "about_ticker": "ISSUER", "session": "THIS_SESSION", "event_type": "EARNINGS",
        "event_summary": "company cuts full year outlook", "event_timing": "INTRADAY",
        "explicit_attribution": True,
        "attribution_quote": "Shares of ACME fell 4% after ACME cut its full year outlook sharply.",
        "event_quote": "ACME cut its full year outlook sharply after reporting quarterly results.",
        "mechanism_quote": "", "is_preview_or_evergreen": False,
    }
    pack = SimpleNamespace(included_evidence_ids=("e1",), lead_only_evidence=(), evidence_inventory=(item,))
    observation = {"ticker": "ACME", "target_return": -4.0, "market_comove": {"direction_relation": "OPPOSITE_DIRECTION"},
                   "sector_comove": {"direction_relation": "UNKNOWN"}}
    temporal = build_temporal_identity("2026-04-08")
    llm = _FakeExtractor(payload)
    result = desk_decide(pack, observation=observation, temporal_identity=temporal, quote_policy="pre_close_v1",
                         llm=llm, provider_budget=None, provider_identity=None, model_identity=None,
                         run_deadline_epoch_ms=None, company_name="ACME", market_facts={})
    extracted = check_direction(validate_quotes(parse_extraction(json.dumps(payload)), item, temporal, "pre_close_v1"), -4.0)
    replay_gate = select_and_gate(cluster_events([(item, extracted)]), {"observation": observation}, GateConfig("desk"))
    assert result.gate_decision.publish == replay_gate.publish
    assert result.gate_decision.reason_code == replay_gate.reason_code
    assert result.analyst_decision.candidate_hypotheses
    (cluster_trace,) = result.artifact["clusters"]
    assert set(cluster_trace) == {
        "type", "scope", "origins", "explicit_origins", "has_primary_source",
        "best_attribution", "best_event", "pairing_rule",
    }
    assert cluster_trace["best_attribution"]["quote_sha256"]
    assert cluster_trace["best_event"]["quote_sha256"]
    assert item["excerpt_text"] not in json.dumps(cluster_trace)
    from catalyst_agents.attribution.extraction import build_extraction_messages
    assert llm.messages == build_extraction_messages(result.facts, item)
    assert llm.options == {"max_tokens": 600, "response_format": {"type": "json_object"},
                           "extra_body": {"thinking": {"type": "disabled"}}}


@pytest.mark.skipif(
    not (Path(__file__).parents[3] / "scripts" / "dev_run.py").is_file(),
    reason="private evaluation runner is not part of the public export",
)
def test_dev_run_accepts_desk_cascade_choice(monkeypatch):
    path = Path(__file__).parents[3] / "scripts" / "dev_run.py"
    spec = importlib.util.spec_from_file_location("catalyst_dev_run_desk_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("CATALYST_EVIDENCE_WINDOW_POLICY", "pre_close_v1")
    module._configure_evidence_window_policy("desk_cascade_v1")
    assert __import__("os").environ["CATALYST_EVIDENCE_WINDOW_POLICY"] == "desk_cascade_v1"


def test_desk_decide_registers_its_role_limit_and_stops_on_budget_errors():
    import pytest
    from catalyst_agents.nodes.desk_decision import DESK_EXTRACTOR_ROLE
    from catalyst_agents.runtime.provider_budget import (
        ProviderBudgetGuard,
        ProviderPriceUnavailable,
    )

    class _Refusing:
        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            raise AssertionError("dispatched without a reservation")

    item = {"evidence_id": "e1", "content_state": "FULL_TEXT", "eligible_at": "2026-04-08T19:00:00Z",
            "publisher": "P", "source_class": "reported_news", "canonical_url": "https://x.test/a",
            "excerpt_text": "ACME fell."}
    pack = SimpleNamespace(included_evidence_ids=("e1",), lead_only_evidence=(), evidence_inventory=(item,))
    budget = ProviderBudgetGuard(max_provider_calls=10, max_cost_usd=1.0, prices={}, role_token_limits={})
    # v6 smoke: an unregistered role raised ProviderPriceUnavailable, which was swallowed as a per-item
    # failure; every extraction failed silently and every case abstained.
    with pytest.raises(ProviderPriceUnavailable):
        desk_decide(pack, observation={"ticker": "ACME", "target_return": -1.0},
                    temporal_identity=build_temporal_identity("2026-04-08"), quote_policy="pre_close_v1",
                    llm=_Refusing(), provider_budget=budget, provider_identity="p", model_identity="m",
                    run_deadline_epoch_ms=None, company_name="ACME", market_facts={})
    assert DESK_EXTRACTOR_ROLE in budget._role_limits
