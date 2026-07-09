"""
TDD tests for evaluation metrics.
Updated S1: theater metrics (attribution_f1, grounding_rate, temporal_precision) deleted.
Replaced with CauseMatch, CitationFaithfulness, RefusalCorrectness, DirectionAccuracy.
"""
from __future__ import annotations

import pytest

from catalyst_eval.schema.golden_event import Cause, CauseCategory, GoldenEvent
from catalyst_eval.schema.result import AttributionResult, PredictedCause, RetrievedEvidence
from catalyst_eval.metrics.category_accuracy import CategoryAccuracy
from catalyst_eval.metrics.confidence_calibration import ConfidenceCalibration
from catalyst_eval.metrics.cause_match import CauseMatch
from catalyst_eval.metrics.citation_faithfulness import CitationFaithfulness
from catalyst_eval.metrics.refusal_correctness import RefusalCorrectness


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

GOLDEN = GoldenEvent(
    id="t1",
    ticker="AAPL",
    trade_date="2026-01-15",
    price_move_pct=-4.2,
    should_refuse=False,
    causes=[
        Cause(
            text="China export ban on H20 chips",
            category=CauseCategory.GEOPOLITICAL,
            evidence_ids=[],
        ),
        Cause(
            text="Sector selloff",
            category=CauseCategory.SECTOR,
            evidence_ids=[],
        ),
    ],
)

PREDICTED_GOOD = AttributionResult(
    ticker="AAPL",
    trade_date="2026-01-15",
    causes=[
        PredictedCause(
            text="China export ban on chips expanded",
            category="geopolitical",
            confidence=0.7,
            evidence_ids=["c1"],
            direction="negative",
        ),
        PredictedCause(
            text="Sector selloff in semiconductors",
            category="sector",
            confidence=0.2,
            evidence_ids=["c2"],
            direction="negative",
        ),
    ],
    summary="...",
    retrieved_evidence=[
        RetrievedEvidence(asset_id="c1", content_md="China chip ban news"),
        RetrievedEvidence(asset_id="c2", content_md="Semiconductor selloff report"),
    ],
    cost_breakdown=[],
    total_cost_usd=0.03,
    total_tokens=7500,
)

PREDICTED_BAD = AttributionResult(
    ticker="AAPL",
    trade_date="2026-01-15",
    causes=[
        PredictedCause(
            text="Earnings disappointment",
            category="earnings",
            confidence=0.9,
            evidence_ids=["c1"],
            direction="negative",
        ),
    ],
    summary="...",
    retrieved_evidence=[
        RetrievedEvidence(asset_id="c1", content_md="Some chunk content"),
    ],
    cost_breakdown=[],
    total_cost_usd=0.02,
    total_tokens=5000,
)

EMPTY_PREDICTED = AttributionResult(
    ticker="AAPL", trade_date="2026-01-15",
    causes=[], summary="",
)


# ---------------------------------------------------------------------------
# Fixture judge functions (offline — no live LLM in tests)
# ---------------------------------------------------------------------------

def _fixture_unrelated(prompt: str) -> dict:
    return {"pairings": []}


def _fixture_perfect_match(prompt: str) -> dict:
    return {
        "pairings": [
            {"pred_idx": 0, "golden_idx": 0, "verdict": "same_event_same_direction"},
            {"pred_idx": 1, "golden_idx": 1, "verdict": "same_event_same_direction"},
        ]
    }


def _fixture_faithfulness_all_supported(prompt: str) -> dict:
    return {
        "per_cause_verdicts": [
            {"verdict": "supported", "reason": "Evidence directly supports"},
            {"verdict": "supported", "reason": "Evidence directly supports"},
        ]
    }


# ---------------------------------------------------------------------------
# CategoryAccuracy
# ---------------------------------------------------------------------------

class TestCategoryAccuracy:
    def setup_method(self):
        self.metric = CategoryAccuracy()

    def test_name(self):
        assert self.metric.name == "category_accuracy"

    def test_correct_categories(self):
        score = self.metric.compute(PREDICTED_GOOD, GOLDEN)
        assert score >= 0.5, f"Expected >= 0.5, got {score}"

    def test_wrong_category(self):
        score = self.metric.compute(PREDICTED_BAD, GOLDEN)
        assert score == 0.0, f"Expected 0.0, got {score}"

    def test_case_insensitive(self):
        mixed_case_pred = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[
                PredictedCause(
                    text="geopolitical event",
                    category="GEOPOLITICAL",
                    confidence=0.8,
                    evidence_ids=[],
                    direction="negative",
                ),
            ],
            summary="",
        )
        score = self.metric.compute(mixed_case_pred, GOLDEN)
        assert score == 1.0

    def test_empty_predicted_causes(self):
        score = self.metric.compute(EMPTY_PREDICTED, GOLDEN)
        assert score == 0.0

    def test_score_bounds(self):
        score = self.metric.compute(PREDICTED_GOOD, GOLDEN)
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# ConfidenceCalibration
# ---------------------------------------------------------------------------

