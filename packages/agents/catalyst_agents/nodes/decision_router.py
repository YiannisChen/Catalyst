"""DecisionRouter node — deterministic routing from Critic output."""
from __future__ import annotations

from catalyst_agents.state import AttributionState, Phase

MAX_EXPANSIONS = 2


def decision_router(state: AttributionState) -> dict:
    """Map the current state to one deterministic downstream edge."""
    if state.get("error_type") == "system_error":
        return {
            "router_edge": "system_error",
            "router_reason": "upstream_system_error",
            "phase": Phase.ROUTER,
        }
    if state.get("ticker_consistent") is False:
        return {
            "router_edge": "insufficient",
            "router_reason": "ticker_mismatch_guard",
            "phase": Phase.ROUTER,
        }
    if state.get("market_session_valid") is False:
        return {
            "router_edge": "insufficient",
            "router_reason": "market_session_guard",
            "phase": Phase.ROUTER,
        }
    if state.get("magnitude_plausible") is False:
        return {
            "router_edge": "insufficient",
            "router_reason": "magnitude_guard",
            "phase": Phase.ROUTER,
        }

    decision = state.get("critic_decision")
    if decision is None:
        return {
            "router_edge": "insufficient",
            "router_reason": "missing_critic_decision",
            "phase": Phase.ROUTER,
        }

    if decision.next_action == "proceed":
        if not state.get("graded_evidence", []):
            return {
                "router_edge": "insufficient",
                "router_reason": "empty_graded_evidence_guard",
                "phase": Phase.ROUTER,
            }
        return {
            "router_edge": "judge",
            "router_reason": "critic_proceed",
            "phase": Phase.ROUTER,
        }

    if decision.next_action == "expand_macro":
        expansions_used = int(state.get("expansions_used", 0) or 0)
        max_expansions = int(state.get("max_expansions", MAX_EXPANSIONS) or MAX_EXPANSIONS)
        if expansions_used < max_expansions:
            return {
                "router_edge": "expand_macro",
                "router_reason": "critic_expand_macro",
                "phase": Phase.ROUTER,
            }
        return {
            "router_edge": "insufficient",
            "router_reason": "expansions_exhausted",
            "phase": Phase.ROUTER,
        }

    if decision.next_action == "expand_related":
        return {
            "router_edge": "insufficient",
            "router_reason": "layer3_not_implemented",
            "phase": Phase.ROUTER,
        }

    return {
        "router_edge": "insufficient",
        "router_reason": "critic_refused",
        "phase": Phase.ROUTER,
    }


def route_after_decision_router(state: dict) -> str:
    """Return the next graph edge chosen by DecisionRouter."""
    return state.get("router_edge", "insufficient")
