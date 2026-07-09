"""
S1: Rebuild the Eval Ruler — TDD tests.
Tests written BEFORE implementation — should fail until phases complete.

Phase 1: Delete Theater Metrics
Phase 2: CauseMatch Judge
Phase 3: CitationFaithfulness Judge
Phase 4: RefusalCorrectness
Phase 5: DirectionAccuracy
Phase 6: Judge Cache
Phase 7: Three-Arm Experiment
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest


# ===========================================================================
# Phase 1: Delete Theater Metrics + Relocate Cache
# ===========================================================================

class TestPhase1DeleteTheaterMetrics:
    """Verify temporal_precision, grounding_rate, attribution_f1 are deleted."""

    def test_temporal_precision_module_deleted(self):
        """Importing temporal_precision must raise ImportError — it's deleted."""
        with pytest.raises(ImportError):
            import catalyst_eval.metrics.temporal_precision  # noqa: F401
            # If import somehow succeeds, force failure
            from catalyst_eval.metrics.temporal_precision import TemporalPrecision  # noqa: F401
        # Double-check: the file must not exist on disk
        path = Path(__file__).resolve().parents[1] / "catalyst_eval" / "metrics" / "temporal_precision.py"
        assert not path.exists(), f"temporal_precision.py still exists at {path}"

    def test_grounding_rate_module_deleted(self):
        """Importing grounding_rate must raise ImportError — it's deleted."""
        with pytest.raises(ImportError):
            import catalyst_eval.metrics.grounding_rate  # noqa: F401
            from catalyst_eval.metrics.grounding_rate import GroundingRate  # noqa: F401
        path = Path(__file__).resolve().parents[1] / "catalyst_eval" / "metrics" / "grounding_rate.py"
        assert not path.exists(), f"grounding_rate.py still exists at {path}"

    def test_attribution_f1_module_deleted(self):
        """Importing attribution_f1 must raise ImportError — deleted, not facaded."""
        with pytest.raises(ImportError):
            import catalyst_eval.metrics.attribution_f1  # noqa: F401
            from catalyst_eval.metrics.attribution_f1 import AttributionF1  # noqa: F401
        path = Path(__file__).resolve().parents[1] / "catalyst_eval" / "metrics" / "attribution_f1.py"
        assert not path.exists(), f"attribution_f1.py still exists at {path}"

    def test_eval_cache_directory_exists_and_tracked(self):
        """eval_cache/ must exist at packages/eval/eval_cache/."""
        path = Path(__file__).resolve().parents[1] / "eval_cache"
        assert path.exists(), f"eval_cache directory missing: {path}"
        assert path.is_dir(), f"eval_cache is not a directory: {path}"


# ===========================================================================
# Phase 2: CauseMatch Judge
# ===========================================================================

