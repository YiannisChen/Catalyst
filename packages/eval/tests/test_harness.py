"""
TDD tests for harness runner and experiment comparator.
Updated S1: uses CauseMatch instead of attribution_f1.
"""
from __future__ import annotations

import pytest

from catalyst_eval.harness.runner import evaluate, EvalReport
from catalyst_eval.harness.experiment import compare, ComparisonReport
from catalyst_eval.metrics.cause_match import CauseMatch
from catalyst_eval.metrics.category_accuracy import CategoryAccuracy
from catalyst_eval.schema.golden_event import GoldenEvent, Cause, CauseCategory
from catalyst_eval.schema.result import AttributionResult, PredictedCause, RetrievedEvidence


# ---------------------------------------------------------------------------
# Fixture judge for CauseMatch (offline, no live LLM)
# ---------------------------------------------------------------------------

def _fixture_judge(prompt: str) -> dict:
    return {
        "pairings": [
            {"pred_idx": 0, "golden_idx": 0, "verdict": "same_event_same_direction"},
        ]
    }


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def mock_good_agent(ticker: str, date: str) -> AttributionResult:
    return AttributionResult(
        ticker=ticker,
        trade_date=date,
        causes=[
            PredictedCause(
                text="China chip ban",
                category="geopolitical",
                confidence=0.7,
                evidence_ids=["c1"],
                direction="negative",
            )
        ],
        summary="Test",
        retrieved_evidence=[RetrievedEvidence(asset_id="c1", content_md="chunk")],
        cost_breakdown=[
            {
                "node": "judge",
                "input_tokens": 3000,
                "output_tokens": 500,
                "model_id": "test",
                "cost_usd": 0.02,
            }
        ],
        total_cost_usd=0.02,
        total_tokens=3500,
    )


def mock_bad_agent(ticker: str, date: str) -> AttributionResult:
    return AttributionResult(
        ticker=ticker,
        trade_date=date,
        causes=[
            PredictedCause(
                text="Random guess",
                category="technical",
                confidence=0.9,
                evidence_ids=[],
                direction="negative",
            )
        ],
        summary="Test",
        retrieved_evidence=[],
        cost_breakdown=[],
        total_cost_usd=0.01,
        total_tokens=2000,
    )


GOLDEN_SET = [
    GoldenEvent(
        id="t1",
        ticker="AAPL",
        trade_date="2026-01-15",
        price_move_pct=-4.2,
        should_refuse=False,
        causes=[
            Cause(
                text="China export ban",
                category=CauseCategory.GEOPOLITICAL,
                evidence_ids=[],
            )
        ],
    )
]


# ---------------------------------------------------------------------------
# EvalReport structure
# ---------------------------------------------------------------------------

class TestEvaluate:
    def test_evaluate_returns_scores(self):
        report = evaluate(
            predict_fn=mock_good_agent,
            golden_set=GOLDEN_SET,
            metrics=[CauseMatch(judge_fn=_fixture_judge), CategoryAccuracy()],
        )
        assert isinstance(report, EvalReport)
        assert "cause_match_f1" in report.scores
        assert "category_accuracy" in report.scores

    def test_evaluate_tracks_cost(self):
        report = evaluate(
            predict_fn=mock_good_agent,
            golden_set=GOLDEN_SET,
            metrics=[CauseMatch(judge_fn=_fixture_judge)],
        )
        assert hasattr(report, "avg_cost_usd")
        assert hasattr(report, "avg_tokens")
        assert report.avg_cost_usd == pytest.approx(0.02)
        assert report.avg_tokens == pytest.approx(3500.0)

    def test_evaluate_per_event_breakdown(self):
        report = evaluate(
            predict_fn=mock_good_agent,
            golden_set=GOLDEN_SET,
            metrics=[CauseMatch(judge_fn=_fixture_judge)],
        )
        assert isinstance(report.per_event, list)
        assert len(report.per_event) == len(GOLDEN_SET)
        first = report.per_event[0]
        assert "event_id" in first
        assert "cause_match_f1" in first

    def test_evaluate_empty_golden_set(self):
        report = evaluate(
            predict_fn=mock_good_agent,
            golden_set=[],
            metrics=[CauseMatch(judge_fn=_fixture_judge), CategoryAccuracy()],
        )
        assert isinstance(report, EvalReport)
        assert report.scores["cause_match_f1"] == pytest.approx(0.0)
        assert report.scores["category_accuracy"] == pytest.approx(0.0)
        assert report.avg_cost_usd == pytest.approx(0.0)
        assert report.avg_tokens == pytest.approx(0.0)

    def test_evaluate_scores_in_bounds(self):
        report = evaluate(
            predict_fn=mock_good_agent,
            golden_set=GOLDEN_SET,
            metrics=[CauseMatch(judge_fn=_fixture_judge), CategoryAccuracy()],
        )
        for metric_name, score in report.scores.items():
            assert 0.0 <= score <= 1.0, f"{metric_name} score out of bounds: {score}"


# ---------------------------------------------------------------------------
# ComparisonReport and compare()
# ---------------------------------------------------------------------------

class TestCompare:
    def test_compare_returns_comparison(self):
        comparison = compare(
            configs={"good": mock_good_agent, "bad": mock_bad_agent},
            golden_set=GOLDEN_SET,
            metrics=[CauseMatch(judge_fn=_fixture_judge)],
        )
        assert isinstance(comparison, ComparisonReport)
        assert "good" in comparison.results
        assert "bad" in comparison.results

    def test_compare_costs_tracked(self):
        comparison = compare(
            configs={"good": mock_good_agent, "bad": mock_bad_agent},
            golden_set=GOLDEN_SET,
            metrics=[CauseMatch(judge_fn=_fixture_judge)],
        )
        assert "good" in comparison.costs
        assert "bad" in comparison.costs
        assert "avg_cost_usd" in comparison.costs["good"]
        assert "avg_tokens" in comparison.costs["good"]

    def test_compare_to_markdown(self):
        comparison = compare(
            configs={"good": mock_good_agent, "bad": mock_bad_agent},
            golden_set=GOLDEN_SET,
            metrics=[CauseMatch(judge_fn=_fixture_judge)],
        )
        md = comparison.to_markdown()
        assert isinstance(md, str)
        assert "|" in md
        assert "Config" in md
        assert "cause_match" in md
        assert "good" in md
        assert "bad" in md
        assert "Avg Cost" in md
        assert "Avg Tokens" in md

    def test_compare_to_markdown_table_structure(self):
        comparison = compare(
            configs={"good": mock_good_agent, "bad": mock_bad_agent},
            golden_set=GOLDEN_SET,
            metrics=[CauseMatch(judge_fn=_fixture_judge)],
        )
        md = comparison.to_markdown()
        lines = [ln.strip() for ln in md.splitlines() if ln.strip()]
        assert len(lines) >= 3
        separator_lines = [ln for ln in lines if set(ln.replace("|", "").replace("-", "").replace(" ", "")) == set()]
        assert len(separator_lines) >= 1, "No separator row found in markdown table"
