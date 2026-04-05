"""
TDD tests for Task 2.2 evaluation metrics.
Written before implementation — all tests should initially fail on import errors.
"""
from __future__ import annotations

import pytest

from catalyst_eval.schema.golden_event import Cause, CauseCategory, GoldenEvent
from catalyst_eval.schema.result import AttributionResult, PredictedCause
from catalyst_eval.metrics.attribution_f1 import AttributionF1
from catalyst_eval.metrics.category_accuracy import CategoryAccuracy
from catalyst_eval.metrics.grounding_rate import GroundingRate
from catalyst_eval.metrics.temporal_precision import TemporalPrecision
from catalyst_eval.metrics.confidence_calibration import ConfidenceCalibration


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

GOLDEN = GoldenEvent(
    id="t1",
    ticker="AAPL",
    trade_date="2026-01-15",
    price_move_pct=-4.2,
    causes=[
        Cause(
            text="China export ban on H20 chips",
            category=CauseCategory.GEOPOLITICAL,
            weight=0.6,
            temporal_anchor="pre-market",
            evidence_ids=[],
        ),
        Cause(
            text="Sector selloff",
            category=CauseCategory.SECTOR,
            weight=0.3,
            temporal_anchor="intraday",
            evidence_ids=[],
        ),
    ],
)

PREDICTED_GOOD = AttributionResult(
    ticker="AAPL",
    trade_date="2026-01-15",
    causes=[
        PredictedCause(
            text="China chip export restrictions expanded",
            category="geopolitical",
            confidence=0.7,
            evidence_ids=["c1"],
            direction="negative",
        ),
        PredictedCause(
            text="Broad semiconductor selloff",
            category="sector",
            confidence=0.2,
            evidence_ids=["c2"],
            direction="negative",
        ),
    ],
    summary="...",
    retrieved_chunks=["c1", "c2"],
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
    retrieved_chunks=["c1"],
    cost_breakdown=[],
    total_cost_usd=0.02,
    total_tokens=5000,
)


# ---------------------------------------------------------------------------
# AttributionF1
# ---------------------------------------------------------------------------

class TestAttributionF1:
    def setup_method(self):
        self.metric = AttributionF1()

    def test_name(self):
        assert self.metric.name == "attribution_f1"

    def test_good_prediction_score_above_threshold(self):
        score = self.metric.compute(PREDICTED_GOOD, GOLDEN)
        assert score > 0.5, f"Expected > 0.5, got {score}"

    def test_bad_prediction_score_below_threshold(self):
        score = self.metric.compute(PREDICTED_BAD, GOLDEN)
        assert score < 0.3, f"Expected < 0.3, got {score}"

    def test_score_bounds(self):
        score = self.metric.compute(PREDICTED_GOOD, GOLDEN)
        assert 0.0 <= score <= 1.0

    def test_empty_predicted_causes(self):
        empty_pred = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[],
            summary="",
        )
        score = self.metric.compute(empty_pred, GOLDEN)
        assert score == 0.0

    def test_perfect_match(self):
        """Predicted causes that are verbatim copies of golden causes should score high."""
        perfect_pred = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[
                PredictedCause(
                    text="China export ban on H20 chips",
                    category="geopolitical",
                    confidence=1.0,
                    evidence_ids=[],
                    direction="negative",
                ),
                PredictedCause(
                    text="Sector selloff",
                    category="sector",
                    confidence=1.0,
                    evidence_ids=[],
                    direction="negative",
                ),
            ],
            summary="",
        )
        score = self.metric.compute(perfect_pred, GOLDEN)
        assert score == pytest.approx(1.0), f"Expected 1.0, got {score}"


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
        empty_pred = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[],
            summary="",
        )
        score = self.metric.compute(empty_pred, GOLDEN)
        assert score == 0.0

    def test_score_bounds(self):
        score = self.metric.compute(PREDICTED_GOOD, GOLDEN)
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# TemporalPrecision
# ---------------------------------------------------------------------------