class TestCauseMatchPairings:
    """Test cause_match pairing output and derived metrics."""

    def test_import_cause_match(self):
        """CauseMatch must be importable from metrics."""
        from catalyst_eval.metrics.cause_match import CauseMatch
        assert hasattr(CauseMatch, 'name')
        assert CauseMatch.name == "cause_match"

    def test_empty_predicted_on_answerable_case_not_skipped(self):
        """Empty predicted on answerable: recall=0, F1=0 (NOT skipped).

        This is the gaming-path closure: an agent that abstains on hard
        answerable cases must be penalized, not silently skipped.
        """
        from catalyst_eval.metrics.cause_match import CauseMatch
        from catalyst_eval.schema.golden_event import GoldenEvent, Cause, CauseCategory
        from catalyst_eval.schema.result import AttributionResult

        golden = GoldenEvent(
            id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=-4.2,
            should_refuse=False,
            causes=[Cause(text="China chip ban", category=CauseCategory.GEOPOLITICAL)],
        )
        predicted = AttributionResult(
            ticker="AAPL", trade_date="2026-01-15",
            causes=[], summary="No comment",
        )
        metric = CauseMatch(judge_fn=_fixture_judge_cause_match)
        result = metric.compute(predicted, golden)
        # Empty predicted on answerable case → recall=0, F1=0
        assert result["recall"] == 0.0, f"Expected recall=0, got {result['recall']}"
        assert result["f1"] == 0.0, f"Expected F1=0, got {result['f1']}"
        # Precision is undefined (0/0) — excluded from aggregates
        assert result["precision"] is None or result["precision"] == 0.0

    def test_refusal_case_skipped(self):
        """Golden with should_refuse=True must be skipped by cause_match."""
        from catalyst_eval.metrics.cause_match import CauseMatch
        from catalyst_eval.schema.golden_event import GoldenEvent, Cause, CauseCategory
        from catalyst_eval.schema.result import AttributionResult

        golden = GoldenEvent(
            id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=0.0,
            should_refuse=True, causes=[],
        )
        predicted = AttributionResult(
            ticker="AAPL", trade_date="2026-01-15",
            causes=[], summary="",
        )
        metric = CauseMatch(judge_fn=_fixture_judge_cause_match)
        result = metric.compute(predicted, golden)
        assert result.get("skipped") is True, f"Refusal case should be skipped, got {result}"

    def test_empty_golden_causes_skipped(self):
        """Empty golden causes (but should_refuse=False) must be skipped."""
        from catalyst_eval.metrics.cause_match import CauseMatch
        from catalyst_eval.schema.golden_event import GoldenEvent
        from catalyst_eval.schema.result import AttributionResult, PredictedCause

        golden = GoldenEvent(
            id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=-4.2,
            should_refuse=False, causes=[],
        )
        predicted = AttributionResult(
            ticker="AAPL", trade_date="2026-01-15",
            causes=[PredictedCause(text="Some cause", category="earnings", confidence=0.5,
                                    evidence_ids=[], direction="negative")],
            summary="",
        )
        metric = CauseMatch(judge_fn=_fixture_judge_cause_match)
        result = metric.compute(predicted, golden)
        assert result.get("skipped") is True, f"Empty golden causes should be skipped, got {result}"

    def test_perfect_match_pairings(self):
        """Perfect match produces precision=1.0, recall=1.0, F1=1.0."""
        from catalyst_eval.metrics.cause_match import CauseMatch
        from catalyst_eval.schema.golden_event import GoldenEvent, Cause, CauseCategory
        from catalyst_eval.schema.result import AttributionResult, PredictedCause

        golden = GoldenEvent(
            id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=-4.2,
            should_refuse=False,
            causes=[
                Cause(text="China chip ban", category=CauseCategory.GEOPOLITICAL),
                Cause(text="Sector selloff", category=CauseCategory.SECTOR),
            ],
        )
        predicted = AttributionResult(
            ticker="AAPL", trade_date="2026-01-15",
            causes=[
                PredictedCause(text="China chip ban expanded", category="geopolitical",
                               confidence=0.7, evidence_ids=["c1"], direction="negative"),
                PredictedCause(text="Tech sector selloff", category="sector",
                               confidence=0.5, evidence_ids=["c2"], direction="negative"),
            ],
            summary="",
        )
        metric = CauseMatch(judge_fn=_fixture_judge_perfect_match)
        result = metric.compute(predicted, golden)
        assert result["precision"] == 1.0, f"Expected precision=1.0, got {result}"
        assert result["recall"] == 1.0, f"Expected recall=1.0, got {result}"
        assert result["f1"] == 1.0, f"Expected F1=1.0, got {result}"


# ===========================================================================
# Phase 3: CitationFaithfulness Judge
# ===========================================================================

