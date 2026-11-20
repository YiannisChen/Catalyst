"""One cascade stage of the publisher as a LangGraph graph: extract -> date (v2) -> gate -> decide.

With every v2 rule off this is nodes.desk_decision.desk_decide step for step (same inventory filter, facts, extraction
calls, clusters, gate and artifact); tests/test_graphs_equivalence.py and the dev replay check it. With the recency
rule on, the usable records are dated (event_date_v1, one budgeted call per record, identical requests answered once
per run) and a stale top cluster ends the stage (engine.gate, reason STALE_PRIMARY).
"""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Lock
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from catalyst_agents.attribution.extraction import (
    EXTRACTION_PROMPT_VERSION,
    cluster_events,
)
from catalyst_agents.engine import event_date as ED
from catalyst_agents.engine import gate as G
from catalyst_agents.engine import recency as R
from catalyst_agents.engine.rules import V2Rules
from catalyst_agents.nodes.desk_decision import (
    DESK_EXTRACTOR_CONCURRENCY,
    DeskDecisionResult,
    _cluster_trace_rows,
    _item_dict,
    _value,
    build_analyst_decision,
    extract_items,
    gate_facts,
)


class StageState(TypedDict, total=False):
    # inputs
    context_pack: Any
    observation: Any
    temporal_identity: Any
    quote_policy: str
    company_name: str | None
    market_facts: Any
    # outputs of the nodes
    inventory: list
    included_ids: tuple
    facts: dict
    records: list
    calls: dict
    attempts: list
    usages: list
    dates: dict
    clusters: list
    gate: Any
    facts_for_gate: dict
    result: DeskDecisionResult


