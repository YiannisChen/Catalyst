"""Tests for the deterministic DecisionRouter node."""
from __future__ import annotations

from catalyst_agents.state import CriticDecision, Phase
from catalyst_agents.nodes.decision_router import (
    MAX_EXPANSIONS,
    decision_router,
    route_after_decision_router,
)


def _base_state() -> dict:
    return {
        "critic_decision": CriticDecision(
            sufficiency="partial",
            next_action="proceed",
            magnitude_coverage=0.65,
            reasoning="Need the normal Judge path.",
        ),
        "error_type": None,
        "expansions_used": 0,
        "max_expansions": MAX_EXPANSIONS,
    }


def test_decision_router_system_error_routes_to_system_error():
    state = {**_base_state(), "error_type": "system_error"}

    result = decision_router(state)

    assert result["router_edge"] == "system_error"
    assert result["router_reason"] == "upstream_system_error"
    assert result["phase"] == Phase.ROUTER


def test_decision_router_proceed_routes_to_judge():
    state = _base_state()

    result = decision_router(state)

    assert result["router_edge"] == "judge"
    assert result["router_reason"] == "critic_proceed"


def test_decision_router_expand_macro_with_budget_routes_expand_macro():
    state = {
        **_base_state(),
        "critic_decision": CriticDecision(
            sufficiency="partial",
            next_action="expand_macro",
            magnitude_coverage=0.45,
            reasoning="Need macro expansion.",
        ),
        "expansions_used": 1,
    }

    result = decision_router(state)

    assert result["router_edge"] == "expand_macro"
    assert result["router_reason"] == "critic_expand_macro"


def test_decision_router_expand_macro_exhausted_routes_to_insufficient():
    state = {
        **_base_state(),
        "critic_decision": CriticDecision(
            sufficiency="partial",
            next_action="expand_macro",
            magnitude_coverage=0.45,
            reasoning="Need macro expansion.",
        ),
        "expansions_used": MAX_EXPANSIONS,
    }

    result = decision_router(state)

    assert result["router_edge"] == "insufficient"
    assert result["router_reason"] == "expansions_exhausted"


def test_decision_router_expand_related_short_circuits_to_insufficient():
    state = {
        **_base_state(),
        "critic_decision": CriticDecision(
            sufficiency="partial",
            next_action="expand_related",
            magnitude_coverage=0.35,
            reasoning="Need related-entity search.",
        ),
    }

    result = decision_router(state)

    assert result["router_edge"] == "insufficient"
    assert result["router_reason"] == "layer3_not_implemented"


def test_decision_router_refuse_routes_to_insufficient():
    state = {
        **_base_state(),
        "critic_decision": CriticDecision(
            sufficiency="insufficient",
            next_action="refuse",
            magnitude_coverage=0.1,
            reasoning="Not enough evidence.",
        ),
    }

    result = decision_router(state)

    assert result["router_edge"] == "insufficient"
    assert result["router_reason"] == "critic_refused"


def test_decision_router_missing_decision_falls_back_to_insufficient():
    state = {**_base_state(), "critic_decision": None}

    result = decision_router(state)

    assert result["router_edge"] == "insufficient"
    assert result["router_reason"] == "missing_critic_decision"


def test_route_after_decision_router_uses_router_edge():
    assert route_after_decision_router({"router_edge": "judge"}) == "judge"
    assert route_after_decision_router({}) == "insufficient"


def test_decision_router_ticker_mismatch_short_circuits_to_insufficient():
    state = {**_base_state(), "ticker_consistent": False}
    result = decision_router(state)
    assert result["router_edge"] == "insufficient"
    assert result["router_reason"] == "ticker_mismatch_guard"


def test_decision_router_invalid_market_session_short_circuits_to_insufficient():
    state = {**_base_state(), "market_session_valid": False}
    result = decision_router(state)
    assert result["router_edge"] == "insufficient"
    assert result["router_reason"] == "market_session_guard"


def test_decision_router_does_not_block_when_ticker_consistent_none():
    state = {**_base_state(), "ticker_consistent": None}
    result = decision_router(state)
    assert result["router_edge"] == "judge"


def test_decision_router_magnitude_guard_routes_insufficient():
    state = {**_base_state(), "magnitude_plausible": False}
    result = decision_router(state)
    assert result["router_edge"] == "insufficient"
    assert result["router_reason"] == "magnitude_guard"
