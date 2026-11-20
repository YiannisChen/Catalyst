"""Budgeted per-item extraction and the frozen M1 desk publication gate."""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock
from types import SimpleNamespace
from typing import Any, Callable

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    AttributionStatus,
    AttributionType,
    CandidateHypothesis,
    CauseType,
    EvidenceDecision,
    EvidenceDisposition,
    HypothesisRole,
    MagnitudeFit,
    ResearchDecision,
    SourceQuote,
)
from catalyst_agents.attribution.extraction import (
    EXTRACTION_PROMPT_VERSION,
    EventCluster,
    EventType,
    Extraction,
    GateConfig,
    attach_quote_roles,
    build_extraction_messages,
    check_direction,
    cluster_events,
    parse_extraction,
    render_statement,
    select_and_gate,
    validate_quotes,
)
from catalyst_agents.runtime.control import RunDeadlineExceededError
from catalyst_agents.runtime.provider_budget import ProviderBudgetError, RoleTokenLimit
from catalyst_agents.runtime.provider_capability import (
    extract_provider_usage,
    invoke_with_bounded_retry,
)

DESK_EXTRACTOR_ROLE = "desk_extractor"
# One evidence item: <=2,500 excerpt chars plus facts and instructions; JSON reply capped at 600 tokens.
DESK_EXTRACTOR_LIMIT = RoleTokenLimit(max_input_tokens=4_000, max_output_tokens=600)
# Per-item calls in flight at once. Each call is output-bound (~3-5 s), so wall time is roughly
# ceil(items / concurrency) waves; 6 made a 40-item stage take ~32 s.
DESK_EXTRACTOR_CONCURRENCY = 16


@dataclass(frozen=True)
class DeskDecisionResult:
    analyst_decision: AnalystDecision
    gate_decision: Any
    extraction_results: tuple[dict[str, Any], ...]
    clusters: tuple[EventCluster, ...]
    artifact: dict[str, Any]
    logical_calls: int
    provider_attempts: int
    attempts: tuple[Any, ...]
    attempt_usages: tuple[tuple[int | None, int | None], ...]
    records: tuple[tuple[dict[str, Any], Extraction], ...] = ()
    facts: dict[str, Any] | None = None       # extractor facts (ticker, company, returns, observation)
    gate_facts: dict[str, Any] | None = None


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json").get(key, default)
    return getattr(obj, key, default)


def _item_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    return item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(vars(item))


def _rendered_pairs(cluster: EventCluster) -> list[tuple[dict[str, Any], str]]:
    """Mirror render_statement's chosen source sentences to bind quotes by ID."""
    from catalyst_agents.attribution.extraction import _origin

    chosen: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    origins: set[str] = set()

    def add(pair: tuple[dict, str] | None) -> None:
        if pair is None or len(chosen) >= 3:
            return
        item, quote = pair
        if quote and quote not in seen:
            chosen.append((item, quote))
            seen.add(quote)
            origins.add(_origin(item))

    add(cluster.best_attribution)
    add(cluster.best_mechanism)
    add(cluster.best_event)
    for prefer_attribution in (True, False):
        for item, ext in cluster.members:
            if ext is None or _origin(item) in origins:
                continue
            quote = ext.attribution_quote if prefer_attribution else ext.event_quote
            if prefer_attribution and not ext.explicit_attribution:
                continue
            add((item, quote))
    out: list[tuple[dict[str, Any], str]] = []
    total = 0
    for item, quote in chosen:
        if len(quote) > 900 or total + len(quote) > 1800:
            continue
        out.append((item, quote))
        total += len(quote)
    return out


def _cluster_trace_rows(clusters: list[EventCluster], diagnostics: tuple[dict, ...]) -> tuple[dict[str, Any], ...]:
    """Pair each cluster summary with the classifier details captured during its gate check."""
    return tuple({
        "type": cluster.event_type.value, "scope": cluster.scope.value,
        "origins": cluster.independent_origins, "explicit_origins": cluster.explicit_origins,
        "has_primary_source": cluster.has_primary_source,
        "best_attribution": diagnostic["best_attribution"],
        "best_event": diagnostic["best_event"],
        "pairing_rule": diagnostic["pairing_rule"],
    } for cluster, diagnostic in zip(clusters, diagnostics, strict=True))


