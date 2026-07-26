from __future__ import annotations

from attribution_fixtures import make_hypothesis


def test_lexicographic_order():
    from catalyst_agents.attribution.ranking import rank_hypotheses

    h1 = make_hypothesis(cause="earnings_guidance", gate_passed=True, direct_support=True, dedup_clusters=2, max_relevance=0.9, degradation_flags=0, max_counter_relevance=0.1, is_novel=True)
    h2 = make_hypothesis(cause="market", gate_passed=True, direct_support=True, dedup_clusters=1, max_relevance=1.0, degradation_flags=0, max_counter_relevance=0.0, is_novel=True)
    h3 = make_hypothesis(cause="product_demand", gate_passed=True, direct_support=True, dedup_clusters=2, max_relevance=0.8, degradation_flags=0, max_counter_relevance=0.1, is_novel=True)

    ranked = rank_hypotheses([h1, h2, h3])
    assert [item.cause_label for item in ranked] == ["earnings_guidance", "product_demand", "market"]


def test_gate_failed_ranks_last():
    from catalyst_agents.attribution.ranking import rank_hypotheses

    h1 = make_hypothesis(cause="market", gate_passed=False, direct_support=True, dedup_clusters=2, max_relevance=1.0)
    h2 = make_hypothesis(cause="unexplained", gate_passed=True, direct_support=False, dedup_clusters=0, max_relevance=0.0)

    assert [item.cause_label for item in rank_hypotheses([h1, h2])] == ["unexplained", "market"]


def test_no_confidence_score_in_output():
    from catalyst_agents.attribution.ranking import rank_hypotheses

    ranked = rank_hypotheses([make_hypothesis(cause="market", gate_passed=True)])
    for item in ranked:
        assert not hasattr(item, "confidence_score")
        assert not hasattr(item, "probability")


def test_stable_tie_breaker():
    from catalyst_agents.attribution.ranking import rank_hypotheses

    h1 = make_hypothesis(cause="earnings_guidance", gate_passed=True, direct_support=True, dedup_clusters=1, max_relevance=0.8, degradation_flags=0, max_counter_relevance=0.1, is_novel=True)
    h2 = make_hypothesis(cause="market", gate_passed=True, direct_support=True, dedup_clusters=1, max_relevance=0.8, degradation_flags=0, max_counter_relevance=0.1, is_novel=True)

    assert [item.cause_label for item in rank_hypotheses([h2, h1])] == ["market", "earnings_guidance"]
    assert [item.cause_label for item in rank_hypotheses([h1, h2])] == ["market", "earnings_guidance"]


def test_each_ranking_criterion_independent_complete_order():
    from catalyst_agents.attribution.ranking import rank_hypotheses

    items = [
        make_hypothesis(cause="unexplained", gate_passed=False, direct_support=True, dedup_clusters=2, max_relevance=1.0, degradation_flags=0, max_counter_relevance=0.0, is_novel=True),
        make_hypothesis(cause="market", gate_passed=True, direct_support=False, dedup_clusters=2, max_relevance=1.0, degradation_flags=0, max_counter_relevance=0.0, is_novel=True),
        make_hypothesis(cause="sector", gate_passed=True, direct_support=True, dedup_clusters=1, max_relevance=1.0, degradation_flags=0, max_counter_relevance=0.0, is_novel=True),
        make_hypothesis(cause="macro", gate_passed=True, direct_support=True, dedup_clusters=2, max_relevance=0.7, degradation_flags=0, max_counter_relevance=0.0, is_novel=True),
        make_hypothesis(cause="legal_regulatory", gate_passed=True, direct_support=True, dedup_clusters=2, max_relevance=0.8, degradation_flags=1, max_counter_relevance=0.0, is_novel=True),
        make_hypothesis(cause="product_demand", gate_passed=True, direct_support=True, dedup_clusters=2, max_relevance=0.8, degradation_flags=0, max_counter_relevance=0.2, is_novel=True),
        make_hypothesis(cause="earnings_guidance", gate_passed=True, direct_support=True, dedup_clusters=2, max_relevance=0.8, degradation_flags=0, max_counter_relevance=0.1, is_novel=False),
        make_hypothesis(cause="market", gate_passed=True, direct_support=True, dedup_clusters=2, max_relevance=0.8, degradation_flags=0, max_counter_relevance=0.1, is_novel=True),
    ]
    assert [item.cause_label for item in rank_hypotheses(items)] == [
        "market",
        "earnings_guidance",
        "product_demand",
        "legal_regulatory",
        "macro",
        "sector",
        "market",
        "unexplained",
    ]


def test_exact_rank_ties_do_not_depend_on_input_order():
    from catalyst_agents.attribution.ranking import rank_hypotheses

    first = make_hypothesis(cause="market", transmission_mechanism="Alpha mechanism")
    second = make_hypothesis(cause="market", transmission_mechanism="Beta mechanism")

    forward = [item.transmission_mechanism for item in rank_hypotheses([first, second])]
    reverse = [item.transmission_mechanism for item in rank_hypotheses([second, first])]

    assert forward == reverse == ["Alpha mechanism", "Beta mechanism"]
