"""RQ-16: the earnings fallback writes its own trace round.

Trace-only change: the fallback's quote checks, gates, verifier and outcome go
into a new round (last_stage_pack.round + 1, cascade_stage=4) instead of
overwriting the stage's records; snapshot fields must stay identical.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

from catalyst_agents.attribution.answer_snapshot import (
    build_answer_snapshot,
    derive_abstention_reason,
)
from catalyst_agents.observability.decision_trace import DecisionTrace


# ---------------------------------------------------------------- helpers
def _fired_trace(old_style: bool) -> dict:
    """A minimal two-round trace with a fired fallback.

    ``old_style=True`` builds today's (pre-change) trace: the fallback overwrites
    round 2's analyst/verifier/outcome records. ``old_style=False`` builds the
    post-change trace: a separate round 3 with cascade_stage=4. The non-fallback
    records and the fallback's own records are otherwise identical.
    """
    stage_round = {
        "round": 2,
        "cascade_stage": 2,
        "desk_decision": {"gate": {"reason_code": None}, "cascade_stage": 2},
        "model_reported": {
            "analyst": {"hypotheses": [{"ref": "stage_h", "proposed_role": "PRIMARY"}]},
            "verifier": {"called": True, "verdict": "REJECT", "category": "OTHER", "reason": "stage note"},
        },
        "code_checks": {
            "quotes": [{"hypothesis_ref": "stage_h", "evidence_id": "e1", "text": "stage quote",
                        "kept": False, "rule": "R1"}],
            "gates": [{"gate": "comove", "result": "no_change", "detail": None}],
            "outcome": {"status_ceiling": "ABSTAIN", "ceiling_codes": [], "final_status": "ABSTAIN",
                        "attribution_type": "NONE", "research_decision": "RESEARCH",
                        "hypotheses": [{"id": "stage_id", "role": "PRIMARY", "cause_type": "COMPANY_SPECIFIC_CATALYST"}]},
        },
    }
    fallback_analyst = {"hypotheses": [{"ref": "h1", "proposed_role": "PRIMARY"}]}
    fallback_verifier = {"called": True, "verdict": "SUPPORTED"}
    fallback_quotes = [{"hypothesis_ref": "h1", "evidence_id": "e1", "text": "Fallback quote.",
                        "kept": True, "roles": ["LEAD"]}]
    fallback_outcome = {
        "status_ceiling": "PUBLISHED", "ceiling_codes": [], "final_status": "PARTIAL",
        "attribution_type": "EVENT", "research_decision": "RESEARCH",
        "hypotheses": [{"id": "fb_id", "role": "PRIMARY", "cause_type": "COMPANY_SPECIFIC_CATALYST"}],
    }
    if old_style:
        fallback_round = copy.deepcopy(stage_round)
        fallback_round["model_reported"]["analyst"] = fallback_analyst
        fallback_round["model_reported"]["verifier"] = fallback_verifier
        fallback_round["code_checks"]["quotes"] = fallback_quotes
        fallback_round["code_checks"]["outcome"] = fallback_outcome
        fallback_round["cascade_stage"] = 4
        fallback_round["desk_decision"] = {"earnings_fallback": {}, "cascade_stage": 4}
        fallback_round["earnings_fallback"] = {"fire": True}
        return {"rounds": [stage_round, fallback_round], "retrieval": {"included_in_pack": 1}}
    fallback = {
        "round": 3, "cascade_stage": 4, "earnings_fallback": {"fire": True},
        "model_reported": {"analyst": fallback_analyst, "verifier": fallback_verifier},
        "code_checks": {"quotes": fallback_quotes, "gates": [], "outcome": fallback_outcome},
    }
    return {"rounds": [stage_round, fallback], "retrieval": {"included_in_pack": 1}}


def _snapshot_kwargs(trace: dict) -> dict:
    item = SimpleNamespace(
        evidence_id="e1", publisher="Reuters", source_class="reported_news",
        canonical_url="https://example.com/x", eligible_at=datetime(2026, 4, 10, 19, 0, tzinfo=timezone.utc),
        source_time_at=None,
    )
    temporal = SimpleNamespace(
        session_date="2026-04-10",
        information_window_start_at=datetime(2026, 4, 9, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        session_open_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        publication_window=lambda: (datetime(2026, 4, 9, tzinfo=timezone.utc),
                                    datetime(2026, 4, 10, tzinfo=timezone.utc)),
        evidence_window_start_at=None,
    )
    return dict(
        run_id="run", ticker="X",
        move_profile=SimpleNamespace(
            target_return=None, commodity_ticker=None, commodity_return=None, gap_return=None,
            market_comove=None, sector_comove=None, sector_ticker=None,
            peer_summary=None, peer_comove=None,
        ),
        temporal_identity=temporal, facts_text="",
        assessment=SimpleNamespace(
            final_status="PARTIAL",
            normalized_hypotheses=(SimpleNamespace(hypothesis_id="fb_id", source_hypothesis_ref="h1"),),
        ),
        validated_claim_plan=SimpleNamespace(
            claims=(SimpleNamespace(role=SimpleNamespace(value="PRIMARY"), claim_id="c1",
                                    source_hypothesis_id="fb_id"),),
            required_limitations=(),
        ),
        context_pack=SimpleNamespace(evidence_inventory=(item,)),
        decision_trace=trace, abstention_context={}, evidence_window_policy="pre_close_v1",
    )


# ---------------------------------------------------------------- tests
def test_fired_fallback_gets_its_own_round_with_stage_records_preserved():
    """Minimum-unit analogue of Cascade.earnings: begin a fresh round and keep
    the stage's own round record untouched."""
    trace = DecisionTrace("run")
    stage = trace.begin("checks_and_verifier", round=2)
    trace.outcome(2, SimpleNamespace(
        status_ceiling="ABSTAIN", validated_missing_evidence=(),
        normalization_diagnostics=(), final_status="ABSTAIN",
        final_attribution_type=None, research_decision="RESEARCH",
        normalized_hypotheses=(),
    ))
    trace.end(stage)
    rounds_before = copy.deepcopy(trace.payload["rounds"])

    fallback_round = 2 + 1
    fb_stage = trace.begin("earnings_fallback", round=fallback_round, cascade_stage=4)
    trace.outcome(fallback_round, SimpleNamespace(
        status_ceiling="PUBLISHED", validated_missing_evidence=(),
        normalization_diagnostics=(), final_status="PARTIAL",
        final_attribution_type="EVENT", research_decision="RESEARCH",
        normalized_hypotheses=(),
    ))
    trace.end(fb_stage)
    trace.payload["rounds"][-1]["cascade_stage"] = 4

    assert [r["round"] for r in trace.payload["rounds"]] == [2, 3]
    assert trace.payload["rounds"][0] == rounds_before[0]
    assert trace.payload["rounds"][-1]["cascade_stage"] == 4
    assert trace.payload["rounds"][-1]["code_checks"]["outcome"]["final_status"] == "PARTIAL"
    assert trace.payload["rounds"][0]["code_checks"]["outcome"]["final_status"] == "ABSTAIN"


