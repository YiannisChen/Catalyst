"""Tests for retrieval_policy.py — tier boost + T5 cap (pure functions)."""

from __future__ import annotations

from catalyst_data.retrieval_policy import (
    boost_by_tier,
    cap_t5_opinion,
    apply_retrieval_policy,
    DEFAULT_TIER_BOOST,
)


def _make_result(article_id: str, score: float, tier: int) -> dict:
    return {"article_id": article_id, "rrf_score": score, "source_tier": tier}


class TestBoostByTier:
    def test_t1_boosted_above_t5(self):
        results = [
            _make_result("a1", 0.5, 5),  # T5 × 0.9 = 0.45
            _make_result("a2", 0.5, 1),  # T1 × 1.5 = 0.75
        ]
        boosted = boost_by_tier(results)
        assert boosted[0]["article_id"] == "a2"  # T1 first
        assert boosted[1]["article_id"] == "a1"

    def test_preserves_raw_score(self):
        results = [_make_result("a1", 0.42, 1)]
        boosted = boost_by_tier(results)
        assert boosted[0]["_raw_rrf_score"] == 0.42
        assert boosted[0]["rrf_score"] == 0.42 * DEFAULT_TIER_BOOST[1]

    def test_unknown_tier_uses_t4(self):
        results = [
            _make_result("a1", 0.5, 99),  # unknown → T4 × 1.0
        ]
        boosted = boost_by_tier(results)
        assert boosted[0]["rrf_score"] == 0.5

    def test_no_tier_field_uses_t4(self):
        results = [{"article_id": "a1", "rrf_score": 0.5}]
        boosted = boost_by_tier(results)
        assert boosted[0]["rrf_score"] == 0.5  # T4 × 1.0

    def test_input_not_mutated(self):
        results = [_make_result("a1", 0.5, 5)]
        original = dict(results[0])
        boost_by_tier(results)
        assert results[0] == original  # unmodified


class TestCapT5Opinion:
    def test_all_t5_capped_at_30pct(self):
        """10 results all T5 → only 3 survive (30% of 10 = 3)."""
        results = [_make_result(f"a{i}", 1.0 - i * 0.01, 5) for i in range(10)]
        capped = cap_t5_opinion(results, max_pct=0.30)
        assert len(capped) == 3

    def test_two_t5_out_of_ten_survive(self):
        """2 T5 out of 10 = 20% ≤ 30% → both survive."""
        results = (
            [_make_result(f"a{i}", 1.0 - i * 0.01, 4) for i in range(8)]
            + [_make_result("t51", 0.5, 5), _make_result("t52", 0.4, 5)]
        )
        capped = cap_t5_opinion(results, max_pct=0.30)
        t5_in_capped = [r for r in capped if r["source_tier"] == 5]
        assert len(t5_in_capped) == 2

    def test_four_t5_out_of_ten_capped_to_three(self):
        """4 T5 out of 10 → trim to 3 (30%)."""
        results = (
            [_make_result(f"a{i}", 1.0 - i * 0.01, 4) for i in range(6)]
            + [_make_result(f"t5{i}", 0.5 - i * 0.01, 5) for i in range(4)]
        )
        capped = cap_t5_opinion(results, max_pct=0.30)
        t5_in_capped = [r for r in capped if r["source_tier"] == 5]
        assert len(t5_in_capped) == 3

    def test_less_than_three_results_skip_cap(self):
        """< 3 results → cap skipped entirely."""
        results = [_make_result("a1", 1.0, 5), _make_result("a2", 0.9, 5)]
        capped = cap_t5_opinion(results, max_pct=0.30)
        assert len(capped) == 2

    def test_no_t5_results_no_change(self):
        results = [_make_result(f"a{i}", 1.0 - i * 0.1, 4) for i in range(5)]
        capped = cap_t5_opinion(results, max_pct=0.30)
        assert len(capped) == 5

    def test_trims_lowest_scored_t5(self):
        """Higher-scored T5 survive; lowest-scored T5 trimmed."""
        results = (
            [_make_result(f"a{i}", 2.0 - i * 0.01, 4) for i in range(6)]
            + [
                _make_result("t5_high", 0.9, 5),
                _make_result("t5_mid", 0.5, 5),
                _make_result("t5_low1", 0.3, 5),
                _make_result("t5_low2", 0.1, 5),
            ]
        )
        capped = cap_t5_opinion(results, max_pct=0.30)
        t5_ids = {r["article_id"] for r in capped if r["source_tier"] == 5}
        assert "t5_high" in t5_ids  # highest-scored T5 survives
        assert "t5_low2" not in t5_ids  # lowest-scored T5 trimmed


class TestApplyRetrievalPolicy:
    def test_composes_boost_cap_slice(self):
        results = [_make_result(f"a{i}", 1.0 - i * 0.01, 5) for i in range(20)]
        final = apply_retrieval_policy(results, top_k=5)
        assert len(final) <= 5

    def test_pure_function_no_side_effects(self):
        results = [_make_result("a1", 0.5, 1)]
        original = dict(results[0])
        apply_retrieval_policy(results)
        assert results[0] == original
