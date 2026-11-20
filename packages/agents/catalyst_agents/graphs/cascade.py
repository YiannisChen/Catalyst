"""The desk cascade of a run as a LangGraph graph: stage_1 -> (abstain) -> stage_2 -> (abstain) -> earnings.

This replaces the hand-written cascade that ran inside graph.run_v1_graph (desk_cascade_v1 + discovery_v2). Each
stage is a graphs.desk_stage graph followed by the frozen normalisation (quote rules, ceilings, the same-event
verifier). The ladder is fixed and only abstention moves the run down it; a later rung is never more permissive:
stage 2 uses the event-time quote policy and verifier, and the earnings rung publishes only the issuer's own filing.

With every v2 rule off the graph reproduces A′: the same calls, decisions, trace rounds, events and artifacts as the
code it replaced (checked by replaying the recorded dev split; docs/design/v2/implementation.md).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import datetime
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from catalyst_agents.engine.rules import V2Rules
from catalyst_agents.graphs.desk_stage import DeskStage
from catalyst_agents.nodes.desk_decision import read_reason_code
from catalyst_agents.runtime.earnings_timing import earnings_timings_for_context_pack


@dataclass
class CascadeInputs:
    run_id: str
    ticker: str
    temporal_identity: Any
    stage2_temporal_identity: Any
    data_runtime_identity: Any
    cutoff: str
    requested_manifest_id: str
    observation_provider: Any
    retriever: Any
    stage2_retriever: Any
    policy_config: Any
    research_concurrency: int
    research_stage_timeout_seconds: float
    persistence: Any
    packing_policy_version: str
    template_bytes: bytes
    template_version: str
    structured_provider: Any
    run_deadline_epoch_ms: int | None
    observation_policy_version: str
    move_profile: Any
    foundation: Any
    analyst_llm: Any
    provider_budget: Any
    provider_identity: str | None
    analyst_model_identity: str | None
    capability_registry: Any
    normalization_policy_version: str
    corrective_policy: Any
    attribution_policy: Any
    investigator_deps: Any
    desk_context: Any
    web_source_title: str | None
    web_source_company_name: str | None
    tier: str | None
    trace: Any
    emit: Callable[[str, dict], None]
    control: Any
    rules: V2Rules = field(default_factory=V2Rules)
    market_db: Any = None
    foundation_builder: Callable[..., Any] | None = None


@dataclass
class CascadeResult:
    assessment: Any
    final_pack: Any
    foundation: Any
    temporal_identity: Any
    primary_verifier: Any
    stage1_verifier_calls: dict
    stage2_verifier_calls: dict
    analyst_logical_calls: int
    analyst_provider_attempts: int
    analyst_attempt_usages: tuple
    final_retriever: Any
    desk_result1: Any
    desk_result2: Any
    final_desk: Any
    final_v2: Any
    investigation_v2: Any
    investigation_payload: Any
    web_source_filter_removed_all: bool
    basis: str | None
    cascade_stage: int
    earnings: dict | None
    last_attempted_stage: int | None = None
    published_stage: int | None = None


class CascadeState(TypedDict, total=False):
    result: CascadeResult


STAGE2_ENV = "CATALYST_CASCADE_STAGE2"


def stage2_enabled() -> bool:
    """Round 2 (peer/sector widening) runs only when CATALYST_CASCADE_STAGE2=1."""
    return os.environ.get(STAGE2_ENV, "").strip() == "1"


def _status(assessment: Any) -> str:
    return str(getattr(assessment.final_status, "value", assessment.final_status))


def _cached_sec(fn, *args, **kwargs):
    """Call an SEC reader from the local cache only. A run never fetches SEC live (owner, 2026-11-16): with a
    stale submissions file the earnings fallback fetched every 8-K index page of the issuer and held CRM
    2026-03-13 for many minutes past the run limit. The daily job keeps the cache current."""
    from catalyst_data.ingestion.collectors import sec_filings

    with sec_filings.cached_only():
        return fn(*args, **kwargs)


def _first_quote(hypothesis: Any) -> dict:
    """The candidate's first verbatim quote, for live progress while the cause is checked."""
    for quote in getattr(hypothesis, "quotes", ()) or ():
        text = " ".join(str(getattr(quote, "text", "") or "").split())
        if text:
            return {"quote": text[:600], "evidence_id": str(getattr(quote, "evidence_id", "") or "") or None}
    return {}