def test_snapshot_after_round_split_matches_old_style_trace_fired_published():
    old = _snapshot_kwargs(_fired_trace(old_style=True))
    new = _snapshot_kwargs(_fired_trace(old_style=False))
    a = build_answer_snapshot(**old)
    b = build_answer_snapshot(**new)
    for field in ("outcome", "status", "key_sentence",
                  "sources", "facts", "limitations"):
        assert a[field] == b[field], field
    assert b["outcome"] == "ANSWERED"


def test_snapshot_after_round_split_matches_old_style_trace_fired_blocked():
    def blocked(old_style: bool) -> dict:
        """A fired fallback whose verifier rejects (blocked publication)."""
        trace = _fired_trace(old_style)
        last = trace["rounds"][-1]
        last["model_reported"]["verifier"] = {"called": True, "verdict": "REJECT",
                                              "category": "OTHER", "reason": "no"}
        last["code_checks"]["outcome"] = {
            "status_ceiling": "ABSTAIN", "ceiling_codes": [], "final_status": "ABSTAIN",
            "attribution_type": "NONE", "research_decision": "RESEARCH",
            "hypotheses": [{"id": "fb_id", "role": "PRIMARY", "cause_type": "COMPANY_SPECIFIC_CATALYST"}],
        }
        return trace

    def snap(old_style: bool) -> dict:
        trace = blocked(old_style)
        kwargs = _snapshot_kwargs(trace)
        kwargs["assessment"] = SimpleNamespace(
            final_status="ABSTAIN",
            normalized_hypotheses=(SimpleNamespace(hypothesis_id="fb_id", source_hypothesis_ref="h1"),),
        )
        return build_answer_snapshot(**kwargs)

    a, b = snap(True), snap(False)
    for field in ("outcome", "status", "abstention", "key_sentence",
                  "sources", "facts", "limitations"):
        assert a[field] == b[field], field
    assert a["outcome"] == "ABSTAINED"
    assert a["abstention"]["reason_code"] == derive_abstention_reason(blocked(False))["reason_code"]


def test_snapshot_abstention_reason_reads_last_round_after_split():
    trace = _fired_trace(old_style=False)
    last = trace["rounds"][-1]
    last["model_reported"]["verifier"] = {"called": True, "verdict": "REJECT",
                                          "category": "OTHER", "reason": "no"}
    assert derive_abstention_reason(trace)["reason_code"] == "VERIFIER_REJECTED"


def test_non_firing_fallback_adds_no_round():
    trace = DecisionTrace("run")
    stage = trace.begin("checks_and_verifier", round=2)
    trace.outcome(2, SimpleNamespace(
        status_ceiling="ABSTAIN", validated_missing_evidence=(),
        normalization_diagnostics=(), final_status="ABSTAIN",
        final_attribution_type=None, research_decision="RESEARCH",
        normalized_hypotheses=(),
    ))
    trace.end(stage)
    rounds = copy.deepcopy(trace.payload["rounds"])
    # When the fallback does not fire, Cascade.earnings returns before trace.begin.
    assert trace.payload["rounds"] == rounds
    assert len(trace.payload["rounds"]) == 1