def _cause_type(event_type: EventType, scope: Any) -> CauseType:
    if event_type is EventType.ANALYST_ACTION:
        return CauseType.REPORTING_OR_ANALYST_CONTINUATION
    if event_type in (EventType.MACRO,):
        return CauseType.MACRO_EVENT
    if event_type in (EventType.SECTOR, EventType.COMMODITY):
        return CauseType.SECTOR_MOVE
    if event_type in (EventType.EARNINGS, EventType.GUIDANCE, EventType.DEAL_MA,
                      EventType.REGULATORY_LEGAL, EventType.PRODUCT_OPERATIONS,
                      EventType.MANAGEMENT, EventType.CAPITAL_RETURN):
        return CauseType.COMPANY_SPECIFIC_CATALYST
    return CauseType.COMPANY_SPECIFIC_CATALYST if str(getattr(scope, "value", scope)) == "ISSUER" else CauseType.SECTOR_MOVE


def build_analyst_decision(
    gate_decision: Any,
    secondary_cluster: EventCluster | None,
    records: list[tuple[dict[str, Any], Extraction]],
    *,
    included_evidence_ids: tuple[str, ...],
) -> AnalystDecision:
    """Adapt M1's deterministic gate output to the existing analyst contract."""
    candidates: list[CandidateHypothesis] = []
    supporting: dict[str, list[str]] = {}
    primary = gate_decision.primary if gate_decision.publish else None
    clusters: list[tuple[str, EventCluster]] = []
    if primary is not None:
        clusters.append(("h1", primary))
        if secondary_cluster is not None and secondary_cluster is not primary and secondary_cluster.explicit_origins >= 1:
            clusters.append(("h2", secondary_cluster))
    for ref, cluster in clusters:
        pairs = _rendered_pairs(cluster)
        quotes = tuple(SourceQuote(evidence_id=str(item.get("evidence_id", "")), text=text)
                       for item, text in pairs)
        evidence_ids = tuple(dict.fromkeys(q.evidence_id for q in quotes if q.evidence_id))
        supporting[ref] = list(evidence_ids)
        candidates.append(CandidateHypothesis(
            hypothesis_ref=ref,
            cause_type=_cause_type(cluster.event_type, cluster.scope),
            statement=render_statement(cluster),
            mechanism=None,
            quotes=quotes,
            supporting_evidence_ids=evidence_ids,
            magnitude_fit=MagnitudeFit.UNKNOWN,
            proposed_role=HypothesisRole.PRIMARY if ref == "h1" else HypothesisRole.SECONDARY,
        ))
    included = set(included_evidence_ids)
    evidence_decisions = []
    for evidence_id in included_evidence_ids:
        refs = tuple(ref for ref, ids in supporting.items() if evidence_id in ids)
        evidence_decisions.append(EvidenceDecision(
            evidence_id=evidence_id,
            disposition=EvidenceDisposition.SUPPORT if refs else EvidenceDisposition.IRRELEVANT,
            supports_hypothesis_refs=refs,
            reason_code="desk_selected" if refs else "desk_not_selected",
        ))
    # A support quote must also be in the included inventory; source quote selection
    # is pack-derived, but fail closed if that invariant is ever broken.
    if any(eid not in included for ids in supporting.values() for eid in ids):
        raise ValueError("desk decision selected evidence outside the included inventory")
    published = bool(candidates)
    return AnalystDecision(
        schema_version="1.0",
        evidence_decisions=tuple(evidence_decisions),
        candidate_hypotheses=tuple(candidates),
        research_decision=ResearchDecision.READY if published else ResearchDecision.ABSTAIN,
        recommended_status=AttributionStatus.SUFFICIENT if published else AttributionStatus.ABSTAIN,
        proposed_attribution_type=(AttributionType.EVIDENCE_BACKED_CAUSAL if published
                                   else AttributionType.NO_MATERIAL_PUBLIC_CATALYST),
    )


def read_reason_code(extraction: Extraction) -> str:
    """What one read article says about the move, for live progress (no text leaves the run)."""
    if not extraction.valid:
        return "read_failed"
    about = getattr(extraction.about_ticker, "value", extraction.about_ticker)
    if about not in ("ISSUER", "GROUP"):
        return "read_not_about_company"
    if extraction.explicit_attribution and extraction.attribution_quote:
        return "read_states_cause"
    if extraction.event_quote:
        return "read_reports_event"
    return "read_no_cause"