class DeskStage:
    """Builds the stage graph over the run's model client, budget and v2 rules."""

    def __init__(self, *, llm: Any, provider_budget: Any, provider_identity: str | None, model_identity: str | None,
                 run_deadline_epoch_ms: int | None, rules: V2Rules, ticker: str,
                 filings_202: Callable[[datetime, datetime], list] | None = None,
                 date_cache: dict | None = None,
                 on_item: Callable[[dict, Any], None] | None = None):
        self.llm, self.budget = llm, provider_budget
        self.provider_identity, self.model_identity = provider_identity, model_identity
        self.deadline = run_deadline_epoch_ms
        self.rules, self.ticker = rules, ticker
        self.filings_202 = filings_202
        self.date_cache = date_cache if date_cache is not None else {}
        self.on_item = on_item
        g = StateGraph(StageState)
        g.add_node("extract", self.extract)
        g.add_node("date", self.date)
        g.add_node("gate", self.gate)
        g.add_node("decide", self.decide)
        g.add_edge(START, "extract")
        g.add_conditional_edges("extract", lambda s: "date" if self.rules.recency else "gate", ["date", "gate"])
        g.add_edge("date", "gate")
        g.add_edge("gate", "decide")
        g.add_edge("decide", END)
        self.graph = g.compile()

    # -- nodes ---------------------------------------------------------------------------------------------------
    def extract(self, s: StageState) -> dict:
        pack = s["context_pack"]
        included_ids = tuple(getattr(pack, "included_evidence_ids", ()))
        lead_ids = {str(_value(item, "evidence_id", "")) for item in getattr(pack, "lead_only_evidence", ())}
        allowed = set(included_ids) | lead_ids
        inventory = [_item_dict(item) for item in getattr(pack, "evidence_inventory", ())]
        inventory = [item for item in inventory if str(item.get("evidence_id", "")) in allowed
                     and str(getattr(item.get("content_state"), "value", item.get("content_state"))).upper() == "FULL_TEXT"]
        observation = s["observation"]
        obs = observation.model_dump(mode="json") if hasattr(observation, "model_dump") else observation
        obs = obs if isinstance(obs, dict) else vars(obs)
        company_name = s.get("company_name")
        if company_name is None:
            try:
                from catalyst_data.ingestion.collectors.web_discovery_v2 import _company
                company_name = _company(str(obs.get("ticker") or ""))
            except Exception:
                company_name = None
        ti = s["temporal_identity"]
        facts = {
            "ticker": obs.get("ticker"), "company_name": company_name, "session_date": ti.session_date,
            "target_return": obs.get("target_return"), "market_return": obs.get("market_return"),
            "sector_return": obs.get("sector_return"), "observation": obs,
        }
        records, calls, attempts, usages = extract_items(
            inventory, facts=facts, temporal_identity=ti, quote_policy=s["quote_policy"], llm=self.llm,
            provider_budget=self.budget, provider_identity=self.provider_identity, model_identity=self.model_identity,
            run_deadline_epoch_ms=self.deadline, on_item=self.on_item,
        )
        return {"inventory": inventory, "included_ids": included_ids, "facts": facts, "records": records,
                "calls": dict(calls), "attempts": list(attempts), "usages": list(usages), "company_name": company_name}

    def date(self, s: StageState) -> dict:
        """event_date_v1 for every usable record (budgeted like the extractor; one answer per identical request)."""
        from catalyst_agents.runtime.control import RunDeadlineExceededError
        from catalyst_agents.runtime.provider_budget import (
            ProviderBudgetError,
            RoleTokenLimit,
        )
        from catalyst_agents.runtime.provider_capability import (
            extract_provider_usage,
            invoke_with_bounded_retry,
        )
        usable = set(R.usable_ids(s["records"]))
        jobs = [(item, ext) for item, ext in s["records"] if str(item.get("evidence_id")) in usable]
        register = getattr(self.budget, "register_role_limit", None)
        limits = getattr(self.budget, "_role_limits", {}) or {}
        if callable(register) and ED.ROLE not in limits:
            register(ED.ROLE, RoleTokenLimit(max_input_tokens=4_000, max_output_tokens=ED.MAX_OUTPUT_TOKENS))
        calls, attempts, usages = dict(s["calls"]), list(s["attempts"]), list(s["usages"])
        lock = Lock()

        def one(job):
            item, ext = job
            messages = ED.messages(self.ticker, s.get("company_name"), item, ext)
            digest = hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            with lock:
                if digest in self.date_cache:
                    return str(item.get("evidence_id")), self.date_cache[digest]
            if self.deadline is not None and time.time() * 1000 >= self.deadline:
                return str(item.get("evidence_id")), {"date": None, "status": "REJECTED:deadline_exceeded"}
            bound = self.llm.bind(**ED.REQUEST_OPTIONS) if callable(getattr(self.llm, "bind", None)) else None
            try:
                res = invoke_with_bounded_retry(
                    (lambda: bound.invoke(messages)) if bound is not None
                    else (lambda: self.llm.invoke(messages, **ED.REQUEST_OPTIONS)),
                    role=ED.ROLE, semantic_input_hash=digest, budget=self.budget, provider=self.provider_identity,
                    model_id=self.model_identity, usage_extractor=extract_provider_usage)
            except (ProviderBudgetError, RunDeadlineExceededError):
                raise
            except Exception as exc:
                return str(item.get("evidence_id")), {"date": None, "status": f"REJECTED:call_failed:{type(exc).__name__}"}
            checked = ED.check(str(getattr(res.result, "content", res.result)), item)
            with lock:
                calls["logical_calls"] += res.counts.logical_calls
                calls["provider_attempts"] += res.counts.provider_attempts
                attempts.extend(res.attempts)
                usages.extend(res.attempt_usages)
                self.date_cache[digest] = checked
            return str(item.get("evidence_id")), checked

        with ThreadPoolExecutor(max_workers=DESK_EXTRACTOR_CONCURRENCY) as pool:
            dated = dict(pool.map(one, jobs))
        return {"dates": dated, "calls": calls, "attempts": attempts, "usages": usages}

    def gate(self, s: StageState) -> dict:
        facts_for_gate = gate_facts(s["observation"], s["temporal_identity"], s["market_facts"])
        clusters = cluster_events(s["records"])
        veto = None
        if self.rules.recency:
            ti = s["temporal_identity"]
            prior_close, close = ti.information_window_start_at, ti.session_close_at
            filings = self.filings_202 if self.rules.anchor else None
            dates = s.get("dates") or {}
            veto = lambda c: R.stale(c, dates=dates, prior_close=prior_close, close=close,  # noqa: E731
                                     filings_202=filings)
        return {"clusters": clusters, "gate": G.gate(clusters, facts_for_gate, veto=veto),
                "facts_for_gate": facts_for_gate}

    def decide(self, s: StageState) -> dict:
        gate, records, clusters = s["gate"], s["records"], s["clusters"]
        secondary = gate.secondary if gate.primary is not None else None
        decision = build_analyst_decision(gate, secondary, records, included_evidence_ids=s["included_ids"])
        extraction_rows = tuple({
            "evidence_id": str(item.get("evidence_id", "")),
            "fields": {key: (getattr(extraction, key).value if hasattr(getattr(extraction, key), "value")
                             else getattr(extraction, key))
                       for key in ("about_ticker", "session", "event_type", "event_summary", "event_timing",
                                   "explicit_attribution", "attribution_quote", "event_quote", "mechanism_quote",
                                   "is_preview_or_evergreen")},
            "valid": extraction.valid, "invalid_reason": extraction.invalid_reason,
            "quote_failures": list(extraction.quote_failures),
        } for item, extraction in records)
        cluster_rows = _cluster_trace_rows(clusters, gate.diagnostics)
        calls, attempts, usages = s["calls"], s["attempts"], s["usages"]
        artifact = {
            "prompt_version": EXTRACTION_PROMPT_VERSION,
            "extractions": extraction_rows, "clusters": cluster_rows,
            "gate": {"publish": gate.publish, "basis": gate.basis, "reason_code": gate.reason_code,
                     "diagnostics": list(gate.diagnostics)},
            "calls": calls, "attempts": [a.model_dump(mode="json") if hasattr(a, "model_dump") else vars(a)
                                         for a in attempts],
            "usage": [list(usage) for usage in usages],
        }
        if self.rules.any:  # v2 record; absent when every rule is off (the artifact is then A′'s)
            artifact["v2"] = {"rules": self.rules.to_dict(), "event_date_prompt": ED.PROMPT_VERSION,
                              "event_dates": s.get("dates") or {}}
        result = DeskDecisionResult(decision, gate, extraction_rows, tuple(clusters), artifact,
                                    calls["logical_calls"], calls["provider_attempts"], tuple(attempts),
                                    tuple(usages), records=tuple(records), facts=s["facts"],
                                    gate_facts=s["facts_for_gate"])
        return {"result": result}

    def run(self, context_pack: Any, *, observation: Any, temporal_identity: Any, quote_policy: str,
            company_name: str | None, market_facts: Any) -> DeskDecisionResult:
        out = self.graph.invoke({"context_pack": context_pack, "observation": observation,
                                 "temporal_identity": temporal_identity, "quote_policy": quote_policy,
                                 "company_name": company_name, "market_facts": market_facts})
        return out["result"]