class TestConfidenceCalibration:
    def setup_method(self):
        self.metric = ConfidenceCalibration()

    def test_name(self):
        assert self.metric.name == "confidence_calibration"

    def test_returns_float_in_bounds(self):
        score = self.metric.compute(PREDICTED_GOOD, GOLDEN)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_empty_causes_returns_zero(self):
        score = self.metric.compute(EMPTY_PREDICTED, GOLDEN)
        assert score == 0.0

    def test_well_calibrated_is_higher_than_poorly_calibrated(self):
        well_calibrated = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[
                PredictedCause(text="geopolitical event", category="geopolitical",
                               confidence=0.6, evidence_ids=[], direction="negative"),
                PredictedCause(text="sector move", category="sector",
                               confidence=0.7, evidence_ids=[], direction="negative"),
            ],
            summary="",
        )
        poorly_calibrated = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[
                PredictedCause(text="earnings miss", category="earnings",
                               confidence=0.95, evidence_ids=[], direction="negative"),
                PredictedCause(text="macro headwinds", category="macro",
                               confidence=0.9, evidence_ids=[], direction="negative"),
            ],
            summary="",
        )
        good_score = self.metric.compute(well_calibrated, GOLDEN)
        bad_score = self.metric.compute(poorly_calibrated, GOLDEN)
        assert good_score > bad_score, (
            f"Well-calibrated ({good_score}) should outscore poorly calibrated ({bad_score})"
        )


# ---------------------------------------------------------------------------
# CauseMatch (replaces AttributionF1)
# ---------------------------------------------------------------------------

class TestCauseMatch:
    def setup_method(self):
        self.metric = CauseMatch(judge_fn=_fixture_unrelated)

    def test_name(self):
        assert self.metric.name == "cause_match"

    def test_empty_predicted_on_answerable_case(self):
        """Empty predicted on answerable case → recall=0, F1=0 (NOT skipped)."""
        result = self.metric.compute(EMPTY_PREDICTED, GOLDEN)
        assert result["recall"] == 0.0
        assert result["f1"] == 0.0
        assert not result["skipped"]

    def test_perfect_match_with_fixture(self):
        metric = CauseMatch(judge_fn=_fixture_perfect_match)
        result = metric.compute(PREDICTED_GOOD, GOLDEN)
        assert result["precision"] == 1.0
        assert result["recall"] == 1.0
        assert result["f1"] == 1.0

    def test_refusal_skipped(self):
        refusal_golden = GoldenEvent(
            id="t2", ticker="AAPL", trade_date="2026-01-15", price_move_pct=0.0,
            should_refuse=True, causes=[],
        )
        result = self.metric.compute(PREDICTED_GOOD, refusal_golden)
        assert result["skipped"] is True


# ---------------------------------------------------------------------------
# CitationFaithfulness
# ---------------------------------------------------------------------------

class TestCitationFaithfulness:
    def setup_method(self):
        self.metric = CitationFaithfulness(judge_fn=_fixture_faithfulness_all_supported)

    def test_name(self):
        assert self.metric.name == "citation_faithfulness"

    def test_all_supported_scores_one(self):
        result = self.metric.compute(PREDICTED_GOOD, GOLDEN)
        assert result["score"] == 1.0

    def test_refusal_skipped(self):
        refusal_golden = GoldenEvent(
            id="t2", ticker="AAPL", trade_date="2026-01-15", price_move_pct=0.0,
            should_refuse=True, causes=[],
        )
        result = self.metric.compute(PREDICTED_GOOD, refusal_golden)
        assert result["skipped"] is True

    def test_empty_causes_skipped(self):
        result = self.metric.compute(EMPTY_PREDICTED, GOLDEN)
        assert result["skipped"] is True


# ---------------------------------------------------------------------------
# RefusalCorrectness
# ---------------------------------------------------------------------------

class TestRefusalCorrectness:
    def setup_method(self):
        self.metric = RefusalCorrectness()

    def test_name(self):
        assert self.metric.name == "refusal_correctness"

    def test_correct_refusal_scores_one(self):
        refusal_golden = GoldenEvent(
            id="t2", ticker="AAPL", trade_date="2026-01-15", price_move_pct=0.0,
            should_refuse=True, causes=[],
        )
        result = self.metric.compute(EMPTY_PREDICTED, refusal_golden)
        assert result["score"] == 1.0

    def test_false_negative_refusal_scores_zero(self):
        refusal_golden = GoldenEvent(
            id="t2", ticker="AAPL", trade_date="2026-01-15", price_move_pct=0.0,
            should_refuse=True, causes=[],
        )
        result = self.metric.compute(PREDICTED_BAD, refusal_golden)
        assert result["score"] == 0.0

    def test_not_applicable_when_not_should_refuse(self):
        result = self.metric.compute(PREDICTED_GOOD, GOLDEN)
        assert result["skipped"] is True