class TestTemporalPrecision:
    def setup_method(self):
        self.metric = TemporalPrecision()

    def test_name(self):
        assert self.metric.name == "temporal_precision"

    def test_matching_date(self):
        score = self.metric.compute(PREDICTED_GOOD, GOLDEN)
        assert score == 1.0

    def test_mismatched_date(self):
        wrong_date_pred = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-16",
            causes=[],
            summary="",
        )
        score = self.metric.compute(wrong_date_pred, GOLDEN)
        assert score == 0.0


# ---------------------------------------------------------------------------
# GroundingRate
# ---------------------------------------------------------------------------

class TestGroundingRate:
    def setup_method(self):
        self.metric = GroundingRate()

    def test_name(self):
        assert self.metric.name == "grounding_rate"

    def test_grounded_prediction(self):
        # PREDICTED_GOOD has evidence_ids ["c1", "c2"] and retrieved_chunks ["c1", "c2"]
        score = self.metric.compute(PREDICTED_GOOD, GOLDEN)
        assert score > 0.5, f"Expected > 0.5, got {score}"

    def test_no_evidence(self):
        no_evidence_pred = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[
                PredictedCause(
                    text="Some cause",
                    category="earnings",
                    confidence=0.5,
                    evidence_ids=[],
                    direction="negative",
                ),
            ],
            summary="",
            retrieved_chunks=["c1"],
        )
        score = self.metric.compute(no_evidence_pred, GOLDEN)
        assert score == 0.0

    def test_empty_causes_returns_zero(self):
        empty_pred = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[],
            summary="",
        )
        score = self.metric.compute(empty_pred, GOLDEN)
        assert score == 0.0

    def test_partial_grounding(self):
        """One cause grounded, one not — score should be 0.5."""
        partial_pred = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[
                PredictedCause(
                    text="Grounded cause",
                    category="geopolitical",
                    confidence=0.8,
                    evidence_ids=["c1"],
                    direction="negative",
                ),
                PredictedCause(
                    text="Ungrounded cause",
                    category="sector",
                    confidence=0.4,
                    evidence_ids=[],
                    direction="negative",
                ),
            ],
            summary="",
            retrieved_chunks=["c1"],
        )
        score = self.metric.compute(partial_pred, GOLDEN)
        assert score == pytest.approx(0.5)

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
        empty_pred = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[],
            summary="",
        )
        score = self.metric.compute(empty_pred, GOLDEN)
        assert score == 0.0

    def test_well_calibrated_is_higher_than_poorly_calibrated(self):
        """
        A predictor that is correct AND assigns high confidence should outscore
        one that is wrong but assigns high confidence.
        """
        # Correct categories, moderate confidence — should be better calibrated
        well_calibrated = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[
                PredictedCause(
                    text="geopolitical event",
                    category="geopolitical",
                    confidence=0.6,
                    evidence_ids=[],
                    direction="negative",
                ),
                PredictedCause(
                    text="sector move",
                    category="sector",
                    confidence=0.7,
                    evidence_ids=[],
                    direction="negative",
                ),
            ],
            summary="",
        )
        # Wrong categories, very high confidence — poorly calibrated
        poorly_calibrated = AttributionResult(
            ticker="AAPL",
            trade_date="2026-01-15",
            causes=[
                PredictedCause(
                    text="earnings miss",
                    category="earnings",
                    confidence=0.95,
                    evidence_ids=[],
                    direction="negative",
                ),
                PredictedCause(
                    text="macro headwinds",
                    category="macro",
                    confidence=0.9,
                    evidence_ids=[],
                    direction="negative",
                ),
            ],
            summary="",
        )
        good_score = self.metric.compute(well_calibrated, GOLDEN)
        bad_score = self.metric.compute(poorly_calibrated, GOLDEN)
        assert good_score > bad_score, (
            f"Well-calibrated ({good_score}) should outscore poorly calibrated ({bad_score})"
        )
