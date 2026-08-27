from __future__ import annotations

import json

from catalyst_agents.attribution.hypothesis import Hypothesis


CAUSE_ORDER = {
    "market": 0,
    "sector": 1,
    "earnings_guidance": 2,
    "product_demand": 3,
    "legal_regulatory": 4,
    "macro": 5,
    "peer_propagation": 6,
    "supply_chain_propagation": 7,
    "mixed": 8,
    "unexplained": 9,
}


def ranking_key(h: Hypothesis) -> tuple[int, int, int, float, int, float, int, int, str]:
    return (
        0 if h.prerequisite_gate_passed else 1,
        0 if h.direct_support_exists else 1,
        -min(h.independent_supporting_cluster_count, 2),
        -h.max_supporting_critic_relevance,
        h.source_support_degradation_count,
        h.max_counter_evidence_relevance,
        0 if h.is_novel else 1,
        CAUSE_ORDER[h.cause_label],
        json.dumps(h.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    )


def rank_hypotheses(hypotheses: list[Hypothesis] | tuple[Hypothesis, ...]) -> list[Hypothesis]:
    return sorted(list(hypotheses), key=ranking_key)