def extract_items(
    inventory: list[dict[str, Any]], *, facts: dict[str, Any], temporal_identity: Any, quote_policy: str,
    llm: Any, provider_budget: Any, provider_identity: str | None, model_identity: str | None,
    run_deadline_epoch_ms: int | None, cache: dict[str, Extraction] | None = None,
    on_item: Callable[[dict[str, Any], Extraction], None] | None = None,
) -> tuple[list[tuple[dict[str, Any], Extraction]], dict[str, int], list[Any], list[tuple[int | None, int | None]]]:
    """Run the M1 extractor once per item (budgeted, concurrent); ``cache`` maps message digests to results.

    ``on_item`` is told about each item as soon as its extraction finishes, so a caller can show progress;
    an error in it never affects the extraction.
    """
    from catalyst_agents.runtime.earnings_timing import current_earnings_timing_cache

    timing_cache = current_earnings_timing_cache()
    if timing_cache is not None:
        ticker = str(facts.get("ticker") or "")
        for item in inventory:
            timing = timing_cache.for_item(item, ticker=ticker)
            if timing is not None:
                item["_earnings_release_timing"] = timing

    pending: list[tuple[dict[str, Any], list[dict[str, str]], str]] = []
    for item in inventory:
        messages = build_extraction_messages(facts, item)
        digest = hashlib.sha256(json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        pending.append((item, messages, digest))
    cached = {digest: cache[digest] for _, _, digest in pending if cache is not None and digest in cache}

    register = getattr(provider_budget, "register_role_limit", None)
    limits = getattr(provider_budget, "_role_limits", {}) or {}
    if callable(register) and DESK_EXTRACTOR_ROLE not in limits:
        register(DESK_EXTRACTOR_ROLE, DESK_EXTRACTOR_LIMIT)
    calls = {"logical_calls": 0, "provider_attempts": 0}
    usages: list[tuple[int | None, int | None]] = []
    attempts: list[Any] = []
    call_lock = Lock()

    def extract(job: tuple[dict[str, Any], list[dict[str, str]], str]):
        item, messages, digest = job
        if digest in cached:
            return item, cached[digest], None
        if run_deadline_epoch_ms is not None and time.time() * 1000 >= run_deadline_epoch_ms:
            return item, Extraction.invalid("deadline_exceeded"), None
        options = {"max_tokens": 600, "response_format": {"type": "json_object"},
                   "extra_body": {"thinking": {"type": "disabled"}}}
        binder = getattr(llm, "bind", None)
        bound = binder(**options) if callable(binder) else None
        def invoke():
            return bound.invoke(messages) if bound is not None else llm.invoke(messages, **options)
        try:
            bounded = invoke_with_bounded_retry(
                invoke, role=DESK_EXTRACTOR_ROLE, semantic_input_hash=digest,
                budget=provider_budget, provider=provider_identity, model_id=model_identity,
                usage_extractor=extract_provider_usage,
            )
        except (ProviderBudgetError, RunDeadlineExceededError):
            raise  # a budget, price or deadline stop ends the run as it does for the analyst
        except Exception as exc:
            # Any other failed extraction only loses that item (the replay treated it the same way).
            return item, Extraction.invalid(f"extraction_failed:{type(exc).__name__}"), None
        raw = str(getattr(bounded.result, "content", bounded.result))
        observation = facts.get("observation")
        move_profile = SimpleNamespace(**observation) if isinstance(observation, dict) else observation
        validated = validate_quotes(parse_extraction(raw), item, temporal_identity, quote_policy)
        result = check_direction(attach_quote_roles(
            validated, item, temporal_identity, quote_policy,
            ticker=str(facts.get("ticker") or ""), move_profile=move_profile,
        ), facts.get("target_return"))
        with call_lock:
            calls["logical_calls"] += bounded.counts.logical_calls
            calls["provider_attempts"] += bounded.counts.provider_attempts
            attempts.extend(bounded.attempts)
            usages.extend(bounded.attempt_usages)
        return item, result, bounded

    completed: dict[str, tuple[dict[str, Any], Extraction]] = {}
    with ThreadPoolExecutor(max_workers=DESK_EXTRACTOR_CONCURRENCY) as pool:
        futures = [pool.submit(extract, job) for job in pending]
        for future in as_completed(futures):
            item, result, _ = future.result()
            completed[str(item.get("evidence_id", ""))] = (item, result)
            if on_item is not None:
                try:
                    on_item(item, result)
                except Exception:  # noqa: BLE001 - progress reporting must not change the run
                    pass
    if cache is not None:
        cache.update({digest: completed[str(item.get("evidence_id", ""))][1] for item, _, digest in pending
                      if str(item.get("evidence_id", "")) in completed})
    records = [completed[str(item.get("evidence_id", ""))] for item, _, _ in pending
               if str(item.get("evidence_id", "")) in completed]
    return records, calls, attempts, usages


def gate_facts(observation: Any, temporal_identity: Any, market_facts: Any) -> dict[str, Any]:
    obs = observation.model_dump(mode="json") if hasattr(observation, "model_dump") else observation
    obs = obs if isinstance(obs, dict) else vars(obs)
    facts = {"observation": obs, "session_close": getattr(temporal_identity, "session_close_at", None)}
    if isinstance(market_facts, dict):
        facts.update({k: v for k, v in market_facts.items() if k != "observation"})
    return facts


def gate_records(records: list[tuple[dict[str, Any], Extraction]], facts: dict[str, Any]) -> tuple[list[EventCluster], Any]:
    """Cluster records and apply the frozen desk gate over every cluster."""
    clusters = cluster_events(records)
    return clusters, select_and_gate(clusters, facts, GateConfig("desk"))


def desk_decide(
    context_pack: Any, *, observation: Any, temporal_identity: Any, quote_policy: str,
    llm: Any, provider_budget: Any, provider_identity: str | None,
    model_identity: str | None, run_deadline_epoch_ms: int | None,
    company_name: str | None, market_facts: Any,
) -> DeskDecisionResult:
    """Extract eligible evidence concurrently, then apply frozen ``desk`` gate."""
    included_ids = tuple(getattr(context_pack, "included_evidence_ids", ()))
    lead_ids = {str(_value(item, "evidence_id", "")) for item in getattr(context_pack, "lead_only_evidence", ())}
    allowed = set(included_ids) | lead_ids
    inventory = [_item_dict(item) for item in getattr(context_pack, "evidence_inventory", ())]
    inventory = [item for item in inventory if str(item.get("evidence_id", "")) in allowed
                 and str(getattr(item.get("content_state"), "value", item.get("content_state"))).upper() == "FULL_TEXT"]

    obs = observation.model_dump(mode="json") if hasattr(observation, "model_dump") else observation
    obs = obs if isinstance(obs, dict) else vars(obs)
    if company_name is None:
        try:
            from catalyst_data.ingestion.collectors.web_discovery_v2 import _company
            company_name = _company(str(obs.get("ticker") or ""))
        except Exception:
            company_name = None
    facts = {
        "ticker": obs.get("ticker"), "company_name": company_name,
        "session_date": temporal_identity.session_date,
        "target_return": obs.get("target_return"), "market_return": obs.get("market_return"),
        "sector_return": obs.get("sector_return"), "observation": obs,
    }
    records, calls, attempts, usages = extract_items(
        inventory, facts=facts, temporal_identity=temporal_identity, quote_policy=quote_policy, llm=llm,
        provider_budget=provider_budget, provider_identity=provider_identity, model_identity=model_identity,
        run_deadline_epoch_ms=run_deadline_epoch_ms,
    )
    facts_for_gate = gate_facts(observation, temporal_identity, market_facts)
    clusters, gate = gate_records(records, facts_for_gate)
    secondary = gate.secondary if gate.primary is not None else None
    decision = build_analyst_decision(gate, secondary, records, included_evidence_ids=included_ids)
    extraction_rows = tuple({
        "evidence_id": str(item.get("evidence_id", "")),
        "fields": {key: (getattr(extraction, key).value if hasattr(getattr(extraction, key), "value") else getattr(extraction, key))
                   for key in ("about_ticker", "session", "event_type", "event_summary", "event_timing",
                               "explicit_attribution", "attribution_quote", "event_quote", "mechanism_quote",
                               "is_preview_or_evergreen")},
        "valid": extraction.valid, "invalid_reason": extraction.invalid_reason,
        "quote_failures": list(extraction.quote_failures),
    } for item, extraction in records)
    cluster_rows = _cluster_trace_rows(clusters, gate.diagnostics)
    artifact = {
        "prompt_version": EXTRACTION_PROMPT_VERSION,
        "extractions": extraction_rows, "clusters": cluster_rows,
        "gate": {"publish": gate.publish, "basis": gate.basis, "reason_code": gate.reason_code,
                 "diagnostics": list(gate.diagnostics)},
        "calls": calls, "attempts": [a.model_dump(mode="json") if hasattr(a, "model_dump") else vars(a) for a in attempts],
        "usage": [list(usage) for usage in usages],
    }
    # Persistence is supplied by the production graph and recorded there, where
    # the run-scoped PackPersistence boundary is available.
    return DeskDecisionResult(decision, gate, extraction_rows, tuple(clusters), artifact,
                              calls["logical_calls"], calls["provider_attempts"], tuple(attempts), tuple(usages),
                              records=tuple(records), facts=facts, gate_facts=facts_for_gate)


__all__ = ["DeskDecisionResult", "build_analyst_decision", "desk_decide", "extract_items", "gate_facts", "gate_records",
           "read_reason_code"]
