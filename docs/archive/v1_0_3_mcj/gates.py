from __future__ import annotations

import math
from typing import Any

from catalyst_agents.attribution.relationships import effective_edges


EVIDENCE_CAUSES = {"earnings_guidance", "product_demand", "legal_regulatory", "macro"}
CRITIC_CATEGORY_COMPATIBILITY = {
    "earnings_guidance": {"earnings"},
    "product_demand": {"other"},
    "legal_regulatory": {"regulatory"},
    "macro": {"macro", "geopolitical"},
    "sector": {"sector"},
}


def _is_valid_support(item: dict[str, Any], *, cutoff: str | None, cause: str | None = None) -> bool:
    if cutoff is None or not item.get("available_at"):
        return False
    try:
        relevance = float(item["relevance"])
    except (KeyError, TypeError, ValueError):
        return False
    if not math.isfinite(relevance) or relevance <= 0.5 or item.get("temporal_match") is not True:
        return False
    if str(item["available_at"]) > cutoff:
        return False
    compatible = CRITIC_CATEGORY_COMPATIBILITY.get(cause or "")
    return compatible is None or item.get("critic_category") in compatible


def _has_valid_support(evidence: list[dict[str, Any]], *, cutoff: str | None, cause: str | None = None) -> bool:
    return any(_is_valid_support(item, cutoff=cutoff, cause=cause) for item in evidence)


def _finite_peer_return(context: dict[str, Any], counterparty: str) -> bool:
    peer_returns = context.get("peer_returns_by_ticker") or {}
    if counterparty in peer_returns:
        value = peer_returns[counterparty]
        try:
            return value is not None and math.isfinite(float(value))
        except (TypeError, ValueError):
            return False
    return bool(context.get("peer_context_available"))


def _relationship_matches(
    edges: Any,
    *,
    ticker: str,
    session_date: str | None,
    counterparty: str,
    relationship_types: set[str],
) -> bool:
    if isinstance(edges, dict) and "edges" in edges:
        if not session_date:
            return False
        return any(
            edge["to_ticker"] == counterparty and edge["relationship_type"] in relationship_types
            for edge in effective_edges(edges, ticker=ticker, session_date=session_date)
        )
    if isinstance(edges, dict):
        relationships = edges.get(ticker, {})
        return any(counterparty in relationships.get(kind, []) for kind in relationship_types)
    return False


def _propagation_gate(
    *, cause: str, context: dict[str, Any], evidence: list[dict[str, Any]], edges: Any
) -> tuple[bool, str]:
    ticker = context.get("ticker")
    if not ticker:
        return False, "target ticker required"
    cutoff = context.get("cutoff")
    candidates = set()
    explicit = context.get("counterparty") or context.get("peer_ticker")
    if explicit:
        candidates.add(str(explicit))
    for item in evidence:
        candidates.update(str(value) for value in item.get("ticker_associations", ()) if value != ticker)
    relationship_types = {"peer"} if cause == "peer_propagation" else {"supplier", "customer"}
    for counterparty in sorted(candidates):
        if not _finite_peer_return(context, counterparty):
            continue
        if not _relationship_matches(
            edges,
            ticker=ticker,
            session_date=context.get("session_date"),
            counterparty=counterparty,
            relationship_types=relationship_types,
        ):
            continue
        if any(
            counterparty in tuple(item.get("ticker_associations", ()))
            and _is_valid_support(item, cutoff=cutoff)
            for item in evidence
        ):
            return True, "relationship, same-session context, and evidence prerequisites pass"
    return False, "effective relationship, same-session context, and pre-cutoff evidence required"


def check_prerequisite(*, cause: str, context: dict[str, Any], evidence: list[dict[str, Any]], edges: Any) -> tuple[bool, str]:
    cutoff = context.get("cutoff")
    if cause == "market":
        available = context.get("benchmark_ohlcv_exists") or context.get("benchmark_return_pct") is not None
        return (True, "benchmark OHLCV available") if available else (False, "benchmark OHLCV required")
    if cause == "sector":
        available = context.get("sector_ohlcv_exists") or context.get("sector_return_pct") is not None
        if not available:
            return False, "sector ETF OHLCV required"
        if evidence and not _has_valid_support(evidence, cutoff=cutoff, cause=cause):
            return False, "compatible pre-cutoff sector evidence required for narrative claim"
        return True, "sector OHLCV available"
    if cause in EVIDENCE_CAUSES:
        if _has_valid_support(evidence, cutoff=cutoff, cause=cause):
            return True, "compatible supporting evidence available"
        return False, "compatible pre-cutoff supporting evidence required"
    if cause in {"peer_propagation", "supply_chain_propagation"}:
        return _propagation_gate(cause=cause, context=context, evidence=evidence, edges=edges)
    if cause == "mixed":
        passed = context.get("gate_passed_causes", ())
        distinct = {item for item in passed if item not in {"mixed", "unexplained"}}
        return len(distinct) >= 2, "mixed requires two distinct passed causes"
    if cause == "unexplained":
        passed = bool(context.get("context_quality_ok", True)) and not context.get("any_other_gate_passed", False)
        return passed, "unexplained requires adequate context and no other gate"
    return False, f"unknown cause: {cause}"


def all_gates_failed(results: list[tuple[str, bool, str]]) -> bool:
    return bool(results) and all(not passed for _, passed, _ in results)