class TestCitationFaithfulness:
    """Test per-cause citation faithfulness."""

    def test_import_citation_faithfulness(self):
        from catalyst_eval.metrics.citation_faithfulness import CitationFaithfulness
        assert CitationFaithfulness.name == "citation_faithfulness"

    def test_skip_refusal_case(self):
        from catalyst_eval.metrics.citation_faithfulness import CitationFaithfulness
        from catalyst_eval.schema.golden_event import GoldenEvent
        from catalyst_eval.schema.result import AttributionResult

        golden = GoldenEvent(
            id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=0.0,
            should_refuse=True, causes=[],
        )
        predicted = AttributionResult(
            ticker="AAPL", trade_date="2026-01-15",
            causes=[], summary="",
        )
        metric = CitationFaithfulness(judge_fn=_fixture_judge_faithfulness)
        result = metric.compute(predicted, golden)
        assert result.get("skipped") is True

    def test_skip_empty_causes(self):
        from catalyst_eval.metrics.citation_faithfulness import CitationFaithfulness
        from catalyst_eval.schema.golden_event import GoldenEvent
        from catalyst_eval.schema.result import AttributionResult

        golden = GoldenEvent(
            id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=-4.2,
            should_refuse=False, causes=[],
        )
        predicted = AttributionResult(
            ticker="AAPL", trade_date="2026-01-15",
            causes=[], summary="",
        )
        metric = CitationFaithfulness(judge_fn=_fixture_judge_faithfulness)
        result = metric.compute(predicted, golden)
        assert result.get("skipped") is True

    def test_all_supported_scores_one(self):
        from catalyst_eval.metrics.citation_faithfulness import CitationFaithfulness
        from catalyst_eval.schema.golden_event import GoldenEvent
        from catalyst_eval.schema.result import AttributionResult, PredictedCause, RetrievedEvidence

        golden = GoldenEvent(
            id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=-4.2,
            should_refuse=False, causes=[],
        )
        predicted = AttributionResult(
            ticker="AAPL", trade_date="2026-01-15",
            causes=[
                PredictedCause(text="Cause A", category="geopolitical", confidence=0.8,
                               evidence_ids=["c1"], direction="negative"),
                PredictedCause(text="Cause B", category="sector", confidence=0.6,
                               evidence_ids=["c2"], direction="negative"),
            ],
            summary="",
            retrieved_evidence=[
                RetrievedEvidence(asset_id="c1", content_md="China chip ban news"),
                RetrievedEvidence(asset_id="c2", content_md="Semiconductor selloff report"),
            ],
        )
        metric = CitationFaithfulness(judge_fn=_fixture_judge_faithfulness_all_supported)
        result = metric.compute(predicted, golden)
        assert result["score"] == 1.0, f"Expected 1.0, got {result}"


# ===========================================================================
# Phase 4: RefusalCorrectness
# ===========================================================================

class TestRefusalCorrectness:
    """Test deterministic refusal correctness decision table."""

    def test_import(self):
        from catalyst_eval.metrics.refusal_correctness import RefusalCorrectness
        assert RefusalCorrectness.name == "refusal_correctness"

    def test_correct_refusal_scores_one(self):
        from catalyst_eval.metrics.refusal_correctness import RefusalCorrectness
        from catalyst_eval.schema.golden_event import GoldenEvent
        from catalyst_eval.schema.result import AttributionResult

        golden = GoldenEvent(
            id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=0.0,
            should_refuse=True, causes=[],
        )
        predicted = AttributionResult(
            ticker="AAPL", trade_date="2026-01-15",
            causes=[], summary="",
        )
        metric = RefusalCorrectness()
        result = metric.compute(predicted, golden)
        assert result["score"] == 1.0

    def test_false_negative_refusal_scores_zero(self):
        """should_refuse=True but agent produced causes → 0.0"""
        from catalyst_eval.metrics.refusal_correctness import RefusalCorrectness
        from catalyst_eval.schema.golden_event import GoldenEvent
        from catalyst_eval.schema.result import AttributionResult, PredictedCause

        golden = GoldenEvent(
            id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=0.0,
            should_refuse=True, causes=[],
        )
        predicted = AttributionResult(
            ticker="AAPL", trade_date="2026-01-15",
            causes=[PredictedCause(text="Some cause", category="earnings", confidence=0.5,
                                    evidence_ids=[], direction="negative")],
            summary="Something happened",
        )
        metric = RefusalCorrectness()
        result = metric.compute(predicted, golden)
        assert result["score"] == 0.0

    def test_not_applicable_when_not_should_refuse(self):
        from catalyst_eval.metrics.refusal_correctness import RefusalCorrectness
        from catalyst_eval.schema.golden_event import GoldenEvent
        from catalyst_eval.schema.result import AttributionResult, PredictedCause

        golden = GoldenEvent(
            id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=-4.2,
            should_refuse=False,
            causes=[],
        )
        predicted = AttributionResult(
            ticker="AAPL", trade_date="2026-01-15",
            causes=[PredictedCause(text="Some cause", category="earnings", confidence=0.5,
                                    evidence_ids=[], direction="negative")],
            summary="",
        )
        metric = RefusalCorrectness()
        result = metric.compute(predicted, golden)
        assert result.get("skipped") is True or result.get("score") is None