class Cascade:
    def __init__(self, ctx: CascadeInputs):
        self.ctx = ctx
        self.date_cache: dict = {}
        self.filings_202 = None
        if ctx.rules.anchor or ctx.rules.earnings:
            from catalyst_data.ingestion.collectors import sec_filings
            self.filings_202 = lambda start, end: _cached_sec(sec_filings.between, ctx.ticker, start, end, item="2.02")
        g = StateGraph(CascadeState)
        g.add_node("stage_1", self.stage_1)
        g.add_node("stage_2", self.stage_2)
        g.add_node("earnings", self.earnings)
        g.add_edge(START, "stage_1")
        g.add_conditional_edges("stage_1", self.after_stage_1, ["stage_2", "earnings", END])
        g.add_conditional_edges("stage_2", self.after_stage_2, ["earnings", END])
        g.add_edge("earnings", END)
        self.graph = g.compile()

    def _desk(self) -> DeskStage:
        c = self.ctx
        return DeskStage(llm=c.analyst_llm, provider_budget=c.provider_budget, provider_identity=c.provider_identity,
                         model_identity=c.analyst_model_identity, run_deadline_epoch_ms=c.run_deadline_epoch_ms,
                         rules=c.rules, ticker=c.ticker, filings_202=self.filings_202, date_cache=self.date_cache,
                         on_item=self._emit_read)

    def _emit_read(self, item: dict, extraction: Any) -> None:
        """Live progress: one analyst.decision per article as soon as it has been read."""
        self.ctx.emit("analyst.decision", {
            "kind": "evidence", "evidence_id": str(item.get("evidence_id", "")),
            "disposition": "READ", "reason_code": read_reason_code(extraction),
        })

    def _verifier(self, policy: str):
        c = self.ctx
        if not c.attribution_policy.verifier_enabled:
            return None
        from catalyst_agents.attribution.verifier import make_verifier
        return make_verifier(c.analyst_llm, budget=c.provider_budget, provider=c.provider_identity,
                             model_id=c.analyst_model_identity, deadline_epoch_ms=c.run_deadline_epoch_ms,
                             evidence_window_policy=policy)

    def _emit_decision(self, raw_decision: Any, stage: dict, *, detailed: bool) -> None:
        emit = self.ctx.emit
        def v(x):
            return str(getattr(x, "value", x))
        for d in getattr(raw_decision, "evidence_decisions", ()):
            emit("analyst.decision", {"kind": "evidence", "evidence_id": d.evidence_id,
                                      "disposition": v(d.disposition) if detailed else d.disposition.value,
                                      "reason_code": d.reason_code, "note": d.note or None})
        for h in getattr(raw_decision, "candidate_hypotheses", ()):
            emit("analyst.decision", {
                "kind": "candidate_cause", "ref": h.hypothesis_ref,
                "cause_type": v(h.cause_type) if detailed else h.cause_type.value,
                "proposed_role": (str(getattr(getattr(h, "proposed_role", None), "value", getattr(h, "proposed_role", None)))
                                  if detailed else h.proposed_role.value)})
        emit("analyst.done", {
            "duration_ms": max(0, int(stage.get("duration_ms", 0))),
            "evidence_decisions": [{"evidence_id": d.evidence_id,
                                    "disposition": v(d.disposition) if detailed else d.disposition.value,
                                    "reason_code": d.reason_code, "note": d.note or None}
                                   for d in getattr(raw_decision, "evidence_decisions", ())],
            "hypotheses": [{"ref": h.hypothesis_ref, "cause_type": v(h.cause_type) if detailed else h.cause_type.value,
                            "proposed_role": (str(getattr(getattr(h, "proposed_role", None), "value",
                                                          getattr(h, "proposed_role", None)))
                                              if detailed else h.proposed_role.value),
                            **_first_quote(h)}
                           for h in getattr(raw_decision, "candidate_hypotheses", ())],
            "research_decision": (str(getattr(getattr(raw_decision, "research_decision", None), "value",
                                              getattr(raw_decision, "research_decision", "UNKNOWN")))
                                  if detailed else raw_decision.research_decision.value),
        })

    def _emit_checks(self, round_trace: dict, stage: dict) -> None:
        emit = self.ctx.emit
        checks = round_trace.get("code_checks", {})
        emit("checks.done", {
            "quotes": [{key: item[key] for key in ("hypothesis_ref", "evidence_id", "rule", "kept")}
                       for item in checks.get("quotes", ())],
            "gates": checks.get("gates", ()), "duration_ms": max(0, int(stage.get("duration_ms", 0)))})
        emit("gates.done", {"gates": checks.get("gates", ()), "duration_ms": max(0, int(stage.get("duration_ms", 0)))})
        verifier = round_trace.get("model_reported", {}).get("verifier", {})
        emit("verifier.done", {"called": bool(verifier.get("called", False)), "verdict": verifier.get("verdict"),
                                "category": verifier.get("category"), "reason": verifier.get("reason"),
                                "skipped_because": verifier.get("skipped_because")})

    def _persist(self, artifact_id: str, artifact_type: str, payload: dict) -> None:
        persist = getattr(self.ctx.persistence, "persist_artifact", None)
        if callable(persist):
            persist(run_id=self.ctx.run_id, artifact_id=artifact_id, artifact_type=artifact_type, payload=payload)

    # -- stage 1 ------------------------------------------------------------------------------------------------
    def stage_1(self, s: CascadeState) -> dict:
        from catalyst_agents.attribution.assessment import normalize_decision
        from catalyst_agents.graphs.web_source_decision import web_source_decision
        from catalyst_agents.runtime.control import raise_if_control_expired
        c, trace = self.ctx, self.ctx.trace
        ti, foundation = c.temporal_identity, c.foundation
        primary_verifier = self._verifier("pre_close_v1")
        stage = trace.begin("evidence_analyst", round=1)
        desk_result1 = self._desk().run(
            foundation.context_pack, observation=foundation.context_pack.observation, temporal_identity=ti,
            quote_policy="pre_close_v1", company_name=c.web_source_company_name,
            market_facts={"session_close": ti.session_close_at})
        self._persist(f"desk_decision:{c.run_id}:1", "desk_decision", {**desk_result1.artifact, "cascade_stage": 1})
        trace.end(stage)
        raw_decision = desk_result1.analyst_decision
        web_source_filter_removed_all = False
        if c.web_source_title is not None:
            proposed = tuple(getattr(raw_decision, "candidate_hypotheses", ()))
            raw_decision = web_source_decision(raw_decision, title=c.web_source_title, ticker=c.ticker,
                                                company_name=c.web_source_company_name or c.ticker)
            web_source_filter_removed_all = bool(proposed) and not raw_decision.candidate_hypotheses
        self._emit_decision(raw_decision, stage, detailed=True)
        raise_if_control_expired(c.control)
        stage = trace.begin("checks_and_verifier", round=1)
        assessment = normalize_decision(
            raw_decision, foundation.context_pack, c.capability_registry, c.normalization_policy_version,
            policy=c.corrective_policy, evidence_state_hash=foundation.state["evidence_state_hash"], quote_rules=True,
            evidence_window_policy="pre_close_v1", filing_rule=c.rules.filing,
            release_timings=earnings_timings_for_context_pack(foundation.context_pack),
            primary_verifier=primary_verifier, trace=trace)
        trace.end(stage)
        trace.outcome(1, assessment)
        trace.payload["rounds"][-1]["cascade_stage"] = 1
        trace.payload["rounds"][-1]["desk_decision"] = {**desk_result1.artifact, "cascade_stage": 1}
        stage1_calls = dict(getattr(primary_verifier, "calls", None) or {})
        self._emit_checks(trace.payload["rounds"][-1], stage)
        result = CascadeResult(
            assessment=assessment, final_pack=foundation.context_pack, foundation=foundation, temporal_identity=ti,
            primary_verifier=primary_verifier, stage1_verifier_calls=stage1_calls, stage2_verifier_calls={},
            analyst_logical_calls=desk_result1.logical_calls, analyst_provider_attempts=desk_result1.provider_attempts,
            analyst_attempt_usages=tuple(desk_result1.attempt_usages), final_retriever=c.retriever,
            desk_result1=desk_result1, desk_result2=None, final_desk=desk_result1, final_v2=None,
            investigation_v2=None, investigation_payload=None,
            web_source_filter_removed_all=web_source_filter_removed_all,
            basis=desk_result1.gate_decision.basis if _status(assessment) != "ABSTAIN" else None,
            cascade_stage=1, earnings=None,
            last_attempted_stage=1,
            published_stage=1 if _status(assessment) != "ABSTAIN" else None)
        return {"result": result}

    def after_stage_1(self, s: CascadeState) -> str:
        c = self.ctx
        if _status(s["result"].assessment) != "ABSTAIN":
            return END
        if c.attribution_policy.web_evidence == "discovery_v2":
            if stage2_enabled():
                return "stage_2"
            # Product default (2026-11-16): round 2 widened to peer/sector news but published nothing in 97 local
            # days while costing ~12 s and ~40 calls; abstentions go straight to the earnings rung.
            c.trace.payload["cascade_stage_2"] = "skipped"
        return "earnings" if c.rules.earnings else END

    # -- stage 2 ------------------------------------------------------------------------------------------------
    def stage_2(self, s: CascadeState) -> dict:
        from catalyst_agents.attribution.assessment import normalize_decision
        from catalyst_agents.graphs.research_context import (
            FoundationGraphInputs,
            FoundationRetrievalStageInput,
            run_foundation_retrieval_stage,
        )
        c, trace, emit = self.ctx, self.ctx.trace, self.ctx.emit
        r: CascadeResult = s["result"]
        if c.attribution_policy.investigator_mode is not None:
            raise ValueError("unknown investigator mode")
        if c.stage2_temporal_identity is None or c.stage2_retriever is None:
            raise ValueError("desk_cascade_v1 discovery_v2 requires stage-two temporal and retrieval inputs")
        logical, attempts, usages = r.analyst_logical_calls, r.analyst_provider_attempts, r.analyst_attempt_usages
        investigation_payload = None
        ti2 = c.stage2_temporal_identity
        from catalyst_agents.foundation import (
            build_foundation_graph as default_foundation_builder,
        )
        foundation_stage = run_foundation_retrieval_stage(FoundationRetrievalStageInput(
            foundation_inputs=FoundationGraphInputs(
                run_id=c.run_id, round=2, temporal_identity=ti2,
                data_runtime_identity=c.data_runtime_identity, ticker=c.ticker, cutoff=c.cutoff,
                requested_manifest_id=c.requested_manifest_id, observation_provider=c.observation_provider,
                retriever=c.stage2_retriever, policy_config=c.policy_config,
                research_concurrency=c.research_concurrency,
                research_stage_timeout_seconds=c.research_stage_timeout_seconds, persistence=c.persistence,
                packing_policy_version=c.packing_policy_version, template_bytes=c.template_bytes,
                template_version=c.template_version, structured_provider=c.structured_provider,
                run_deadline_epoch_ms=c.run_deadline_epoch_ms,
                observation_policy_version=c.observation_policy_version, move_profile_override=c.move_profile,
            ),
            foundation_builder=c.foundation_builder or default_foundation_builder,
            trace=trace, emit=emit, stage_name="cascade_stage_2_research_and_context_pack",
            round=2, temporal_identity=ti2, control=c.control,
            trace_cascade_stage=2, cascade_retrieval_stage=2,
        ))
        foundation2 = foundation_stage.foundation
        stage = trace.begin("evidence_analyst", round=2, cascade_stage=2)
        desk_result2 = self._desk().run(
            foundation2.context_pack, observation=foundation2.context_pack.observation, temporal_identity=ti2,
            quote_policy="event_time_p1", company_name=c.web_source_company_name,
            market_facts={"session_close": ti2.session_close_at})
        trace.end(stage)
        self._persist(f"desk_decision:{c.run_id}:2", "desk_decision", {**desk_result2.artifact, "cascade_stage": 2})
        raw_decision2 = desk_result2.analyst_decision
        self._emit_decision(raw_decision2, stage, detailed=False)
        stage = trace.begin("checks_and_verifier", round=2, cascade_stage=2)
        primary_verifier = self._verifier("event_time_p1")
        assessment = normalize_decision(
            raw_decision2, foundation2.context_pack, c.capability_registry, c.normalization_policy_version,
            policy=c.corrective_policy, evidence_state_hash=foundation2.state["evidence_state_hash"],
            quote_rules=True, evidence_window_policy="event_time_p1", filing_rule=c.rules.filing,
            release_timings=earnings_timings_for_context_pack(foundation2.context_pack),
            primary_verifier=primary_verifier, trace=trace)
        trace.end(stage)
        trace.outcome(2, assessment)
        trace.payload["rounds"][-1]["cascade_stage"] = 2
        trace.payload["rounds"][-1]["desk_decision"] = {**desk_result2.artifact, "cascade_stage": 2}
        stage2_calls = dict(getattr(primary_verifier, "calls", None) or {})
        stage2_trace = trace.payload["rounds"][-1]
        self._emit_checks(stage2_trace, stage)
        logical += desk_result2.logical_calls
        attempts += desk_result2.provider_attempts
        usages += desk_result2.attempt_usages
        final_pack, final_v2, investigation_v2 = foundation2.context_pack, None, None
        published = _status(assessment) != "ABSTAIN"
        basis = ((final_v2["gate"].basis if final_v2 is not None else desk_result2.gate_decision.basis)
                 if published else None)
        result = CascadeResult(
            assessment=assessment, final_pack=final_pack, foundation=foundation2, temporal_identity=ti2,
            primary_verifier=primary_verifier, stage1_verifier_calls=r.stage1_verifier_calls,
            stage2_verifier_calls=stage2_calls, analyst_logical_calls=logical, analyst_provider_attempts=attempts,
            analyst_attempt_usages=usages, final_retriever=c.stage2_retriever, desk_result1=r.desk_result1,
            desk_result2=desk_result2, final_desk=desk_result2, final_v2=final_v2, investigation_v2=investigation_v2,
            investigation_payload=investigation_payload, web_source_filter_removed_all=r.web_source_filter_removed_all,
            basis=basis, cascade_stage=3 if final_v2 is not None else 2, earnings=None,
            last_attempted_stage=3 if investigation_v2 is not None else 2,
            published_stage=(3 if investigation_v2 is not None else 2) if published else None)
        return {"result": result}

    def after_stage_2(self, s: CascadeState) -> str:
        return "earnings" if self.ctx.rules.earnings and _status(s["result"].assessment) == "ABSTAIN" else END

    # -- earnings fallback (v2 rule 3) ---------------------------------------------------------------------------
    def earnings(self, s: CascadeState) -> dict:
        from catalyst_agents.attribution.assessment import normalize_decision
        from catalyst_agents.attribution.extraction import GateDecision, _make_cluster
        from catalyst_agents.engine import earnings as E
        from catalyst_agents.nodes.desk_decision import (
            DeskDecisionResult,
            _item_dict,
            build_analyst_decision,
        )
        c, trace = self.ctx, self.ctx.trace
        r: CascadeResult = s["result"]
        ti = c.temporal_identity
        prior_close, close = ti.information_window_start_at, ti.session_close_at
        from catalyst_data.ingestion.collectors import sec_filings
        window = _cached_sec(sec_filings.between, c.ticker, prior_close, close)
        pack = r.final_pack
        included = set(pack.included_evidence_ids)
        items = [_item_dict(i) for i in pack.evidence_inventory if i.evidence_id in included]
        z = E.research_z(c.market_db, c.ticker, ti.session_date) if c.market_db else None
        title_for = None
        if c.rules.filing:
            from catalyst_data.read import sec_units as sec_structure
            title_for = sec_structure.title_for
        decision = E.decide(tier=c.tier, filings=window, z=z, prior_close=prior_close, close=close, pack_items=items,
                            title_for=title_for)
        trace.payload["earnings_fallback"] = {k: v for k, v in decision.items() if k != "quote"} | (
            {"quote": decision["quote"]} if decision.get("quote") else {})
        if not decision["fire"]:
            r.earnings = decision
            return {"result": r}
        item = next(i for i in items if i.get("evidence_id") == decision["evidence_id"])
        if c.rules.filing:  # v2.1: no verified filing fact, no fallback
            decision = self._verify_fact(decision, item)
            trace.payload["earnings_fallback"].update({k: decision.get(k) for k in ("fact", "fact_checks")})
            if not decision["fact"]:
                decision = {**decision, "fire": False, "reason": "fact_unverified"}
                trace.payload["earnings_fallback"].update(fire=False, reason="fact_unverified")
                r.earnings = decision
                return {"result": r}
        records = E.answer_records(item, decision["quote"])
        cluster = _make_cluster(records)
        gate = GateDecision(True, cluster, None, "PUBLISHED", (), E.BASIS)
        analyst = build_analyst_decision(gate, None, records, included_evidence_ids=tuple(pack.included_evidence_ids))
        policy = "event_time_p1" if r.desk_result2 is not None else "pre_close_v1"
        verifier = self._verifier(policy)
        # The fallback is checked on the final pack, but writes its own trace round
        # so the stage's own verifier, gates and outcome are preserved untouched.
        fallback_round = pack.round + 1
        fallback_pack = dataclass_replace(pack, round=fallback_round) if hasattr(pack, "__dataclass_fields__") else pack.model_copy(update={"round": fallback_round})
        stage = trace.begin("earnings_fallback", round=fallback_round, cascade_stage=4)
        state_hash = r.foundation.state["evidence_state_hash"]
        assessment = normalize_decision(
            analyst, fallback_pack, c.capability_registry, c.normalization_policy_version, policy=c.corrective_policy,
            evidence_state_hash=state_hash, quote_rules=True, evidence_window_policy=policy,
            filing_rule=c.rules.filing, release_timings=earnings_timings_for_context_pack(fallback_pack),
            primary_verifier=verifier, trace=trace)
        trace.end(stage)
        trace.outcome(fallback_round, assessment)
        trace.payload["rounds"][-1]["cascade_stage"] = 4
        trace.payload["rounds"][-1]["earnings_fallback"] = trace.payload["earnings_fallback"]
        calls = dict(getattr(verifier, "calls", None) or {})
        stage2_calls = {
            "logical_calls": int(r.stage2_verifier_calls.get("logical_calls", 0)) + int(calls.get("logical_calls", 0)),
            "provider_attempts": int(r.stage2_verifier_calls.get("provider_attempts", 0))
            + int(calls.get("provider_attempts", 0)),
            "attempt_usages": tuple(r.stage2_verifier_calls.get("attempt_usages", ())) + tuple(calls.get("attempt_usages", ())),
        }
        desk = DeskDecisionResult(analyst, gate, (), (cluster,), {"earnings_fallback": decision}, 0, 0, (), (),
                                  records=tuple(records))
        self._persist(f"desk_decision:{c.run_id}:earnings", "desk_decision",
                      {"earnings_fallback": {k: (v.isoformat() if isinstance(v, datetime) else v)
                                             for k, v in decision.items()}, "cascade_stage": 4})
        published = _status(assessment) != "ABSTAIN"
        if published and c.rules.filing:
            assessment = self._prefix_fact(assessment, decision["fact"])
        r.assessment, r.primary_verifier, r.stage2_verifier_calls = assessment, verifier, stage2_calls
        r.last_attempted_stage = 4
        if published:
            r.final_desk, r.basis, r.cascade_stage = desk, E.BASIS, 4
            r.published_stage = 4
        r.earnings = {**decision, "published": published}
        return {"result": r}

    def _verify_fact(self, decision: dict, item: dict) -> dict:
        """v2.1: the filing fact, from SEC metadata verified here: the accession is in the issuer's own submissions
        index (acceptance time, form and items read from it), the quoted exhibit's URL is that accession's, the
        index names the company, and the items include 2.02. Any check failing leaves ``fact`` None."""
        from catalyst_data.ingestion.collectors import sec_filings

        from catalyst_agents.engine import earnings as E
        c = self.ctx
        own = next((f for f in _cached_sec(sec_filings.filings_8k, c.ticker) if f["accession"] == decision["accession"]), None)
        company = _cached_sec(sec_filings.company, c.ticker)
        digits = decision["accession"].replace("-", "")
        checks = {"accession_in_issuer_index": own is not None, "company_named": bool(company),
                  "exhibit_is_this_filing": digits in str(item.get("canonical_url") or "").replace("-", ""),
                  "item_2_02": bool(own) and "2.02" in own["items"]}
        if not all(checks.values()):
            return {**decision, "fact": None, "fact_checks": checks}
        return {**decision, "fact": E.filing_fact(company, c.ticker, own), "fact_checks": checks}

    @staticmethod
    def _prefix_fact(assessment, fact: str):
        """The published fallback's PRIMARY statement: the filing fact, then the quoted release headline."""
        from catalyst_agents.attribution.analyst import HypothesisRole
        hyps = tuple(h.model_copy(update={"statement": f"{fact} {h.statement}"})
                     if h.normalized_role is HypothesisRole.PRIMARY else h
                     for h in assessment.normalized_hypotheses)
        return assessment.model_copy(update={"normalized_hypotheses": hyps})

    def run(self) -> CascadeResult:
        return self.graph.invoke({})["result"]


def run_desk_cascade(ctx: CascadeInputs) -> CascadeResult:
    return Cascade(ctx).run()