# ===========================================================================
# Phase 5: DirectionAccuracy (derived from cause_match pairings)
# ===========================================================================

class TestDirectionAccuracy:
    """Test direction accuracy derived from cause_match pairings."""

    def test_import(self):
        from catalyst_eval.metrics.direction_accuracy import DirectionAccuracy
        assert DirectionAccuracy.name == "direction_accuracy"

    def test_from_pairings(self):
        from catalyst_eval.metrics.direction_accuracy import DirectionAccuracy

        pairings = [
            {"pred_idx": 0, "golden_idx": 0, "verdict": "same_event_same_direction"},
            {"pred_idx": 1, "golden_idx": 1, "verdict": "same_event_wrong_direction"},
        ]
        result = DirectionAccuracy.compute_from_pairings(pairings)
        assert result["direction_accuracy"] == 0.5
        assert result["matched_count"] == 2
        assert result["correct_direction_count"] == 1

    def test_no_matches_null(self):
        from catalyst_eval.metrics.direction_accuracy import DirectionAccuracy

        pairings = [
            {"pred_idx": 0, "golden_idx": 0, "verdict": "unrelated"},
        ]
        result = DirectionAccuracy.compute_from_pairings(pairings)
        assert result["direction_accuracy"] is None
        assert result["matched_count"] == 0


# ===========================================================================
# Phase 6: Judge Cache
# ===========================================================================

class TestJudgeCache:
    """Test judge cache key contract and invalidation."""

    def test_cache_path_is_tracked(self):
        """Cache must be at packages/eval/eval_cache/ (JSON dodges *.sqlite gitignore)."""
        path = Path(__file__).resolve().parents[1] / "eval_cache" / "judge_cache.json"
        assert path.parent.exists(), f"eval_cache directory missing: {path.parent}"

    def test_cache_key_includes_rubric_version(self):
        """Rubric version change must invalidate cache."""
        from catalyst_eval.metrics.judge_base import make_cache_key

        key_v1 = make_cache_key(
            case_id="g001", arm="C", metric="cause_match",
            rubric_version="1.0", prompt="test prompt",
        )
        key_v2 = make_cache_key(
            case_id="g001", arm="C", metric="cause_match",
            rubric_version="2.0", prompt="test prompt",
        )
        assert key_v1 != key_v2, "Rubric version change must produce different cache keys"



# ===========================================================================
# Fixture Judge Functions (offline — no live LLM in tests)
# ===========================================================================

def _fixture_judge_cause_match(prompt: str) -> dict:
    """Fixture judge for cause_match — returns empty pairings (unrelated)."""
    return {"pairings": []}


def _fixture_judge_perfect_match(prompt: str) -> dict:
    """Fixture judge: 2 pred ↔ 2 golden, both same_event_same_direction."""
    return {
        "pairings": [
            {"pred_idx": 0, "golden_idx": 0, "verdict": "same_event_same_direction"},
            {"pred_idx": 1, "golden_idx": 1, "verdict": "same_event_same_direction"},
        ]
    }


def _fixture_judge_faithfulness(prompt: str) -> dict:
    """Fixture judge for citation_faithfulness."""
    return {"per_cause_verdicts": []}


def _fixture_judge_faithfulness_all_supported(prompt: str) -> dict:
    """Fixture judge: both causes supported."""
    return {
        "per_cause_verdicts": [
            {"verdict": "supported", "reason": "Evidence directly supports claim"},
            {"verdict": "supported", "reason": "Evidence directly supports claim"},
        ]
    }
